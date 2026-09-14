#!/usr/bin/env bash
# ============================================================
# AI-LLM Ubuntu 一键构建脚本
#   作用：在新装的 Ubuntu 上重建「开发环境 + 分发物」
#   产出：
#     1) dist-linux/AI-LLM/         PyInstaller 独立可执行（onedir）
#     2) dist/ai-llm-gateway_<ver>_all.deb   可安装包（dpkg 安装）
#   用法：
#     bash packaging/build_ubuntu.sh              # 全量（venv+PyInstaller+deb）
#     SKIP_PYINSTALLER=1 bash packaging/build_ubuntu.sh   # 只出 .deb（快）
# 前提：Ubuntu 22.04+；已联网；仓库已克隆（本脚本即位于仓库 packaging/ 下）。
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-python3}"

echo "==> [1/4] 系统依赖（可用 sudo，会 apt-get install）"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip dpkg-dev curl git

echo "==> [2/4] 构建用虚拟环境（$ROOT/build-venv）"
if [ ! -x "$ROOT/build-venv/bin/python" ]; then
  "$PY" -m venv "$ROOT/build-venv"
fi
VBPY="$ROOT/build-venv/bin/python"
"$VBPY" -m pip install --quiet --disable-pip-version-check --upgrade pip
"$VBPY" -m pip install --quiet --disable-pip-version-check -r "$ROOT/requirements.txt"

echo "==> [3/5] 生成 .deb 安装包（纯 Python，无编译）"
"$VBPY" "$ROOT/packaging/make_deb.py"
DEB="$(ls -t "$ROOT"/dist/ai-llm-gateway_*_all.deb | head -n1)"
echo "     .deb: $DEB"

if [ "${SKIP_PYINSTALLER:-0}" = "1" ]; then
  echo "==> SKIP_PYINSTALLER=1，跳过 PyInstaller 构建。"
else
  echo "==> [4/5] PyInstaller 独立可执行（Linux onedir）"
  "$VBPY" -m pip install --quiet --disable-pip-version-check pyinstaller
  ( cd "$ROOT" && "$VBPY" -m PyInstaller packaging/AI-LLM.spec \
      --noconfirm --distpath dist-linux --workpath build-linux )
  echo "     onedir: $ROOT/dist-linux/AI-LLM"
fi

echo "==> [5/5] 开发运行（可选，后台）"
echo "     手动：cd $ROOT && source build-venv/bin/activate && python server.py"
echo ""
echo "完成！分发物："
echo "   - 安装包: $DEB       安装: sudo apt install ./$DEB"
echo "   - 独立版: $ROOT/dist-linux/AI-LLM/AI-LLM（双击或终端运行）"
echo "   - 启动命令(装deb后): ai-llm --open    自启: systemctl --user enable --now ai-llm"
echo "   - 数据目录: ~/.local/share/ai-llm（首启自动下载免费模型）"