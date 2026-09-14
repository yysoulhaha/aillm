# -*- coding: utf-8 -*-
"""上游请求传输层（feat#4 供应商级代理 可插拔）。

连接池设计（对齐主流网关工程实践）：
- 每个事件循环线程内按 (proxy) 缓存持久客户端，
  复用 TCP/TLS/HTTP 连接，避免每请求新建客户端导致的握手开销；
  同时也让探测测得的延迟更接近真实值（不再混入 TLS 握手时间）。
- trust_env=False：不读取环境代理变量，避免无代理环境/沙箱里
  httpx 尝试连接环境代理而卡死（曾导致探测长时间挂起）。
- 超时：连接 5s 硬限（connect），读/写 120s 默认，可被调用方按请求覆盖；
  非流式总时长由上层用 asyncio.wait_for 封顶。
"""
import asyncio
import threading

import httpx


class UpstreamError(RuntimeError):
    """上游错误，携带 HTTP 状态码以便上层做重试策略判断。
    retry_after：上游 Retry-After 头（秒），供熔断器精确设定冷却窗口。"""
    def __init__(self, status: int, message: str = "", retry_after=None):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}: {message[:200]}")

    @property
    def retryable_supplier_fault(self) -> bool:
        """是否视为供应商故障（触发熔断、触发重试）：
        - 401/402/403/407/429：认证/额度/限流 → 供应商故障
        - 5xx：服务端错误 → 供应商故障
        - 其它（400/404/413 等）：通常是请求/模型问题，不视为供应商故障
        """
        return self.status in (401, 402, 403, 407, 429) or 500 <= self.status < 600


class SupplierBusy(RuntimeError):
    """本网关侧并发闸门已满（非供应商故障，不计入熔断）。
    上层应跳到下一 fallback 供应商。"""


def retry_after_of(headers):
    """从响应头解析 Retry-After（秒），缺失/非法返回 None。
    兼容 httpx.Headers（大小写不敏感）与普通 dict（需遍历键名匹配）。"""
    if not headers:
        return None
    try:
        v = headers.get("retry-after")
        if v is None:
            for k in headers.keys():
                if k.lower() == "retry-after":
                    v = headers[k]
                    break
        if v is None:
            return None
        return max(0.0, float(v))
    except Exception:
        return None


_pools = threading.local()       # 线程隔离的连接池（主事件循环 / 探测线程各一套）


def _pool():
    if not hasattr(_pools, "clients"):
        _pools.clients = {}
    return _pools.clients


def _get_client(proxy):
    key = (proxy or "")
    pool = _pool()
    c = pool.get(key)
    if c is None:
        connect = _connect_timeout()
        timeout = httpx.Timeout(180.0, connect=connect)
        limits = httpx.Limits(max_connections=200,
                              max_keepalive_connections=50,
                              keepalive_expiry=60)
        c = httpx.AsyncClient(proxy=proxy, timeout=timeout, limits=limits,
                              trust_env=False)
        pool[key] = c
    return c


def _connect_timeout():
    """从配置读取 connectTimeout（秒），默认 5。"""
    try:
        import core.config as config
        return float(config.load_config().get("upstream", {})
                     .get("connectTimeout", 5))
    except Exception:
        return 5.0


def _pick_transport(supplier):
    """返回 client（始终使用标准 httpx，连接池按 proxy 复用）。"""
    return _get_client(supplier.get("proxy"))


# ---------------- 每供应商并发闸门（对齐 Bifrost 的有界并发队列） ----------------
# 慢供应商在熔断触发前会被并发请求堆满；闸门限制单供应商同时在途请求数，
# 等待超时直接抛 SupplierBusy 跳到下一 fallback，避免拖高整体 P99。
_sems = {}                       # sid -> (asyncio.Semaphore, limit)
_sems_lock = threading.Lock()


def _max_concurrent():
    try:
        import core.config as config
        return int(config.load_config().get("upstream", {})
                   .get("maxConcurrentPerSupplier", 16))
    except Exception:
        return 16


class SupplierSlot:
    """供应商并发闸门句柄：acquire() 成功后必须 release()（幂等）。"""

    def __init__(self, sid, timeout=10.0):
        self.sid = sid
        self.timeout = float(timeout or 0)
        self._sem = None

    async def acquire(self):
        limit = _max_concurrent()
        if limit <= 0:
            return
        with _sems_lock:
            entry = _sems.get(self.sid)
            if entry is None or entry[1] != limit:
                entry = (asyncio.Semaphore(limit), limit)
                _sems[self.sid] = entry
        self._sem = entry[0]
        if self.timeout > 0:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.timeout)
        else:
            await self._sem.acquire()

    def release(self):
        if self._sem is not None:
            sem = self._sem
            self._sem = None
            try:
                sem.release()
            except Exception:
                pass


def supplier_slot(sid, timeout=10.0):
    """创建一个供应商并发闸门句柄。timeout<=0 表示无限等待。"""
    return SupplierSlot(sid, timeout)


def _max_response_bytes():
    try:
        import core.config as config
        return int(float(config.load_config().get("upstream", {})
                         .get("maxResponseMB", 64)) * 1024 * 1024)
    except Exception:
        return 64 * 1024 * 1024


async def call_upstream(method, url, headers, content, supplier, timeout=None):
    """非流式：返回 (status, headers_dict, body_bytes)。连接由池回收，不逐请求关闭。
    增加响应体上限保护：超过上限即按 502 失败，防止超大响应一次性吃爆内存。"""
    max_resp = _max_response_bytes()
    client = _pick_transport(supplier)
    req = client.build_request(method, url, headers=headers, content=content)
    resp = await client.send(req, stream=True)
    try:
        parts, total = [], 0
        async for chunk in resp.aiter_raw():
            total += len(chunk)
            if max_resp and total > max_resp:
                raise UpstreamError(
                    502, f"上游响应超过上限 {max_resp // (1024*1024)}MB")
            parts.append(chunk)
        return resp.status_code, dict(resp.headers), b"".join(parts)
    except UpstreamError:
        await resp.aclose()
        raise
    finally:
        try:
            await resp.aclose()
        except Exception:
            pass


async def open_stream(method, url, headers, content, supplier):
    """建立流式连接并校验状态码。

    返回 (response, client)：调用方用 `resp.aiter_raw()` 迭代，
    结束后须 `await resp.aclose()`（连接归还池），不得关闭池内 client。
    非 200 立即抛 UpstreamError（供上层 Fallback/熔断），不产出任何字节。
    """
    client = _pick_transport(supplier)
    req = client.build_request(method, url, headers=headers, content=content)
    resp = await client.send(req, stream=True)
    if resp.status_code != 200:
        body = await resp.aread()
        await resp.aclose()
        raise UpstreamError(resp.status_code,
                            body[:200].decode("utf-8", "replace"),
                            retry_after=retry_after_of(resp.headers))
    return resp, client


async def stream_upstream(method, url, headers, content, supplier, timeout=None):
    """流式：异步生成原始字节块（SSE）。连接复用池内客户端，不关闭。"""
    client = _pick_transport(supplier)
    async with client.stream(method, url, headers=headers, content=content,
                             timeout=float(timeout) if timeout else None) as r:
        async for chunk in r.aiter_raw():
            yield chunk


async def close_pool():
    """关闭当前线程事件循环内所有池化客户端（shutdown 时调用）。"""
    pool = _pool()
    for c in list(pool.values()):
        try:
            await c.aclose()
        except Exception:
            pass
    pool.clear()
