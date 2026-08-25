from datetime import UTC, datetime, timedelta

from app.db import Database
from app.users.repository import UserScopedRepository
from app.users.service import PermissionDenied, UserService


def test_invite_single_use_and_isolation(tmp_path):
    db = Database(tmp_path / "test.db")
    db.migrate()
    service = UserService(db)
    admin = service.bootstrap_first_admin("wx-admin", "管理员")
    code, _ = service.create_invite(admin)
    member = service.redeem_invite(code, "wx-member", "成员")
    repo = UserScopedRepository(db)
    repo.add_sector(admin.id, "半导体")
    repo.add_sector(member.id, "医药")
    assert repo.sectors(admin.id) == ["半导体"]
    assert repo.sectors(member.id) == ["医药"]
    try:
        service.redeem_invite(code, "wx-other", "其他")
    except PermissionDenied:
        pass
    else:
        raise AssertionError("邀请码不得二次使用")
    service.set_status(admin, member.id, "deleted")
    assert service.by_wechat("wx-member") is None
    assert repo.sectors(admin.id) == ["半导体"]


def test_invite_default_expiry_is_fifteen_minutes(tmp_path):
    db = Database(tmp_path / "expiry.db")
    db.migrate()
    service = UserService(db)
    admin = service.bootstrap_first_admin("wx-admin", "管理员")
    _, expires_at = service.create_invite(admin)
    remaining = datetime.fromisoformat(expires_at) - datetime.now(UTC)
    assert timedelta(minutes=14, seconds=50) < remaining <= timedelta(minutes=15)


def test_expired_invite_is_rejected(tmp_path):
    db = Database(tmp_path / "expired.db")
    db.migrate()
    service = UserService(db, invite_ttl_minutes=-1)
    admin = service.bootstrap_first_admin("wx-admin", "管理员")
    code, _ = service.create_invite(admin)
    try:
        service.redeem_invite(code, "wx-member", "成员")
    except PermissionDenied:
        pass
    else:
        raise AssertionError("过期邀请码不得使用")
