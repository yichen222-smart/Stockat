#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from gateway.platforms.weixin import send_weixin_direct


def load_weixin_environment() -> None:
    for key in list(os.environ):
        if key.startswith("WEIXIN_"):
            os.environ.pop(key, None)
    env_path = Path(os.environ.get("HERMES_HOME", "")) / ".env"
    if not env_path.is_file():
        raise RuntimeError("Hermes environment file is missing")
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.startswith("WEIXIN_"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
            value = value[1:-1]
        os.environ[key] = value


async def main() -> int:
    load_weixin_environment()
    if len(sys.argv) != 2:
        print(json.dumps({"error": "chat_id required"}))
        return 2
    result = await send_weixin_direct(
        extra={
            "account_id": os.environ.get("WEIXIN_ACCOUNT_ID", ""),
            "base_url": os.environ.get("WEIXIN_BASE_URL", "https://ilinkai.weixin.qq.com"),
            "cdn_base_url": os.environ.get("WEIXIN_CDN_BASE_URL", "https://novac2c.cdn.weixin.qq.com/c2c"),
        },
        token=os.environ.get("WEIXIN_TOKEN", ""),
        chat_id=sys.argv[1],
        message=sys.stdin.read(),
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))