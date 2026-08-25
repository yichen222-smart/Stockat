from __future__ import annotations

import json
import os
import re
import subprocess


class DeliveryError(RuntimeError):
    pass


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|token|secret|authorization)(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(to|chat_id|user_id)=([^\s,;]+)"),
)


def summarize_delivery_error(exc: BaseException, limit: int = 300) -> str:
    """Keep an actionable single-line cause while removing credentials and IDs."""
    summary = " ".join(str(exc).split()) or type(exc).__name__
    summary = _SECRET_PATTERNS[0].sub(r"\1\2[REDACTED]", summary)
    summary = _SECRET_PATTERNS[1].sub("[REDACTED]", summary)
    summary = _SECRET_PATTERNS[2].sub(r"\1=[REDACTED]", summary)
    return f"{type(exc).__name__}: {summary}"[:limit]


def send_text(
    chat_id: str, message: str, gateway_home: str | None = None
) -> dict:
    if not chat_id or not message.strip():
        raise ValueError("发送目标和消息不能为空")
    root = os.environ.get("GRID_ASSISTANT_ROOT", "/opt/grid-assistant")
    python = os.environ.get("HERMES_PYTHON", f"{root}/runtime/hermes-venv/bin/python")
    helper = f"{root}/scripts/send_weixin.py"
    resolved_home = gateway_home or os.environ.get("HERMES_HOME", f"{root}/data/hermes")
    process = subprocess.run(
        [python, helper, chat_id],
        input=message,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
        env={**os.environ, "HERMES_HOME": resolved_home},
    )
    if process.returncode:
        raise DeliveryError((process.stderr or process.stdout or "微信发送失败")[:300])
    result = json.loads(process.stdout)
    if not result.get("success"):
        raise DeliveryError(str(result.get("error", "微信发送失败")))
    return result
