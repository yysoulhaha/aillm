# -*- coding: utf-8 -*-
"""AI-Gateway-C 桌面客户端（Python 原生窗口版）。

依赖：pip install pywebview
运行：py -3 client.py  （自动连接本机 http://127.0.0.1:18111/app）
若网关未启动，会尝试在本进程拉起 server.py。
"""
import subprocess
import sys
import time
import urllib.request

import pathlib

HERE = pathlib.Path(__file__).resolve().parent
PORT = 18111
URL = f"http://127.0.0.1:{PORT}/app"


def _gw_up():
    try:
        urllib.request.urlopen(URL, timeout=2)
        return True
    except Exception:
        return False


def _ensure_server():
    if _gw_up():
        return
    subprocess.Popen([sys.executable, str(HERE / "server.py")],
                     creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
    for _ in range(20):
        time.sleep(1)
        if _gw_up():
            return


def main():
    _ensure_server()
    import webview
    webview.create_window("AI-Gateway-C", URL, width=1100, height=760)
    webview.start()


if __name__ == "__main__":
    main()
