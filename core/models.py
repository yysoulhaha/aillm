# -*- coding: utf-8 -*-
"""模型别名表达式解析（feat#5）。

支持请求体 model 字段写成：
  - 具体模型 id（如 meta/llama-3.1-8b-instruct）
  - 分组别名：auto / fast / daily / vision
  - 表达式：<别名>:<约束>
       auto:free       只在「免费/limited」供应商里选
       auto:paid       只在「付费/unlimited」供应商里选
       auto:ctx>=524288  只选上下文 >= 524288 的模型
       auto:ctx<=8000
       vision:free     支持多模态且免费
约束可组合（用 & 连接）：auto:free&ctx>=32000
"""
import re


def parse_model_request(req):
    """req: model 字符串。返回 (alias, constraints)。

    alias: 具体 id 或分组名(auto/fast/daily/vision)
    constraints: dict {free,paid,ctx_min,ctx_max,vision,tools}

    注意：形如 "deepseek/deepseek-chat:free" 的 OpenRouter 风格 ID 自带
    ':free' 后缀且含供应商前缀('/')——这类整体视为具体模型 ID，
    不做约束拆分；约束表达式只对无 '/' 前缀的别名生效。
    """
    req = (req or "").strip()
    constraints = {"free": None, "paid": None, "ctx_min": None,
                   "ctx_max": None, "vision": None, "tools": None}
    head = req.split(":", 1)[0]
    if ":" in req and "/" in head:          # vendor/model(:suffix) → 整体是 ID
        return req, constraints
    if ":" not in req:
        return req, constraints
    alias, expr = req.split(":", 1)
    alias = alias.strip()
    for part in expr.split("&"):
        part = part.strip().lower()
        if part == "free":
            constraints["free"] = True
        elif part == "paid":
            constraints["paid"] = True
        elif part == "vision":
            constraints["vision"] = True
        elif part == "tools":
            constraints["tools"] = True
        elif part.startswith("ctx>="):
            constraints["ctx_min"] = _num(part[5:])
        elif part.startswith("ctx<="):
            constraints["ctx_max"] = _num(part[5:])
        elif part.startswith("ctx>"):
            constraints["ctx_min"] = _num(part[4:]) + 1
        elif part.startswith("ctx<"):
            constraints["ctx_max"] = _num(part[4:]) - 1
    return alias, constraints


def _num(s):
    try:
        return int(re.sub(r"[^\d]", "", s))
    except Exception:
        return 0


def match_constraints(supplier, model_meta, constraints):
    """supplier: 供应商对象；model_meta: {id, contextWindow, multimodal, supports_tools}。"""
    if constraints["free"] is True:
        if not _is_free(supplier):
            return False
    if constraints["paid"] is True:
        if _is_free(supplier):
            return False
    if constraints["vision"] is True:
        if not model_meta.get("multimodal"):
            return False
    if constraints["tools"] is True:
        if not model_meta.get("supports_tools"):
            return False
    ctx = model_meta.get("contextWindow") or 0
    if constraints["ctx_min"] and ctx < constraints["ctx_min"]:
        return False
    if constraints["ctx_max"] and ctx > constraints["ctx_max"]:
        return False
    return True


def _is_free(supplier):
    """判定供应商是否为「免费/limited」来源。"""
    if supplier.get("quota") == "limited":
        return True
    for k in supplier.get("keys", []):
        if isinstance(k, dict) and k.get("key") == "__KEYLESS__":
            return True
        if k == "__KEYLESS__":
            return True
    return False


GROUP_ALIASES = {"auto", "fast", "daily", "vision"}
