# FlashQuant 服务器部署交接文档

> 给接手的 agent：本文档包含把 FlashQuant 部署到 Linux 服务器所需的全部信息，可独立照做，无需再问。

## 1. 项目与部署目标

- **项目**：FlashQuant（GitHub: `pipi-520/FlashQuant`，分支 `master`）——多源新闻情绪量化工具。
- **本次部署目标**：在 Linux 服务器上 24/7 运行实时新闻事件监测 `news_aggregator/monitor.py`，命中高影响事件即推送到企业微信。
- **日报已托管，服务器不用管日报**：每日新闻聚合 + LLM 综述 + 日报推送已由 GitHub Actions 自动跑（工作日 18:30 北京时间），服务器不需要重复部署日报定时任务（可选，见 §6）。

## 2. 服务器环境要求

- **系统**：Ubuntu 22.04（推荐）
- **Python**：3.12（脚本会自动建 venv）
- **地域**：**香港轻量服务器**（腾讯云/阿里云香港）
  - 原因：英文源（Google News、白宫、SEC EDGAR、FRED、美联储官网）在大陆服务器会被墙，静默失败；香港能同时访问中英文源。
- 内存 ≥ 1GB 即可，磁盘 ≥ 5GB。

## 3. 代码获取

```bash
sudo mkdir -p /opt/chaogu
sudo git clone https://github.com/pipi-520/FlashQuant.git /opt/chaogu
cd /opt/chaogu
```

代码已是最新（含日报 LLM 综述、企业微信拆分推送、.env 自动加载等），直接 clone 即可。

## 4. 密钥配置（.env）

在 `/opt/chaogu/.env` 里配置密钥（复制模板后填写）：

```bash
sudo cp .env.example .env
sudo vim .env
```

| 变量 | 必需 | 用途 |
|---|---|---|
| `WECOM_WEBHOOK` | ✅ 必须 | 企业微信群机器人 webhook，格式 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...` |
| `SERVERCHAN_SENDKEY` | 推荐 | Server酱微信推送（备用通道） |
| `OPENAI_BASE_URL` | ✅（日报综述需要） | DeepSeek API 地址 `https://api.deepseek.com/v1` |
| `OPENAI_API_KEY` | ✅（日报综述需要） | DeepSeek key（`sk-...`） |
| `OPENAI_MODEL` | ✅（日报综述需要） | 填 `deepseek-v4-pro`（注意：旧名 `deepseek-chat` 已停用） |
| `FRED_API_KEY` | 可选 | 美联储经济数据 |
| `QUIVER_TOKEN` | 可选 | Quiver 另类数据 |

> 实际密钥值从本地开发机的 `.env` 文件复制（该文件被 gitignore，未在仓库里）。

## 5. 部署步骤（systemd，推荐）

```bash
# 一键安装：自动建 venv、装依赖、注册 systemd 服务（monitor 24/7 + 日报 timer）
sudo bash /opt/chaogu/scripts/install_all.sh /opt/chaogu

# 自检（检查环境/依赖/密钥/网络/服务）
sudo bash /opt/chaogu/scripts/check_deploy.sh /opt/chaogu
```

说明：
- `install_all.sh` 会安装两个 systemd 单元：
  - `chaogu-monitor.service`：实时监测，`Restart=always` + `RestartSec=5`（崩溃 5 秒自动拉起、开机自启）。
  - `chaogu-daily.timer`：工作日 18:30（服务器本地时间）跑日报。
- 若只想装 monitor（不装日报 timer），用 `install_service.sh` 代替。

### 备选：Docker 部署

```bash
cd /opt/chaogu
docker compose up -d --build
docker compose logs -f
```

环境变量由 `docker-compose.yml` 从根目录 `.env` 自动读取；`.dockerignore` 已确保密钥不进镜像。

## 6. 部署后验证

```bash
# 服务状态（应为 active (running)）
systemctl status chaogu-monitor

# 实时日志
journalctl -u chaogu-monitor -f

# 手动跑一轮测试（不推送）
/opt/chaogu/.venv/bin/python news_aggregator/monitor.py --once --dry-run --no-boards

# 手动跑一轮并推送（验证企业微信能收到）
/opt/chaogu/.venv/bin/python news_aggregator/monitor.py --once
```

验证成功的标志：
1. `systemctl status chaogu-monitor` 显示 `active (running)`。
2. 日志里能看到抓取源计数（如 `[fetch] 财联社: N 条`）。
3. `monitor.py --once` 后，企业微信收到告警消息。

## 7. 关键配置说明（config.yaml）

部署前确认 `config.yaml` 里这些项（一般不用改，已是最优默认）：

- `monitor.poll_interval: 15`：轮询秒数。
- `monitor.impact_min: 0.3`：影响分告警阈值（只推高影响事件，避免刷屏）。
- `monitor.push.wecom: true`：企业微信推送开关。
- `monitor.push.serverchan: true`：Server酱推送开关。
- `summary.enabled: true` / `summary.top_n: 100`：日报 LLM 综述（日报由 GitHub Actions 跑，服务器不涉及）。
- `sentiment.backend: lexicon`：情绪打分后端（词典，零依赖；可改 finbert/llm）。

## 8. 已知坑与注意事项（务必看）

1. **英文源被墙**：必须选香港/海外服务器，否则英文源静默失败（不影响中文源，但会丢数据）。
2. **企业微信超长**：日报超过 4096 字节会自动拆成多条发送（已实现，无需处理）。
3. **.env 自动加载**：`run.py`/`monitor.py` 启动时已自动加载 `.env`（`load_env_file`），systemd 的 `EnvironmentFile` 是双保险，不冲突。
4. **时区**：`chaogu-daily.timer` 的 `OnCalendar=Mon..Fri 18:30` 用的是**服务器本地时间**，部署后确认服务器时区是否正确（`timedatectl`）。
5. **只告警不下单**：monitor 只推送告警，不涉及任何真实交易，安全。
6. **GitHub Actions 已配好，不要动**：日报 workflow、企业微信 secret、DeepSeek secret 都已在 GitHub 仓库配置完成，服务器部署**不要**去改这些。

## 9. 运维命令速查

```bash
sudo systemctl restart chaogu-monitor      # 重启
sudo systemctl stop chaogu-monitor         # 停止
journalctl -u chaogu-monitor -n 100        # 最近 100 行日志
sudo bash /opt/chaogu/scripts/check_deploy.sh /opt/chaogu   # 自检
```

## 10. 目录结构（服务器上 /opt/chaogu）

```
/opt/chaogu/
├─ news_aggregator/        # 聚合 + 实时监测核心（monitor.py 在这里）
├─ scripts/                # install_all.sh / check_deploy.sh 等部署脚本
├─ deploy/                 # systemd unit 模板
├─ config.yaml             # 统一配置（阈值/推送/综述）
├─ requirements-server.txt # 云端最小依赖
├─ .env                    # 密钥（需自己创建）
└─ news/                   # 运行数据（seen.json 去重缓存等）
```
