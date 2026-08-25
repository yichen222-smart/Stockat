from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(os.environ.get("GRID_ASSISTANT_ROOT", "/opt/grid-assistant"))


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


@dataclass(frozen=True)
class Settings:
    root: Path
    database_path: Path
    timezone: str
    report_hour: int
    report_minute: int
    late_send_cutoff_hour: int
    late_send_cutoff_minute: int
    rsshub_url: str
    hermes_bin: Path
    hermes_home: Path
    invite_ttl_minutes: int
    log_level: str

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        root = Path(root or PROJECT_ROOT).resolve()
        raw = _load_yaml(root / "config" / "app.yaml")
        schedule = raw.get("schedule", {})
        return cls(
            root=root,
            database_path=Path(
                os.environ.get("GRID_ASSISTANT_DB", root / "data" / "app.db")
            ),
            timezone=str(raw.get("timezone", "Asia/Shanghai")),
            report_hour=int(schedule.get("report_hour", 8)),
            report_minute=int(schedule.get("report_minute", 30)),
            late_send_cutoff_hour=int(schedule.get("late_send_cutoff_hour", 9)),
            late_send_cutoff_minute=int(schedule.get("late_send_cutoff_minute", 15)),
            rsshub_url=str(
                os.environ.get("RSSHUB_URL", raw.get("rsshub_url", "http://127.0.0.1:1200"))
            ).rstrip("/"),
            hermes_bin=Path(
                os.environ.get("HERMES_BIN", root / "bin" / "hermes")
            ),
            hermes_home=Path(
                os.environ.get("HERMES_HOME", root / "data" / "hermes")
            ),
            invite_ttl_minutes=int(raw.get("invite_ttl_minutes", 15)),
            log_level=str(os.environ.get("LOG_LEVEL", raw.get("log_level", "INFO"))),
        )
