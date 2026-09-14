@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 AI-LLM 网关与服务端…
start "" pythonw server.py
timeout /t 3 /nobreak >nul
echo 正在打开桌面客户端窗口…
start "" "gateway-client\target\release\ai-llm-client.exe"
