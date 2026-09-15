# -*- mode: python ; coding: utf-8 -*-
"""AI-LLM 网关单文件版（Windows）：产出 AI-LLM-win64.exe（内置解释器+资源）。

与 packaging/AI-LLM.spec 复用同一批数据/隐式依赖，仅改为 onefile 模式，
供 GitHub Actions（windows-latest）构建后挂到 Release。
"""
import os
import pathlib

from PyInstaller.utils.hooks import collect_data_files

if "SPECPATH" in globals():
    HERE = pathlib.Path(SPECPATH)
else:
    HERE = pathlib.Path(os.path.dirname(__file__)).resolve()
ROOT = HERE.parent

datas = collect_data_files("tiktoken") + [(os.path.join(str(ROOT), "web"), "web")]
binaries = []

hiddenimports = [
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "h11",
    "httptools",
    "sniffio",
    "anyio._backends._asyncio",
]

a = Analysis(
    [os.path.join(str(ROOT), "server.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

_icon = os.path.join(str(ROOT), "packaging", "icon.ico")
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    exclude_binaries=False,
    name="AI-LLM-win64",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)