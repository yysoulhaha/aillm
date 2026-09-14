# -*- coding: utf-8 -*-
"""AI 模型网关主服务（端口 18123）。

集成优化：
  #1 熔断+多级Fallback  #2 配额感知  #3 按key模型过滤  #4 供应商级代理
  #5 模型别名表达式  #6 全链路协议转换日志  #7 全局系统提示词
  #8 Web健康度面板  #9 Token压缩  #10 多协议深度互转
"""
import asyncio
import json
import os
import queue as _queue
import re
import socket
import time
import pathlib
import sys
import threading
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime

# 数据/路径解析见 core/paths.py（跨 Windows/Linux；AI_LLM_HOME 可显式覆盖）
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, PlainTextResponse

import core.config as config


def _global_crash_hook(exc_type, exc, tb):
    """全局崩溃钩子：窗口化打包下任何未捕获异常都落盘，便于用户/运维排查。"""
    try:
        import traceback
        crash = config.DATA_ROOT / "logs" / "crash.log"
        crash.parent.mkdir(parents=True, exist_ok=True)
        crash.write_text("".join(traceback.format_exception(exc_type, exc, tb)),
                         encoding="utf-8")
    except Exception:
        pass


sys.excepthook = _global_crash_hook
threading.excepthook = lambda a: _global_crash_hook(a.exc_type, a.exc_value, a.exc_traceback)
import core.stats as stats
import core.models as models
import core.breaker as breaker
import core.router as router
import core.proxy as proxy
import core.protocols as protocols
import core.token_compress as token_compress
import core.prober as prober
from core.proxy import UpstreamError, SupplierBusy

_TIKTOKEN_ENC = None


def _get_enc():
    """tiktoken 编码器模块级缓存（避免每请求重复加载）。"""
    global _TIKTOKEN_ENC
    if _TIKTOKEN_ENC is None:
        import tiktoken
        _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
    return _TIKTOKEN_ENC


def _messages_text(msgs):
    """提取消息纯文本（只取 text 部分，跳过图片等非文本 part，避免 token 高估）。"""
    parts = []
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
    return " ".join(parts)


def _estimate_tokens(text):
    """精确 token 计算：使用 tiktoken 库。调用方应尽量用 asyncio.to_thread 挪出事件循环。"""
    if not text:
        return 0
    try:
        return len(_get_enc().encode(text))
    except Exception:
        # fallback: 英文约 4 字符/token，中文约 1.5 字符/token
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        non_cjk = len(text) - cjk
        return max(1, int(non_cjk / 4 + cjk / 1.5))


def _estimate_chars_tokens(n):
    """从近似字符数估 token（流式无 usage 兜底用，无需 decode 原文）。"""
    return max(1, int((n or 0) / 4))


def _extract_content_text(resp):
    """从响应中提取纯文本内容用于估算 token。"""
    if not isinstance(resp, dict):
        return ""
    choices = resp.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        return msg.get("content") or ""
    return ""
import core.smart_router as smart_router
import core.models as models
import core.model_params as model_params
import core.benchmarks as benchmarks
from core.proxy import UpstreamError

HERE = pathlib.Path(__file__).resolve().parent
LOG_PATH = config.DATA_ROOT / "logs" / "gateway.log"
WEB_DIR = HERE / "web"

# ---------------- 应用版本 ----------------
APP_NAME = "AI智能网关"
APP_VERSION = "2.1.1"

# ---------------- 全链路日志：后台线程异步落盘 + 按大小轮转 ----------------
# 请求热路径不再做同步文件 I/O（同步 write 会阻塞事件循环）；
# 日志超过 5MB 自动滚动为 gateway.log.1/.2/.3，避免无限增长。
_LOG_QUEUE = _queue.SimpleQueue()
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 3


def _log_writer():
    while True:
        line = _LOG_QUEUE.get()
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > _LOG_MAX_BYTES:
                for i in range(_LOG_BACKUPS - 1, 0, -1):
                    src = LOG_PATH.with_name(LOG_PATH.name + f".{i}")
                    dst = LOG_PATH.with_name(LOG_PATH.name + f".{i + 1}")
                    if src.exists():
                        dst.replace(src)
                LOG_PATH.replace(LOG_PATH.with_name(LOG_PATH.name + ".1"))
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


threading.Thread(target=_log_writer, daemon=True, name="gw-log").start()


def log_chain(tag, **kw):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {tag} " + " | ".join(f"{k}={str(v)[:400]}" for k, v in kw.items())
    _LOG_QUEUE.put(line)


def log_exc(tag, exc):
    """把异常与 traceback 写进全链路日志（不再裸吞）。"""
    try:
        import traceback
        tb = "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__))[-1500:]
        log_chain("EXC-" + tag, error=str(exc)[:300], tb=tb)
    except Exception:
        pass


def _max_body_bytes():
    try:
        return int(float((config.load_config().get("upstream") or {})
                         .get("maxRequestBodyMB", 32)) * 1024 * 1024)
    except Exception:
        return 32 * 1024 * 1024


async def _read_json_body(request: Request):
    """带大小上限地读取并解析 JSON 请求体（防超大上下文/压缩炸弹）。"""
    limit = _max_body_bytes()
    total = 0
    chunks = []
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise ValueError(f"请求体超过上限 {limit // (1024*1024)}MB")
            chunks.append(chunk)
    except Exception as e:
        if isinstance(e, ValueError):
            raise e
        raise
    try:
        raw = b"".join(chunks)
    except Exception:
        raw = b""
    if not raw:
        return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("请求体不是合法 JSON")
    return body if isinstance(body, dict) else {}


_CONTENT_PAT = re.compile(rb'"content"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _approx_content_bytes(chunk):
    """近似统计 chunk 里 content 文本片段长度（字节，含转义），
    不做完整 decode+json.loads，供「上游无 usage」时的 token 兜底估算。"""
    n = 0
    for m in _CONTENT_PAT.findall(chunk):
        n += len(m)
    return n


async def _iter_sse_events(chunks):
    """把上游字节流按 SSE 帧边界(\\n\\n)重组后逐个产出，解决网络块切分
    导致事件被切开而丢内容的问题。缓冲上限 2MB，超限只回吐剩余部分。"""
    buf = bytearray()
    MAX_BUF = 2 * 1024 * 1024
    async for chunk in chunks:
        buf.extend(chunk)
        while True:
            idx = buf.find(b"\n\n")
            if idx < 0:
                break
            ev = bytes(buf[:idx + 2])
            del buf[:idx + 2]
            yield ev
        if len(buf) > MAX_BUF:
            yield bytes(buf)
            buf.clear()
    if buf:
        yield bytes(buf)


def _run_prober():
    """在隔离线程（独立事件循环）中运行探测循环，避免探测阻塞主事件循环导致启动延迟。"""
    try:
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _loop.run_until_complete(prober.loop())
    except Exception:
        pass




async def _startup():
    """启动：优选探测循环 + 模型参数补全 + VIP 到期即时摘除与每小时巡检。"""
    try:
        await _startup_core()
    except BaseException as _e:   # 含 uvicorn 的 SystemExit(3) 兜底，落盘便于排查
        _write_crash_log(_e)
        raise


def _write_crash_log(exc: BaseException):
    try:
        import traceback
        crash = config.DATA_ROOT / "logs" / "crash.log"
        crash.parent.mkdir(parents=True, exist_ok=True)
        crash.write_text("".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__)), encoding="utf-8")
    except Exception:
        pass


async def _startup_core():
    # 默认鉴权：确保 adminToken 存在（首次自动生成并落盘），方便运维取用
    tok = config.get_admin_token()
    if tok:
        log_chain("ADMIN", token=("X-Admin-Token: " + tok if not os.environ.get("GW_ADMIN_TOKEN") else "env-覆盖"))
    threading.Thread(target=_run_prober, daemon=True).start()
    model_params.start_background_fill()


async def _shutdown():
    """关停：释放连接池。"""
    try:
        await proxy.close_pool()
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_app):
    await _startup()
    yield
    await _shutdown()


app = FastAPI(title=APP_NAME, lifespan=lifespan)


@app.exception_handler(ValueError)
async def _value_error_handler(_request, exc):
    """把带大小上限读取 / JSON 解析的 ValueError 转成友好 4xx，而不是 500。"""
    msg = str(exc) or "请求体不合法"
    code = 413 if ("上限" in msg or "MB" in msg) else 400
    return JSONResponse({"error": msg}, status_code=code)


def _breaker_fail(sid, err):
    """按异常类型记熔断；UpstreamError 携带的 Retry-After 作为冷却窗口。"""
    breaker.on_failure(sid, reset_override=getattr(err, "retry_after", None))


def _is_loopback(request: Request) -> bool:
    """本机访问判定：127.0.0.1 / ::1 / localhost（含反代后 Host 头）。"""
    host = (request.client.host if request.client else "") or ""
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    h = (request.headers.get("host") or "").split(":")[0].strip().lower()
    return h in ("127.0.0.1", "::1", "localhost")


async def _guard_admin(request: Request):
    """管理 API 鉴权：
    - 本机访问默认放行（面板本来就在本机打开，避免误伤）
    - 非本机访问需携带 X-Admin-Token 头（首次启动自动生成并落盘 config.json，
      或用环境变量 GW_ADMIN_TOKEN 覆盖；设空字符串 = 显式关闭远程鉴权）。"""
    if _is_loopback(request):
        return None
    token = config.get_admin_token()
    if token and (request.headers.get("x-admin-token") or "") != token:
        return JSONResponse(
            {"error": "需要 X-Admin-Token 请求头（本机打开面板无需；远程访问见 "
                      "data/config.json 的 adminToken，或设 GW_ADMIN_TOKEN）"},
            status_code=401)
    return None


def _inject_system_prompt(body, cfg):
    tmpl = cfg.get("systemPromptTemplate", "")
    if not tmpl:
        return body
    mode = cfg.get("systemPromptMode", "append")
    msgs = body.get("messages", [])
    if mode == "override":
        body["messages"] = [{"role": "system", "content": tmpl}] + [
            m for m in msgs if m.get("role") != "system"]
    else:  # append
        has_sys = any(m.get("role") == "system" for m in msgs)
        if has_sys:
            for m in msgs:
                if m.get("role") == "system":
                    m["content"] = m["content"] + "\n\n" + tmpl
        else:
            msgs.insert(0, {"role": "system", "content": tmpl})
    return body


def _default_max_tokens(body, cfg):
    """客户端未显式传 max_tokens 且请求带 tools（agent 工具调用）时，
    注入更大的输出上限，避免上游默认小值把 tool_call 截断成半截 JSON。"""
    if body.get("max_tokens") or not body.get("tools"):
        return body
    dft = int(float((cfg.get("upstream") or {}).get("defaultMaxTokens", 16000)))
    body["max_tokens"] = max(dft, 1)
    return body


_CONTEXT_ERR_PAT = re.compile(
    r"context.{0,20}length|context_length|maximum context|prompt is too long|"
    r"exceed.{0,20}(max|limit)|too many tokens|输入|上下文", re.I)


def _is_context_error(ue):
    """上游 400 是否因上下文超限（触发瘦身重试而非直接失败）。"""
    if ue.status != 400:
        return False
    return bool(_CONTEXT_ERR_PAT.search(str(ue)))


async def _execute_plan(plan, body, cfg, stream):
    """对单个候选计划发请求，成功返回 (openai_resp_dict 或 None表示流式已处理)，失败抛异常。
    同供应商配置多把 key 时：遇 429 限流自动切换下一把 key 重试（所有 key 均被打满才向上抛）。"""
    keys = plan.get("keys") or [plan["key"]]
    ki = plan.get("key_idx", 0)          # round-robin 选中的起点下标；429 时向后轮换
    while True:
        key = keys[ki]
        # 出站协议转换
        out_body, _ = protocols.openai_to_outbound(body, plan["protocol"])
        out_body["model"] = plan["model_id"]
        # OpenAI 兼容上游：请求随最后一块返回 usage，免去逐 chunk 正则扫描+全量解析
        if stream and (plan["protocol"] or "openai") == "openai" \
                and (cfg.get("upstream") or {}).get("includeUsage", True):
            out_body.setdefault("stream_options", {"include_usage": True})
        url = plan["base_url"] + plan["path"]
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json" if not stream else "text/event-stream",
        }
        content = json.dumps(out_body, ensure_ascii=False).encode("utf-8")
        log_chain("OUT", supplier=plan["supplier_id"], proto=plan["protocol"],
                  url=url, model=plan["model_id"], quota=plan["quota"])
        try:
            if stream:
                return await _stream_supplier(plan, url, headers, content, cfg)
            # 每供应商并发闸门：慢供应商在熔断触发前不再堆积请求
            slot = proxy.supplier_slot(
                plan["supplier_id"],
                float((cfg.get("upstream") or {}).get("slotWaitSeconds", 10)))
            try:
                await slot.acquire()
            except asyncio.TimeoutError:
                raise SupplierBusy(f"供应商并发已满(等待>{slot.timeout}s)")
            try:
                # 非流式：总时长封顶，静默/卡死供应商快速失败进入下一 fallback
                req_to = float((cfg.get("upstream") or {}).get("requestTimeout", 120))
                status, h, raw = await asyncio.wait_for(
                    proxy.call_upstream("POST", url, headers, content,
                                        {"proxy": plan["proxy"]}),
                    timeout=req_to)
            finally:
                slot.release()
            if status != 200:
                raise UpstreamError(status, raw[:200].decode("utf-8", "replace"),
                                    retry_after=proxy.retry_after_of(h))
            try:
                resp = json.loads(raw.decode("utf-8"))
            except Exception:
                raise UpstreamError(502, "上游响应非 JSON")
            resp_openai = protocols.response_to_openai(resp, plan["protocol"], plan["model_id"])
            # Token 消耗统计（优先用上游返回值，否则估算）
            u = resp_openai.get("usage", {}) if isinstance(resp_openai, dict) else {}
            pt = u.get("prompt_tokens", 0) or 0
            ct = u.get("completion_tokens", 0) or 0
            if pt == 0 and ct == 0:
                # 上游未返回 usage，从消息内容估算（只取文本 part）；tiktoken 挪出事件循环
                input_text = _messages_text(body.get("messages", []))
                output_text = _extract_content_text(resp_openai)
                pt = await asyncio.to_thread(_estimate_tokens, input_text)
                ct = await asyncio.to_thread(_estimate_tokens, output_text)
            stats.record_usage(plan["supplier_id"], plan["model_id"], pt, ct)
            log_chain("IN", supplier=plan["supplier_id"], status=status,
                      tokens=f"{pt}+{ct}",
                      content=(resp_openai.get("choices", [{}])[0].get("message", {}).get("content", "") or "")[:200])
            return resp_openai
        except UpstreamError as ue:
            # 429 限流且还有备用 key：当前 key 短期隔离（独立账号配额），切下一把非冷却 key 重试
            if getattr(ue, "status", 0) == 429:
                kc = float((cfg.get("upstream") or {}).get("keyCooldownSeconds", 120))
                router.mark_key_429(plan["supplier_id"], keys[ki], kc)
                nxt = -1
                for j in range(ki + 1, len(keys)):
                    if router.key_avail(plan["supplier_id"], keys[j]):
                        nxt = j
                        break
                if nxt >= 0:
                    log_chain("ROTATE", supplier=plan["supplier_id"],
                              key_idx=ki, total=len(keys), cooldown=kc,
                              error="429限流，自动切换下一把 key")
                    ki = nxt
                    continue
                log_chain("ROTATE", supplier=plan["supplier_id"],
                          total=len(keys), error="全部 key 均限流/冷却中")
            raise


async def _stream_supplier(plan, url, headers, content, cfg):
    """流式：连接复用 + 首字节(TTFT)门控 + 空闲心跳 + 完成时记账/熔断。

    - 并发闸门：单供应商在途请求受 maxConcurrentPerSupplier 限制，等待超时抛
      SupplierBusy 跳下一 fallback（慢上游在熔断触发前不再堆积请求）。
    - 首字节等待超过 streamFirstByteTimeout → 抛 UpstreamError(504)，
      交由上层 Fallback 换下一家（静默上游快速失败，不再拖住整条链路）。
    - 流开始后按 sseKeepAliveSeconds 向下游发 `: keepalive` 心跳，
      防 NAT/代理掐断长空闲流；空闲超过 streamIdleTimeout 优雅收尾。
    - 上游中途断流：向下游发一条 SSE error 事件（客户端不再拿到静默残缺
      内容而不自知），同时记入熔断器。
    - 客户端主动断开不记失败，避免误伤供应商。
    """
    up = cfg.get("upstream", {})
    fb_t = float(up.get("streamFirstByteTimeout", 30))
    idle_t = float(up.get("streamIdleTimeout", 120))
    # 免费/limited 池抖动频繁，单个源等不起 120~180s 的空闲沉默：
    # 免费池空闲上限压到 freePoolIdleTimeout（默认 60s），到点即优雅收尾、让上层换路；
    # unlimited 源（sensenova/agnes/note3…）仍给足 streamIdleTimeout，不误伤长推理。
    if idle_t > 0:
        sup = config.get_supplier(cfg, plan["supplier_id"])
        if models._is_free(sup):
            idle_t = min(idle_t, float(up.get("freePoolIdleTimeout", 60)))
    ka_t = float(up.get("sseKeepAliveSeconds", 15))

    slot = proxy.supplier_slot(
        plan["supplier_id"],
        float((cfg.get("upstream") or {}).get("slotWaitSeconds", 10)))
    try:
        await slot.acquire()
    except asyncio.TimeoutError:
        log_chain("FAIL", supplier=plan["supplier_id"],
                  error=f"并发闸门等待>{slot.timeout}s")
        raise SupplierBusy(f"供应商并发已满(等待>{slot.timeout}s)")

    t0 = time.time()
    try:
        # TTFT 门控覆盖「等响应头 + 等首字节」全程：静默/慢头上游快速失败 → fallback
        try:
            resp, client = await asyncio.wait_for(
                proxy.open_stream("POST", url, headers, content,
                                  {"proxy": plan["proxy"]}),
                timeout=fb_t)
        except asyncio.TimeoutError:
            log_chain("FAIL", supplier=plan["supplier_id"],
                      error=f"响应头超时>{fb_t}s")
            raise UpstreamError(504, f"响应头超时(>{fb_t}s)")
        ttft = None

        if hasattr(resp, "aiter_raw"):
            iter_ = resp.aiter_raw()
            remain = max(0.0, fb_t - (time.time() - t0))
            try:
                first = await asyncio.wait_for(iter_.__anext__(), timeout=remain)
            except StopAsyncIteration:
                first = None
            except asyncio.TimeoutError:
                await _close_stream_resp(resp)
                log_chain("FAIL", supplier=plan["supplier_id"],
                          error=f"首字节超时>{fb_t}s")
                raise UpstreamError(504, f"首字节超时(>{fb_t}s)")
        else:   # 非流式响应对象：一次性读完整内容
            iter_ = None
            first = getattr(resp, "content", None) or None
        ttft = round(time.time() - t0, 3)
        idle_since = time.time()

        usage_done = False
        up_usage = (0, 0)
        output_chars = [0]        # OpenAI 直通时的近似输出字符量（兜底 token 估算）
        conv_buf = bytearray()    # SSE 跨 chunk 重组缓冲（防网络块切分丢事件）
        conv_empty = [0]          # 连续无产出数据帧计数（疑似协议错乱）
        ended_ok = False
        client_closed = False
        upstream_error = None
        # 直通路径状态：是否看到 [DONE] / finish_reason=length（用于截断透明化）
        saw_done = [False]
        length_hit = [False]
        had_tools = b'"tools"' in content
        # 有状态流式转换器（anthropic/gemini → openai，支持 tool calls）
        conv = protocols.make_stream_converter(plan["protocol"], plan["model_id"])

        async def _emit(chunk):
            """喂一个上游原始字节块：
            - OpenAI 直通：字节级 usage 扫描 + 近似内容统计 + 原样透传（不再逐块 decode+loads）
            - 其它协议：先按 SSE 帧(\\n\\n)重组再交给有状态转换器，防事件被网络块切开。"""
            nonlocal usage_done, up_usage
            if conv is None:
                if b"[DONE]" in chunk:
                    saw_done[0] = True
                if b'"finish_reason":"leng' in chunk or b'"finish_reason": "leng' in chunk:
                    length_hit[0] = True
                # content_filter 透传会让 CodeBuddy 等客户端误判为"内容审核触发"并清空整段内容，
                # 统一改为 stop，保持行为与主流 provider 一致
                if b'"finish_reason"' in chunk and b'content_filter' in chunk:
                    chunk = chunk.replace(b'"finish_reason":"content_filter"',
                                          b'"finish_reason":"stop"')
                    chunk = chunk.replace(b'"finish_reason": "content_filter"',
                                          b'"finish_reason": "stop"')
                if not usage_done and b'"usage"' in chunk:
                    m = re.search(rb'"usage"\s*:\s*\{[^{}]*\}', chunk)
                    if m:
                        try:
                            u = json.loads(b"{" + m.group(0) + b"}").get("usage", {})
                            if u.get("prompt_tokens") or u.get("completion_tokens"):
                                up_usage = (int(u.get("prompt_tokens", 0)),
                                            int(u.get("completion_tokens", 0)))
                                stats.record_usage(plan["supplier_id"], plan["model_id"],
                                                   up_usage[0], up_usage[1])
                                usage_done = True
                        except Exception as exc:
                            log_exc("USAGE", exc)
                if not usage_done:
                    output_chars[0] += _approx_content_bytes(chunk)
                yield chunk                          # 原样透传
                return
            # ---- 协议转换路径：SSE 帧重组 + 连续失败检测 ----
            conv_buf.extend(chunk)
            if len(conv_buf) > 2 * 1024 * 1024:
                raise UpstreamError(502, "SSE 缓冲膨胀(>2MB)，上游流异常")
            while True:
                idx = conv_buf.find(b"\n\n")
                if idx < 0:
                    break
                ev = bytes(conv_buf[:idx + 2])
                del conv_buf[:idx + 2]
                if ev.lstrip()[:1] in (b":", b""):
                    continue                        # SSE 注释/空帧（含心跳）
                pieces = conv.feed(ev)
                if pieces:
                    conv_empty[0] = 0
                else:
                    conv_empty[0] += 1
                    if conv_empty[0] >= 40:
                        raise UpstreamError(502, "SSE 连续 40 帧解析失败(疑似协议错乱)")
                for piece in pieces:
                    yield piece

        async def gen():
            nonlocal usage_done, ended_ok, client_closed, idle_since, upstream_error
            reader_task = None
            reader_error = []      # 上游读取异常（区别于正常结束哨兵）
            try:
                # 后台读取协程：把上游块喂进队列（队列 get 可安全超时取消，
                # 不会像取消 aiter_raw.__anext__ 那样破坏流迭代器）
                if iter_ is not None:
                    queue = asyncio.Queue(maxsize=64)

                    async def _reader():
                        try:
                            async for chunk in iter_:
                                await queue.put(chunk)
                        except Exception as e:
                            reader_error.append(e)   # 上游断流/读失败，向上传递
                        finally:
                            try:
                                await queue.put(None)   # 结束哨兵
                            except Exception:
                                pass

                    reader_task = asyncio.create_task(_reader())

                    if first is not None:
                        try:
                            async for p in _emit(first):
                                yield p
                        except UpstreamError as ue:
                            upstream_error = ue
                            log_chain("STREAM-ERR", supplier=plan["supplier_id"],
                                      error=str(ue)[:200])
                    while upstream_error is None:
                        try:
                            chunk = await asyncio.wait_for(
                                queue.get(), timeout=ka_t)
                        except asyncio.TimeoutError:
                            if time.time() - idle_since > idle_t:
                                # 网关侧空闲超时：视为异常中断而非"优雅收尾"，
                                # 下游收到明确错误事件而不是被静默截断的"完整流"。
                                upstream_error = UpstreamError(
                                    504, f"流式空闲>{idle_t}s(推理长停顿或上游卡住)")
                                log_chain("STREAM-IDLE", supplier=plan["supplier_id"],
                                          model=plan["model_id"],
                                          idle=round(time.time() - t0, 1))
                                break
                            yield b": keepalive\n\n"   # 防 NAT/代理掐断空闲长流
                            continue
                        if chunk is None:
                            if reader_error:
                                upstream_error = reader_error[0]
                            break
                        idle_since = time.time()
                        try:
                            async for p in _emit(chunk):
                                yield p
                        except UpstreamError as ue:
                            upstream_error = ue
                            log_chain("STREAM-ERR", supplier=plan["supplier_id"],
                                      error=str(ue)[:200])
                            break
                else:   # 非流式响应对象：首块即全量
                    if first is not None:
                        try:
                            async for p in _emit(first):
                                yield p
                        except UpstreamError as ue:
                            upstream_error = ue
                            log_chain("STREAM-ERR", supplier=plan["supplier_id"],
                                      error=str(ue)[:200])
                if conv is not None and upstream_error is None and conv_buf:
                    # 尾部残留字节（可能是最后事件不带空行）：容错喂给转换器
                    try:
                        for piece in conv.feed(bytes(conv_buf)):
                            yield piece
                    except Exception as exc:
                        upstream_error = exc
                        log_exc("SSE-TAIL", exc)
                    conv_buf.clear()
                ended_ok = upstream_error is None
                if ended_ok and conv is not None:
                    # 上游未发收尾事件时补齐 [DONE]
                    for piece in conv.finalize():
                        yield piece
                elif ended_ok and conv is None and not saw_done[0]:
                    # 直通路径上游没发 [DONE]：补标准终结符，避免客户端挂起等流
                    yield b"data: [DONE]\n\n"
                if ended_ok and length_hit[0] and had_tools:
                    # 上游以 length 结束且请求带 tools：输出被 max_tokens 截断，
                    # 很可能是半截 tool_call。客户端可见 finish_reason=length；
                    # 网关侧留痕便于在面板日志定位。
                    log_chain("WARN-STREAM", supplier=plan["supplier_id"],
                              model=plan["model_id"],
                              finish="length",
                              detail="输出被输出上限截断，tool_call 可能不完整(可调大 defaultMaxTokens)")
                if upstream_error is not None:
                    # 明确告知下游流已异常中断（而非静默截断）
                    yield protocols._openai_sse_error(f"上游流中断: {upstream_error}")
                    log_chain("STREAM-ERR", supplier=plan["supplier_id"],
                              error=str(upstream_error)[:200])
            except GeneratorExit:
                client_closed = True
                raise
            finally:
                if reader_task is not None and not reader_task.done():
                    reader_task.cancel()
                slot.release()      # 幂等：归还并发闸门
                pt = up_usage[0]
                ct = up_usage[1]
                if not usage_done:
                    try:
                        body_json = json.loads(content.decode("utf-8"))
                        input_text = _messages_text(body_json.get("messages", []))
                        pt = await asyncio.to_thread(_estimate_tokens, input_text)
                        if conv is not None:
                            output_text = conv.collected_text
                            ct = await asyncio.to_thread(_estimate_tokens, output_text)
                        else:
                            # 直通路径：字节级近似字符量估算（免重载 tiktoken 与全量解析）
                            ct = _estimate_chars_tokens(output_chars[0])
                        stats.record_usage(plan["supplier_id"], plan["model_id"], pt, ct)
                    except Exception as exc:
                        log_exc("USAGE-FALLBACK", exc)
                lat = round(time.time() - t0, 3)
                if client_closed:
                    pass   # 客户端断开：不记失败，避免误伤供应商
                elif ended_ok:
                    stats.record_success(plan["supplier_id"], lat)
                    breaker.on_success(plan["supplier_id"])
                    stats.record_model_result(plan["supplier_id"], ok=True,
                                              latency=lat, ttft=ttft)
                    stats.record_success_event(plan["supplier_id"])
                    log_chain("IN-STREAM", supplier=plan["supplier_id"], status="ok",
                              model=plan["model_id"], tokens=f"{pt}+{ct}", ttft=ttft)
                else:
                    # 异常结束（供应商断连/超时）：记入熔断器，避免反复选到同一家
                    stats.record_failure(plan["supplier_id"], "stream-abnormal-end")
                    _breaker_fail(plan["supplier_id"], upstream_error)
                    stats.record_model_result(plan["supplier_id"], ok=False,
                                              latency=lat, ttft=ttft)
                    log_chain("IN-STREAM", supplier=plan["supplier_id"],
                              status="abnormal", model=plan["model_id"])
                await _close_stream_resp(resp)

        return StreamingResponse(gen(), media_type="text/event-stream")
    except BaseException:
        slot.release()      # gen 未启动/建流失败的兜底归还（release 幂等）
        raise


async def _close_stream_resp(resp):
    """关闭流式响应，把连接归还连接池（不关闭池内 client）。"""
    try:
        await resp.aclose()
    except Exception:
        pass


async def _route_core(body, pool, stream):
    cfg = config.load_config()
    body = _inject_system_prompt(body, cfg)
    body = _default_max_tokens(body, cfg)
    # token 压缩（feat#9）
    comp_cfg = cfg.get("tokenCompression", {})
    new_msgs, compressed, note = token_compress.compress_messages(body.get("messages", []), comp_cfg)
    if compressed:
        body["messages"] = new_msgs
        log_chain("COMPRESS", note=note)

    # 🧠 智能路由：开关开启时对所有别名(auto/auto2/…)做任务分类
    hint, route_meta = None, None
    alias0, _c0 = models.parse_model_request(body.get("model") or "")
    if cfg.get("smartRouting", {}).get("enabled"):
        try:
            route_meta = await smart_router.classify_best_effort(
                body.get("messages", []), cfg)
            hint = smart_router.hint_for(route_meta, cfg)
        except Exception as e:
            log_chain("ROUTE", error=str(e)[:120])

    info = {}
    prefer_non_free = bool(body.get("tools")) and \
        (cfg.get("upstream") or {}).get("toolPreferNonFree", False)
    est_toks = await asyncio.to_thread(
        token_compress.estimate_messages, body.get("messages") or [])
    plans, alias = router.select(body.get("model"), cfg, pool, hint=hint,
                                 info=info, prefer_non_free=prefer_non_free,
                                 est_input_tokens=est_toks)
    if route_meta:
        sid0 = plans[0]["supplier_id"] if plans else ""
        mid0 = plans[0]["model_id"] if plans else ""
        stats.record_auto(route_meta["category"], route_meta["via"], sid0, mid0)
        log_chain("ROUTE", category=route_meta["category"], via=route_meta["via"],
                  relaxed=info.get("relaxed"),
                  target=(f"{sid0}/{mid0}" if plans else "-"))
    if not plans:
        return JSONResponse({"error": f"无可用供应商匹配 model={body.get('model')} pool={pool}"}, status_code=502)
    last_err = None
    tried = []            # (supplier_id, model_id, fail_reason) 供穷尽诊断
    shrunk = False
    i = 0
    while i < len(plans):
        plan = plans[i]
        if not breaker.allow_request(plan["supplier_id"]):
            log_chain("SKIP", supplier=plan["supplier_id"], reason="breaker-open")
            tried.append((plan["supplier_id"], plan["model_id"], "熔断中"))
            i += 1
            continue
        t0 = time.time()
        try:
            result = await _execute_plan(plan, body, cfg, stream)
            if stream:
                return result
            stats.record_success(plan["supplier_id"], time.time() - t0)
            breaker.on_success(plan["supplier_id"])
            stats.record_model_result(plan["supplier_id"], ok=True,
                                      latency=time.time() - t0)
            stats.record_success_event(plan["supplier_id"])
            return JSONResponse(result)
        except SupplierBusy as busy:
            # 网关侧并发闸门满：非供应商故障，不计熔断，直接换下一家
            tried.append((plan["supplier_id"], plan["model_id"], "并发满"))
            last_err = str(busy)
            log_chain("FAIL", supplier=plan["supplier_id"], error=str(busy)[:200])
            i += 1
            continue
        except UpstreamError as ue:
            # 上下文超限：同一家瘦身重试一次（不换供应商），agent 长线程不断链
            if not shrunk and _is_context_error(ue):
                new_msgs = token_compress.shrink_for_retry(body.get("messages", []))
                if new_msgs:
                    body["messages"] = new_msgs
                    shrunk = True
                    log_chain("COMPRESS", note="context-overflow shrink, "
                              f"retry {plan['supplier_id']}/{plan['model_id']}")
                    continue
            lat = time.time() - t0
            is_429 = ue.status == 429
            stats.record_failure(plan["supplier_id"], str(ue))
            if ue.retryable_supplier_fault:
                _breaker_fail(plan["supplier_id"], ue)   # 携带 Retry-After 时精确冷却
            stats.record_model_result(plan["supplier_id"], ok=False,
                                      latency=lat, is_429=is_429)
            last_err = str(ue)
            reason = "限流(429)" if is_429 else f"HTTP {ue.status}"
            tried.append((plan["supplier_id"], plan["model_id"], reason))
            log_chain("FAIL", supplier=plan["supplier_id"],
                      supplier_fault=ue.retryable_supplier_fault, error=str(ue)[:200])
            i += 1
            continue
        except Exception as e:
            lat = time.time() - t0
            is_timeout = (isinstance(e, asyncio.TimeoutError)
                          or "timed out" in str(e).lower()
                          or "timeout" in str(e).lower())
            stats.record_failure(plan["supplier_id"], str(e))
            breaker.on_failure(plan["supplier_id"])
            stats.record_model_result(plan["supplier_id"], ok=False,
                                      latency=lat, is_timeout=is_timeout)
            last_err = str(e)
            reason = "超时" if is_timeout else type(e).__name__
            tried.append((plan["supplier_id"], plan["model_id"], reason))
            log_chain("FAIL", supplier=plan["supplier_id"], error=str(e)[:200])
            i += 1
            continue

    # 穷尽诊断：告诉用户每条路由为什么失败
    diag_lines = [f"{sid}/{mid}: {reason}" for sid, mid, reason in tried]
    rate_limited = sum(1 for _, _, r in tried if "429" in r or "限流" in r)
    no_key = sum(1 for _, _, r in tried if "Key" in r or "401" in r or "403" in r)
    timeouts = sum(1 for _, _, r in tried if "超时" in r)

    summary_parts = []
    if rate_limited:
        summary_parts.append(f"{rate_limited} 限流")
    if no_key:
        summary_parts.append(f"{no_key} 鉴权失败")
    if timeouts:
        summary_parts.append(f"{timeouts} 超时")
    other = len(tried) - len(summary_parts)
    if other > 0:
        summary_parts.append(f"{other} 其他")
    summary = f"{len(tried)} 条路由已检查（{', '.join(summary_parts)}）"

    return JSONResponse({
        "error": f"全部候选失败: {last_err}" if last_err else "全部候选失败",
        "diagnostics": {"summary": summary, "detail": diag_lines},
        "hint": "可增加 Key 或稍后重试"
    }, status_code=502)


# ---------------- HTTP 路由 ----------------
@app.get("/")
async def index():
    """统一入口：单页面板（监控/模型/添加/路由/调用方法/高级设置）。"""
    return FileResponse(str(HERE / "web" / "index.html"))


@app.get("/api/dynamics")
async def api_dynamics():
    """各供应商动态评分快照：EWMA 延迟/可靠性/惩罚值（面板监控用）。"""
    return stats.get_dynamics_summary()


# ---------------- 版本信息 ----------------
@app.get("/api/version")
async def api_version():
    """当前版本信息（本地展示用）。"""
    return {"name": APP_NAME, "version": APP_VERSION}


@app.get("/v1/models")
async def list_models(pool: str = None):
    """不传 pool = 列出所有启用模型；显式 ?pool= 才过滤。"""
    cfg = config.load_config()
    out = {"object": "list", "data": []}
    for s in config.get_enabled_suppliers(cfg, pool):
        for m in s.get("models", []):
            if m.get("enabled", True):
                out["data"].append({
                    "id": m["id"], "object": "model", "owned_by": s.get("name", s["id"]),
                    "quota": s.get("quota", "unlimited"),
                })
    # 别名提示
    out["aliases"] = (["auto", "fast", "daily", "vision",
                       "auto:free", "auto:paid", "auto:ctx>=N", "vision:free"]
                      + list((config.load_config().get("customRoutes") or {}).keys()))
    return out


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await _read_json_body(request)
    # 不传 pool = 在「所有启用供应商」中挑最合适的；池只作可选的访问范围过滤
    pool = request.query_params.get("pool") or None
    stream = bool(body.get("stream"))
    return await _route_core(body, pool, stream)


@app.post("/v1/messages")
async def anthropic_inbound(request: Request):
    """feat#10：Anthropic 客户端入站 -> 内部 OpenAI -> 回 Anthropic。
    流式：OpenAI SSE → Anthropic 事件流完整转换（message_start/content_block_*/
    message_delta/message_stop/error，支持 tool_calls → input_json_delta）。"""
    body = await _read_json_body(request)
    pool = request.query_params.get("pool") or None
    openai_body, _ = protocols.inbound_to_openai(body, "anthropic")
    stream = bool(openai_body.get("stream"))
    # 路由（内部 openai）。需要把结果转回 anthropic。
    cfg = config.load_config()
    openai_body = _inject_system_prompt(openai_body, cfg)
    comp_cfg = cfg.get("tokenCompression", {})
    nm, comp, note = token_compress.compress_messages(openai_body.get("messages", []), comp_cfg)
    if comp:
        openai_body["messages"] = nm
    pnf = bool(openai_body.get("tools")) and (cfg.get("upstream") or {}).get("toolPreferNonFree", False)
    _est = await asyncio.to_thread(
        token_compress.estimate_messages, openai_body.get("messages") or [])
    plans, alias = router.select(openai_body.get("model"), cfg, pool,
                                 prefer_non_free=pnf, est_input_tokens=_est)
    if not plans:
        return JSONResponse({"error": "no supplier"}, status_code=502)
    last_err = None
    for plan in plans:
        if not breaker.allow_request(plan["supplier_id"]):
            continue
        t0 = time.time()
        try:
            result = await _execute_plan(plan, openai_body, cfg, stream)
            if stream:
                conv = protocols.OpenAISSEToAnthropic(plan["model_id"])

                async def gen():
                    try:
                        # 按 SSE 帧边界重组，防上游块切分导致事件被拆
                        async for ev in _iter_sse_events(result.body_iterator):
                            for piece in conv.feed(ev):
                                yield piece
                        for piece in conv.finalize():   # 补齐收尾事件
                            yield piece
                    except GeneratorExit:
                        raise

                return StreamingResponse(gen(), media_type="text/event-stream")
            stats.record_success(plan["supplier_id"], time.time() - t0)
            breaker.on_success(plan["supplier_id"])
            return JSONResponse(protocols.openai_resp_to_anthropic(result, plan["model_id"]))
        except SupplierBusy as busy:
            last_err = str(busy)
            continue
        except Exception as e:
            stats.record_failure(plan["supplier_id"], str(e))
            _breaker_fail(plan["supplier_id"], e)
            last_err = str(e)
            continue
    return JSONResponse({"error": last_err}, status_code=502)


@app.post("/v1/responses")
async def responses_inbound(request: Request):
    """feat: OpenAI Responses API 入站（Codex CLI / Responses 客户端）
    -> 内部 OpenAI -> 回 Responses（流式/非流式，支持 tool_calls 事件）。"""
    body = await _read_json_body(request)
    pool = request.query_params.get("pool") or None
    openai_body = protocols.responses_to_openai(body)
    stream = bool(openai_body.get("stream"))
    cfg = config.load_config()
    comp_cfg = cfg.get("tokenCompression", {})
    nm, comp, note = token_compress.compress_messages(openai_body.get("messages", []), comp_cfg)
    if comp:
        openai_body["messages"] = nm
    pnf = bool(openai_body.get("tools")) and (cfg.get("upstream") or {}).get("toolPreferNonFree", False)
    _est = await asyncio.to_thread(
        token_compress.estimate_messages, openai_body.get("messages") or [])
    plans, alias = router.select(openai_body.get("model"), cfg, pool,
                                 prefer_non_free=pnf, est_input_tokens=_est)
    if not plans:
        return JSONResponse({"error": "no supplier"}, status_code=502)
    last_err = None
    for plan in plans:
        if not breaker.allow_request(plan["supplier_id"]):
            continue
        t0 = time.time()
        try:
            result = await _execute_plan(plan, openai_body, cfg, stream)
            if stream:
                conv = protocols.OpenAISSEToResponses(plan["model_id"])

                async def gen():
                    try:
                        # 按 SSE 帧边界重组，防上游块切分导致事件被拆
                        async for ev in _iter_sse_events(result.body_iterator):
                            for piece in conv.feed(ev):
                                yield piece
                        for piece in conv.finalize():   # 补齐收尾响应
                            yield piece
                    except GeneratorExit:
                        raise

                return StreamingResponse(gen(), media_type="text/event-stream")
            stats.record_success(plan["supplier_id"], time.time() - t0)
            breaker.on_success(plan["supplier_id"])
            return JSONResponse(protocols.openai_resp_to_responses(result, plan["model_id"]))
        except SupplierBusy as busy:
            last_err = str(busy)
            continue
        except Exception as e:
            stats.record_failure(plan["supplier_id"], str(e))
            _breaker_fail(plan["supplier_id"], e)
            last_err = str(e)
            continue
    return JSONResponse({"error": last_err}, status_code=502)


@app.post("/v1beta/models/{model}:generateContent")
async def gemini_inbound(model: str, request: Request):
    """feat#10：Gemini 客户端入站 -> 内部 OpenAI -> 回 Gemini。"""
    body = await _read_json_body(request)
    body["model"] = model
    pool = request.query_params.get("pool") or None
    openai_body, _ = protocols.inbound_to_openai(body, "gemini")
    stream = bool(openai_body.get("stream"))
    cfg = config.load_config()
    comp_cfg = cfg.get("tokenCompression", {})
    nm, comp, note = token_compress.compress_messages(openai_body.get("messages", []), comp_cfg)
    if comp:
        openai_body["messages"] = nm
    pnf = bool(openai_body.get("tools")) and (cfg.get("upstream") or {}).get("toolPreferNonFree", False)
    _est = await asyncio.to_thread(
        token_compress.estimate_messages, openai_body.get("messages") or [])
    plans, alias = router.select(openai_body.get("model"), cfg, pool,
                                 prefer_non_free=pnf, est_input_tokens=_est)
    if not plans:
        return JSONResponse({"error": "no supplier"}, status_code=502)
    last_err = None
    for plan in plans:
        if not breaker.allow_request(plan["supplier_id"]):
            continue
        t0 = time.time()
        try:
            result = await _execute_plan(plan, openai_body, cfg, stream)
            if stream:
                # OpenAI SSE → Gemini 流式（candidates/parts/functionCall）
                conv = protocols.OpenAISSEToGemini(plan["model_id"])

                async def gen():
                    try:
                        # 按 SSE 帧边界重组，防上游块切分导致事件被拆
                        async for ev in _iter_sse_events(result.body_iterator):
                            for piece in conv.feed(ev):
                                yield piece
                        for piece in conv.finalize():
                            yield piece
                    except GeneratorExit:
                        raise

                return StreamingResponse(gen(), media_type="text/event-stream")
            stats.record_success(plan["supplier_id"], time.time() - t0)
            breaker.on_success(plan["supplier_id"])
            return JSONResponse(protocols.openai_resp_to_gemini(result, plan["model_id"]))
        except SupplierBusy as busy:
            last_err = str(busy)
            continue
        except Exception as e:
            stats.record_failure(plan["supplier_id"], str(e))
            _breaker_fail(plan["supplier_id"], e)
            last_err = str(e)
            continue
    return JSONResponse({"error": last_err}, status_code=502)


# ---------------- 管理 API（feat#8 面板） ----------------
@app.get("/api/health")
async def api_health():
    cfg = config.load_config()
    sup = []
    for s in config.get_enabled_suppliers(cfg):
        st = stats.get_stat(s["id"])
        sup.append({"id": s["id"], "quota": s.get("quota"), "protocol": s.get("protocol"),
                    "breaker": breaker.get_state(s["id"]),
                    "success": st["success"], "fail": st["fail"],
                    "rate": round(stats.success_rate(s["id"]), 3),
                    "last_latency": st["last_latency"], "last_error": st["last_error"]})
    return {"suppliers": sup}


@app.get("/api/config")
async def api_get_config(request: Request):
    denied = await _guard_admin(request)
    if denied:
        return denied
    return config.load_config()


@app.post("/api/config")
async def api_set_config(request: Request):
    denied = await _guard_admin(request)
    if denied:
        return denied
    cfg = await _read_json_body(request)
    config.save_config(cfg)
    return {"ok": True}


# ---------------- 管理 API（面板专用） ----------------
@app.get("/api/logs")
async def api_logs(request: Request, lines: int = 300):
    """读取网关全链路日志尾部（deque 尾读，不整文件载入内存）。"""
    denied = await _guard_admin(request)
    if denied:
        return denied
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=max(1, lines))
        return PlainTextResponse("".join(tail))
    except Exception:
        return PlainTextResponse("")


@app.get("/api/metrics")
async def api_metrics(minutes: int = 30):
    """近 N 分钟请求量曲线（按分钟聚合）。"""
    return stats.get_metrics(minutes)


@app.get("/metrics")
async def api_prom_metrics():
    """Prometheus 文本指标（便于接入抓取/告警，只含聚合计数，不含任何密钥）。"""
    lines = ["# HELP gateway_supplier_requests_total 供应商请求计数",
             "# TYPE gateway_supplier_requests_total counter"]
    for sid, s in stats.get_stats().items():
        lines.append(f'gateway_supplier_requests_total{{supplier="{sid}",status="success"}} {s.get("success", 0)}')
        lines.append(f'gateway_supplier_requests_total{{supplier="{sid}",status="fail"}} {s.get("fail", 0)}')
        last = s.get("last_latency")
        if last is not None:
            lines.append(f'gateway_supplier_last_latency_seconds{{supplier="{sid}"}} {last}')
    lines.append("# HELP gateway_breaker_state 熔断器状态(0=closed 1=open 2=half-open)")
    lines.append("# TYPE gateway_breaker_state gauge")
    for sid in stats.get_stats().keys():
        st = breaker.get_state(sid)
        code = {"closed": 0, "open": 1, "half-open": 2}.get(st.get("state"), 0)
        lines.append(f'gateway_breaker_state{{supplier="{sid}"}} {code}')
        lines.append(f'gateway_breaker_effective_reset_seconds{{supplier="{sid}"}} {st.get("effective_reset", 0)}')
    perf = stats.get_dynamics_summary()
    lines.append("# TYPE gateway_penalty gauge")
    for sid, p in perf.items():
        lines.append(f'gateway_penalty{{supplier="{sid}"}} {p.get("penalty", 0)}')
    return PlainTextResponse("\n".join(lines),
                             media_type="text/plain; version=0.0.4")


@app.get("/api/usage")
async def api_usage(hours: int = 24):
    """Token 消耗统计：按供应商汇总 + 每家消耗最大的模型。支持最长720小时(30天)。"""
    hours = max(1, min(720, int(hours)))
    return stats.get_usage_report(hours)


@app.get("/api/top-models")
async def api_top_models(hours: int = 168, limit: int = 5):
    """全供应商调用量排行，按请求次数降序。"""
    hours = max(1, min(720, int(hours)))
    limit = max(1, min(20, int(limit)))
    return stats.get_top_models(hours, limit)


@app.get("/api/auto/stats")
async def api_auto_stats(hours: int = 24):
    """auto 智能分流统计：类别占比 / 判定来源 / 各类 Top 命中模型。"""
    hours = max(1, min(72, int(hours)))
    return stats.get_auto_report(hours)


@app.get("/api/catalog")
async def api_catalog():
    """模型广场：跨供应商的模型卡片（参数/评分/健康度），数据来自本地缓存。"""
    cfg = config.load_config()
    probes = stats.get_probe_stats(12)
    health = stats.get_stats()
    pools = cfg.get("pools", {})
    out = []
    for s in cfg.get("suppliers", []):
        sid = s.get("id")
        h = health.get(sid, {})
        pr = probes.get(sid, {})
        in_pools = [p for p, members in pools.items() if sid in (members or [])]
        for m in s.get("models", []):
            mid = m.get("id") or ""
            if not mid:
                continue
            info = model_params.get_cached(mid)
            tag, score = benchmarks.model_score(mid)
            out.append({
                "model": mid,
                "supplier": sid,
                "protocol": s.get("protocol", "openai"),
                "enabled": m.get("enabled", True),
                "multimodal": bool(m.get("multimodal")),
                "modalities": m.get("modalities", []),
                "ctx_local": m.get("contextWindow") or 0,
                "pools": in_pools,
                "quota": s.get("quota", "unlimited"),
                "success_rate": h.get("success", 0) / h["total"] if h.get("total") else None,
                "avg_latency": pr.get("avg_latency"),
                "bench_tag": tag,
                "bench_score": score,
                "params_text": info.get("params_text") if info else None,
                "ctx_text": info.get("ctx_text") if info else None,
                "released": info.get("released") if info else None,
                "org": info.get("org") if info else None,
                "desc": info.get("desc") if info else None,
                "source_url": info.get("source") if info else None,
                "params_lookup": ("cached" if info else
                                  ("miss" if model_params._load().get(mid.lower(), {}).get("_miss")
                                   else "pending")),
            })
    return {"models": out,
            "params_cache": {"ok": model_params.cached_count(),
                             "miss": model_params.miss_count()}}


_refresh_task = None


@app.post("/api/model_params/refresh")
async def api_model_params_refresh():
    """触发在线补全所有启用模型的参数。立即返回，后台逐条限速执行，
    避免同步网络调用阻塞事件循环；进度看 /api/catalog 的 params_cache。"""
    global _refresh_task
    if _refresh_task and not _refresh_task.done():
        return {"ok": True, "started": 0, "running": True}
    # 清掉历史负缓存（早期查找逻辑较弱产生的 miss），允许用新策略重试
    purged = 0
    for k in list(model_params._load().keys()):
        if model_params._load()[k].get("_miss"):
            model_params._load().pop(k)
            purged += 1
    model_params._save()
    cfg = config.load_config()
    targets, seen = [], set()
    for s in cfg.get("suppliers", []):
        if not s.get("enabled", True):
            continue
        for m in s.get("models", []):
            mid = (m.get("id") or "").strip().lower()
            if (mid and mid not in seen
                    and model_params.get_cached(mid) is None
                    and not model_params._load().get(mid, {}).get("_miss")):
                seen.add(mid)
                targets.append(mid)

    async def _run():
        import asyncio as _aio
        for mid in targets:
            await _aio.to_thread(model_params.lookup_online, mid)
        model_params._save()
        log_chain("PARAMS", filled=len(targets))

    _refresh_task = asyncio.create_task(_run())
    return {"ok": True, "started": len(targets), "purged_miss": purged,
            "running": True}


@app.get("/api/probe/report")
async def api_probe_report():
    """自动优选排名与建议。"""
    rep = prober.build_report()
    rep["status"] = prober.status()
    rep["cfg"] = config.load_config().get("autoProbe", {})
    return rep


@app.post("/api/probe/run")
async def api_probe_run():
    """手动触发一轮探测。"""
    results = await prober.run_round()
    return {"ok": True, "results": results}


@app.post("/api/probe/apply")
async def api_probe_apply():
    """采纳建议：给综合分最高的供应商打 preferred 标记（路由置顶）。"""
    rep = prober.build_report()
    rows = [r for r in rep["rows"] if r["score"] is not None]
    if not rows:
        return JSONResponse({"ok": False, "error": "无可评估供应商"}, status_code=400)
    best = rows[0]
    cfg = config.load_config()
    for s in cfg.get("suppliers", []):
        s["preferred"] = (s["id"] == best["id"])
    config.save_config(cfg)
    return {"ok": True, "applied": best["id"], "score": best["score"]}


@app.post("/api/settings/reset")
async def api_settings_reset(request: Request):
    """一键恢复默认设置。保留：模型数据（供应商/池）、自定义路由；
    只重置其余偏好类设置（提示词/压缩/探测/智能路由开关等）。"""
    denied = await _guard_admin(request)
    if denied:
        return denied
    cfg = config.load_config()
    keep = ("port", "suppliers", "pools", "customRoutes")
    preserved = {k: cfg.get(k) for k in keep}
    fresh = config._default_config()
    fresh.update(preserved)
    config.save_config(fresh)
    log_chain("SETTINGS-RESET")
    return {"ok": True}


@app.post("/api/supplier/test")
async def api_supplier_test(request: Request):
    """测试上游连通性并拉取模型列表（OpenAI 兼容 /models）。"""
    denied = await _guard_admin(request)
    if denied:
        return denied
    body = await _read_json_body(request)
    base = (body.get("baseUrl") or "").strip().rstrip("/")
    key = (body.get("key") or "").strip()
    if not base:
        return {"ok": False, "error": "缺少 baseUrl"}
    headers = {}
    if key and key != "__KEYLESS__":
        headers["Authorization"] = f"Bearer {key}"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(base + "/models", headers=headers)
            data = r.json()
            models = []
            for m in (data.get("data") or []):
                if isinstance(m, dict) and m.get("id"):
                    models.append(m["id"])
                elif isinstance(m, str):
                    models.append(m)
            return {"ok": True, "status": r.status_code, "count": len(models), "models": models[:400]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.post("/api/suppliers/upsert")
async def api_upsert_supplier(request: Request):
    denied = await _guard_admin(request)
    if denied:
        return denied
    sup = await _read_json_body(request)
    if not sup.get("id"):
        return JSONResponse({"ok": False, "error": "缺少 id"}, status_code=400)
    cfg = config.load_config()
    config.upsert_supplier(cfg, sup)
    config.save_config(cfg)
    return {"ok": True}


@app.delete("/api/suppliers/{sid}")
async def api_delete_supplier(sid: str, request: Request):
    denied = await _guard_admin(request)
    if denied:
        return denied
    cfg = config.load_config()
    target = config.get_supplier(cfg, sid)
    if target and target.get("locked"):
        return JSONResponse(
            {"ok": False,
             "error": f"{sid} 为发布方锁定的渠道（locked），不可删除；"
                      "如需移除可整体恢复默认设置或等待清单更新"},
            status_code=403)
    before = len(cfg.get("suppliers", []))
    cfg["suppliers"] = [s for s in cfg.get("suppliers", []) if s.get("id") != sid]
    removed = before - len(cfg["suppliers"])
    for k in list(cfg.get("pools", {}).keys()):
        cfg["pools"][k] = [i for i in cfg["pools"][k] if i != sid]
    config.save_config(cfg)
    return {"ok": True, "removed": removed}


def _is_up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except Exception:
        return False


def _open_browser_when_up(url: str, timeout: float = 45):
    """启动后等端口就绪再开面板（首次 import 需几秒）。"""
    import webbrowser
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_up(config.get_port()):
            webbrowser.open(url)
            return
        time.sleep(1)


def main():
    args = [a for a in sys.argv[1:]]
    open_ui = "--open" in args
    # 窗口化打包（console=False）下 sys.stdout/stderr 为 None，uvicorn 日志
    # 格式化会访问 .isatty() 而崩溃 → 重定向到 discard 设备（业务日志走 gateway.log）
    _devnull = None
    if getattr(sys, "frozen", False) and (sys.stdout is None or sys.stderr is None):
        _devnull = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = _devnull
        if sys.stderr is None:
            sys.stderr = _devnull
    try:
        port = config.get_port()
    except Exception:
        port = 18123
    try:
        host = config.get_bind_host()
    except Exception:
        host = "127.0.0.1"
    url = f"http://127.0.0.1:{port}/"
    # 幂等：已在运行则只开面板并退出（“点开就用”）
    if _is_up(port):
        if open_ui:
            import webbrowser
            webbrowser.open(url)
        return
    if open_ui:
        threading.Thread(target=_open_browser_when_up, args=(url,),
                         daemon=True).start()
    try:
        import uvloop       # 性能可选依赖：Linux/macOS 下提升事件循环吞吐
        uvloop.install()
    except Exception:
        pass
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    except Exception:
        import traceback
        crash = config.DATA_ROOT / "logs" / "crash.log"
        try:
            crash.parent.mkdir(parents=True, exist_ok=True)
            crash.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass


if __name__ == "__main__":
    main()
