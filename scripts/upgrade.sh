#!/usr/bin/env bash
# 一键升级 crisp_tgbot 到最新 Release，并重启服务
# 用法：
#   bash upgrade.sh                      # 自动识别安装目录
#   bash scripts/upgrade.sh              # 在主目录或 scripts 目录下执行均可
#   bash upgrade.sh /root/tgbot          # 手动指定安装目录
set -e
REPO="4kercc/Crisp_Telegram_Bot"

# 智能判断目标安装目录：
# 1. 如果传参了，使用参数目录
# 2. 如果当前目录存在 crisp_tgbot，使用当前目录
# 3. 如果脚本位于 scripts/ 子目录，且上一级目录存在 crisp_tgbot，使用上一级目录
# 4. 如果 systemd 服务文件存在且指定了 WorkingDirectory/ExecStart，优先取该路径
if [ -n "$1" ]; then
    DIR="$1"
elif [ -f "$(pwd)/crisp_tgbot" ]; then
    DIR="$(pwd)"
elif [ -f "$(dirname "$0")/../crisp_tgbot" ]; then
    DIR="$(cd "$(dirname "$0")/.." && pwd)"
elif [ -f "/root/tgbot/crisp_tgbot" ]; then
    DIR="/root/tgbot"
else
    DIR="$(pwd)"
fi

cd "$DIR"
echo "==> 目标安装目录: $DIR"

echo "==> 获取最新 Release 下载地址"
URL=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
      | grep -o '"browser_download_url": *"[^"]*/crisp_tgbot"' | head -1 | cut -d'"' -f4)
if [ -z "$URL" ]; then
    echo "获取下载地址失败，请检查网络后重试"
    exit 1
fi
TAG=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4)
echo "    最新版本: $TAG"
echo "    下载地址: $URL"

echo "==> 下载新版本到 $DIR/crisp_tgbot.new"
curl -fL --progress-bar -o "$DIR/crisp_tgbot.new" "$URL"
chmod +x "$DIR/crisp_tgbot.new"

echo "==> 停止旧进程"
if systemctl stop crisp-tgbot 2>/dev/null; then
    echo "    已停止 systemd 服务 crisp-tgbot"
else
    pkill -f crisp_tgbot 2>/dev/null && echo "    已停止手动运行的进程" || echo "    没有发现运行中的进程"
fi
sleep 2

mv -f "$DIR/crisp_tgbot.new" "$DIR/crisp_tgbot"

echo "==> 启动"
if systemctl start crisp-tgbot 2>/dev/null; then
    echo "    已启动 systemd 服务"
    echo "==> 完成。查看日志：journalctl -u crisp-tgbot -f"
else
    nohup ./crisp_tgbot --host 0.0.0.0 > bot.log 2>&1 &
    echo "    已用 nohup 方式后台启动（建议后续配置 systemd 常驻）"
    echo "==> 完成。查看日志：tail -f bot.log"
fi
echo "    config.yml 与控制台密码保留在 $DIR，无需重新配置。"
