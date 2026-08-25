from __future__ import annotations

import sys

import qrcode


def main() -> int:
    token_or_url, output_path = sys.argv[1:3]
    url = (
        token_or_url
        if token_or_url.startswith(("https://", "http://"))
        else "https://liteapp.weixin.qq.com/q/7GiQu1"
        f"?qrcode={token_or_url}&bot_type=3"
    )
    image = qrcode.make(url)
    image.save(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
