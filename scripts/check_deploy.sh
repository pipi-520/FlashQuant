#!/usr/bin/env bash
# 部署自检：检查环境 / 依赖 / 密钥 / 网络 / 服务。
# 用法：sudo bash scripts/check_deploy.sh [APP_DIR]
set -uo pipefail

APP_DIR="${1:-/opt/chaogu}"
PASS=0
FAIL=0

ok()  { echo "  [OK]   $1"; PASS=$((PASS+1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }
sec() { echo; echo "== $1 =="; }

probe() { curl -s -m 8 -o /dev/null "$1"; }

sec "基础环境"
command -v python3 >/dev/null 2>&1 && ok "python3 存在：$(python3 --version 2>&1)" || bad "python3 未安装"
[ -d "$APP_DIR" ] && ok "应用目录存在：$APP_DIR" || bad "应用目录不存在：$APP_DIR"
[ -d "$APP_DIR/.venv" ] && ok "虚拟环境存在" || bad "虚拟环境不存在（先运行 install_all.sh）"

sec "Python 依赖"
if [ -d "$APP_DIR/.venv" ]; then
  "$APP_DIR/.venv/bin/python" -c "import akshare,pandas,numpy,requests,yaml" >/dev/null 2>&1 \
    && ok "核心库可 import" || bad "核心库 import 失败（akshare/pandas/numpy/requests/yaml）"
fi

sec "密钥配置"
if [ -s "$APP_DIR/.env" ]; then
  grep -qE '^SERVERCHAN_SENDKEY=.+' "$APP_DIR/.env" 2>/dev/null \
    && ok ".env 已配置 Server酱" || bad ".env 缺少 SERVERCHAN_SENDKEY"
  grep -qE '^WECOM_WEBHOOK=.+' "$APP_DIR/.env" 2>/dev/null \
    && ok ".env 已配置企业微信" || echo "  [提示] .env 未配置 WECOM_WEBHOOK（企业微信推送不可用）"
else
  bad "未找到 $APP_DIR/.env（复制 .env.example 填写）"
fi

sec "网络连通性（香港/海外应全通；大陆服务器英文源会不通）"
probe "https://quote.eastmoney.com" && ok "中文源(东财)可达" || bad "中文源(东财)不可达"
probe "https://news.google.com"      && ok "英文源(Google News)可达" || bad "英文源(Google News)不可达"
probe "https://sctapi.ftqq.com"      && ok "Server酱 API 可达" || bad "Server酱 API 不可达"

sec "systemd 服务"
if systemctl list-unit-files 2>/dev/null | grep -q chaogu-monitor.service; then
  systemctl is-active --quiet chaogu-monitor && ok "chaogu-monitor 运行中" || bad "chaogu-monitor 未运行"
else
  bad "chaogu-monitor 未安装（运行 install_all.sh）"
fi
if systemctl list-unit-files 2>/dev/null | grep -q chaogu-daily.timer; then
  systemctl is-active --quiet chaogu-daily.timer && ok "chaogu-daily.timer 已启用" || bad "chaogu-daily.timer 未启用"
else
  echo "  [提示] chaogu-daily.timer 未安装（日报也可由 GitHub Actions 承担）"
fi

echo
echo "结果：通过 $PASS 项，失败 $FAIL 项"
if [ "$FAIL" -eq 0 ]; then
  echo "部署环境就绪 ✅"
else
  echo "请按上面 FAIL 项排查。"
fi
