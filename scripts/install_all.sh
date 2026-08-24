#!/usr/bin/env bash
# 一键安装：实时监测(monitor，24/7) + 每日聚合日报(daily.timer，工作日 18:30)
#         + 告警闭环回看(alert-review.timer，工作日 17:30) + 自检。
# 用法：sudo bash scripts/install_all.sh [APP_DIR]
set -euo pipefail

APP_DIR="${1:-/opt/chaogu}"
SVC="chaogu-monitor"
DAILY="chaogu-daily"
REVIEW="chaogu-alert-review"

echo "[1/7] 准备目录：$APP_DIR"
mkdir -p "$APP_DIR"

if [ ! -d "$APP_DIR/.venv" ]; then
  echo "[2/7] 创建虚拟环境"
  python3 -m venv "$APP_DIR/.venv"
else
  echo "[2/7] 虚拟环境已存在，跳过"
fi

echo "[3/7] 安装依赖（requirements-server.txt）"
REQ="$APP_DIR/requirements-server.txt"
[ -f "$REQ" ] || REQ="$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
"$APP_DIR/.venv/bin/pip" install -r "$REQ"

echo "[4/7] 安装 systemd 单元"
sed "s|/opt/chaogu|$APP_DIR|g" "$APP_DIR/deploy/chaogu-monitor.service" > "/etc/systemd/system/$SVC.service"
sed "s|/opt/chaogu|$APP_DIR|g" "$APP_DIR/deploy/chaogu-daily.service"  > "/etc/systemd/system/$DAILY.service"
sed "s|/opt/chaogu|$APP_DIR|g" "$APP_DIR/deploy/chaogu-daily.timer"   > "/etc/systemd/system/$DAILY.timer"
sed "s|/opt/chaogu|$APP_DIR|g" "$APP_DIR/deploy/chaogu-alert-review.service" > "/etc/systemd/system/$REVIEW.service"
sed "s|/opt/chaogu|$APP_DIR|g" "$APP_DIR/deploy/chaogu-alert-review.timer"   > "/etc/systemd/system/$REVIEW.timer"

echo "[5/7] 启用并启动"
systemctl daemon-reload
systemctl enable --now "$SVC"
systemctl enable --now "$DAILY.timer"
systemctl enable --now "$REVIEW.timer"

echo "[6/7] 自检"
bash "$APP_DIR/scripts/check_deploy.sh" "$APP_DIR"

echo "[7/7] 触发一次告警回看（验证企微推送链路）"
systemctl start "$REVIEW" || true

echo
echo "已部署："
echo "  - $SVC        实时监测（15 秒轮询，自启+崩溃重启）"
echo "  - $DAILY.timer 每工作日 18:30 日报推送"
echo "  - $REVIEW.timer 每工作日 17:30 告警闭环回看"
echo "  查看日志：journalctl -u $SVC -f"
