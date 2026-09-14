# -*- coding: utf-8 -*-
"""自动优选探测器。

两级探测策略：
  A. 存活探测 —— 每轮每家测 1 个代表模型（游标轮流），驱动供应商级熔断/Fallback 判断
  B. 轮询巡检 —— 每轮再测 K 个其它启用模型（跨轮覆盖全部模型），产出模型级健康表

综合分 = 稳定性(成功率) × 速度分(最快/该家延迟) × 能力权重
  能力权重：优先用手填 capability；否则查公开测评知识表(core/benchmarks.py)
采纳建议 = 榜首打 preferred 标记，路由同配额级置顶。
"""
import asyncio
import json
import time

import core.config as config
import core.proxy as proxy
import core.stats as stats
import core.benchmarks as benchmarks

_state = {"last_run": None, "probing": False, "last_results": []}
_cursor = {}          # sid -> 下一个巡检下标（跨轮轮询所有启用模型）


def status():
    return dict(_state)


def _pick_key(supplier):
    ks = supplier.get("keys", [])
    for k in ks:                      # 优先无模型限制的 Key
        if isinstance(k, dict) and not k.get("supportedModels"):
            return k.get("key")
    for k in ks:
        if not isinstance(k, dict):
            return k
    if ks:
        k0 = ks[0]
        return k0.get("key") if isinstance(k0, dict) else k0
    return None


async def _probe_model(supplier, model, cfg):
    """对指定供应商的指定模型发一次极小请求。"""
    sid = supplier["id"]
    key = _pick_key(supplier)
    base = supplier["baseUrl"].rstrip("/")
    url = base + "/chat/completions"
    auth = f"Bearer {key}" if key and key != "__KEYLESS__" else ""
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    body = {"model": model,
            "messages": [{"role": "user", "content": "1"}],
            "max_tokens": 8, "stream": False}
    content = json.dumps(body, ensure_ascii=False).encode("utf-8")
    timeout = int(cfg.get("autoProbe", {}).get("timeoutSeconds", 20))
    t0 = time.time()
    try:
        code, _h, _raw = await proxy.call_upstream(
            "POST", url, headers, content,
            {"proxy": supplier.get("proxy")},
            timeout=timeout)
        lat = time.time() - t0
        return {"sid": sid, "model": model, "ok": code == 200,
                "latency": round(lat, 3),
                "error": None if code == 200 else f"HTTP {code}"}
    except Exception as e:
        return {"sid": sid, "model": model, "ok": False,
                "latency": round(time.time() - t0, 3), "error": str(e)[:120]}


def _round_models(supplier, per_round):
    """本轮要测的模型列表：第1个=存活探测，其余=轮询巡检（游标推进）。"""
    models = [m["id"] for m in supplier.get("models", []) if m.get("enabled", True)]
    if not models:
        return []
    sid = supplier["id"]
    i = _cursor.get(sid, 0) % len(models)
    live = models[i]
    picks = [live]
    for step in range(1, max(1, per_round)):
        picks.append(models[(i + step) % len(models)])
    _cursor[sid] = i + 1
    return picks


async def run_round():
    """跑一轮探测（并发），结果写入 stats 并返回明细。"""
    cfg = config.load_config()
    sups = config.get_enabled_suppliers(cfg)
    per_round = int(cfg.get("autoProbe", {}).get("probeModelsPerRound", 2))
    jobs = []
    for s in sups:
        for m in _round_models(s, per_round):
            jobs.append(_probe_model(s, m, cfg))
    results = await asyncio.gather(*jobs, return_exceptions=True)
    out = []
    for r in results:
        if isinstance(r, Exception):
            r = {"sid": "?", "model": "", "ok": False,
                 "latency": None, "error": str(r)[:120]}
        stats.record_probe(r["sid"], r["ok"], r["latency"] if r["latency"] else 999,
                           r.get("model", ""))
        out.append(r)
    _state["last_run"] = time.time()
    _state["last_results"] = out
    return out


async def loop():
    """后台循环：按 autoProbe 配置周期探测；关闭时低频空转。"""
    while True:
        try:
            cfg = config.load_config()
            ap = cfg.get("autoProbe", {})
            if ap.get("enabled"):
                if not _state["probing"]:
                    _state["probing"] = True
                    try:
                        await run_round()
                    finally:
                        _state["probing"] = False
                await asyncio.sleep(max(5, int(ap.get("intervalMinutes", 15))) * 60)
            else:
                await asyncio.sleep(30)
        except Exception:
            await asyncio.sleep(60)


def build_report():
    """综合排名 + 模型级健康。推荐 = 综合分最高者。"""
    cfg = config.load_config()
    ps = stats.get_probe_stats(6)
    hs = stats.get_stats()
    rows = []
    for s in config.get_enabled_suppliers(cfg):
        sid = s["id"]
        p = ps.get(sid, {})
        st = hs.get(sid, {})
        rate = p.get("rate")
        if rate is None and st.get("total"):
            rate = st["success"] / st["total"]
        cap = benchmarks.supplier_capability(s)
        pools = [k for k, v in cfg.get("pools", {}).items() if sid in v]
        # 模型级健康（最差的排前面，便于一眼看到问题模型）
        mh = [{"model": m, **v} for m, v in (p.get("models") or {}).items()]
        mh.sort(key=lambda x: (x["rate"], -x["avg_latency"]))
        rows.append({"id": sid, "protocol": s.get("protocol"),
                     "quota": s.get("quota"), "pools": pools,
                     "capability": cap["weight"],
                     "capability_source": cap["source"],
                     "bench_score": cap["score"],
                     "preferred": bool(s.get("preferred")),
                     "probes": p.get("probes", 0),
                     "success_rate": rate,
                     "avg_latency": p.get("avg_latency") or st.get("last_latency"),
                     "models_health": mh})
    lats = [r["avg_latency"] for r in rows if r["avg_latency"]]
    min_lat = min(lats) if lats else None
    for r in rows:
        stab = r["success_rate"] if r["success_rate"] is not None else 0.5
        speed = (min_lat / r["avg_latency"]) if (min_lat and r["avg_latency"]) else 0.5
        r["stability"] = round(stab, 3)
        r["speed"] = round(min(speed, 1.5), 3)
        r["score"] = round(stab * r["speed"] * r["capability"], 3)
    rows.sort(key=lambda r: -r["score"])
    if rows:
        rows[0]["recommended"] = True
        for r in rows[1:]:
            r["recommended"] = False
    return {"rows": rows, "generated_at": time.time()}
