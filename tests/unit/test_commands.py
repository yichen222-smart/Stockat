from app.commands.router import CommandRouter
from app.db import Database
from app.users.service import UserService


def test_extended_commands_and_fill_isolation(tmp_path):
    db = Database(tmp_path / "app.db")
    db.migrate()
    service = UserService(db)
    admin = service.bootstrap_first_admin("wx-admin", "管理员")
    code, _ = service.create_invite(admin)
    service.redeem_invite(code, "wx-member", "成员")

    router = CommandRouter(db)
    router.market.resolve_stock = lambda query: ("600036", "招商银行")
    assert "已添加网格股票" in router.handle("wx-admin", "添加网格股票：招商银行")
    assert "已添加网格股票" in router.handle("wx-member", "添加网格股票：600036")
    assert "成交已记录" in router.handle("wx-admin", "我今天在 600036 买入一档")
    assert "成交已记录" in router.handle("wx-member", "我今天在 600036 卖出一档")

    admin_state = db.fetchone(
        "SELECT consecutive_buy_count,consecutive_sell_count FROM user_grid_state WHERE user_id=?",
        (admin.id,),
    )
    member = service.by_wechat("wx-member")
    member_state = db.fetchone(
        "SELECT consecutive_buy_count,consecutive_sell_count FROM user_grid_state WHERE user_id=?",
        (member.id,),
    )
    assert (admin_state["consecutive_buy_count"], admin_state["consecutive_sell_count"]) == (1, 0)
    assert (member_state["consecutive_buy_count"], member_state["consecutive_sell_count"]) == (0, 1)
    assert "已撤销" in router.handle("wx-member", "撤销上一条网格成交记录")
    assert "已暂停" in router.handle("wx-member", "暂停股票新闻：600036")


def test_new_member_receives_initialization_welcome(tmp_path):
    db = Database(tmp_path / "app.db")
    db.migrate()
    service = UserService(db)
    admin = service.bootstrap_first_admin("wx-admin", "管理员")
    code, _ = service.create_invite(admin)
    reply = CommandRouter(db).handle("wx-new", f"加入 {code}", "新用户")
    assert "欢迎使用 Stockat 股小喵" in reply
    assert "添加板块：银行" in reply
    assert "添加股票：600519" in reply
    assert "添加网格股票：600036" in reply
    assert "记录买入：股票代码" in reply
