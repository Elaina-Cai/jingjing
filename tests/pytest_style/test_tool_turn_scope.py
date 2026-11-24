import asyncio
from types import SimpleNamespace

from loop import ports


def test_missing_scope_never_bootstraps_tools(monkeypatch):
    import core.bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, "init_singletons", lambda **kw: None)
    monkeypatch.setattr(bootstrap, "get_tools", lambda: (_ for _ in ()).throw(AssertionError("无范围不能调用工具")))
    result = asyncio.run(ports.act("search_messages", {"_chat": "wxid_private"}))
    assert not result.ok and "scope" in result.error


def test_scope_comes_from_turn_not_model_args(monkeypatch):
    import core.terminal.authority as authority
    monkeypatch.setattr(authority, "get_authority_service", lambda **kw: None)
    import core.bootstrap as bootstrap
    seen = []

    class Tool:
        def set_context(self, ctx):
            self.ctx = ctx

        async def execute(self, **kwargs):
            seen.append(dict(self.ctx.extra))
            return SimpleNamespace(output="ok", error="")

    monkeypatch.setattr(bootstrap, "init_singletons", lambda **kw: None)
    monkeypatch.setattr(bootstrap, "get_tools", lambda: {"search_messages": Tool()})

    @ports.scoped_turn
    async def turn(db, *, platform, chat_id, user_message=""):
        return await ports.act("search_messages", {"_chat": "wxid_private", "platform": "wechat", "account_id": "evil"})

    result = asyncio.run(turn("data/platforms/qq/bot/account.db", platform="qq", chat_id="group:1"))
    assert result.ok
    assert seen[0]["platform"] == "qq"
    assert seen[0]["account_id"] == "bot"
    assert seen[0]["chat_id"] == "group:1"
    assert seen[0]["is_group"] is True
    assert not asyncio.run(ports.act("search_messages", {})).ok


def test_turn_scope_is_task_local_and_resets_on_error():
    @ports.scoped_turn
    async def turn(db, *, platform, chat_id):
        await asyncio.sleep(0)
        assert ports.current_read_scope().chat_id == chat_id
        if chat_id == "wxid_bad":
            raise ValueError("test")

    async def run():
        results = await asyncio.gather(
            turn("data/platforms/wechat/bot/account.db", platform="wechat", chat_id="wxid_good"),
            turn("data/platforms/wechat/bot/account.db", platform="wechat", chat_id="wxid_bad"), return_exceptions=True)
        assert isinstance(results[1], ValueError)
        assert ports.current_read_scope() is None

    asyncio.run(run())


def test_look_cannot_read_other_window(monkeypatch):
    monkeypatch.setattr(ports, "resolve_chat", lambda db, name: name)

    async def forbidden_read(*args, **kwargs):
        raise AssertionError("拒绝跨会话应在读取正文之前")

    monkeypatch.setattr(ports, "recent_in_chat", forbidden_read)

    @ports.scoped_turn
    async def turn(db, *, platform, chat_id):
        return await ports.look("win:wxid_private", db=db)

    result = asyncio.run(turn("data/platforms/wechat/bot/account.db", platform="wechat", chat_id="room@chatroom"))
    assert not result.ok and "read_scope_denied" in result.error


def test_look_by_id_checks_conversation_before_render(monkeypatch):
    class Connection:
        def execute(self, *args):
            return SimpleNamespace(fetchall=lambda: [{"chat_id": "wxid_private"}])

        def close(self):
            pass

    monkeypatch.setattr(ports, "_connect", lambda db: Connection())
    monkeypatch.setattr(ports, "_rows", lambda *a: (_ for _ in ()).throw(AssertionError("不能渲染未授权消息")))

    @ports.scoped_turn
    async def turn(db, *, platform, chat_id):
        return await ports.look("msg:123", db=db)

    result = asyncio.run(turn("data/platforms/wechat/bot/account.db", platform="wechat", chat_id="room@chatroom"))
    assert not result.ok and "read_scope_denied" in result.error


def test_current_window_still_can_be_read(monkeypatch):
    monkeypatch.setattr(ports, "resolve_chat", lambda db, name: name)
    monkeypatch.setattr(ports, "chat_label", lambda db, chat: "当前群")

    async def read(*args, **kwargs):
        return [ports.Row(1, "room@chatroom", "group", "A", "2026-11-06", "本群原文")]

    monkeypatch.setattr(ports, "recent_in_chat", read)

    @ports.scoped_turn
    async def turn(db, *, platform, chat_id):
        return await ports.look("win:room@chatroom", db=db)

    result = asyncio.run(turn("data/platforms/wechat/bot/account.db", platform="wechat", chat_id="room@chatroom"))
    assert result.ok and "本群原文" in result.text


def test_tool_instance_context_is_task_local():
    from agents.tools.base import BaseTool

    class Tool(BaseTool):
        async def execute(self, **kwargs):
            before = self.ctx
            await asyncio.sleep(0)
            assert self.ctx is before

    tool = Tool()

    async def call():
        tool.set_context(object())
        await tool.execute()

    async def run():
        await asyncio.gather(call(), call())

    asyncio.run(run())
