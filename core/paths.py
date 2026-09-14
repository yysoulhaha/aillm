# -*- coding: utf-8 -*-
"""运行时路径解析：代码/资源目录与数据目录分离。

打包分发（PyInstaller）时代码位于只读应用目录，数据（config/logs/usage/
device 等）应落在可写用户目录；开发/便携运行沿用项目内 data/。
规则（自 2.0.1 起跨 Windows / Linux / macOS）：
  1) 环境变量 AI_LLM_HOME 显式指定 → 以此为数据根目录
  2) 打包运行（sys.frozen）且未指定 →
       Windows: %LocalAppData%/AI-LLM（无则 ~/.ai-llm）
       Linux/macOS: $XDG_DATA_HOME/ai-llm（无则 ~/.local/share/ai-llm）
  3) 开发/便携运行 → 项目根可写则用项目根（Windows dev 行为不变）；
     项目根不可写（如 .deb 装到 /opt/AI-LLM 只读目录）→ 回退用户数据目录
返回值为“根目录”，各子路径在下方统一为 /data、/logs。
"""
import os
import pathlib
import sys


def app_home() -> pathlib.Path:
    """代码/资源所在目录（含 web/、core/、种子 data/）。"""
    return pathlib.Path(__file__).resolve().parent.parent


def _platform_data_root() -> pathlib.Path:
    """按 OS 取“用户数据目录”的默认根。"""
    if sys.platform == "win32":
        base = (os.environ.get("LOCALAPPDATA")
                or os.environ.get("APPDATA") or "")
        return pathlib.Path(base) / "AI-LLM" if base \
            else pathlib.Path.home() / ".ai-llm"
    base = os.environ.get("XDG_DATA_HOME") or str(pathlib.Path.home() / ".local" / "share")
    return pathlib.Path(base) / "ai-llm"


def data_home() -> pathlib.Path:
    """数据根目录（其下分 data/、logs/）。"""
    env = os.environ.get("AI_LLM_HOME")
    if env and env.strip():
        return pathlib.Path(env.strip())
    if getattr(sys, "frozen", False):
        return _platform_data_root()
    root = app_home()
    try:
        if os.access(root, os.W_OK):
            return root
    except Exception:
        pass
    # 项目根不可写（.deb 装在 /opt/AI-LLM 等只读目录）→ 用户数据目录
    return _platform_data_root()