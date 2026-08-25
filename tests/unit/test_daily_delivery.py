from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.db import Database
from app.messaging.hermes_weixin import summarize_delivery_error
from app.market_data.akshare_adapter import MarketDataError
from app.scheduler import daily as daily_module
from app.scheduler.daily import (
    DailyRun,
    NON_TRADING_DAY_REMINDER,
    WEIXIN_REPLY_REMINDER,
)
from app.settings import Settings
from app.users.service import UserService, utcnow


def make_settings(tmp_path: Path) -> Settings:
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    return Settings(
        root=root,
        database_path=root / "data" / "app.db",
        timezone="Asia/Shanghai",
        report_hour=8,
        report_minute=30,
        late_send_cutoff_hour=9,
        late_send_cutoff_minute=15,
        rsshub_url="http://127.0.0.1:1200",
        hermes_bin=root / "bin" / "hermes",
        hermes_home=root / "data" / "hermes",
        invite_ttl_minutes=15,
        log_level="INFO",
    )


def test_missing_delivery_route_fails_closed(tmp_path):
    settings = make_settings(tmp_path)
    run = DailyRun(settings, Database(settings.database_path))
    with pytest.raises(RuntimeError, match="缺少独立微信发送路由"):
        run._require_gateway_home({"gateway_home": None})


def test_delivery_error_summary_preserves_cause_and_redacts_sensitive_values():
    summary = summarize_delivery_error(
        RuntimeError(
            "ret=-2 errmsg=prepare failed token=secret-value "
            "sk-1234567890 to=private-peer"
        )
    )
    assert "ret=-2" in summary
    assert "prepare failed" in summary
    assert "secret-value" not in summary
    assert "sk-1234567890" not in summary
    assert "private-peer" not in summary


def test_calendar_failure_without_cache_fails_closed(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    run = DailyRun(settings, Database(settings.database_path))

    def unavailable():
        raise MarketDataError("offline")

    monkeypatch.setattr(run.market, "trading_calendar", unavailable)
    is_open, source = run._is_trading_day(__import__("datetime").date(2026, 7, 31))
    assert is_open is False
    assert source == "unavailable"


def test_report_parts_resume_without_resending_sent_part(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    db = Database(settings.database_path)
    db.migrate()
    user = UserService(db).bootstrap_first_admin("wx-admin", "管理员")
    gateway_home = settings.root / "data" / "hermes"
    gateway_home.mkdir()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO user_delivery_routes(user_id,gateway_home,updated_at) VALUES(?,?,?)",
            (user.id, str(gateway_home), utcnow()),
        )

    content = (
        "【第1条/共3条｜盘前提示】\n第一段\n"
        "【第2条/共3条｜你的关注】\n第二段\n"
        "【第3条/共3条｜网格参数】\n第三段"
    )
    run = DailyRun(settings, db)
    run._store_generated_report(user.id, user.wechat_user_id, "2026-07-31", content)
    calls: list[str] = []

    def fail_second(chat_id, message, gateway_home=None):
        del chat_id, gateway_home
        calls.append(message)
        if "第2条" in message:
            raise RuntimeError("temporary")
        return {"success": True}

    monkeypatch.setattr(daily_module, "send_text", fail_second)
    monkeypatch.setattr(daily_module.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="第2段"):
        run._send_report_parts(
            user.id, "2026-07-31", user.wechat_user_id, content, str(gateway_home)
        )
    assert sum("第1条" in item for item in calls) == 1
    assert sum("第2条" in item for item in calls) == 3
    assert sum("第3条" in item for item in calls) == 0

    resumed: list[str] = []

    def succeed(chat_id, message, gateway_home=None):
        del chat_id, gateway_home
        resumed.append(message)
        return {"success": True}

    monkeypatch.setattr(daily_module, "send_text", succeed)
    run._send_report_parts(
        user.id, "2026-07-31", user.wechat_user_id, content, str(gateway_home)
    )
    assert sum("第1条" in item for item in resumed) == 0
    assert sum("第2条" in item for item in resumed) == 1
    assert sum("第3条" in item for item in resumed) == 1
    states = db.fetchall(
        "SELECT status FROM user_report_parts WHERE user_id=? ORDER BY part_index",
        (user.id,),
    )
    assert resumed.count(WEIXIN_REPLY_REMINDER) == 1
    assert [row["status"] for row in states] == ["sent", "sent", "sent", "sent"]


def test_delivery_retry_uses_weixin_cooldown():
    delay = DailyRun._delivery_retry_delay(
        RuntimeError("sendmessage rate limited; cooldown active for 30.0s"), 0
    )
    assert delay == 32.0


def test_report_parts_wait_between_successful_weixin_messages(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    db = Database(settings.database_path)
    db.migrate()
    user = UserService(db).bootstrap_first_admin("wx-admin", "管理员")
    gateway_home = settings.root / "data" / "hermes"
    gateway_home.mkdir()
    content = (
        "【第1条/共3条｜盘前提示】\n第一段\n"
        "【第2条/共3条｜你的关注】\n第二段\n"
        "【第3条/共3条｜网格参数】\n第三段"
    )
    run = DailyRun(settings, db)
    run._store_generated_report(user.id, user.wechat_user_id, "2026-08-10", content)
    sleeps: list[float] = []
    monkeypatch.setattr(daily_module, "send_text", lambda *_args, **_kwargs: {"success": True})
    monkeypatch.setattr(daily_module.time, "sleep", sleeps.append)

    run._send_report_parts(
        user.id, "2026-08-10", user.wechat_user_id, content, str(gateway_home)
    )

    assert sleeps == [32.0, 32.0, 32.0]


def test_reply_reminder_is_earnest_and_explicit():
    assert WEIXIN_REPLY_REMINDER.startswith("报告老大，小的已播报完毕！")
    assert "请回复“收到”" in WEIXIN_REPLY_REMINDER
    assert "明天就无法继续给老大发送盘前消息" in WEIXIN_REPLY_REMINDER
    assert "(｡•́︿•̀｡)" in WEIXIN_REPLY_REMINDER


def test_interaction_required_delivery_is_not_retried(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    db = Database(settings.database_path)
    db.migrate()
    user = UserService(db).bootstrap_first_admin("wx-admin", "管理员")
    gateway_home = settings.root / "data" / "hermes"
    gateway_home.mkdir()
    run = DailyRun(settings, db)
    content = "单段测试报告"
    run._store_generated_report(user.id, user.wechat_user_id, "2026-08-14", content)
    calls = 0

    def interaction_required(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("recipient interaction required: prepare failed")

    monkeypatch.setattr(daily_module, "send_text", interaction_required)
    monkeypatch.setattr(
        daily_module.time,
        "sleep",
        lambda _seconds: pytest.fail("non-retryable error must not sleep"),
    )

    with pytest.raises(RuntimeError, match="收件人"):
        run._send_report_parts(
            user.id,
            "2026-08-14",
            user.wechat_user_id,
            content,
            str(gateway_home),
        )
    assert calls == 1


def test_non_trading_day_sends_one_fixed_reminder_per_user(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    db = Database(settings.database_path)
    db.migrate()
    stamp = utcnow()
    with db.transaction() as conn:
        for index, role in enumerate(("admin", "member", "member"), 1):
            user_id = f"user-{index}"
            gateway_home = settings.root / "data" / (
                "hermes" if index == 1 else f"hermes-member{index}"
            )
            gateway_home.mkdir()
            conn.execute(
                """
                INSERT INTO users(
                    id,wechat_user_id,display_name,role,status,
                    morning_report_enabled,created_at,updated_at
                ) VALUES(?,?,?,?, 'active',1,?,?)
                """,
                (user_id, f"wx-{index}", f"用户{index}", role, stamp, stamp),
            )
            conn.execute(
                "INSERT INTO user_delivery_routes(user_id,gateway_home,updated_at) VALUES(?,?,?)",
                (user_id, str(gateway_home), stamp),
            )

    run = DailyRun(settings, db)
    run._store_generated_report(
        "user-1", "wx-1", "2026-08-23", "不应在非交易日发送的强制预览"
    )
    monkeypatch.setattr(run, "_is_trading_day", lambda _target: (False, "cache"))
    monkeypatch.setattr(run, "_wait_until_report_time", lambda: None)
    monkeypatch.setattr(
        run,
        "_collect_common_material",
        lambda *_args: pytest.fail("non-trading day must not collect news"),
    )
    sent: list[str] = []
    monkeypatch.setattr(
        daily_module,
        "send_text",
        lambda _chat_id, message, gateway_home=None: sent.append(message)
        or {"success": True},
    )

    result = run.execute(
        scheduled=True,
        as_of=datetime(2026, 8, 23, 8, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result == {
        "status": "ok",
        "report_type": "non_trading_day_reminder",
        "users": 3,
        "sent": 3,
        "failed": 0,
        "calendar": "cache",
    }
    assert sent == [NON_TRADING_DAY_REMINDER] * 3
    parts = db.fetchall(
        "SELECT part_count,status FROM user_report_parts ORDER BY user_id,part_index"
    )
    assert [(row["part_count"], row["status"]) for row in parts] == [
        (1, "sent"),
        (1, "sent"),
        (1, "sent"),
    ]

    rerun = run.execute(
        scheduled=True,
        as_of=datetime(2026, 8, 23, 8, 40, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert rerun["users"] == 0
    assert sent == [NON_TRADING_DAY_REMINDER] * 3


def test_systemd_timer_has_bounded_same_day_retries():
    timer = Path(__file__).parents[2] / "infra" / "systemd" / "grid-assistant.timer"
    text = timer.read_text(encoding="utf-8")
    assert "08:05:00" in text
    assert "08:40:00" in text
    assert "09:00:00" in text
    assert "Mon..Fri" not in text
    assert "Persistent=true" in text


def test_daily_service_does_not_require_every_gateway():
    service = Path(__file__).parents[2] / "infra" / "systemd" / "grid-assistant.service"
    text = service.read_text(encoding="utf-8")
    assert "Requires=hermes-gateway" not in text


def test_monday_news_window_starts_friday_1500(tmp_path):
    settings = make_settings(tmp_path)
    cache = settings.root / "data" / "market_cache" / "trading_calendar.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(["2026-08-06", "2026-08-07", "2026-08-10"]),
        encoding="utf-8",
    )
    run = DailyRun(settings, Database(settings.database_path))
    start, end = run._news_window(
        datetime(2026, 8, 10).date(),
        datetime(2026, 8, 10, 8, 20, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert start.astimezone(ZoneInfo("Asia/Shanghai")).isoformat() == "2026-08-07T15:00:00+08:00"
    assert end.astimezone(ZoneInfo("Asia/Shanghai")).isoformat() == "2026-08-10T08:20:00+08:00"
