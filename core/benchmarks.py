# -*- coding: utf-8 -*-
"""公开测评知识表（能力维度自动计分）。

来源：LMSYS Chatbot Arena / 主流公开榜单的模型家族综合水平，
整理为「家族正则 → 0-100 分」。榜单会过时，此表可随时手工更新；
供应商上手填的 capability 权重始终优先于本表。
"""
import functools
import re

# (家族标签, 正则, 分数0-100)
RULES = [
    ("GPT/o系列旗舰", r"gpt-5|gpt-4\.5|o[134](|-mini|$)", 96),
    ("GPT-4.1",      r"gpt-4\.1($|-preview)", 92),
    ("GPT-4o",       r"gpt-4o($|-2024|-audio)", 90),
    ("GPT轻量",      r"gpt-4\.1-(mini|nano)|gpt-4o-mini", 82),
    ("Claude旗舰",   r"claude-(opus|4-5|4-1|4)", 96),
    ("Claude-Sonnet", r"claude-sonnet", 93),
    ("Claude-Haiku", r"claude-haiku", 84),
    ("Gemini Pro",   r"gemini-3|gemini-2\.5-pro", 94),
    ("Gemini Flash", r"gemini-.*flash", 86),
    ("DeepSeek主力", r"deepseek-(r\d|v4($|-)|chat)", 91),
    ("DeepSeek轻量", r"deepseek-.*flash", 80),
    ("Qwen大杯",     r"qwen.*(max|plus)|qwen3-\d{2,}", 88),
    ("GLM-5",        r"glm-5", 89),
    ("Kimi",         r"kimi", 85),
    ("Llama大杯",    r"llama-4|llama-3\.[23]-70|405b", 84),
    ("Llama小杯",    r"llama-3\.1-8b|llama-3\.2-(3|11)b", 66),
    ("Gemma",        r"gemma", 70),
    ("Mistral大",    r"mistral-large", 86),
    ("MiniMax",      r"minimax|abab", 84),
    ("Nemotron Ultra", r"nemotron-3-ultra", 82),
    ("HY3",          r"hy3", 78),
    ("X-Preview系",  r"x-preview|ox-alpha", 80),
    ("Pickle/Muse",  r"big-pickle|muse-spark", 76),
    ("Laguna",       r"laguna", 72),
    ("Pollinations别名", r"^openai(-fast)?$", 74),
    ("Inkling",      r"inkling", 79),
    ("Dots",         r"dots", 77),
]

DEFAULT_SCORE = 60


def model_score(model_id):
    """返回 (家族标签, 分数)。家族表未命中时用 datalearner 参数量兜底。
    注意：结果依赖异步补全的参数缓存，故不做 lru_cache，避免陈旧负缓存。"""
    mid = (model_id or "").lower()
    for tag, pat, score in RULES:
        try:
            if re.search(pat, mid):
                return tag, score
        except Exception:
            continue
    # 参数兜底：查本地缓存（后台线程会自动补全），按参数量/上下文折算
    try:
        import core.model_params as mp
        info = mp.get_cached(mid)
        if info:
            s = mp.param_score(info.get("params"), info.get("ctx"))
            if s is not None:
                size = info.get("params_text") or "未知规模"
                return f"参数推断({size})", s
    except Exception:
        pass
    return "未知家族", DEFAULT_SCORE


def supplier_capability(supplier):
    """供应商能力权重：
    - 手填 capability (>0) 直接用（source=manual）
    - 否则取其启用模型的最高测评分 → 折算权重 0.55~1.25
    返回 {"weight": float, "source": str, "score": int}
    """
    manual = supplier.get("capability")
    try:
        if manual is not None and float(manual) > 0:
            return {"weight": round(float(manual), 3), "source": "manual",
                    "score": None}
    except Exception:
        pass
    best, tag = DEFAULT_SCORE, "未知家族"
    for m in supplier.get("models", []):
        if m.get("enabled", True):
            t, sc = model_score(m.get("id"))
            if sc > best or tag == "未知家族":
                best, tag = sc, t
    weight = round(0.55 + best / 100 * 0.7, 3)
    return {"weight": min(weight, 1.25), "source": f"benchmark:{tag}",
            "score": best}
