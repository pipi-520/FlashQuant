"""多通道推送：企业微信 + Server酱(微信) + Telegram + ntfy。"""

import json
import os


# ---------- 企业微信 ----------

def send_wecom_markdown(webhook: str, content: str) -> bool:
    """向企业微信群机器人发送 markdown 消息；webhook 为空时仅打印（干跑）。"""
    webhook = (webhook or "").strip()
    if not webhook:
        print("[push] 企业微信 webhook 未配置，干跑跳过")
        return False
    if "://" not in webhook:
        webhook = "https://" + webhook
    import requests
    # 企业微信 markdown 消息上限 4096 字节，超长截断（UTF-8 安全）
    raw = content.encode("utf-8")
    if len(raw) > 4000:
        content = raw[:3950].decode("utf-8", errors="ignore") + "\n\n…（内容过长，已截断）"
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        data = r.json() if r.text else {}
        ok = r.status_code == 200 and data.get("errcode") == 0
        print(f"[push] 企业微信: {r.status_code} {json.dumps(data, ensure_ascii=False)}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"[push] 企业微信发送失败: {e}")
        return False


def get_webhook() -> str:
    return os.environ.get("WECOM_WEBHOOK", "")


# ---------- Server酱（微信推送，无需VPN） ----------

def send_serverchan(sendkey: str, title: str, content: str) -> bool:
    if not sendkey:
        print("[push] Server酱 sendkey 未配置，跳过")
        return False
    import requests
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    try:
        r = requests.post(url, data={"title": title[:32], "desp": content}, timeout=10)
        data = r.json() if r.text else {}
        ok = r.status_code == 200 and data.get("code") == 0
        print(f"[push] Server酱: {r.status_code} {json.dumps(data, ensure_ascii=False)}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"[push] Server酱发送失败: {e}")
        return False


# ---------- Telegram ----------

def send_telegram(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        print("[push] Telegram token/chat_id 未配置，跳过")
        return False
    import requests
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        ok = r.status_code == 200 and r.json().get("ok") is True
        print(f"[push] Telegram: {r.status_code}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"[push] Telegram发送失败: {e}")
        return False


# ---------- ntfy ----------

def send_ntfy(topic: str, title: str, message: str) -> bool:
    if not topic:
        print("[push] ntfy topic 未配置，跳过")
        return False
    import requests
    url = f"https://ntfy.sh/{topic}"
    try:
        r = requests.post(url, json={"title": title, "message": message}, timeout=10)
        ok = r.status_code == 200
        print(f"[push] ntfy: {r.status_code}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"[push] ntfy发送失败: {e}")
        return False


# ---------- 统一入口 ----------

def push_alert(cfg: dict, title: str, content: str) -> dict:
    """按 config['monitor']['push'] 多通道发送告警。返回各通道结果。

    密钥读取优先级：config -> 环境变量。
    """
    m = (cfg.get("monitor") or {})
    pc = m.get("push") or {}
    if not pc:
        pc = {"wecom": True, "serverchan": True}

    results = {}
    if pc.get("wecom", True):
        webhook = (m.get("wecom_webhook") or ""
                   or (cfg.get("news") or {}).get("wecom_webhook") or ""
                   or os.environ.get("WECOM_WEBHOOK", ""))
        results["wecom"] = send_wecom_markdown(webhook, content)

    if pc.get("serverchan", True):
        key = m.get("serverchan_sendkey") or os.environ.get("SERVERCHAN_SENDKEY", "")
        results["serverchan"] = send_serverchan(key, title, content)

    if pc.get("telegram", False):
        token = m.get("telegram_token") or os.environ.get("TELEGRAM_TOKEN", "")
        chat = m.get("telegram_chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")
        results["telegram"] = send_telegram(token, chat, content)

    if pc.get("ntfy", False):
        topic = m.get("ntfy_topic") or os.environ.get("NTFY_TOPIC", "")
        results["ntfy"] = send_ntfy(topic, title, content)

    return results

