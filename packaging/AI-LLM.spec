# -*- mode: python ; coding: utf-8 -*-
"""AI-LLM 网关打包：onedir + 无控制台（GUI 启动，日志落 data-home/logs/）。

- 数据目录与代码目录分离：core/paths.py 统管（Windows→%LocalAppData%/AI-LLM，
  Linux→$XDG_DATA_HOME/ai-llm 或 ~/.local/share/ai-llm），安装目录保持只读可升级。
- web/ 静态资源随包分发（只读）。
- tiktoken 需内置 cl100k_base 词表。
- 本 spec 跨平台：ROOT 由 spec 自身位置推导，Windows 构建出 exe，
  Ubuntu 构建出 Linux 可执行（见 packaging/build_ubuntu.sh）。
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
    [],
    exclude_binaries=True,
    name="AI-LLM",
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
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AI-LLM",
)