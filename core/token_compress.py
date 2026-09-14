# -*- coding: utf-8 -*-
"""Token 压缩中间件（feat#9）。

当上下文估计 token 超过阈值时，对发往上游的 messages 做压缩，省 token（OmniRoute 称 15-95%）。
策略（可配置）：
  - truncate：保留 system + 最近的若干轮，超预算的早轮整体丢弃（head 摘要 + tail 原文）
  - summarize：把被丢弃的早期轮次合并成一段摘要占位（此处用简单拼接摘要，研究可接 LLM）
可开关（tokenCompression.enabled）。

事件驱动的瘦身（shrink_for_retry，上下文超限后触发）：
  1. 先把超大的 tool 结果 / 超长文本块按「头 + 尾」截断（旧结果优先，保留近期上下文）
  2. 截断收益不足时，再丢最旧轮次（保留最近一半）
借鉴：open-multi-agent 的 compressToolResults / strands-agents 优雅截断 tool 结果。
"""
import threading
from functools import lru_cache

_ENC = None


def _warm_tokenizer():
    """后台预热 tiktoken（其首次 get_encoding 需联网下载 BPE 文件）。
    预热期间估算走启发式，绝不阻塞请求。"""
    global _ENC
    try:
        import tiktoken
        _ENC = tiktoken.get_encoding("o200k_base")
    except Exception:
        _ENC = False


threading.Thread(target=_warm_tokenizer, daemon=True, name="tiktoken-warm").start()


@lru_cache(maxsize=256)
def _est_text_tokens(text):
    e = _ENC
    if e:
        try:
            return max(1, len(e.encode(text or "")))
        except Exception:
            pass
    return max(1, len(text or "") // 4)


def _est_tokens(text):
    if text is None:
        return 0
    if isinstance(text, str):
        return _est_text_tokens(text)
    return _est_text_tokens(json_dumps(text))


def estimate_messages(messages):
    return sum(_est_tokens(m.get("content") if isinstance(m.get("content"), str) else m.get("content"))
               for m in messages)


def json_dumps(o):
    import json
    try:
        return json.dumps(o, ensure_ascii=False)
    except Exception:
        return str(o)


_MAX_BLOCK_CHARS = 6000      # 单块内容超过该长度才截断（head+tail 保留）
_HEAD = 1500                 # 截断后保留的头部字符
_TAIL = 1500                 # 截断后保留的尾部字符
_TRUNC_MARK = "\n…[已截断，保留头尾]…\n"


def _cap_text(text):
    """超长文本按「头+尾」截断（不改变结构语义：整段内容仍是普通文本）。"""
    if not isinstance(text, str) or len(text) <= _MAX_BLOCK_CHARS:
        return text
    return text[:_HEAD] + _TRUNC_MARK + text[-_TAIL:]


def _cap_block(msg):
    """对单条消息内容做块级截断：str 直接截；OpenAI 内容块列表逐个 text 截。"""
    content = msg.get("content")
    if isinstance(content, str):
        capped = _cap_text(content)
        if capped is not content:
            return {**msg, "content": capped}
    elif isinstance(content, list):
        changed = False
        parts = []
        for p in content:
            if isinstance(p, dict):
                for k in ("text", "input_text", "output_text"):
                    if isinstance(p.get(k), str) and len(p[k]) > _MAX_BLOCK_CHARS:
                        p = {**p, k: _cap_text(p[k])}
                        changed = True
            parts.append(p)
        if changed:
            return {**msg, "content": parts}
    return msg


def shrink_for_retry(messages, keep=0.5):
    """上下文超限后的事件驱动瘦身（返回新 messages，无法瘦则 None）：
    1. 先对超大的 tool/文本块做头尾截断（旧结果优先，保近期上下文）
    2. 截断收益不足时再丢最旧轮次（保留最近 keep 比例，至少 4 条）
    借鉴 open-multi-agent compressToolResults / strands-agents 优雅截断策略。"""
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    conv = [m for m in messages if m.get("role") != "system"]
    capped = [_cap_block(m) for m in conv]
    truncated_any = any(c is not m for c, m in zip(capped, conv))
    before = estimate_messages(conv)
    if truncated_any:
        reduced = sys_msgs + capped
        if estimate_messages(reduced) <= max(1, before * 0.75):
            return reduced
        if len(conv) <= 4:      # 无可丢且截断收益不足：返回截断版（仍比原小）
            return reduced
        out = sys_msgs + capped[-max(4, int(len(conv) * keep)):]
        return out
    if len(conv) <= 4:
        return None
    n = max(4, int(len(conv) * keep))
    out = sys_msgs + conv[-n:]
    if len(out) >= len(messages):
        return None
    return out


def compress_messages(messages, cfg):
    """返回 (new_messages, compressed_bool, note)。"""
    enabled = cfg.get("enabled", False)
    if not enabled:
        return messages, False, ""
    max_tok = cfg.get("maxContextTokens", 8000)
    strategy = cfg.get("strategy", "truncate")
    total = estimate_messages(messages)
    if total <= max_tok:
        return messages, False, ""
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    conv = [m for m in messages if m.get("role") != "system"]
    if strategy == "truncate":
        kept, used = [], 0
        for m in reversed(conv):
            cost = _est_tokens(m.get("content") if isinstance(m.get("content"), str) else json_dumps(m.get("content")))
            if used + cost <= max_tok or not kept:
                kept.insert(0, m)
                used += cost
            else:
                break
        new = sys_msgs + kept
        note = f"truncate: {total}->{estimate_messages(new)} tok, 丢弃 {len(conv)-len(kept)} 轮"
        return new, True, note
    else:  # summarize
        if len(conv) <= 1:
            return messages, False, ""
        drop = conv[:-1]
        last = conv[-1]
        summary = "【早期对话摘要】" + " ".join(
            (m.get("content") if isinstance(m.get("content"), str) else json_dumps(m.get("content")))[:200] for m in drop)
        new = sys_msgs + [{"role": "user", "content": summary}, last]
        note = f"summarize: {total}->{estimate_messages(new)} tok"
        return new, True, note
