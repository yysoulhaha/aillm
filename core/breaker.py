# -*- coding: utf-8 -*-
"""熔断器（feat#1 供应商级熔断）。

状态机：closed -> open -> half-open -> closed/open
- closed：正常；连续失败达到 threshold 进入 open
- open：熔断期内直接拒绝（触发 fallback），不请求上游
- half-open：熔断期过后放一个探活请求，成功则 closed，失败则回 open
"""
import time
import threading

_states = {}   # supplier_id -> dict
_lock = threading.Lock()


def _ensure(sid, threshold=5, reset=60):
    if sid not in _states:
        _states[sid] = {
            "state": "closed", "consecutive_fail": 0,
            "threshold": threshold, "reset": reset,
            "opened_at": 0, "tested_at": 0,
            "probing": 0, "half_open_max": 1,
            "open_count": 0, "effective_reset": reset,
        }
    return _states[sid]


def configure(sid, threshold, reset):
    with _lock:
        st = _ensure(sid, threshold, reset)
        st["threshold"] = max(1, threshold)
        st["reset"] = max(1, reset)


_RESET_CAP = 600.0     # 指数退避上限（秒）
_RESET_MAX_N = 12      # 2^n 防爆顶


def _open(st, reset_override=None):
    """进入 open 态：默认按 base * 2^(n-1) 指数退避（上限 600s）；
    上游返回 Retry-After 时以其为冷却窗口（精确限流恢复）。"""
    st["state"] = "open"
    base = max(1.0, float(st["reset"]))
    n = max(0, st.get("open_count", 0))
    backoff = min(base * (2 ** min(max(0, n - 1), _RESET_MAX_N)), _RESET_CAP)
    if reset_override:
        try:
            backoff = min(max(float(reset_override), 1.0), _RESET_CAP)
        except Exception:
            pass
    st["effective_reset"] = backoff
    st["opened_at"] = time.time()


def allow_request(sid):
    """返回是否允许发请求：
    - closed：放行
    - open：熔断期内拒绝（触发 fallback）；冷却窗口 = effective_reset
    - 到期自动进 half-open，只放 1 个探测请求；其余在探测完成前继续拒绝，
      避免半开状态下多路并发同时压探导致误恢复/误熔断。
    """
    with _lock:
        st = _ensure(sid)
        if st["state"] == "open":
            if time.time() - st["opened_at"] >= st.get("effective_reset", st["reset"]):
                st["state"] = "half-open"
                st["tested_at"] = time.time()
                st["probing"] = 1
                return True
            return False
        if st["state"] == "half-open":
            if st["probing"] >= st["half_open_max"]:
                return False
            st["probing"] += 1
            return True
        return True


def on_success(sid):
    with _lock:
        st = _ensure(sid)
        if st["probing"] > 0:
            st["probing"] -= 1
        st["state"] = "closed"
        st["consecutive_fail"] = 0
        st["open_count"] = 0          # 恢复健康，退避计数归零
        st["effective_reset"] = st["reset"]


def on_failure(sid, reset_override=None):
    """记录失败并按需熔断。reset_override：上游 Retry-After 秒数。"""
    with _lock:
        st = _ensure(sid)
        if st["probing"] > 0:
            st["probing"] -= 1
        st["consecutive_fail"] += 1
        if st["state"] == "half-open":
            st["open_count"] = st.get("open_count", 0) + 1
            _open(st, reset_override)
        elif st["consecutive_fail"] >= st["threshold"]:
            st["open_count"] = st.get("open_count", 0) + 1
            _open(st, reset_override)


def get_state(sid):
    with _lock:
        st = _ensure(sid)
        return {"state": st["state"], "consecutive_fail": st["consecutive_fail"],
                "open_count": st.get("open_count", 0),
                "effective_reset": round(st.get("effective_reset", st["reset"]), 1)}
