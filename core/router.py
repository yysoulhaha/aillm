# -*- coding: utf-8 -*-
"""路由层。

feat#1 熔断+多级Fallback / #2 配额感知 / #3 按key模型过滤 /
#5 别名表达式 / 🧠智能路由hint（bench界、速度优先、松弛梯子）/
new-api式同级加权随机。
"""
import json
import random
import re
import time

import core.models as models
import core.config as config
import core.breaker as breaker
import core.benchmarks as benchmarks
import core.stats as stats

_rr = {}          # supplier_id -> 下一个 key 下标（round-robin）
_KEY_COOLDOWN = {}   # (supplier_id, key) -> 解冻时刻(unix)：429 被打满的账号短期隔离


def mark_key_429(sid, key, seconds):
    """单把 key 429 后隔离 cooldown 秒：独立账号配额打满，round-robin 期间跳过它。"""
    _KEY_COOLDOWN[(sid, key)] = time.time() + max(5.0, float(seconds))


def key_avail(sid, key, now=None):
    now = time.time() if now is None else now
    return (sid, key) not in _KEY_COOLDOWN or _KEY_COOLDOWN[(sid, key)] <= now


def _bench(mid):
    return benchmarks.model_score(mid)[1]


def _model_meta(supplier, mid):
    for m in supplier.get("models", []):
        if m.get("id") == mid:
            return m
    return {"id": mid, "enabled": True}


def _keys_supporting(supplier, mid):
    out = []
    for i, k in enumerate(supplier.get("keys", [])):
        if isinstance(k, dict):
            sup = k.get("supportedModels")
            if sup is None or mid in sup:
                out.append((i, k.get("key")))
        else:
            out.append((i, k))
    return out


def _probe_latency(sid):
    p = stats.get_probe_stats(12).get(sid)
    if p and p.get("avg_latency") is not None:
        return p["avg_latency"]
    st = stats.get_stat(sid)
    return st.get("last_latency")


def _match_hint(meta, h):
    """候选模型是否满足选择提示；ctx 未知视为通过。"""
    if not h:
        return True
    b = _bench(meta["id"])
    if "benchMin" in h and b < h["benchMin"]:
        return False
    if "benchMax" in h and b > h["benchMax"]:
        return False
    if h.get("vision") and not meta.get("multimodal"):
        return False
    if h.get("tools") and not _supports_tools(meta):
        return False
    if "minCtx" in h:
        ctx = meta.get("contextWindow") or 0
        if ctx and ctx < h["minCtx"]:
            return False
    return True


_TOOL_PAT = re.compile(
    r"gpt|claude|gemini|deepseek|qwen.{0,4}(?:\d{2,}|max|plus)|"
    r"llama.?3\.[23]|llama.?4|70b|120b|ultra|nemotron.{0,6}super|"
    r"glm.[45]|mistral-large|command-a|minimax|mimo",
    re.I)


def _supports_tools(meta):
    """判断模型是否支持工具调用：
    1. 显式标记 supports_tools=True → 支持
    2. 显式标记 False 或 absent → 按名称启发式推断
    """
    v = meta.get("supports_tools")
    if v is True:
        return True
    if v is False:
        return False
    # 启发式：大模型/知名系列大概率支持工具调用
    return bool(_TOOL_PAT.search(meta.get("id", "")))


def _collect(sups, alias, constraints, h):
    """按当前约束收集 (supplier, model_id) 候选。"""
    out = []
    for s in sups:
        if alias in models.GROUP_ALIASES:
            for m in s.get("models", []):
                if not m.get("enabled", True):
                    continue
                meta = _model_meta(s, m["id"])
                if constraints["vision"] and not meta.get("multimodal"):
                    continue
                if not models.match_constraints(s, meta, constraints):
                    continue
                if not _match_hint(meta, h):
                    continue
                out.append((s, m["id"]))
        else:
            meta = _model_meta(s, alias)
            if meta.get("enabled", True) and \
               models.match_constraints(s, meta, constraints) and \
               _match_hint(meta, h):
                out.append((s, alias))
    return out


def _relax_ladder(hint):
    """松弛梯子：严格 → 去bench界 → 去vision → 去minCtx → 无hint。"""
    if not hint:
        return [None]
    ladder = [dict(hint)]
    step = {k: v for k, v in hint.items() if k not in ("benchMin", "benchMax")}
    if step != hint:
        ladder.append(step)
    step2 = {k: v for k, v in step.items() if k != "vision"}
    if step2 != step:
        ladder.append(step2)
    step3 = {k: v for k, v in step2.items() if k != "minCtx"}
    if step3 != step2:
        ladder.append(step3)
    ladder.append(None)
    uniq, seen = [], set()
    for x in ladder:
        key = json.dumps(x, sort_keys=True) if x else "none"
        if key not in seen:
            seen.add(key)
            uniq.append(x)
    return uniq


def _ctx_fits(supplier, mid, est_tokens):
    """候选是否装得下估算输入（上下文预检查）。声明 contextWindow 缺失视为通过；
    请求具体模型且无其它替代时也放行（避免把唯一候选也跳过）。"""
    meta = _model_meta(supplier, mid)
    ctx = meta.get("contextWindow") or 0
    if not ctx or not est_tokens:
        return True
    return est_tokens <= ctx * 0.9    # 留 10% 头寸给 system/tools/输出


def _weighted_shuffle(entries):
    """同排序键内加权随机（new-api 式：weight+10 基础偏移）。
    entries 元素为 (rank_tuple, weight, supplier, mid)，返回同结构列表。"""
    if len(entries) <= 1:
        return list(entries)
    total = sum(e[1] for e in entries)
    order, pool = [], list(entries)
    while pool:
        pick = random.uniform(0, total)
        acc, idx = 0, 0
        for i, e in enumerate(pool):
            acc += e[1]
            if acc >= pick:
                idx = i
                break
        total -= pool[idx][1]
        order.append(pool.pop(idx))
    return order


def _collect_custom(sups, route_cfg):
    """自定义路由：候选 = 路由配置列出的、且供应商已启用的模型。"""
    wanted = set(route_cfg.get("models") or [])
    out = []
    for s in sups:
        for m in s.get("models", []):
            if m.get("id") in wanted and m.get("enabled", True):
                out.append((s, m["id"]))
    return out


def _dynamic_score(supplier, mid, hint):
    """多轴综合分 0~100。

    轴：静态能力 + 实测速度 + 实测可靠性（余量暂占位）
    权重按 hint.prefer 微调；无实测数据(total<3)→100% 静态回退。
    最终 -= 惩罚扣分。
    """
    sid = supplier["id"]
    static = _bench(mid)
    # 模型级路由惩罚（routingPenalty）：用于人工降权「看似高分实则大上下文空转」的模型，
    # 与供应商级 penalty 语义一致（*2 后从动态分中扣除，使其不再霸榜 LONG_CTX/CODE 等路由）。
    # 提前计算并在「静态回退」与「综合分」两条路径都应用，避免高分空转模型在无实测数据时依旧霸榜。
    mp = float(_model_meta(supplier, mid).get("routingPenalty") or 0)

    perf = stats.get_supplier_perf(sid)
    if not perf or perf["total_requests"] < 3:
        return round(max(0, static - mp * 2), 1)           # 纯静态回退（含模型级惩罚）

    # 实测速度分：1s→95, 10s→50, 20s+→5
    lat = perf.get("latency_ewma")
    speed = max(5, min(95, 95 - (lat - 1) / 19 * 90)) if lat else 60
    # 流式场景用 TTFT 替代整体延迟
    ttft = perf.get("ttft_ewma")
    if ttft is not None:
        speed_ttft = max(5, min(95, 95 - ttft * 30))
        speed = min(speed, speed_ttft)                  # 取较差者

    # 可靠性
    reliability = perf.get("success_ewma", 1.0) * 100

    # 余量（暂用固定值占位，后续接 FLM monthlyTokenBudget）
    headroom = 80.0

    # 按场景调整权重
    prefer = (hint or {}).get("prefer", "")
    if prefer == "speed":
        weights = {"cap": 0.20, "speed": 0.45, "rel": 0.25, "head": 0.10}
    elif isinstance((hint or {}).get("benchMin"), (int, float)):
        weights = {"cap": 0.50, "speed": 0.15, "rel": 0.25, "head": 0.10}
    else:
        weights = {"cap": 0.35, "speed": 0.25, "rel": 0.25, "head": 0.15}

    score = (static * weights["cap"] +
             speed * weights["speed"] +
             reliability * weights["rel"] +
             headroom * weights["head"])

    # 惩罚扣分
    penalty = stats.get_penalty(sid)
    score -= penalty * 2

    # 模型级路由惩罚（复用上面算好的 mp）
    score -= mp * 2

    return round(max(0, score), 1)


def select(req_model, cfg, pool, hint=None, info=None, prefer_non_free=False,
           est_input_tokens=None):
    """返回候选计划列表（已排序）。

    hint: 智能路由提示 {prefer,benchMin,benchMax,vision,minCtx} 或 None；
          对所有别名请求(auto/auto2/…)生效（智能路由开关开启时）。
    info: 调用方可传入 dict，接收 {'relaxed': bool}（是否走了降级）。
    prefer_non_free: 带 tools 的请求场景，免费/limited 源降权，避免免费池抖动中断任务。
    est_input_tokens: 估算的输入 token 数；存在多个候选时，跳过声明
          contextWindow 装不下的模型（预检查，避免上游 400 context_length_exceeded）。
    """
    alias, constraints = models.parse_model_request(req_model)
    custom = (cfg.get("customRoutes") or {}).get(alias)
    pool_members = set(cfg.get("pools", {}).get(pool, [])) if pool else None
    suppliers = config.get_enabled_suppliers(cfg)
    if pool_members:
        suppliers = [s for s in suppliers if s.get("id") in pool_members]

    if custom:
        # 自定义路由：用户选定的模型池，也享受 hint 过滤 + 松弛梯度
        candidates = _collect_custom(suppliers, custom)
        used_hint, relaxed = hint, False
        if hint and candidates:
            filtered = [(s, mid) for s, mid in candidates
                        if _match_hint(_model_meta(s, mid), hint)]
            if filtered:
                candidates = filtered
            else:
                # 松弛梯度：逐步放宽 hint 约束
                for h in _relax_ladder(hint)[1:]:
                    filtered = [(s, mid) for s, mid in candidates
                                if _match_hint(_model_meta(s, mid), h)]
                    if filtered:
                        candidates = filtered
                        used_hint, relaxed = h, True
                        break
        prefer_speed = bool(used_hint and used_hint.get("prefer") == "speed")
    else:
        # 标准路由：auto/fast/daily/vision → 全部启用模型
        used_hint, relaxed = hint, False
        candidates = []
        for i, h in enumerate(_relax_ladder(hint)):
            candidates = _collect(suppliers, alias, constraints, h)
            if candidates:
                used_hint, relaxed = h, (i > 0)
                break
        prefer_speed = bool(used_hint and used_hint.get("prefer") == "speed")
    if info is not None:
        info["relaxed"] = relaxed
        info["used_hint"] = used_hint

    # ---- 上下文预检查：多个候选且估算装不下时，跳过小上下文模型 ----
    if est_input_tokens and len(candidates) > 1:
        kept = [(s, mid) for s, mid in candidates
                if _ctx_fits(s, mid, est_input_tokens)]
        if kept:
            info_skipped = len(candidates) - len(kept)
            candidates = kept
            if info is not None:
                info["ctx_skipped"] = info_skipped

    # ---- 多轴动态评分排序 ----
    scored = []           # (score, weight, supplier, mid)
    for s, mid in candidates:
        sc = _dynamic_score(s, mid, used_hint)
        if prefer_non_free and models._is_free(s):
            sc -= 15      # 免费/limited 源降权（带 tools 请求优先稳定供应商）
        w = int(s.get("weight", 10)) + 10
        if s.get("preferred"):
            sc += 8       # ⭐采纳加成
        scored.append((sc, w, s, mid))

    # 按分数降序，同分±2内加权随机
    scored.sort(key=lambda x: -x[0])
    ordered = []
    i = 0
    while i < len(scored):
        j = i
        while j < len(scored) and (scored[i][0] - scored[j][0]) <= 2:
            j += 1
        ordered.extend(_weighted_shuffle(scored[i:j]))
        i = j

    # ---- 组装计划 + 显式 Fallback 链 ----
    plans, seen = [], set()
    for _, _w, s, mid in ordered:
        sid = s["id"]
        if sid in seen:
            continue
        seen.add(sid)
        plan = _build_plan(s, mid, cfg)
        if plan:
            plans.append(plan)

    wanted_custom = set((custom or {}).get("models") or []) if custom else None
    for s in list(suppliers):
        for fb in s.get("fallback", []):
            fs = config.get_supplier(cfg, fb)
            if fs and fs.get("enabled", True) and fb not in seen:
                seen.add(fb)
                if wanted_custom is not None:
                    # 自定义路由：fallback 也只能用列表内的模型，没有则跳过
                    mid = next((m["id"] for m in fs.get("models", [])
                                if m["id"] in wanted_custom
                                and m.get("enabled", True)), None)
                else:
                    mid = alias if (alias not in models.GROUP_ALIASES
                                    and alias not in (cfg.get("customRoutes") or {})
                                    and _model_meta(fs, alias).get("enabled", True)) else None
                if mid is None and wanted_custom is None:
                    em = [m["id"] for m in fs.get("models", []) if m.get("enabled", True)]
                    mid = em[0] if em else None
                # fallback 也必须满足提示（vision/tools 等），否则跳过，避免把
                # 多模态请求路由到纯文本模型
                if used_hint and mid and not _match_hint(_model_meta(fs, mid), used_hint):
                    continue
                if mid:
                    plan = _build_plan(fs, mid, cfg)
                    if plan:
                        plans.append(plan)
    return plans, alias


def _build_plan(supplier, mid, cfg):
    keys = _keys_supporting(supplier, mid)      # [(完整列表下标, key), ...]
    if not keys:
        return None
    now = time.time()
    live = [(i, k) for i, k in keys if key_avail(supplier["id"], k, now)]
    chosen_pool = live or keys          # 全部在冷却 → 退回全列表（由发送层 429 兜底）
    idxs = [i for i, _ in chosen_pool]
    n = _rr.get(supplier["id"], 0) % len(idxs)
    chosen_idx = idxs[n]
    _rr[supplier["id"]] = n + 1
    key = next(k for i, k in chosen_pool if i == chosen_idx)
    proto = supplier.get("protocol", "openai")
    base = supplier["baseUrl"].rstrip("/")
    path = "/chat/completions"
    if proto == "anthropic":
        path = "/messages"
    elif proto == "gemini":
        path = f"/models/{mid}:generateContent"
    b = supplier.get("breaker") or {}
    breaker.configure(supplier["id"], b.get("threshold", 5), b.get("reset", 60))
    return {
        "supplier_id": supplier["id"],
        "model_id": mid,
        "key": key,
        "key_idx": chosen_idx,          # 选中 key 在完整 keys 列表中的下标（发送层从此处开始轮换）
        "keys": [k for _, k in keys],   # 全量 key 列表：发起层遇 429 自动切下一把重试
        "protocol": proto,
        "base_url": base,
        "path": path,
        "proxy": supplier.get("proxy"),
        "quota": supplier.get("quota", "unlimited"),
        "is_free": models._is_free(supplier),
    }
