# -*- coding: utf-8 -*-
"""运行时健康统计（feat#8 面板可视化 + feat#1 熔断依据）。

线程安全；记录每个供应商的成功/失败次数、最近延迟、最近错误、状态。
Token 消耗数据持久化到 data/usage.json，重启不丢失。
"""
import json
import pathlib
import threading
import time
from collections import deque

from core import paths

_DATA_DIR = paths.data_home() / "data"
_USAGE_FILE = _DATA_DIR / "usage.json"
_PERF_FILE = _DATA_DIR / "perf.json"
_save_timer = None
_perf_timer = None

_stats = {}          # supplier_id -> dict
_lock = threading.Lock()
_series = deque()    # 请求量时间序列 [(ts, sid, ok)]（feat#8 曲线图）
_SERIES_MAX_AGE = 3700  # 秒，保留约 1 小时


def _ensure(sid):
    if sid not in _stats:
        _stats[sid] = {
            "id": sid, "total": 0, "success": 0, "fail": 0,
            "last_latency": None, "last_error": None,
            "last_success_at": None, "status": "unknown",
        }
    return _stats[sid]


def _save_usage():
    """持久化 usage 到磁盘（延迟 2 秒防频繁写入）。"""
    global _save_timer
    if _save_timer:
        _save_timer.cancel()
    _save_timer = threading.Timer(2.0, _do_save_usage)
    _save_timer.daemon = True
    _save_timer.start()


def _do_save_usage():
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _lock:
            data = list(_usage)
        _USAGE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_usage():
    """启动时从磁盘加载 usage。"""
    global _usage
    try:
        if _USAGE_FILE.exists():
            data = json.loads(_USAGE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                cutoff = time.time() - _USAGE_MAX_AGE
                _usage = deque(r for r in data if r.get("ts", 0) >= cutoff)
    except Exception:
        pass


def _record_point(sid, ok):
    now = time.time()
    _series.append((now, sid, ok))
    cutoff = now - _SERIES_MAX_AGE
    while _series and _series[0][0] < cutoff:
        _series.popleft()


def record_success(sid, latency):
    with _lock:
        s = _ensure(sid)
        s["total"] += 1
        s["success"] += 1
        s["last_latency"] = round(latency, 3)
        s["last_error"] = None
        s["last_success_at"] = time.time()
        s["status"] = "ok"
        _record_point(sid, True)


def record_failure(sid, error):
    with _lock:
        s = _ensure(sid)
        s["total"] += 1
        s["fail"] += 1
        s["last_error"] = str(error)[:300]
        s["status"] = "error"
        _record_point(sid, False)


def get_metrics(minutes=30):
    """按分钟聚合近 N 分钟请求量。返回 labels/counts/total/per_supplier。"""
    minutes = max(5, min(120, int(minutes)))
    now = int(time.time())
    with _lock:
        pts = [(t, s, ok) for (t, s, ok) in _series if t >= now - minutes * 60]
    labels, counts = [], []
    for i in range(minutes - 1, -1, -1):
        end = now - i * 60
        labels.append(time.strftime("%H:%M", time.localtime(end)))
        counts.append(0)
    for t, _sid, _ok in pts:
        idx = minutes - 1 - int((now - t) // 60)
        if 0 <= idx < minutes:
            counts[idx] += 1
    per = {}
    for _t, sid, _ok in pts:
        per[sid] = per.get(sid, 0) + 1
    return {"labels": labels, "counts": counts, "total": len(pts),
            "per_supplier": dict(sorted(per.items(), key=lambda kv: -kv[1]))}


def get_stats():
    with _lock:
        return {k: dict(v) for k, v in _stats.items()}


def get_stat(sid):
    with _lock:
        return dict(_ensure(sid))


def success_rate(sid):
    s = get_stat(sid)
    if s["total"] == 0:
        return 1.0
    return s["success"] / s["total"]


# ---------------- Token 消耗流水 ----------------
_usage = deque()         # [{ts,sid,model,pt,ct}]
_USAGE_MAX_AGE = 86400 * 3   # 保留 3 天


def record_usage(sid, model, pt, ct):
    with _lock:
        now = time.time()
        _usage.append({"ts": now, "sid": sid, "model": model,
                       "pt": int(pt or 0), "ct": int(ct or 0)})
        cutoff = now - _USAGE_MAX_AGE
        while _usage and _usage[0]["ts"] < cutoff:
            _usage.popleft()
    _save_usage()


def get_usage_report(hours=24):
    """按供应商聚合 token；并给出每家消耗最大的模型（稳定性参考）。"""
    now = time.time()
    start = now - hours * 3600
    with _lock:
        rows = [r for r in _usage if r["ts"] >= start]
    by_sup, by_model = {}, {}
    for r in rows:
        s = by_sup.setdefault(r["sid"], {"requests": 0, "prompt_tokens": 0,
                                         "completion_tokens": 0, "total_tokens": 0})
        s["requests"] += 1
        s["prompt_tokens"] += r["pt"]
        s["completion_tokens"] += r["ct"]
        s["total_tokens"] += r["pt"] + r["ct"]
        m = by_model.setdefault(r["sid"], {})
        mm = m.setdefault(r["model"], {"requests": 0, "total_tokens": 0})
        mm["requests"] += 1
        mm["total_tokens"] += r["pt"] + r["ct"]
    top_models = {sid: sorted(m.items(), key=lambda kv: -kv[1]["total_tokens"])[:5]
                  for sid, m in by_model.items()}
    return {"hours": hours, "by_supplier": by_sup,
            "top_models_per_supplier": top_models}


def get_top_models(hours=168, limit=5):
    """返回全供应商调用量排行，按请求次数降序。"""
    now = time.time()
    start = now - hours * 3600
    with _lock:
        rows = [r for r in _usage if r["ts"] >= start]
    agg = {}
    for r in rows:
        key = (r["sid"], r["model"])
        if key not in agg:
            agg[key] = {"supplier": r["sid"], "model": r["model"],
                        "requests": 0, "prompt_tokens": 0,
                        "completion_tokens": 0, "total_tokens": 0}
        a = agg[key]
        a["requests"] += 1
        a["prompt_tokens"] += r["pt"]
        a["completion_tokens"] += r["ct"]
        a["total_tokens"] += r["pt"] + r["ct"]
    return sorted(agg.values(), key=lambda x: -x["requests"])[:limit]


# ---------------- 优选探测历史 ----------------
_probes = deque(maxlen=600)   # [{ts,sid,ok,latency}]


def record_probe(sid, ok, latency, model=""):
    with _lock:
        _probes.append({"ts": time.time(), "sid": sid, "model": model or "",
                        "ok": bool(ok), "latency": float(latency or 0)})


def get_probe_stats(window_hours=6):
    now = time.time()
    start = now - window_hours * 3600
    with _lock:
        rows = [p for p in _probes if p["ts"] >= start]
    agg = {}
    for p in rows:
        o = agg.setdefault(p["sid"], {"n": 0, "ok_n": 0, "lat_sum": 0.0})
        o["n"] += 1
        o["ok_n"] += 1 if p["ok"] else 0
        o["lat_sum"] += p["latency"]
        mo = o.setdefault("_models", {}).setdefault(p["model"] or "?", {"n": 0, "ok_n": 0, "lat_sum": 0.0})
        mo["n"] += 1
        mo["ok_n"] += 1 if p["ok"] else 0
        mo["lat_sum"] += p["latency"]
    out = {}
    for sid, o in agg.items():
        models = {m: {"n": v["n"],
                      "rate": round(v["ok_n"] / v["n"], 3),
                      "avg_latency": round(v["lat_sum"] / v["n"], 3)}
                  for m, v in o.pop("_models", {}).items()}
        out[sid] = {"probes": o["n"], "success": o["ok_n"],
                    "rate": (o["ok_n"] / o["n"]) if o["n"] else None,
                    "avg_latency": (o["lat_sum"] / o["n"]) if o["n"] else None,
                    "models": models}
    return out


# ---------------- auto 智能分流流水 ----------------
_auto = deque()          # [{ts,category,via,sid,model}]
_AUTO_MAX_AGE = 86400 * 3   # 保留 3 天


def record_auto(category, via, sid="", model=""):
    if not category:
        return
    with _lock:
        now = time.time()
        _auto.append({"ts": now, "category": category,
                      "via": via or "", "sid": sid or "", "model": model or ""})
        cutoff = now - _AUTO_MAX_AGE
        while _auto and _auto[0]["ts"] < cutoff:
            _auto.popleft()


def get_auto_report(hours=24):
    """各类别请求数/占比、判定来源分布、每类 Top 命中模型。"""
    now = time.time()
    start = now - hours * 3600
    with _lock:
        rows = [r for r in _auto if r["ts"] >= start]
    by_cat, by_via, top = {}, {}, {}
    for r in rows:
        c = r["category"] or "?"
        bc = by_cat.setdefault(c, {"count": 0})
        bc["count"] += 1
        v = r["via"] or "unknown"
        bv = by_via.setdefault(v, 0)
        by_via[v] += 1
        key = f"{r['sid']}/{r['model']}" if r["model"] else r["sid"]
        tm = top.setdefault(c, {})
        tm[key] = tm.get(key, 0) + 1
    total = len(rows)
    top_models = {c: [{"target": k, "count": n} for k, n in
                      sorted(t.items(), key=lambda kv: -kv[1])[:3]]
                  for c, t in top.items()}
    return {"hours": hours, "total": total, "by_category": by_cat,
            "by_via": by_via, "top_targets_per_category": top_models}


# ---------------- 多轴动态评分引擎（EWMA + 惩罚） ----------------
# 每个供应商维护一组指数移动平均指标，供 router._dynamic_score 使用。
# α=0.3：最近一次结果占30%，历史占70%——兼顾灵敏度与平滑度。

_EWMA_ALPHA = 0.3
_perf = {}               # sid -> {latency_ewma, ttft_ewma, success_ewma,
                         #         timeout_count, rate_limit_count,
                         #         total_requests, last_updated}
_penalty = {}            # sid -> float（429×3 / 超时×1 / 失败×0.5 累计）
_PENALTY_CAP = 15
_PENALTY_DECAY_INTERVAL = 300   # 每 5 分钟自动衰减 -1


def _ewma(old, new, alpha=_EWMA_ALPHA):
    if old is None:
        return new
    return old * (1 - alpha) + new * alpha


def _save_perf():
    """持久化动态评分/惩罚（延迟 5 秒防频繁写入）。"""
    global _perf_timer
    if _perf_timer:
        _perf_timer.cancel()
    _perf_timer = threading.Timer(5.0, _do_save_perf)
    _perf_timer.daemon = True
    _perf_timer.start()


def _do_save_perf():
    try:
        with _lock:
            data = {"perf": {k: dict(v) for k, v in _perf.items()},
                    "penalty": dict(_penalty)}
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _PERF_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_perf():
    """启动时恢复动态评分与惩罚，避免重启后把流量打回刚故障过的供应商。"""
    try:
        if _PERF_FILE.exists():
            data = json.loads(_PERF_FILE.read_text(encoding="utf-8"))
            for sid, p in (data.get("perf") or {}).items():
                if isinstance(p, dict) and p.get("total_requests"):
                    _perf[sid] = p
            for sid, v in (data.get("penalty") or {}).items():
                try:
                    _penalty[sid] = min(max(0.0, float(v)), _PENALTY_CAP)
                except Exception:
                    pass
    except Exception:
        pass


def record_model_result(sid, ok=True, latency=None, ttft=None,
                        is_timeout=False, is_429=False):
    """每次真实上游请求后调用。EWMA 更新各项指标并累计惩罚。"""
    now = time.time()
    with _lock:
        p = _perf.setdefault(sid, {
            "latency_ewma": None, "ttft_ewma": None,
            "success_ewma": 1.0,
            "timeout_count": 0, "rate_limit_count": 0,
            "total_requests": 0, "last_updated": now,
        })
        p["total_requests"] += 1
        p["last_updated"] = now

        # 延迟 EWMA：只统计成功请求——「秒败」连接失败不该被当作极速，
        # 否则评分榜会被失败源霸榜首跳（09-13 线上 free-google 3连败 latency=0.02s 即此类）。
        if ok:
            if latency is not None:
                p["latency_ewma"] = _ewma(p["latency_ewma"], latency)
            if ttft is not None:
                p["ttft_ewma"] = _ewma(p["ttft_ewma"], ttft)

        # 成功率 EWMA
        p["success_ewma"] = _ewma(p["success_ewma"], 1.0 if ok else 0.0)

        # 计数器
        if is_timeout:
            p["timeout_count"] += 1
        if is_429:
            p["rate_limit_count"] += 1

        # 惩罚累计：429 权重最重（限流是最强短期健康信号）
        penalty = 0.0
        if is_429:
            penalty = 3.0
        elif is_timeout:
            penalty = 1.0
        elif not ok:
            penalty = 0.5
        if penalty > 0:
            _decay_penalty(sid, now)          # 先做惰性衰减再累加
            _penalty[sid] = min(_penalty.get(sid, 0.0) + penalty,
                                _PENALTY_CAP)
        _save_perf()


def _decay_penalty(sid, now):
    """惰性衰减：距上次交互超过 5 分钟的部分按每 5 分钟 -1 扣减。
    始终返回当前惩罚值（float），供调用方直接使用。"""
    entry = _penalty.get(sid, 0.0)
    if entry <= 0:
        return 0.0
    last = _perf.get(sid, {}).get("last_updated", now)
    elapsed = now - last
    decay_steps = int(elapsed / _PENALTY_DECAY_INTERVAL)
    if decay_steps > 0:
        entry = max(0.0, entry - decay_steps)
        _penalty[sid] = entry
    return entry


def get_penalty(sid):
    """当前惩罚值（含惰性衰减），下限 0 上限 15。"""
    now = time.time()
    with _lock:
        _decay_penalty(sid, now)
        return round(_penalty.get(sid, 0.0), 2)


def record_success_event(sid):
    """成功后轻微恢复惩罚（-0.5，下限0）。"""
    with _lock:
        cur = _penalty.get(sid, 0.0)
        if cur > 0:
            _penalty[sid] = max(0.0, cur - 0.5)
            _save_perf()


def get_supplier_perf(sid):
    """返回单个供应商的动态指标；无数据返回 None。"""
    with _lock:
        p = _perf.get(sid)
        if not p or p["total_requests"] == 0:
            return None
        return dict(p)


def get_dynamics_summary():
    """全部供应商的 perf + penalty 快照（面板监控用）。"""
    now = time.time()
    with _lock:
        out = {}
        for sid, p in _perf.items():
            if p["total_requests"] == 0:
                continue
            out[sid] = {
                **{k: v for k, v in p.items() if k != "last_updated"},
                "penalty": round(_decay_penalty(sid, now), 2),
            }
        return out


# 启动时加载历史数据
_load_usage()
_load_perf()
