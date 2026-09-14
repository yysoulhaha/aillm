# -*- coding: utf-8 -*-
"""模型参数查询（datalearner.com 数据源）。

功能：
- 把网关里的模型 id 映射到 DataLearner 详情页，抓取：参数量、上下文长度、
  发布时间、知识截止、机构、简介
- 结果永久缓存到 data/model_params.json；查不到也记负缓存，避免反复打扰对方
- 后台线程节流补全启用模型的参数（每条间隔 ≥1.2s）

用法：
    model_params.lookup("deepseek-v4-pro")
    -> {"params_text":"1.6万亿","params":1.6e12,"ctx_text":"1M","ctx":1048576,
        "released":"2026-08-13","cutoff":"2025-05","org":"DeepSeek-AI",
        "desc":"...","source":"https://www.datalearner.com/..."}

评分辅助：
    model_params.param_score(params, ctx) -> 分数贡献(0~92)，家族表未命中时兜底
"""
import json
import pathlib
import re
import threading
import time

import httpx

from core import paths
from core import config

CACHE_PATH = paths.data_home() / "data" / "model_params.json"

_BASE = "https://www.datalearner.com/ai-models/pretrained-models"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_lock = threading.Lock()
_cache = None          # model_key -> dict | {"_miss": true}
_dirty = False


# ---------------------------------------------------------------- 缓存
def _load():
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            _cache = {}
    return _cache


def _save():
    global _dirty
    with _lock:
        if not _dirty:
            return
        CACHE_PATH.write_text(json.dumps(_cache, ensure_ascii=False, indent=1),
                              encoding="utf-8")
        _dirty = False


def _put(key, val):
    with _lock:
        _cache[key] = val
        _dirty = True


# ---------------------------------------------------------------- 抓取与解析
def slugify(model_id):
    """模型 id -> DataLearner url slug（尽力猜测）。"""
    s = (model_id or "").strip().lower()
    if "/" in s:                      # meta/llama-3.1-8b-instruct -> 尾段
        s = s.rsplit("/", 1)[-1]
    s = re.sub(r"[._]+", "-", s)      # qwen3.8-27b / gpt_4o -> 连字符
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


# 网关侧附加的标记后缀，不属于真实模型名，搜索前剥掉
_MARK_SUFFIXES = {"free", "vip", "keyless", "contributor", "trial", "shared"}


def _strip_marks(slug):
    """'muse-spark-1-2-contributor-free' -> ['...-contributor-free'原样,
    'muse-spark-1-2', ...] 渐进剥离候选列表（含原样）。"""
    cands = [slug]
    parts = slug.split("-")
    while parts and parts[-1] in _MARK_SUFFIXES:
        parts.pop()
        if parts and parts[-1] not in _MARK_SUFFIXES:
            cands.append("-".join(parts))
    return cands


def _num_from(text):
    """'1.6万亿'/'70B'/'8x7B' -> 数值(个)；失败返回 None。"""
    t = (text or "").replace(",", "").strip()
    m = re.search(r"([\d.]+)\s*(万亿|亿|万|[TtBbMmKk])(?![a-zA-Z])", t)
    if not m:
        return None
    v = float(m.group(1))
    u = m.group(2)
    mult = {"万亿": 1e12, "亿": 1e8, "万": 1e4,
            "T": 1e12, "t": 1e12, "B": 1e9, "b": 1e9,
            "M": 1e6, "m": 1e6, "K": 1e3, "k": 1e3}[u]
    return int(v * mult)


def _ctx_from(text):
    """'1M'/'128K'/'100万' -> tokens 数。"""
    return _num_from(text)


def _label_value(html, label):
    """stat 卡片：标签 span 后面最近的加粗值 div。"""
    pat = re.compile(
        r">" + re.escape(label) + r"</span>.*?<div[^>]*>([^<]{1,40})</div>",
        re.S)
    m = pat.search(html)
    return m.group(1).strip() if m else None


def parse_detail(html, url):
    if "模型参数" not in html and "上下文长度" not in html:
        return None
    p_text = _label_value(html, "模型参数") or ""
    c_text = _label_value(html, "上下文长度") or ""
    m_rel = re.search(r"发布时间[^0-9]{0,30}(\d{4}-\d{2}-\d{2})", html)
    released = m_rel.group(1) if m_rel else None
    # 知识截止：title 属性后最近日期
    m_cut = re.search(r"预训练知识截止时间.{0,600}?(\d{4}-\d{2})", html, re.S)
    cutoff = m_cut.group(1) if m_cut else None
    # 机构：meta author > 机构链接
    m_org = (re.search(r'<meta\s+name="article:author"\s+content="([^"]+)"', html)
             or re.search(r'/ai-organizations/([A-Za-z0-9._-]+)', html))
    org = m_org.group(1).strip() if m_org else None
    # JSON-LD 简介（HTML 里是 UTF-8 文本，只需还原 \uXXXX 转义）
    m_desc = re.search(r'"description"\s*:\s*"([^"]{40,600})"', html)
    desc = None
    if m_desc:
        desc = re.sub(r"\\u([0-9a-fA-F]{4})",
                      lambda m: chr(int(m.group(1), 16)),
                      m_desc.group(1))[:400]
    return {
        "params_text": p_text or None,
        "params": _num_from(p_text),
        "ctx_text": c_text or None,
        "ctx": _ctx_from(c_text),
        "released": released,
        "cutoff": cutoff,
        "org": org,
        "desc": desc,
        "source": url,
        "ts": time.time(),
    }


def _fetch(url):
    try:
        r = httpx.get(url, headers=_HEADERS, timeout=12.0, follow_redirects=True)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return None


def _search_first_slug(keywords):
    from urllib.parse import quote
    html = _fetch(_BASE + "?keywords=" + quote(keywords))
    if not html:
        return None
    want = slugify(keywords)
    best, best_len = None, 999
    for m in re.finditer(r"pretrained-models/([a-z0-9-]+)", html):
        slug = m.group(1)
        d = abs(len(slug) - len(want))
        if slug == want:
            return slug
        if slug.startswith(want[:8]) and d < best_len:
            best, best_len = slug, d
    return best


def lookup_online(model_id):
    """在线查询一次；返回解析 dict 或 None（并写负缓存）。
    候选顺序：原名 → 剥离 -free/-vip 等标记后缀的各级短名；
    每个候选先直猜详情页，全部失败再用最短短名做关键词搜索。"""
    key = model_id.strip().lower()
    cands = _strip_marks(slugify(key))
    info = None
    for slug in cands:                     # 逐候选直猜
        html = _fetch(f"{_BASE}/{slug}")
        info = parse_detail(html, f"{_BASE}/{slug}") if html else None
        if info:
            break
    if info is None:                       # 关键词搜索兜底（用剥完标记的名字）
        slug2 = _search_first_slug(cands[-1])
        if slug2 and slug2 not in cands:
            html = _fetch(f"{_BASE}/{slug2}")
            info = parse_detail(html, f"{_BASE}/{slug2}") if html else None
    with _lock:
        _cache[key] = {"_miss": True} if info is None else info
        _dirty = True
    _save()
    return info


def lookup(model_id):
    """带缓存查询。返回 dict 或 None。"""
    if not model_id:
        return None
    key = model_id.strip().lower()
    c = _load()
    hit = c.get(key)
    if hit is not None:
        return None if hit.get("_miss") else hit
    info = lookup_online(key)
    time.sleep(1.2)                        # 礼貌限速
    return info


def get_cached(model_id):
    """只读缓存，不联网。"""
    hit = _load().get((model_id or "").strip().lower())
    return None if (not hit or hit.get("_miss")) else hit


def miss_count():
    return sum(1 for v in _load().values() if isinstance(v, dict) and v.get("_miss"))


def cached_count():
    return sum(1 for v in _load().values()
               if isinstance(v, dict) and not v.get("_miss"))


# ---------------------------------------------------------------- 评分
def param_score(params, ctx):
    """用参数量/上下文折算能力分(0-92)。家族测评表未命中时的兜底。"""
    if not params:
        return None
    b = params / 1e9
    if b >= 1000:
        s = 90
    elif b >= 400:
        s = 87
    elif b >= 100:
        s = 83
    elif b >= 60:
        s = 78
    elif b >= 30:
        s = 73
    elif b >= 10:
        s = 66
    elif b >= 3:
        s = 58
    else:
        s = 50
    if ctx:
        if ctx >= 900000:
            s += 3
        elif ctx >= 200000:
            s += 2
        elif ctx >= 120000:
            s += 1
    return min(s, 92)


def fmt_params(params):
    if not params:
        return None
    b = params / 1e9
    if b >= 1000:
        return f"{b / 1000:.1f}T".rstrip("0").rstrip(".") .replace(".0T", "T")
    if b >= 1:
        return f"{b:.0f}B" if b == int(b) else f"{b:.1f}B"
    return f"{params / 1e6:.0f}M"


# ---------------------------------------------------------------- 后台补全
_fill_thread = None


def _fill_loop():
    while True:
        try:
            cfg = config.load_config()
            todo = []
            for s in cfg.get("suppliers", []):
                if not s.get("enabled", True):
                    continue
                for m in s.get("models", []):
                    mid = m.get("id") or ""
                    if mid and get_cached(mid) is None and \
                            not _load().get(mid.lower(), {}).get("_miss"):
                        todo.append(mid)
            for mid in todo[:20]:          # 每轮最多补 20 条
                lookup_online(mid)
                time.sleep(1.2)
        except Exception:
            pass
        time.sleep(300)                    # 5 分钟一轮


def start_background_fill():
    global _fill_thread
    if _fill_thread and _fill_thread.is_alive():
        return
    _fill_thread = threading.Thread(target=_fill_loop, daemon=True)
    _fill_thread.start()
