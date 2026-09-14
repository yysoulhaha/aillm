# -*- coding: utf-8 -*-
"""🧠 智能路由：任务感知的 auto 分流。

三层：
  ① 规则速判（零开销）：图片part→VISION；超长→LONG_CTX；代码关键词→CODE；
     推理关键词→REASONING；极短无疑问→SIMPLE
  ② LLM 分类器（仅规则拿不准时）：用最快模型发 ~100token 分类请求，
     返回 JSON；超时/失败回退「不干预」，绝不阻塞主请求
  ③ hint_for(category) → 生成选择提示给 router.select()：
     SIMPLE→速度优先&bench上限；REASONING/CODE→bench下限；
     VISION→多模态；LONG_CTX→按消息估算tokens要求上下文

分类结果由 server 记入 ROUTE 日志与 /api/auto/stats 统计。
"""
import asyncio
import hashlib
import json
import re
import time

import core.config as config
import core.proxy as proxy
import core.stats as stats

# ---------------------------------------------------------------- 规则速判

DEFAULT_RULES = {"simpleMaxChars": 15, "longCtxTokens": 8000}
REASONING_KW = ["证明", "推导", "论证", "为什么", "原因", "分析一下", "深入",
                "step by step", "一步步", "逐步", "推理", "反思", "评估对比",
                "优缺点", "权衡", "策略", "设计一个方案"]
CODE_KW = ["代码", "函数", "报错", "debug", "调试", "python", "javascript",
           "typescript", "java ", "c++", "sql", "正则", "算法", "脚本",
           "实现一个", "写一个接口", "重构", "编译"]


def _text_of(content):
    """提取消息文本；同时检测是否带图片 part。返回 (text, has_image)。"""
    if isinstance(content, str):
        return content, False
    if isinstance(content, list):
        texts, has_img = [], False
        for p in content:
            if not isinstance(p, dict):
                continue
            t = p.get("type", "")
            if "image" in t or "image_url" in p:
                has_img = True
            if isinstance(p.get("text"), str):
                texts.append(p["text"])
        return "\n".join(texts), has_img
    return "", False


def est_tokens(text):
    """粗估 tokens：CJK 字符按 ~1 token/字，其余按 4 字符/token。"""
    t = text or ""
    if not t:
        return 1
    cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    return max(1, cjk + (len(t) - cjk) // 4)


def rule_classify(messages, cfg):
    """返回 (category 或 None, ctx_needed 或 None)。"""
    rules = {**DEFAULT_RULES, **(cfg.get("smartRouting", {}).get("rules") or {})}
    all_text_parts, has_image, last_user = [], False, ""
    for m in messages or []:
        t, img = _text_of(m.get("content"))
        all_text_parts.append(t)
        has_image = has_image or img
        if m.get("role") == "user":
            last_user = t or last_user
    full_text = "\n".join(all_text_parts)

    if has_image:
        return "VISION", None
    ctx_need = est_tokens(full_text)
    if ctx_need >= int(rules["longCtxTokens"]):
        return "LONG_CTX", ctx_need
    low = full_text.lower()
    if "```" in full_text or any(k in low for k in CODE_KW):
        return "CODE", None
    if any(k in low for k in REASONING_KW):
        return "REASONING", None
    lu = (last_user or "").strip()
    if 0 < len(lu) <= int(rules["simpleMaxChars"]) and not any(
            c in lu for c in "？？？？") :
        return "SIMPLE", None
    return None, None


# ---------------------------------------------------------------- LLM 分类器

CLASSIFY_PROMPT = (
    "你是AI任务分类器。判断用户消息，只输出一行JSON，格式："
    '{"complexity":"low|medium|high","is_code":true或false,'
    '"need_vision":true或false,"need_long_ctx":true或false}。'
    "complexity：闲聊/翻译/简单问答=low；常规写作/总结=medium；"
    "数学证明/复杂推理/多步规划/专业分析=high。不要输出任何其它文字。")


def _pick_classifier_target(cfg):
    """选分类模型：配置指定 > 探测延迟最低的免费家启用模型 > 池内第一家。
    classifier.pool 为空 = 在所有启用供应商中选。"""
    sc = cfg.get("smartRouting", {}).get("classifier", {})
    pool = sc.get("pool") or None
    want = (sc.get("model") or "").strip()
    sups = [s for s in config.get_enabled_suppliers(cfg, pool)]
    if want:                                   # 显式指定具体模型 id
        for s in sups:
            for m in s.get("models", []):
                if m.get("enabled", True) and m["id"] == want:
                    return s, m["id"]
    probes = stats.get_probe_stats(12)
    best = None                                # (latency, supplier, model)
    for s in sups:
        p = probes.get(s["id"], {})
        lat = p.get("avg_latency")
        ms = [m["id"] for m in s.get("models", []) if m.get("enabled", True)]
        if not ms:
            continue
        if lat is not None and (best is None or lat < best[0]):
            best = (lat, s, ms[0])
    if best:
        return best[1], best[2]
    for s in sups:                             # 兜底：池内第一家首个启用模型
        ms = [m["id"] for m in s.get("models", []) if m.get("enabled", True)]
        if ms:
            return s, ms[0]
    return None, None


def _extract_json(text):
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


async def llm_classify(messages, cfg):
    """让高速小模型分类；失败返回 None（不干预）。"""
    sc = cfg.get("smartRouting", {}).get("classifier", {})
    supplier, model = _pick_classifier_target(cfg)
    if not supplier:
        return None
    max_chars = int(sc.get("maxChars", 500))
    sample = ""
    for m in reversed(messages or []):
        t, _ = _text_of(m.get("content"))
        if t.strip():
            sample = t.strip()
            break
    sample = sample[:max_chars]
    key = None
    ks = supplier.get("keys", [])
    plain = next((k for k in ks if isinstance(k, dict)
                  and not k.get("supportedModels")), None)
    key = (plain or {}).get("key") if isinstance(plain, dict) else (
        plain if plain else (ks[0].get("key") if ks and isinstance(ks[0], dict)
                             else (ks[0] if ks else None)))
    auth = f"Bearer {key}" if key and key != "__KEYLESS__" else ""
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    body = {"model": model,
            "messages": [{"role": "system", "content": CLASSIFY_PROMPT},
                         {"role": "user", "content": sample}],
            "max_tokens": 60, "temperature": 0, "stream": False}
    try:
        code, _h, raw = await asyncio.wait_for(
            proxy.call_upstream("POST", supplier["baseUrl"].rstrip("/")
                                + "/chat/completions", headers,
                                json.dumps(body, ensure_ascii=False).encode(),
                                {"proxy": supplier.get("proxy")}),
            timeout=float(sc.get("timeoutSeconds", 4)))
        if code != 200:
            return None
        resp = json.loads(raw.decode("utf-8", "replace"))
        content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        d = _extract_json(content)
        if not d:
            return None
        comp = (d.get("complexity") or "").lower()
        if d.get("need_vision"):
            return "VISION"
        if d.get("need_long_ctx"):
            return "LONG_CTX"
        if d.get("is_code"):
            return "CODE"
        if comp == "high":
            return "REASONING"
        if comp == "low":
            return "SIMPLE"
        return None
    except Exception:
        return None


async def classify(messages, cfg):
    """主入口：返回 {category, via, ctx_needed}。category 可为 None(不干预)。
    同步等待 LLM 分类结果（最慢可能 timeoutSeconds 秒）——手动/调试用。"""
    cat, ctx_need = rule_classify(messages, cfg)
    if cat:
        return {"category": cat, "via": "rules", "ctx_needed": ctx_need}
    cat = await llm_classify(messages, cfg)
    return {"category": cat, "via": "llm", "ctx_needed": None}


# ------------------------------------------------------ 低延迟并发分类（生产路径）
# 规则速判零开销；规则不中时 LLM 分类在后台跑并带 TTL 缓存：
#   - budgetMs>0：最多阻塞 budgetMs 毫秒等结果，超时即用规则结论继续（绝不拖死主请求）
#   - budgetMs=0：完全不阻塞，分类在后台完成并写缓存，下一次相似请求直接命中
# 这既保住分类的「智能自动化」价值（重复模式零开销受益），又不给首包增加明显延迟。
_classify_cache = {}          # sha1(texts) -> (decision, expire_ts)


def _cache_key(messages):
    texts = [str(m.get("content") or "") for m in (messages or [])]
    return hashlib.sha1("|".join(texts).encode("utf-8", "replace")).hexdigest()


def _classify_ttl(cfg):
    return float(cfg.get("smartRouting", {}).get("classifier", {})
                 .get("cacheTtlSeconds", 300))


async def _classify_and_cache(key, messages, cfg):
    ttl = _classify_ttl(cfg)
    dec = await llm_classify(messages, cfg)
    if dec:
        _classify_cache[key] = (dec, time.time() + ttl)
    return dec


async def classify_best_effort(messages, cfg):
    """生产路径：规则优先 + 后台/限时 LLM 分类 + TTL 缓存。
    返回 decision dict 或 None；不阻塞主请求超过 classifier.budgetMs。"""
    sc = cfg.get("smartRouting", {}).get("classifier", {})
    budget = max(0.0, float(sc.get("budgetMs", 0))) / 1000.0
    cat, ctx_need = rule_classify(messages, cfg)
    if cat:                                   # 规则命中：零开销，直接返回并写缓存
        dec = {"category": cat, "via": "rules", "ctx_needed": ctx_need}
        _classify_cache[_cache_key(messages)] = (dec, time.time() + _classify_ttl(cfg))
        return dec
    key = _cache_key(messages)
    hit = _classify_cache.get(key)
    if hit and hit[1] > time.time():
        d = hit[0]
        return {"category": d.get("category"), "via": "llm-cache",
                "ctx_needed": None}
    task = asyncio.create_task(_classify_and_cache(key, messages, cfg))
    if budget <= 0:
        return None                           # 后台学习，不阻塞
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=budget)
    except asyncio.TimeoutError:
        return None


def hint_for(decision, cfg):
    """类别 → router.select 的选择提示。"""
    if not decision or not decision.get("category"):
        return None
    routes = cfg.get("smartRouting", {}).get("routes", {})
    r = dict(routes.get(decision["category"]) or {})
    h = {}
    if r.get("prefer"):
        h["prefer"] = r["prefer"]
    if "benchMin" in r:
        h["benchMin"] = float(r["benchMin"])
    if "benchMax" in r:
        h["benchMax"] = float(r["benchMax"])
    if r.get("vision"):
        h["vision"] = True
    if r.get("ctxFromMessages") and decision.get("ctx_needed"):
        h["minCtx"] = int(decision["ctx_needed"] * 1.5)
    return h or None
