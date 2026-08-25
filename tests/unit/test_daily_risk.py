from app.db import Database
from app.scheduler.daily import DailyRun
from app.settings import Settings
from app.users.repository import UserScopedRepository
from app.users.service import UserService


def test_market_risk_stops_all_watchers_without_auto_reactivation(tmp_path):
    db = Database(tmp_path / "app.db")
    db.migrate()
    user = UserService(db).bootstrap_first_admin("wechat-admin", None)
    UserScopedRepository(db).add_stock(
        user.id, "600036", "招商银行", grid_enabled=True
    )
    run = DailyRun(Settings.load(tmp_path), db)

    run._apply_market_risk("600036", "连续两日价格越过正常范围")
    stopped = db.fetchone(
        "SELECT status,stop_reason FROM user_grid_state WHERE user_id=? AND stock_code=?",
        (user.id, "600036"),
    )
    assert stopped["status"] == "STOPPED"

    run._apply_market_risk("600036", None)
    still_stopped = db.fetchone(
        "SELECT status FROM user_grid_state WHERE user_id=? AND stock_code=?",
        (user.id, "600036"),
    )
    assert still_stopped["status"] == "STOPPED"
