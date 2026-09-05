#!/usr/bin/env bash
# 一键升级 crisp_tgbot 到最新 Release，并重启服务
# 用法：
#   bash scripts/upgrade.sh              # 在二进制所在目录执行
#   bash scripts/upgrade.sh /root/tgbot  # 指定安装目录
set -e
REPO="4kercc/Crisp_Telegram_Bot"
DIR="${1:-$(pwd)}"
cd "$DIR"

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

echo "==> 下载新版本"
curl -fL --progress-bar -o crisp_tgbot.new "$URL"
chmod +x crisp_tgbot.new

echo "==> 停止旧进程"
if systemctl stop crisp-tgbot 2>/dev/null; then
    echo "    已停止 systemd 服务 crisp-tgbot"
else
    pkill -f crisp_tgbot 2>/dev/null && echo "    已停止手动运行的进程" || echo "    没有发现运行中的进程"
fi
sleep 2

mv -f crisp_tgbot.new crisp_tgbot

echo "==> 启动"
if systemctl start crisp-tgbot 2>/dev/null; then
    echo "    已启动 systemd 服务"
    echo "==> 完成。查看日志：journalctl -u crisp-tgbot -f"
else
    nohup ./crisp_tgbot --host 0.0.0.0 > bot.log 2>&1 &
    echo "    已用 nohup 方式后台启动（建议后续配置 systemd 常驻）"
    echo "==> 完成。查看日志：tail -f bot.log"
fi
echo "    config.yml 与控制台密码保留在原目录，无需重新配置。"
