#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import re
from pathlib import Path


def validated_home(raw: str) -> Path:
    home = Path(raw).resolve()
    allowed = Path("/opt/grid-assistant/data").resolve()
    if home.parent != allowed or not re.fullmatch(r"hermes-member(?:[3-9]|[1-9][0-9]+)", home.name):
        raise SystemExit("invalid isolated Hermes home")
    if not home.is_dir():
        raise SystemExit("isolated Hermes home is not prepared")
    return home


def main() -> int:
    parser = argparse.ArgumentParser(description="Pair one isolated Weixin gateway")
    parser.add_argument("--home", required=True)
    args = parser.parse_args()
    hermes_home = validated_home(args.home)
    os.environ["HERMES_HOME"] = str(hermes_home)

    from gateway.platforms.weixin import check_weixin_requirements, qr_login
    from hermes_cli.config import save_env_value

    if not check_weixin_requirements():
        raise SystemExit("Weixin gateway dependencies are unavailable")
    credentials = asyncio.run(qr_login(str(hermes_home)))
    if not credentials:
        return 3

    save_env_value("WEIXIN_ACCOUNT_ID", credentials.get("account_id", ""))
    save_env_value("WEIXIN_TOKEN", credentials.get("token", ""))
    if credentials.get("base_url"):
        save_env_value("WEIXIN_BASE_URL", credentials["base_url"])
    save_env_value("WEIXIN_CDN_BASE_URL", "https://novac2c.cdn.weixin.qq.com/c2c")
    save_env_value("WEIXIN_DM_POLICY", "pairing")
    save_env_value("WEIXIN_ALLOW_ALL_USERS", "false")
    save_env_value("WEIXIN_ALLOWED_USERS", "")
    save_env_value("WEIXIN_GROUP_POLICY", "disabled")
    save_env_value("WEIXIN_GROUP_ALLOWED_USERS", "")
    if credentials.get("user_id"):
        save_env_value("WEIXIN_HOME_CHANNEL", credentials["user_id"])
    print(f"PAIRING_COMPLETE member_key={hermes_home.name.removeprefix('hermes-')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())