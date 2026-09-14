# -*- coding: utf-8 -*-
"""协议互转层（feat#6 全链路转换 / feat#10 OpenAI<->Anthropic<->Gemini 三向）。

内部归一化格式 = OpenAI chat/completions 结构。
  - 入站：openai 原样；anthropic /v1/messages、gemini generateContent 转成 openai
  - 出站：按供应商 protocol 把 openai 转成对应格式发给上游
  - 响应：上游响应转回 openai 给客户端
所有转换点都返回“转换前/后”对象，供 server 记录全链路日志（feat#6）。
"""
import json
import time


# 客户端未显式传 max_tokens 时注入的输出上限（agent/tool 场景避免被默认小值截断）
_DEFAULT_MAX_TOKENS = 16384


# ---------------- 协议适配器注册表（插件化：新增协议只需注册一次） ----------------
# 每个协议可注册 5 个能力点，缺失则回退 openai 直通：
#   inbound        : 入站请求体(异构) -> 内部 OpenAI          fn(body)->openai_body
#   outbound       : 内部 OpenAI -> 出站请求体                fn(openai_body)->异构
#   resp_to_openai : 上游响应(异构) -> 内部 OpenAI            fn(resp, model)->openai_resp
#   openai_to_resp : 内部 OpenAI -> 异构响应(非流式)          fn(resp, model)->异构
#   sse            : 上游 SSE(异构) -> OpenAI SSE 流式转换类   cls(model)
PROTOCOL_ADAPTERS = {}


def register_protocol(name, inbound=None, outbound=None,
                      resp_to_openai=None, openai_to_resp=None, sse=None):
    """注册一个协议适配器。返回该协议名，方便链式调用。"""
    e = PROTOCOL_ADAPTERS.setdefault(name, {})
    for k, v in (("inbound", inbound), ("outbound", outbound),
                 ("resp_to_openai", resp_to_openai),
                 ("openai_to_resp", openai_to_resp), ("sse", sse)):
        if v is not None:
            e[k] = v
    return name


def _adapter(protocol):
    if protocol in (None, "openai"):
        return {}
    return PROTOCOL_ADAPTERS.get(protocol) or {}


# ---------------- 入站：异构协议 -> 内部 OpenAI ----------------
def inbound_to_openai(body, protocol):
    fn = _adapter(protocol).get("inbound")
    if not fn:
        return body, None
    return fn(body), protocol


def _anthropic_in_to_openai(body):
    messages = []
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": system if isinstance(system, str) else json.dumps(system)})
    for m in body.get("messages", []):
        msg = {"role": m.get("role"), "content": m.get("content")}
        if m.get("tool_calls"):
            msg["tool_calls"] = m["tool_calls"]
        elif m.get("tool_use"):
            # Anthropic 使用 tool_use
            tool_calls = []
            for tu in m.get("tool_use", []):
                if isinstance(tu, dict) and tu.get("type") == "tool_use":
                    tool_calls.append({
                        "id": tu.get("id"),
                        "type": "function",
                        "function": {"name": tu.get("name"), "arguments": json.dumps(tu.get("input", {}))}
                    })
            if tool_calls:
                msg["tool_calls"] = tool_calls
        messages.append(msg)
    out = {
        "model": body.get("model"),
        "messages": messages,
        "max_tokens": body.get("max_tokens", _DEFAULT_MAX_TOKENS),
        "stream": body.get("stream", False),
    }
    for k in ("temperature", "top_p", "stop", "tools", "tool_choice"):
        if k in body:
            out[k] = body[k]
    return out


def _gemini_in_to_openai(body):
    contents = body.get("contents", [])
    messages = []
    sys = None
    if isinstance(body.get("systemInstruction"), dict):
        parts = body["systemInstruction"].get("parts", [])
        sys = " ".join(p.get("text", "") for p in parts)
    for c in contents:
        role = "assistant" if c.get("role") == "model" else "user"
        parts = c.get("parts", [])
        text_parts = []
        tool_calls = []
        for p in parts:
            if "text" in p:
                text_parts.append(p["text"])
            elif "functionCall" in p:
                fc = p["functionCall"]
                tool_calls.append({
                    "id": p.get("functionCall", {}).get("id") or str(hash(str(p))),
                    "type": "function",
                    "function": {"name": fc.get("name", ""), "arguments": json.dumps(fc.get("args", {}))}
                })
        if text_parts:
            messages.append({"role": role, "content": " ".join(text_parts)})
        if tool_calls:
            # Add tool calls to the last assistant message
            if messages and messages[-1]["role"] == "assistant":
                messages[-1]["tool_calls"] = tool_calls
            else:
                messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
    if sys:
        messages.insert(0, {"role": "system", "content": sys})
    gc = body.get("generationConfig", {})
    out = {
        "model": body.get("model"),
        "messages": messages,
        "max_tokens": gc.get("maxOutputTokens"),
        "temperature": gc.get("temperature"),
        "top_p": gc.get("topP"),
        "stream": body.get("stream", False),
    }
    out = {k: v for k, v in out.items() if v is not None}
    return out


# ---------------- 出站：内部 OpenAI -> 目标协议 ----------------
def openai_to_outbound(body, protocol):
    fn = _adapter(protocol).get("outbound")
    if not fn:
        return body, None
    return fn(body), protocol


def _openai_to_anthropic(body):
    messages = body.get("messages", [])
    system_text = None
    conv = []
    for m in messages:
        if m.get("role") == "system":
            system_text = m.get("content")
            continue
        msg = {"role": m.get("role"), "content": m.get("content")}
        if m.get("tool_calls"):
            msg["tool_calls"] = m["tool_calls"]
        conv.append(msg)
    out = {
        "model": body.get("model"),
        "messages": conv,
        "max_tokens": body.get("max_tokens", _DEFAULT_MAX_TOKENS),
        "stream": body.get("stream", False),
    }
    if system_text:
        out["system"] = system_text
    for k in ("temperature", "top_p", "stop", "tools", "tool_choice"):
        if k in body and body[k] is not None:
            out[k] = body[k]
    return out


def _openai_to_gemini(body):
    messages = body.get("messages", [])
    sys = None
    contents = []
    for m in messages:
        if m.get("role") == "system":
            sys = m.get("content")
            continue
        role = "model" if m.get("role") == "assistant" else "user"
        if m.get("tool_calls"):
            # Gemini function calling format
            for tc in m.get("tool_calls", []):
                fc = tc.get("function", {})
                contents.append({
                    "role": "model",
                    "parts": [{
                        "functionCall": {
                            "name": fc.get("name", ""),
                            "args": json.loads(fc.get("arguments", "{}")) if isinstance(fc.get("arguments"), str) else fc.get("arguments", {}),
                            "id": tc.get("id", "")
                        }
                    }]
                })
        else:
            contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})
    out = {"contents": contents}
    gc = {}
    if body.get("max_tokens"):
        gc["maxOutputTokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        gc["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        gc["topP"] = body["top_p"]
    if body.get("stop"):
        gc["stopSequences"] = body["stop"] if isinstance(body["stop"], list) else [body["stop"]]
    if body.get("tools"):
        # Gemini expects tools in a specific format
        out["tools"] = body["tools"]
    if gc:
        out["generationConfig"] = gc
    if sys:
        out["systemInstruction"] = {"parts": [{"text": sys}]}
    out["stream"] = body.get("stream", False)
    return out


# ---------------- 响应：目标协议 -> OpenAI ----------------
def response_to_openai(resp, protocol, model):
    fn = _adapter(protocol).get("resp_to_openai")
    if not fn:
        return resp
    return fn(resp, model)


def _anthropic_resp_to_openai(resp, model):
    text = ""
    tool_calls = []
    for blk in resp.get("content", []):
        if blk.get("type") == "text":
            text += blk.get("text", "")
        elif blk.get("type") == "tool_use":
            tool_calls.append({
                "id": blk.get("id"),
                "type": "function",
                "function": {"name": blk.get("name"), "arguments": json.dumps(blk.get("input", {}))}
            })
    usage = resp.get("usage", {})
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": resp.get("id", "chatcmpl-anthropic"),
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                  "completion_tokens": usage.get("output_tokens", 0),
                  "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)},
    }


def openai_resp_to_anthropic(resp, model):
    msg = resp["choices"][0]["message"]
    text = msg.get("content") or ""
    usage = resp.get("usage", {})
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except Exception:
            inp = {}
        content.append({"type": "tool_use", "id": tc.get("id") or f"toolu_{tc.get('index', 0)}",
                        "name": fn.get("name", ""), "input": inp})
    if not content:
        content.append({"type": "text", "text": ""})
    fr = (resp["choices"][0].get("finish_reason") or "stop")
    stop_map = {"stop": "end_turn", "length": "max_tokens",
                "tool_calls": "tool_use", "function_call": "tool_use"}
    return {
        "id": resp.get("id", "msg-anthropic"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_map.get(fr, "end_turn"),
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }


def openai_resp_to_gemini(resp, model):
    msg = resp["choices"][0]["message"]
    text = msg.get("content") or ""
    usage = resp.get("usage", {})
    parts = []
    if text:
        parts.append({"text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
    if not parts:
        parts.append({"text": ""})
    fr = (resp["choices"][0].get("finish_reason") or "stop")
    finish_map = {"stop": "STOP", "length": "MAX_TOKENS",
                  "tool_calls": "STOP", "function_call": "STOP"}
    return {
        "candidates": [{"content": {"parts": parts, "role": "model"},
                        "finishReason": finish_map.get(fr, "STOP")}],
        "usageMetadata": {"promptTokenCount": usage.get("prompt_tokens", 0),
                          "candidatesTokenCount": usage.get("completion_tokens", 0)},
    }


def _gemini_resp_to_openai(resp, model):
    text = ""
    tool_calls = []
    for c in resp.get("candidates", []):
        for p in c.get("content", {}).get("parts", []):
            if "text" in p:
                text += p.get("text", "")
            elif "functionCall" in p:
                fc = p["functionCall"]
                tool_calls.append({
                    "id": p.get("functionCall", {}).get("id") or str(hash(str(p))),
                    "type": "function",
                    "function": {"name": fc.get("name", ""), "arguments": json.dumps(fc.get("args", {}))}
                })
    usage = resp.get("usageMetadata", {})
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-gemini",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": usage.get("promptTokenCount", 0),
                  "completion_tokens": usage.get("candidatesTokenCount", 0),
                  "total_tokens": usage.get("promptTokenCount", 0) + usage.get("candidatesTokenCount", 0)},
    }


# ---------------- 有状态流式 SSE 转换器（跨 chunk 状态，支持 tool calls） ----------------
def _sse_line(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


def _openai_chunk(delta, finish_reason=None, model=""):
    return _sse_line({"id": "chatcmpl-gw", "object": "chat.completion.chunk",
                      "model": model,
                      "choices": [{"index": 0, "delta": delta or {},
                                   "finish_reason": finish_reason}]})


def _openai_sse_error(message, code=502):
    return _sse_line({"error": {"message": str(message)[:300],
                                "type": "gateway_upstream_error", "code": code}})


class AnthropicSSEToOpenAI:
    """Anthropic SSE -> OpenAI SSE（完整事件映射：text_delta / tool_use /
    input_json_delta / message_delta / error）。collected_text 供 token 估算。"""

    def __init__(self, model):
        self.model = model
        self.collected_text = ""
        self.done = False
        self._tool_started = set()   # anthropic block index 已发过 tool_calls 起始

    def feed(self, chunk) -> list:
        out = []
        for line in chunk.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                continue
            try:
                ev = json.loads(data)
            except Exception:
                continue
            etype = ev.get("type")
            if etype == "content_block_start":
                blk = ev.get("content_block") or {}
                if blk.get("type") == "tool_use":
                    idx = ev.get("index", 0)
                    out.append(_openai_chunk(
                        {"tool_calls": [{"index": idx, "id": blk.get("id") or f"call_{idx}",
                                         "type": "function",
                                         "function": {"name": blk.get("name", ""),
                                                      "arguments": ""}}]},
                        model=self.model))
                    self._tool_started.add(idx)
            elif etype == "content_block_delta":
                d = ev.get("delta") or {}
                dt = d.get("type")
                if dt == "text_delta":
                    piece = d.get("text", "")
                    if piece:
                        self.collected_text += piece
                        out.append(_openai_chunk({"content": piece}, model=self.model))
                elif dt == "input_json_delta":
                    idx = ev.get("index", 0)
                    if idx not in self._tool_started:   # 容错：漏掉 start 事件
                        out.append(_openai_chunk(
                            {"tool_calls": [{"index": idx, "id": f"call_{idx}",
                                             "type": "function",
                                             "function": {"name": "", "arguments": ""}}]},
                            model=self.model))
                        self._tool_started.add(idx)
                    piece = d.get("partial_json", "")
                    if piece:
                        out.append(_openai_chunk(
                            {"tool_calls": [{"index": idx,
                                             "function": {"arguments": piece}}]},
                            model=self.model))
            elif etype == "message_delta":
                sr = (ev.get("delta") or {}).get("stop_reason")
                if sr:
                    m = {"end_turn": "stop", "max_tokens": "length",
                         "tool_use": "tool_calls", "stop_sequence": "stop"}
                    out.append(_openai_chunk({}, m.get(sr, "stop"), self.model))
            elif etype == "message_stop":
                out.append(b"data: [DONE]\n\n")
                self.done = True
            elif etype == "error":
                out.append(_openai_sse_error(
                    (ev.get("error") or {}).get("message", "upstream error")))
                self.done = True
        return out

    def finalize(self) -> list:
        """上游未发 message_stop 时补发 [DONE]。"""
        if not self.done:
            self.done = True
            return [b"data: [DONE]\n\n"]
        return []


class GeminiSSEToOpenAI:
    """Gemini 流式 -> OpenAI SSE（text + functionCall）。"""

    def __init__(self, model):
        self.model = model
        self.collected_text = ""
        self.done = False
        self._tool_seq = 0

    def feed(self, chunk) -> list:
        out = []
        for line in chunk.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data or data == "[DONE]":
                continue
            try:
                ev = json.loads(data)
            except Exception:
                continue
            for c in ev.get("candidates", []):
                for p in (c.get("content") or {}).get("parts", []):
                    if p.get("text"):
                        self.collected_text += p["text"]
                        out.append(_openai_chunk({"content": p["text"]}, model=self.model))
                    elif "functionCall" in p:
                        fc = p["functionCall"] or {}
                        try:
                            args = json.dumps(fc.get("args") or {}, ensure_ascii=False)
                        except Exception:
                            args = "{}"
                        self._tool_seq += 1
                        out.append(_openai_chunk(
                            {"tool_calls": [{"index": self._tool_seq,
                                             "id": fc.get("id") or f"call_{self._tool_seq}",
                                             "type": "function",
                                             "function": {"name": fc.get("name", ""),
                                                          "arguments": args}}]},
                            finish_reason="tool_calls", model=self.model))
        return out

    def finalize(self) -> list:
        if not self.done:
            self.done = True
            return [b"data: [DONE]\n\n"]
        return []


class OpenAISSEToAnthropic:
    """OpenAI SSE -> Anthropic SSE（/v1/messages 流式入站）。
    产出完整 Anthropic 事件：message_start / content_block_* / message_delta /
    message_stop / error，支持 tool_calls 增量（input_json_delta）。"""

    def __init__(self, model):
        self.model = model
        self.started = False
        self.finished = False
        self._block = 0          # 下一个 anthropic content block 下标
        self._text_open = False
        self._tools = {}         # openai tool index -> {"id","name","block","open"}

    def _event(self, name, data):
        return (f"event: {name}\ndata: "
                f"{json.dumps(data, ensure_ascii=False)}\n\n").encode("utf-8")

    def _ensure_start(self, out):
        if not self.started:
            self.started = True
            out.append(self._event("message_start", {
                "type": "message_start",
                "message": {"type": "message", "role": "assistant",
                            "model": self.model, "content": [],
                            "usage": {"input_tokens": 0, "output_tokens": 0}}}))

    def _close_text(self, out):
        if self._text_open:
            out.append(self._event("content_block_stop",
                                   {"type": "content_block_stop", "index": self._block}))
            self._text_open = False
            self._block += 1

    def _close_all(self):
        out = []
        self._close_text(out)
        for t in self._tools.values():
            if t["open"]:
                out.append(self._event("content_block_stop",
                                       {"type": "content_block_stop", "index": t["block"]}))
                t["open"] = False
        return out

    def feed(self, chunk) -> list:
        out = []
        self._ensure_start(out)
        for line in chunk.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data:
                continue
            if data == "[DONE]":
                out.extend(self._finalize("end_turn"))
                continue
            try:
                ev = json.loads(data)
            except Exception:
                continue
            if ev.get("error"):
                err = ev["error"] if isinstance(ev["error"], dict) else {"message": str(ev["error"])}
                out.extend(self._close_all())
                out.append(self._event("error", {"type": "error", "error": err}))
                self.finished = True
                continue
            for ch in ev.get("choices", []):
                d = ch.get("delta") or {}
                c = d.get("content")
                if c:
                    if not self._text_open:
                        out.append(self._event("content_block_start", {
                            "type": "content_block_start", "index": self._block,
                            "content_block": {"type": "text", "text": ""}}))
                        self._text_open = True
                    out.append(self._event("content_block_delta", {
                        "type": "content_block_delta", "index": self._block,
                        "delta": {"type": "text_delta", "text": c}}))
                for tc in d.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    t = self._tools.get(idx)
                    if t is None:
                        self._close_text(out)
                        fn = tc.get("function") or {}
                        t = {"id": tc.get("id") or f"toolu_{idx}",
                             "name": fn.get("name", ""),
                             "block": self._block, "open": True}
                        self._tools[idx] = t
                        out.append(self._event("content_block_start", {
                            "type": "content_block_start", "index": self._block,
                            "content_block": {"type": "tool_use", "id": t["id"],
                                              "name": t["name"], "input": {}}}))
                        self._block += 1
                    piece = (tc.get("function") or {}).get("arguments")
                    if piece:
                        out.append(self._event("content_block_delta", {
                            "type": "content_block_delta", "index": t["block"],
                            "delta": {"type": "input_json_delta", "partial_json": piece}}))
                fr = ch.get("finish_reason")
                if fr:
                    m = {"stop": "end_turn", "length": "max_tokens",
                         "tool_calls": "tool_use", "function_call": "tool_use"}
                    out.extend(self._finalize(m.get(fr, "end_turn")))
        return out

    def _finalize(self, stop_reason):
        if self.finished:
            return []
        out = self._close_all()
        out.append(self._event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 0}}))
        out.append(self._event("message_stop", {"type": "message_stop"}))
        self.finished = True
        return out

    def finalize(self) -> list:
        """上游未发 finish_reason/[DONE] 时补齐收尾事件。"""
        return self._finalize("end_turn")


class OpenAISSEToGemini:
    """OpenAI SSE -> Gemini 流式（/v1beta generateContent streaming 入站）。"""

    def __init__(self, model):
        self.model = model
        self.finished = False

    def feed(self, chunk) -> list:
        out = []
        for line in chunk.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data or data == "[DONE]":
                continue
            try:
                ev = json.loads(data)
            except Exception:
                continue
            if ev.get("error"):
                out.append(_sse_line({"error": {"code": 502,
                                                "message": str(ev["error"])[:300]}}))
                self.finished = True
                continue
            for ch in ev.get("choices", []):
                d = ch.get("delta") or {}
                parts = []
                if d.get("content"):
                    parts.append({"text": d["content"]})
                for tc in d.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
                if parts:
                    out.append(_sse_line({"candidates": [{
                        "content": {"parts": parts, "role": "model"}, "index": 0}]}))
        return out

    def finalize(self) -> list:
        if self.finished:
            return []
        self.finished = True
        return [_sse_line({"candidates": [{
            "content": {"parts": [{"text": ""}], "role": "model"},
            "finishReason": "STOP", "index": 0}]})]


def make_stream_converter(protocol, model):
    """按上游协议创建流式转换器；openai 协议返回 None（原样透传）。"""
    cls = _adapter(protocol).get("sse")
    if not cls:
        return None
    return cls(model)


# ================= Responses API（feat: Codex CLI / OpenAI Responses 客户端入站） =================

def responses_to_openai(body):
    """OpenAI Responses API 入站 -> 内部 OpenAI chat/completions 结构。
    支持 input 为字符串或消息数组（含 function_call / function_call_output 工具轮），
    instructions/max_output_tokens/tools 等字段映射到 OpenAI 等价项。"""
    messages = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})
    inp = body.get("input", body.get("messages"))
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    else:
        for item in inp or []:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            t = item.get("type")
            role = item.get("role") or "user"
            if t == "function_call":
                messages.append({
                    "role": "assistant", "content": "",
                    "tool_calls": [{
                        "id": item.get("call_id") or item.get("id"),
                        "type": "function",
                        "function": {"name": item.get("name", ""),
                                     "arguments": item.get("arguments", "{}")}}]})
            elif t == "function_call_output":
                messages.append({"role": "tool",
                                 "content": item.get("output", ""),
                                 "tool_call_id": item.get("call_id", "")})
            elif t == "message" or ("content" in item and role in ("user", "assistant")):
                content = item.get("content", "")
                if isinstance(content, list):
                    # 多模态部分：只保留文本（多数轻量模型不支持图像）
                    texts = [p.get("text", "") for p in content
                             if isinstance(p, dict) and p.get("text")]
                    content = "".join(texts)
                messages.append({"role": role, "content": content})
    out = {
        "model": body.get("model"),
        "messages": messages,
        "max_tokens": body.get("max_output_tokens") or body.get("max_tokens", _DEFAULT_MAX_TOKENS),
        "stream": bool(body.get("stream")),
    }
    for k in ("temperature", "top_p", "stop", "tools", "tool_choice"):
        if body.get(k) is not None:
            out[k] = body[k]
    return out


def openai_resp_to_responses(resp, model):
    """内部 OpenAI 响应 -> Responses API 响应（非流式）。"""
    msg = (resp.get("choices") or [{}])[0].get("message", {})
    text = msg.get("content") or ""
    usage = resp.get("usage") or {}
    output = []
    if text:
        output.append({
            "type": "message", "id": f"msg_{resp.get('id', 'gw')[-8:]}",
            "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}]})
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        cid = tc.get("id") or f"call_{i}"
        output.append({
            "type": "function_call", "id": f"fc_{i}",
            "call_id": cid, "name": fn.get("name", ""),
            "arguments": fn.get("arguments") or "{}",
            "status": "completed"})
    return {
        "id": f"resp_{resp.get('id', 'gw')}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0),
                  "total_tokens": usage.get("total_tokens", 0)},
    }


class OpenAISSEToResponses:
    """OpenAI SSE -> Responses API SSE（/v1/responses 流式入站）。
    产出完整事件序列：response.created / response.output_item.added /
    response.content_part.added+delta+done / response.output_text.* /
    response.function_call_arguments.* / response.output_item.done /
    response.completed，支持 tool_calls 增量。"""

    def __init__(self, model):
        self.model = model
        self.resp_id = f"resp_gw_{int(time.time() * 1000)}"
        self.started = False
        self.finished = False
        self.final_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self._out_index = 0
        self._current = None      # {"kind","item_id","text","block","fn_id","fn_name"}
        self.collected_text = ""

    def _event(self, name, data):
        return (f"event: {name}\ndata: "
                f"{json.dumps(data, ensure_ascii=False)}\n\n").encode("utf-8")

    def _base_item(self):
        return {"id": self._current["item_id"], "type": self._current["kind"],
                "status": "in_progress", "output_index": self._out_index}

    def _close_item(self, out):
        if not self._current:
            return
        cur = self._current
        if cur["kind"] == "message":
            out.append(self._event("response.output_text.done", {
                "type": "response.output_text.done", "item_id": cur["item_id"],
                "output_index": self._out_index, "content_index": 0, "text": cur["text"]}))
            out.append(self._event("response.content_part.done", {
                "type": "response.content_part.done", "item_id": cur["item_id"],
                "output_index": self._out_index, "content_index": 0,
                "part": {"type": "output_text", "text": cur["text"], "annotations": []}}))
            item = {"id": cur["item_id"], "type": "message", "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": cur["text"], "annotations": []}]}
        else:
            out.append(self._event("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done", "item_id": cur["item_id"],
                "output_index": self._out_index, "arguments": cur["text"]}))
            item = {"id": cur["item_id"], "type": "function_call", "status": "completed",
                    "call_id": cur["fn_id"], "name": cur["fn_name"],
                    "arguments": cur["text"]}
        out.append(self._event("response.output_item.done", {
            "type": "response.output_item.done", "output_index": self._out_index,
            "item": {**item, "output_index": self._out_index}}))
        self._out_index += 1
        self._current = None

    def feed(self, chunk):
        out = []
        if not self.started:
            self.started = True
            out.append(self._event("response.created", {
                "type": "response.created",
                "response": {"id": self.resp_id, "object": "response",
                             "created_at": int(time.time()), "status": "in_progress",
                             "model": self.model}}))
        for line in chunk.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data or data == "[DONE]":
                continue
            try:
                ev = json.loads(data)
            except Exception:
                continue
            if ev.get("error"):
                out.append(self._event("response.failed", {
                    "type": "response.failed",
                    "response": {"id": self.resp_id, "object": "response",
                                 "status": "failed", "model": self.model,
                                 "error": ev["error"]}}))
                self.finished = True
                continue
            fin = None
            for ch in ev.get("choices", []):
                d = ch.get("delta") or {}
                c = d.get("content")
                fr = ch.get("finish_reason")
                # 文本增量
                if c:
                    if self._current and self._current["kind"] != "message":
                        self._close_item(out)
                    if not self._current:
                        self._current = {"kind": "message",
                                         "item_id": f"msg_{self._out_index}",
                                         "text": "", "block": 0}
                        out.append(self._event("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": self._out_index,
                            "item": {"id": self._current["item_id"], "type": "message",
                                     "role": "assistant", "status": "in_progress",
                                     "content": []}}))
                        out.append(self._event("response.content_part.added", {
                            "type": "response.content_part.added",
                            "item_id": self._current["item_id"],
                            "output_index": self._out_index, "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []}}))
                    self._current["text"] += c
                    self.collected_text += c
                    out.append(self._event("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "item_id": self._current["item_id"],
                        "output_index": self._out_index, "content_index": 0,
                        "delta": c}))
                # 工具调用增量
                for tc in d.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    name = fn.get("name")
                    args_piece = fn.get("arguments")
                    if name:
                        if self._current:
                            self._close_item(out)
                        self._current = {"kind": "function_call",
                                         "item_id": f"fc_{self._out_index}",
                                         "text": "", "block": 0,
                                         "fn_id": tc.get("id") or f"call_{self._out_index}",
                                         "fn_name": name}
                        out.append(self._event("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": self._out_index,
                            "item": {"id": self._current["item_id"],
                                     "type": "function_call", "status": "in_progress",
                                     "call_id": self._current["fn_id"],
                                     "name": name, "arguments": ""}}))
                    if args_piece:
                        if not self._current or self._current["kind"] != "function_call":
                            continue
                        self._current["text"] += args_piece
                        out.append(self._event(
                            "response.function_call_arguments.delta", {
                            "type": "response.function_call_arguments.delta",
                            "item_id": self._current["item_id"],
                            "output_index": self._out_index,
                            "delta": args_piece}))
                if fr:
                    fin = fr
            if fin:
                self._close_item(out)
                out.append(self._event("response.completed", {
                    "type": "response.completed",
                    "response": {"id": self.resp_id, "object": "response",
                                 "created_at": int(time.time()), "status": "completed",
                                 "model": self.model,
                                 "output": [],
                                 "usage": self.final_usage}}))
                self.finished = True
        return out

    def finalize(self):
        """上游未发 finish_reason 时补齐收尾事件。"""
        if self.finished:
            return []
        out = []
        self._close_item(out)
        out.append(self._event("response.completed", {
            "type": "response.completed",
            "response": {"id": self.resp_id, "object": "response",
                         "created_at": int(time.time()), "status": "completed",
                         "model": self.model, "output": [],
                         "usage": self.final_usage}}))
        self.finished = True
        return out


# ---------------- 内置协议注册（新增协议：照此补一个 register_protocol 即可） ----------------
register_protocol("anthropic",
                  inbound=_anthropic_in_to_openai,
                  outbound=_openai_to_anthropic,
                  resp_to_openai=_anthropic_resp_to_openai,
                  openai_to_resp=openai_resp_to_anthropic,
                  sse=AnthropicSSEToOpenAI)
register_protocol("gemini",
                  inbound=_gemini_in_to_openai,
                  outbound=_openai_to_gemini,
                  resp_to_openai=_gemini_resp_to_openai,
                  openai_to_resp=openai_resp_to_gemini,
                  sse=GeminiSSEToOpenAI)
