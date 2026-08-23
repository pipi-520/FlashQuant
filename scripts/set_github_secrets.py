"""设置 GitHub Actions secrets（企业微信 / Server酱 / FRED / Quiver）。

用法（在项目根目录执行）：
    .venv/Scripts/python.exe scripts/set_github_secrets.py --token <你的PAT>

PAT 权限要求（二选一）：
    - classic PAT：勾选 repo 权限
    - fine-grained PAT：Repository permissions -> Actions -> Secrets（Read and write）

脚本从项目根目录 .env 读取密钥，写入 GitHub 仓库的 Actions secrets。
"""

import argparse
import base64
import pathlib
import sys

import requests
from nacl import encoding, public

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = "https://api.github.com"
DEFAULT_REPO = "pipi-520/FlashQuant"

# (.env 里的键, GitHub secret 名)
SECRET_MAP = [
    ("WECOM_WEBHOOK", "WECOM_WEBHOOK"),
    ("SERVERCHAN_SENDKEY", "SERVERCHAN_SENDKEY"),
    ("FRED_API_KEY", "FRED_API_KEY"),
    ("QUIVER_TOKEN", "QUIVER_TOKEN"),
]


def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def encrypt(public_key: str, secret_value: str) -> str:
    pub = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    box = public.SealedBox(pub)
    return base64.b64encode(box.encrypt(secret_value.encode("utf-8"))).decode("utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True, help="GitHub PAT with Actions secrets write permission")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo")
    ap.add_argument("--only", help="只设置指定 secret，逗号分隔（默认全部）")
    args = ap.parse_args()

    headers = {
        "Authorization": f"Bearer {args.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    pk_url = f"{API}/repos/{args.repo}/actions/secrets/public-key"
    r = requests.get(pk_url, headers=headers, timeout=15)
    if r.status_code != 200:
        print(f"[FAIL] 获取公钥失败：{r.status_code} {r.text}")
        print("       请确认 PAT 有 Actions secrets 写权限（fine-grained 需勾选 Secrets: Read and write）")
        return 1
    pk = r.json()
    key_id, key = pk["key_id"], pk["key"]

    env = load_env()
    only = {x.strip() for x in (args.only or "").split(",") if x.strip()}

    ok = 0
    for env_key, secret_name in SECRET_MAP:
        if only and secret_name not in only:
            continue
        value = env.get(env_key, "")
        if not value:
            print(f"[SKIP] {secret_name}：.env 未配置")
            continue
        enc = encrypt(key, value)
        put_url = f"{API}/repos/{args.repo}/actions/secrets/{secret_name}"
        rr = requests.put(put_url, headers=headers, json={"encrypted_value": enc, "key_id": key_id}, timeout=15)
        if rr.status_code in (201, 204):
            print(f"[OK]   {secret_name} 已设置")
            ok += 1
        else:
            print(f"[FAIL] {secret_name}：{rr.status_code} {rr.text}")

    print(f"\n完成：成功 {ok} 个 secret。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
