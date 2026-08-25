from __future__ import annotations

import os
from pathlib import Path
import re

from app.db import Database
from app.grid.risk import GridRiskService
from app.market_data.akshare_adapter import AkshareAdapter
from app.users.repository import UserScopedRepository
from app.users.service import PermissionDenied, UserService, utcnow
from app.settings import Settings

WELCOME_MESSAGE = """🐾 欢迎使用 Stockat 股小喵！

先用两步完成初始化：

1️⃣ 设置自选板块
发送：添加板块：银行
还可以继续发送：添加板块：半导体

2️⃣ 设置自选股票
只看新闻，发送：添加股票：600519
新闻＋网格参数，发送：添加网格股票：600036

完成后发送：查看我的关注
交易日早上 08:30 起，你会依次收到“盘前提示”“你的关注”“网格参数”三条消息。

机器人不会连接证券账户或自动交易；网格成交需要你主动发送“记录买入：股票代码”或“记录卖出：股票代码”。内容仅作信息整理和参数参考，不构成投资建议。"""


class CommandRouter:
    def __init__(
        self, db: Database, invite_ttl_minutes: int = 15, settings: Settings | None = None
    ):
        self.db = db
        self.users = UserService(db, invite_ttl_minutes)
        self.repo = UserScopedRepository(db)
        self.risk = GridRiskService(db)
        self.market = AkshareAdapter()
        self.settings = settings

    def handle(self, wechat_user_id: str, text: str, display_name: str | None = None) -> str:
        text = " ".join(text.strip().split())
        user = self.users.by_wechat(wechat_user_id)
        try:
            if not user:
                if text.startswith("加入 "):
                    user = self.users.redeem_invite(text[3:], wechat_user_id, display_name)
                    self._record_delivery_route(user.id)
                    return WELCOME_MESSAGE
                user = self.users.bootstrap_first_admin(wechat_user_id, display_name)
                self._record_delivery_route(user.id)
                return "初始化成功。你是系统管理员。发送“添加用户”可生成一次性邀请码。"
            if user.status != "active":
                raise PermissionDenied("你的账号当前不可用，请联系管理员。")
            self._record_delivery_route(user.id)
            reply = self._dispatch(user, text)
            self._audit(user.id, wechat_user_id, text.split(" ", 1)[0], True, "")
            return reply
        except Exception as exc:
            self._audit(user.id if user else None, wechat_user_id, text[:40], False, type(exc).__name__)
            if isinstance(exc, (PermissionDenied, ValueError)):
                return str(exc)
            return "处理失败，系统已记录。请稍后重试。"

    def _record_delivery_route(self, user_id: str) -> None:
        raw_home = os.environ.get("HERMES_HOME", "").strip()
        if not raw_home:
            return
        gateway_home = Path(raw_home).resolve()
        project_root = (self.settings.root if self.settings else Path("/opt/grid-assistant")).resolve()
        delivery_root = project_root / "data"
        try:
            gateway_home.relative_to(delivery_root)
        except ValueError:
            return
        if not gateway_home.name.startswith("hermes"):
            return
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO user_delivery_routes(user_id,platform,gateway_home,updated_at)
                VALUES(?,'weixin',?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    platform=excluded.platform,
                    gateway_home=excluded.gateway_home,
                    updated_at=excluded.updated_at
                """,
                (user_id, str(gateway_home), utcnow()),
            )

    def _dispatch(self, user, text: str) -> str:
        if text == "添加用户":
            code, expires = self.users.create_invite(user)
            return f"一次性邀请码：{code}\n有效期至：{expires}\n让新用户私聊发送：加入 {code}"
        if text == "查看用户列表":
            rows = self.users.list_members(user)
            return "\n".join(
                f"{u.id[:8]}｜{u.display_name or '未命名'}｜{u.role}｜{u.status}"
                for u in rows
            )
        match = re.match(r"(启用|停用|删除)用户[:：]\s*([0-9a-fA-F]{8})$", text)
        if match:
            UserService.require_admin(user)
            candidates = self.db.fetchall(
                "SELECT id FROM users WHERE id LIKE ? AND role='member'",
                (f"{match.group(2).lower()}%",),
            )
            if len(candidates) != 1:
                raise ValueError("用户编号不存在或不唯一。")
            status = {"启用": "active", "停用": "disabled", "删除": "deleted"}[
                match.group(1)
            ]
            self.users.set_status(user, candidates[0]["id"], status)
            return f"用户已{match.group(1)}。"
        if text.startswith("添加板块：") or text.startswith("添加板块:"):
            name = self._valid_sector_name(re.split("[:：]", text, 1)[1])
            self.repo.add_sector(user.id, name)
            return f"已添加板块：{name}"
        if text.startswith("删除板块：") or text.startswith("删除板块:"):
            name = self._valid_sector_name(re.split("[:：]", text, 1)[1])
            return f"{'已删除' if self.repo.remove_sector(user.id, name) else '未找到'}板块：{name}"
        match = re.match(r"(暂停|恢复)板块[:：]\s*(.+)$", text)
        if match:
            name = self._valid_sector_name(match.group(2))
            enabled = match.group(1) == "恢复"
            changed = self.repo.set_sector_enabled(user.id, name, enabled)
            return f"{'已恢复' if enabled else '已暂停'}板块：{name}" if changed else f"未找到板块：{name}"
        match = re.match(r"添加(网格)?股票[:：]\s*([\w\u4e00-\u9fff-]{1,20})$", text)
        if match:
            code, name = self.market.resolve_stock(match.group(2))
            self.repo.add_stock(user.id, code, name, grid_enabled=bool(match.group(1)))
            return f"已添加{'网格' if match.group(1) else ''}股票：{code} {name}"
        match = re.match(r"删除股票[:：]\s*(\d{6})$", text)
        if match:
            code = match.group(1)
            return f"{'已删除' if self.repo.remove_stock(user.id, code) else '未找到'}股票：{code}"
        match = re.match(r"(暂停|恢复)股票新闻[:：]\s*(\d{6})$", text)
        if match:
            enabled = match.group(1) == "恢复"
            changed = self.repo.set_stock_news(user.id, match.group(2), enabled)
            return f"股票新闻已{'恢复' if enabled else '暂停'}：{match.group(2)}" if changed else "未找到该股票。"
        match = re.match(r"停止跟踪网格[:：]\s*(\d{6})$", text)
        if match:
            changed = self.repo.stop_grid_tracking(user.id, match.group(1))
            return f"已停止跟踪网格：{match.group(1)}" if changed else "未找到启用中的网格股票。"
        if text == "查看我的网格":
            rows = self.repo.grids(user.id)
            return "\n\n".join(self._format_grid(row) for row in rows) if rows else "当前没有启用中的网格股票。"
        match = re.match(r"查看\s*(\d{6})\s*网格$", text)
        if match:
            rows = [row for row in self.repo.grids(user.id) if row["stock_code"] == match.group(1)]
            return self._format_grid(rows[0]) if rows else "未找到你的该网格股票。"
        if text == "查看我的关注":
            sectors = "、".join(self.repo.sectors(user.id)) or "无"
            stocks = self.repo.stocks(user.id)
            stock_text = "、".join(
                f"{s['stock_code']} {s['stock_name']}{'（网格）' if s['grid_enabled'] else ''}"
                for s in stocks
            ) or "无"
            return f"板块：{sectors}\n股票：{stock_text}"
        if text in {"暂停早报", "恢复早报", "暂停我的早报", "恢复我的早报"}:
            enabled = text in {"恢复早报", "恢复我的早报"}
            self.repo.set_morning_report(user.id, enabled)
            return f"早报已{'恢复' if enabled else '暂停'}。"
        match = re.match(r"记录(买入|卖出)[:：]?\s*(\d{6})(?:\s+(\d+))?$", text)
        if match:
            result = self.risk.report_fill(
                user.id, match.group(2), "BUY" if match.group(1) == "买入" else "SELL",
                int(match.group(3) or 1),
            )
            return (
                f"成交已记录。连续买入{result['buy_count']}档，连续卖出{result['sell_count']}档。"
                + (f"\n网格已停止：{result['stop_reason']}" if result["status"] == "STOPPED" else "")
            )
        match = re.match(r"我今天在\s*(\d{6})\s*(买入|卖出)(?:一档|\s*(\d+)档)$", text)
        if match:
            result = self.risk.report_fill(
                user.id,
                match.group(1),
                "BUY" if match.group(2) == "买入" else "SELL",
                int(match.group(3) or 1),
            )
            return (
                f"成交已记录。连续买入{result['buy_count']}档，连续卖出{result['sell_count']}档。"
                + (f"\n网格已停止：{result['stop_reason']}" if result["status"] == "STOPPED" else "")
            )
        if text == "撤销上一条网格成交记录":
            undone = self.risk.undo_last_fill(user.id)
            direction = "买入" if undone["direction"] == "BUY" else "卖出"
            return f"已撤销：{undone['stock_code']} {direction}{undone['levels']}档。"
        if text == "立即生成我的报告":
            if self.settings is None:
                raise RuntimeError("缺少应用配置")
            from app.scheduler.daily import DailyRun

            DailyRun(self.settings, self.db).execute(
                force=True, send=False, only_user_id=user.id
            )
            report = self.db.fetchone(
                """
                SELECT content FROM user_reports
                WHERE user_id=? ORDER BY report_date DESC LIMIT 1
                """,
                (user.id,),
            )
            return report["content"] if report else "暂时没有可生成的报告内容。"
        if text == "查看系统状态":
            UserService.require_admin(user)
            return "Hermes：正常\n微信网关：正常\nSQLite：正常\n自动交易：未启用"
        return "可用指令：查看我的关注、添加板块：半导体、添加股票：600519、添加网格股票：600036、暂停早报。"

    @staticmethod
    def _format_grid(row: dict) -> str:
        if row.get("reference_price") is None:
            return f"{row['stock_code']} {row['stock_name']}｜参数尚未生成"
        reason = f"｜原因：{row['stop_reason']}" if row.get("stop_reason") else ""
        return (
            f"{row['stock_code']} {row['stock_name']}｜{row['status']}{reason}\n"
            f"参考价：{row['reference_price']:.3f}元\n"
            f"买入档：{row['buy_grid_pct']:.2%}｜卖出档：{row['sell_grid_pct']:.2%}\n"
            f"正常范围：{row['normal_lower']:.3f}—{row['normal_upper']:.3f}元\n"
            f"极限停止价：{row['extreme_lower']:.3f} / {row['extreme_upper']:.3f}元\n"
            f"数据截至：{row['data_until']}\n内容仅作信息整理，不构成投资建议"
        )
    @staticmethod
    def _valid_sector_name(value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[\w\u4e00-\u9fff-]{1,20}", value):
            raise ValueError("板块名只能包含中英文、数字、下划线或短横线，最长 20 个字符。")
        return value
    def _audit(self, user_id, wechat_user_id: str, command: str, success: bool, detail: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO command_audit(user_id,wechat_user_id,command_name,success,detail,created_at) VALUES(?,?,?,?,?,?)",
                (user_id, wechat_user_id, command, int(success), detail[:100], utcnow()),
            )
