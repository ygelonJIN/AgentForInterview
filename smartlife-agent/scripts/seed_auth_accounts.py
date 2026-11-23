"""创建管理员账号（可重复执行）；普通用户在登录页自助注册。

用法：
    venv312/bin/python scripts/seed_auth_accounts.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth_service.config import Settings
from auth_service.db import db_session, init_db
from auth_service.repository import create_account, find_account_by_id, find_account_by_username
from auth_service.security import hash_password

DEMO_ACCOUNTS = (
    {
        "id": "user_001",
        "username": "admin",
        "password": "adminadmin",
        "display_name": "管理员",
        "roles": ("user", "admin"),
    },
)


def main() -> None:
    base_dir = Path(__file__).resolve().parents[1]
    settings = Settings.from_env(base_dir=base_dir)
    init_db(settings.db_path)
    with db_session(settings.db_path) as conn:
        for item in DEMO_ACCOUNTS:
            if find_account_by_username(conn, item["username"]) or find_account_by_id(conn, item["id"]):
                print(f"已存在，跳过：{item['username']}")
                continue
            create_account(
                conn,
                account_id=item["id"],
                username=item["username"],
                password_hash=hash_password(item["password"], settings.bcrypt_rounds),
                display_name=item["display_name"],
                roles=item["roles"],
            )
            print(f"已创建：{item['username']} / {item['password']}（{'/'.join(item['roles'])}）")
    print(f"账号库：{settings.db_path}")


if __name__ == "__main__":
    main()
