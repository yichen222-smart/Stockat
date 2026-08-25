from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.db import Database


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _hash_invite(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class User:
    id: str
    wechat_user_id: str
    display_name: str | None
    role: str
    status: str
    morning_report_enabled: bool


class PermissionDenied(RuntimeError):
    pass


class UserService:
    def __init__(self, db: Database, invite_ttl_minutes: int = 15):
        self.db = db
        self.invite_ttl_minutes = invite_ttl_minutes

    def by_wechat(self, wechat_user_id: str) -> User | None:
        row = self.db.fetchone(
            "SELECT * FROM users WHERE wechat_user_id=? AND status!='deleted'",
            (wechat_user_id,),
        )
        return self._from_row(row) if row else None

    def bootstrap_first_admin(self, wechat_user_id: str, display_name: str | None) -> User:
        now = utcnow()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM users WHERE wechat_user_id=?", (wechat_user_id,)
            ).fetchone()
            if existing:
                return self._from_row(existing)
            count = conn.execute(
                "SELECT COUNT(*) FROM users WHERE status!='deleted'"
            ).fetchone()[0]
            if count:
                raise PermissionDenied("系统已初始化，请使用管理员邀请码加入。")
            user_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO users(
                    id,wechat_user_id,display_name,role,status,created_at,updated_at,last_active_at
                ) VALUES(?,?,?,'admin','active',?,?,?)
                """,
                (user_id, wechat_user_id, display_name, now, now, now),
            )
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return self._from_row(row)

    def create_invite(self, actor: User) -> tuple[str, str]:
        self.require_admin(actor)
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "-".join(
            "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2)
        )
        expires = datetime.now(UTC) + timedelta(minutes=self.invite_ttl_minutes)
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO invite_codes(code_hash,created_by,expires_at,status)
                VALUES(?,?,?,'active')
                """,
                (_hash_invite(code), actor.id, expires.isoformat()),
            )
        return code, expires.isoformat()

    def redeem_invite(
        self, code: str, wechat_user_id: str, display_name: str | None
    ) -> User:
        now = datetime.now(UTC)
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM users WHERE wechat_user_id=? AND status!='deleted'",
                (wechat_user_id,),
            ).fetchone()
            if existing:
                return self._from_row(existing)
            invite = conn.execute(
                "SELECT * FROM invite_codes WHERE code_hash=?", (_hash_invite(code),)
            ).fetchone()
            if not invite or invite["status"] != "active":
                raise PermissionDenied("邀请码无效或已使用。")
            if datetime.fromisoformat(invite["expires_at"]) <= now:
                conn.execute(
                    "UPDATE invite_codes SET status='expired' WHERE code_hash=?",
                    (invite["code_hash"],),
                )
                raise PermissionDenied("邀请码已过期。")
            user_id = str(uuid.uuid4())
            stamp = now.isoformat()
            conn.execute(
                """
                INSERT INTO users(
                    id,wechat_user_id,display_name,role,status,created_at,updated_at,last_active_at
                ) VALUES(?,?,?,'member','active',?,?,?)
                """,
                (user_id, wechat_user_id, display_name, stamp, stamp, stamp),
            )
            conn.execute(
                """
                UPDATE invite_codes
                SET status='used',used_at=?,used_by=?
                WHERE code_hash=? AND status='active'
                """,
                (stamp, user_id, invite["code_hash"]),
            )
            return self._from_row(
                conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            )

    def list_members(self, actor: User) -> list[User]:
        self.require_admin(actor)
        return [
            self._from_row(row)
            for row in self.db.fetchall(
                "SELECT * FROM users WHERE status!='deleted' ORDER BY created_at"
            )
        ]

    def set_status(self, actor: User, target_id: str, status: str) -> None:
        self.require_admin(actor)
        if status not in {"active", "disabled", "deleted"}:
            raise ValueError("不支持的用户状态")
        if target_id == actor.id and status != "active":
            raise PermissionDenied("管理员不能停用自己。")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET status=?,updated_at=? WHERE id=? AND role='member'",
                (status, utcnow(), target_id),
            )

    @staticmethod
    def require_admin(actor: User) -> None:
        if actor.role != "admin" or actor.status != "active":
            raise PermissionDenied("此操作仅管理员可用。")

    @staticmethod
    def _from_row(row) -> User:
        return User(
            id=row["id"],
            wechat_user_id=row["wechat_user_id"],
            display_name=row["display_name"],
            role=row["role"],
            status=row["status"],
            morning_report_enabled=bool(row["morning_report_enabled"]),
        )
