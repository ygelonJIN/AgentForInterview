"""不经过 Streamlit 的身份服务客户端，用来证明同一套接口可以给别的客户端复用。

用法：
    venv312/bin/python scripts/auth_cli.py demo --password demo1234
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth.client import AuthAPIError, AuthClient


def main() -> int:
    parser = argparse.ArgumentParser(description="登录并读取当前账号")
    parser.add_argument("username")
    parser.add_argument("--password", default="")
    parser.add_argument("--base-url", default="")
    args = parser.parse_args()

    password = args.password or getpass.getpass("密码: ")
    client = AuthClient(args.base_url or None)
    try:
        tokens = client.login(args.username, password)
        me = client.me(tokens["access_token"])
        profile = client.profile(tokens["access_token"])
        orders = client.orders(tokens["access_token"])["orders"]
    except AuthAPIError as exc:
        print(f"失败（{exc.status_code}/{exc.code}）：{exc.message}", file=sys.stderr)
        return 1

    print(json.dumps(
        {
            "account": me,
            "profile": profile,
            "orders": len(orders),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
