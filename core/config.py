# -*- coding: utf-8 -*-
"""AI-Gateway-C 配置加载与读写（完全独立，不依赖任何外部项目）。

config.json 结构（feat 映射见各字段注释）：
{
  "schemaVersion": 1,
  "port": 18123,                         # 本网关端口（独立于 B 的 8670）
  "systemPromptTemplate": "",            # feat#7 全局系统提示词（可 append/override）
  "systemPromptMode": "append",          # append | override
  "tokenCompression": {...},             # feat#9
  "pools": {"free":[...], "vip":[...]},  # 池成员（供应商 id；本地标签，用户自理）
  "defaultPool": "free",                 # 未带码时默认池
  "suppliers": [ {供应商对象} ]
}

可靠性升级（相对旧版）：
  - save_config 原子写：临时文件 + os.replace，崩溃/断电不产生半截 JSON
  - 跨进程文件锁：与外部写入方互斥，避免读一半/写一半
  - 损坏自愈：读到坏 JSON 时先备份 .corrupt-*，再回退最近一次有效内存配置
    （不会像旧版直接抛 JSONDecodeError 让全部请求 500）
  - schemaVersion：声明配置结构版本，便于未来做显式迁移

供应商对象字段：
  id, name, baseUrl, protocol(openai|anthropic|gemini),
  quota("limited"|"unlimited"),         # feat#2 配额感知
  proxy(null|"http://..."),             # feat#4 供应商级代理
  fallback([supplierId...]),            # feat#1 多级 fallback 链
  breaker({threshold,resetSeconds}),    # feat#1 熔断
  enabled(bool),
  keys:[ {key, supportedModels:[...], quota} ],  # feat#3 按key模型过滤
  models:[ {id, enabled} ]
"""
import json
import os
import pathlib
import secrets
import tempfile
import threading
import time

from core import paths

HERE = paths.app_home()
DATA_ROOT = paths.data_home()
CONFIG_PATH = DATA_ROOT / "data" / "config.json"
LOCK_PATH = DATA_ROOT / "data" / ".config.lock"
SCHEMA_VERSION = 1

_lock = threading.RLock()
_cfg = None
_cfg_mtime = None
_last_stat_ts = 0.0
_STAT_TTL = 1.0          # 最少间隔 1s 才 stat 一次文件（热重载仍 1s 内生效）

# ------------------------------------------------------------------ 文件锁
def _acquire_process_lock(exclusive: bool):
    """跨进程互斥：Windows 用 msvcrt，POSIX 用 fcntl。返回句柄或 None。
    exclusive=True 用于写（阻塞直到拿到写锁）；False 用于读（共享锁）。"""
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = open(str(LOCK_PATH), "a+")
        # 保证锁区域存在（msvcrt 锁定不能超出文件末尾）
        if os.path.getsize(str(LOCK_PATH)) == 0:
            fd.write(b"\0")
            fd.flush()
        fd.seek(0)
        import sys
        if sys.platform == "win32":
            import msvcrt
            if exclusive:
                # 阻塞式写锁
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            else:
                # 非阻塞尝试共享读锁（Windows 无真正共享读锁，仅尽力而为）
                try:
                    msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
                except Exception:
                    pass
        else:
            import fcntl
            if exclusive:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
            else:
                fcntl.flock(fd.fileno(), fcntl.LOCK_SH)
        return fd
    except Exception:
        try:
            fd.close()
        except Exception:
            pass
        return None


def _release_process_lock(fd):
    if fd is None:
        return
    try:
        import sys
        if sys.platform == "win32":
            import msvcrt
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    finally:
        try:
            fd.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 原子 IO
def _read_text_with_lock():
    """带进程锁读取原始文本；读锁抢不到时直接读（容忍并发写的小窗口，
    靠「重试 + 损坏自愈」兜底）。"""
    fd = _acquire_process_lock(False)
    try:
        if not CONFIG_PATH.exists():
            return None
        return CONFIG_PATH.read_text(encoding="utf-8-sig")
    finally:
        _release_process_lock(fd)


def _write_text_atomic(text: str):
    """原子写：写同目录临时文件再 os.replace；写期间持跨进程写锁。"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = _acquire_process_lock(True)
    try:
        fd2, tmp = tempfile.mkstemp(dir=str(CONFIG_PATH.parent),
                                    prefix=CONFIG_PATH.stem + ".", suffix=".tmp")
        try:
            with os.fdopen(fd2, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, CONFIG_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    finally:
        _release_process_lock(fd)


def _backup_corrupt():
    """把损坏的 config.json 备份为 .corrupt-<ts>.json 并移走，避免读到坏文件。"""
    try:
        if not CONFIG_PATH.exists() or CONFIG_PATH.stat().st_size == 0:
            return
        bak = CONFIG_PATH.with_name(
            "config.corrupt-%d.json" % int(__import__("time").time()))
        CONFIG_PATH.replace(bak)
    except Exception:
        pass


def _file_mtime():
    try:
        return CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else None
    except Exception:
        return None


def _load_from_disk() -> dict:
    """从磁盘读 + 默认补齐。返回 (cfg, ok)；ok=False 表示读到坏 JSON。"""
    text = _read_text_with_lock()
    if text is None:
        # 首次运行：尚无配置文件，交给 load_config 用默认配置兜底
        return None, False
    try:
        stored = json.loads(text)
    except Exception:
        _backup_corrupt()
        return None, False
    if not isinstance(stored, dict):
        _backup_corrupt()
        return None, False
    return _apply_defaults(stored), True


def load_config(force_reload: bool = False):
    """带 mtime 热加载：config.json 被外部改动后，最多 1 秒内自动重读
    （stat 节流，避免每个请求都做文件 stat）。
    可靠性：跨进程写锁 + 坏文件自愈（回退内存里的最近一次有效配置，
    避免一次崩溃写坏导致网关整片 500）。"""
    global _cfg, _cfg_mtime, _last_stat_ts
    with _lock:
        now = time.time()
        if not force_reload and _cfg is not None and now - _last_stat_ts < _STAT_TTL:
            return _cfg
        _last_stat_ts = now
        mtime = _file_mtime()
        if _cfg is None or force_reload or mtime != _cfg_mtime:
            cfg, ok = _load_from_disk()
            if ok:
                _cfg = cfg
                _cfg_mtime = mtime
            elif _cfg is None:
                # 从未成功载入：用默认配置兜底（不写盘，避免覆盖损坏原件）
                _cfg = _default_config()
                _cfg_mtime = mtime
            # else: 保留内存中的最近一次有效配置（_cfg 不变）
        return _cfg


def _apply_defaults(cfg):
    """用默认值补齐缺失字段，保证旧配置升级后新功能默认可用（不覆盖已有值）。
    若 config 的 schemaVersion 高于当前版本（由更新版本的工具写过），则不做
    降级合并，按原样返回——避免把新字段冲掉。"""
    d = _default_config()
    merged = {**d, **cfg}
    ver = int(cfg.get("schemaVersion") or 1)
    if ver > SCHEMA_VERSION:
        return cfg
    for k in ("tokenCompression", "autoProbe",
              "smartRouting", "upstream"):
        if isinstance(d.get(k), dict) and isinstance(merged.get(k), dict):
            merged[k] = {**d[k], **merged[k]}
    sc = merged.get("smartRouting", {})
    if isinstance(sc.get("classifier"), dict):
        sc["classifier"] = {**d["smartRouting"]["classifier"],
                            **sc["classifier"]}
    if isinstance(sc.get("routes"), dict):
        sc["routes"] = {**d["smartRouting"]["routes"], **sc["routes"]}
    return merged


def save_config(cfg=None):
    """原子保存。调用方若传入 cfg 则先替换内存副本再落盘。"""
    global _cfg, _cfg_mtime
    with _lock:
        if cfg is not None:
            _cfg = cfg
        if _cfg is None:
            _cfg = _default_config()
        _cfg.setdefault("schemaVersion", SCHEMA_VERSION)
        # 保留高版本字段（外部更新的结构不在此被清掉）
        try:
            _write_text_atomic(
                json.dumps(_cfg, ensure_ascii=False, indent=2))
            _cfg_mtime = _file_mtime()
        except Exception:
            raise


def get_port():
    env = os.environ.get("AILLM_PORT")
    if env and env.strip().isdigit():
        return int(env.strip())
    return int(load_config().get("port", 18123))


def get_bind_host():
    """监听地址。默认 127.0.0.1（普通用户免防火墙弹窗、防局域网裸奔）；
    需要局域网/公网访问时在 config.json 设 "bindHost": "0.0.0.0"
    或用环境变量 AILLM_HOST（Docker 部署默认 0.0.0.0）。"""
    env = os.environ.get("AILLM_HOST")
    if env is not None and env.strip():
        return env.strip()
    return str(load_config().get("bindHost", "127.0.0.1"))


def get_supplier(cfg, sid):
    for s in cfg.get("suppliers", []):
        if s.get("id") == sid:
            return s
    return None


def get_enabled_suppliers(cfg, pool=None):
    sups = [s for s in cfg.get("suppliers", []) if s.get("enabled", True)]
    if pool:
        members = set(cfg.get("pools", {}).get(pool, []))
        sups = [s for s in sups if s.get("id") in members]
    return sups


# ------------------------------------------------------------------ Admin Token
def get_admin_token(cfg=None) -> str:
    """取管理 API 令牌。config 里没有时自动生成一个并落盘（默认鉴权策略），
    可用环境变量 GW_ADMIN_TOKEN 覆盖（=空字符串表示显式关闭鉴权）。"""
    env = os.environ.get("GW_ADMIN_TOKEN", None)
    if env is not None:
        return env.strip()
    cfg = cfg or load_config()
    tok = (cfg.get("adminToken") or "").strip()
    if not tok:
        tok = "gw-" + secrets.token_urlsafe(16)
        cfg["adminToken"] = tok
        try:
            save_config(cfg)
        except Exception:
            pass
    return tok


def _default_config():
    return {
        "schemaVersion": SCHEMA_VERSION,
        "port": 18123,
        "bindHost": "127.0.0.1",      # 默认只监听本机：普通用户免防火墙弹窗，最稳；改 0.0.0.0 开放局域网
        "systemPromptTemplate": "",
        "systemPromptMode": "append",
        "tokenCompression": {"enabled": False, "maxContextTokens": 8000, "strategy": "truncate"},
        "autoProbe": {"enabled": True, "intervalMinutes": 15,
                      "timeoutSeconds": 20, "probeModelsPerRound": 2},
        "upstream": {
            "connectTimeout": 5,          # 到上游的连接建立超时（秒）
            "requestTimeout": 180,        # 非流式单次请求总时长上限（秒）— 长上下文需更久
            "streamFirstByteTimeout": 60, # 流式首字节(TTFT)超时：长上下文供应商可能需要更久
            "streamIdleTimeout": 180,     # 流式中途空闲上限（秒）— 复杂推理可能长停顿
            "freePoolIdleTimeout": 60,    # 免费/limited 源流式空闲上限：免费池抖动频繁，到点即收尾换路避免白等数分钟
            "keyCooldownSeconds": 120,    # 某把 key 429 后隔离秒数：独立账号配额打满即冷却，round-robin 期间跳过
            "sseKeepAliveSeconds": 15,    # 向下游发的 SSE 心跳间隔（防 NAT/代理掐断）
            "maxConcurrentPerSupplier": 16,  # 每供应商同时在途请求上限；0=不限制
            "slotWaitSeconds": 10,        # 等待并发闸门的超时（秒），超时跳下一 fallback
            "includeUsage": True,         # 流式带 stream_options.include_usage（上游不支持时置 false）
            "maxRequestBodyMB": 32,       # 入站请求体上限（防超大上下文/压缩炸弹）
            "maxResponseMB": 64,          # 非流式上游响应体上限（防内存打爆）
            "defaultMaxTokens": 16000,    # 客户端未显式传 max_tokens 且带 tools 时注入的输出上限
            "toolPreferNonFree": False,   # 带 tools 的请求：默认免费源优先，付费源不抢先（可手动开启）
        },
        "adminToken": "",                 # 管理 API 鉴权令牌；为空时首次启动自动生成
        "smartRouting": {
            "enabled": True,
            "classifier": {"model": "", "pool": "",
                           "timeoutSeconds": 4, "maxChars": 500,
                           "budgetMs": 0,       # LLM 分类可阻塞预算(ms)；0=不阻塞只后台学习
                           "cacheTtlSeconds": 300},   # 分类结果 TTL（秒）
            "rules": {"simpleMaxChars": 15, "longCtxTokens": 8000},
            "routes": {
                "SIMPLE": {"prefer": "speed", "benchMax": 82},
                "REASONING": {"benchMin": 85},
                "CODE": {"benchMin": 85},
                "VISION": {"vision": True},
                "LONG_CTX": {"ctxFromMessages": True}
            }
        },
        "customRoutes": {},
        "pools": {"free": [], "vip": []},
        "defaultPool": "free",
        "suppliers": []
    }


def upsert_supplier(cfg, sup):
    """新增或更新供应商。"""
    sid = sup.get("id")
    for i, s in enumerate(cfg.get("suppliers", [])):
        if s.get("id") == sid:
            cfg["suppliers"][i] = sup
            return
    cfg.setdefault("suppliers", []).append(sup)
