# -*- coding: utf-8 -*-
# --- AI-LLM Windows 打包脚本（在 Windows 机器上运行一次即可） ---
# 前置：Python 3.12+（https://www.python.org/downloads/，勾选 Add to PATH）
#       可选 Inno Setup 6（winget install JRSoftware.InnoSetup）→ 出 Setup.exe
# 产物：
#   dist\AI-LLM\             （onedir 免安装版，整个文件夹直接拷走运行）
#   dist\AI-LLM-Setup-?.?.?.exe （Inno 安装器，版本号读 installer.iss 的 MyAppVersion）
# 用法：在仓库根目录执行  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# 注意：必须在 Windows 上构建（PyInstaller 不支持跨平台）；构建机无需任何密钥。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 1) venv + 依赖
if (-not (Test-Path ".build-venv")) {
    py -3.12 -m venv .build-venv
}
$py = ".build-venv\Scripts\python.exe"
& $py -m pip install --quiet --disable-pip-version-check --upgrade pip
& $py -m pip install --quiet --disable-pip-version-check -r requirements.txt psutil pyinstaller

# 2) onedir 免安装版（无控制台，双击即可）
& $py -X utf8 -m PyInstaller packaging\AI-LLM.spec --noconfirm --distpath dist --workpath build-win
Write-Host "[OK] onedir 免安装版 -> dist\AI-LLM\"

# 3) （可选）Inno Setup 安装器
$iscc = Get-ChildItem "$env:LOCALAPPDATA\Programs\Inno Setup*\ISCC.exe" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($iscc) {
    & $iscc.FullName "packaging\installer.iss"
    $v = Select-String -Path "packaging\installer.iss" -Pattern 'MyAppVersion "([^"]+)"' | ForEach-Object { $_.Matches[0].Groups[1].Value }
    Write-Host "[OK] 安装器 -> dist\AI-LLM-Setup-$v.exe"
} else {
    Write-Host "[SKIP] 未装 Inno Setup，跳过安装器（onedir 已可分发）。winget install JRSoftware.InnoSetup"
}

Remove-Item -Recurse -Force ".build-venv", "build-win" -ErrorAction SilentlyContinue
Write-Host "完成。"