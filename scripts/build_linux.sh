#!/usr/bin/env bash
# 在 Linux 上把项目打包成单文件可执行程序 dist/crisp_tgbot
# 用法：bash scripts/build_linux.sh
# 要求：Python >= 3.8（在目标系统同版本或更老 glibc 的系统上构建，保证兼容性）
set -e
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"

echo "==> 安装依赖与 PyInstaller"
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r requirements.txt "pyinstaller==6.6.0"

echo "==> 打包（PyInstaller onefile）"
"$PYTHON" -m PyInstaller crisp_tgbot.spec --noconfirm --clean

echo "==> 构建完成"
ls -lh dist/crisp_tgbot
echo "部署：scp dist/crisp_tgbot 到服务器后 chmod +x crisp_tgbot && ./crisp_tgbot"
