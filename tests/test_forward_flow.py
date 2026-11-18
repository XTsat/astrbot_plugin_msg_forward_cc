"""forward_message 的端到端接线：原生转发与重建链路的切换。"""

from __future__ import annotations

import asyncio

from conftest import FakeBot, FakeCtx, FakeEvent, make_plugin, make_rule
from astrbot.core.message.components import Plain


async def _noop():
    return None


def native_calls(bot: FakeBot) -> list[str]:
    return [a for a in bot.actions() if a.startswith("forward_")]


class TestNativeForwardFastPath:
    async def test_enabled_by_default_uses_native(self, forward_chain):
        bot = FakeBot()
        p = make_plugin({"rules": [make_rule()]})
        await p.forward_message(FakeEvent(forward_chain, bot))

        assert native_calls(bot) == ["forward_group_single_msg"]
        # 只做「有没有内嵌卡片」的探测（两种参数名各一次），不触发完整重建
        assert bot.actions().count("get_forward_msg") <= 2
        assert "send_group_forward_msg" not in bot.actions()
        assert p.context.sent == []

    async def test_disabled_falls_back_to_rebuild(self, forward_chain):
        bot = FakeBot(fail=True)
        p = make_plugin(
            {"rules": [make_rule()], "forward": {"forward_native_relay": False}}
        )
        await p.forward_message(FakeEvent(forward_chain, bot))

        assert native_calls(bot) == []
        assert "get_forward_msg" in bot.actions()
        assert len(p.context.sent) == 1

    async def test_native_failure_falls_back_to_rebuild(self, forward_chain):
        bot = FakeBot(fail=True)
        p = make_plugin({"rules": [make_rule()]})
        await p.forward_message(FakeEvent(forward_chain, bot))

        assert native_calls(bot) == ["forward_group_single_msg"]
        assert len(p.context.sent) == 1

    async def test_mixed_message_never_uses_native(self, mixed_chain):
        bot = FakeBot()
        p = make_plugin({"rules": [make_rule()]})
        await p.forward_message(FakeEvent(mixed_chain, bot))

        assert native_calls(bot) == []

    async def test_queue_mode_never_uses_native(self, forward_chain):
        bot = FakeBot()
        p = make_plugin(
            {
                "rules": [make_rule(queue_interval_seconds=5)],
                "queue": {"queue_enabled": True},
            }
        )
        p._send_queue = asyncio.Queue()
        p._enqueue_send = lambda *a, **k: p._send_queue.put_nowait((a, k))
        await p.forward_message(FakeEvent(forward_chain, bot))

        assert native_calls(bot) == []
        assert p._send_queue.qsize() == 1


class TestHeaderWithNativeForward:
    async def test_header_sent_before_native(self, forward_chain):
        bot = FakeBot()
        p = make_plugin({"rules": [make_rule(hide_header=False)]})
        await p.forward_message(FakeEvent(forward_chain, bot))

        assert len(p.context.sent) == 1
        assert native_calls(bot) == ["forward_group_single_msg"]

    async def test_header_failure_falls_back_to_rebuild(self, forward_chain):
        """头没发出去就不能走原生转发，否则来源信息被静默丢掉。"""
        bot = FakeBot()
        ctx = FakeCtx(fail_first=1)
        p = make_plugin({"rules": [make_rule(hide_header=False)]}, ctx=ctx)
        await p.forward_message(FakeEvent(forward_chain, bot))

        assert native_calls(bot) == []
        assert len(ctx.sent) == 1


class TestNativeForwardRespectsContentTypes:
    """原生转发不能绕过内容类型筛选。

    聊天记录归入「其他」类型；规则没勾「其他」时，原生转发那条路会把整条记录
    发出去，而重建链路会按类型筛掉。两条路必须给出一致的结论。
    """

    async def test_plain_only_does_not_native_relay(self, forward_chain):
        bot = FakeBot()
        p = make_plugin({"rules": [make_rule(content_types=["plain"])]})
        await p.forward_message(FakeEvent(forward_chain, bot))

        assert native_calls(bot) == []

    async def test_other_selected_does_native_relay(self, forward_chain):
        bot = FakeBot()
        p = make_plugin({"rules": [make_rule(content_types=["other"])]})
        await p.forward_message(FakeEvent(forward_chain, bot))

        assert native_calls(bot) == ["forward_group_single_msg"]


class TestQueueNativeForward:
    """A2：队列模式也先试原生转发，失败再回退重建。

    队列延迟期间记录可能已被撤回、message_id 可能已失效，所以失败必须能安静
    回退到重建链，不能让这条队列消息直接丢掉。
    """

    @staticmethod
    def _queue_plugin(fail_bot: bool = False):
        p = make_plugin(
            {
                "rules": [make_rule(queue_interval_seconds=5)],
                "queue": {"queue_enabled": True},
            }
        )
        p._send_queue = asyncio.Queue()
        p._enqueued = []

        def _fake_enqueue(*a, **k):
            p._enqueued.append({"args": a, "kwargs": k})

        p._enqueue_send = _fake_enqueue
        return p

    async def test_enqueue_records_native_info(self, forward_chain):
        p = self._queue_plugin()
        await p.forward_message(FakeEvent(forward_chain, FakeBot()))

        assert len(p._enqueued) == 1
        info = p._enqueued[0]["kwargs"].get("native_forward")
        assert info == {"platform_id": "qq-main", "message_id": "123456"}

    async def test_enqueue_skips_native_info_without_other_type(self, forward_chain):
        p = make_plugin(
            {
                "rules": [
                    make_rule(queue_interval_seconds=5, content_types=["plain"])
                ],
                "queue": {"queue_enabled": True},
            }
        )
        p._send_queue = asyncio.Queue()
        captured = {}
        p._enqueue_send = lambda *a, **k: captured.update(k)
        await p.forward_message(FakeEvent(forward_chain, FakeBot()))

        assert captured.get("native_forward") in (None, {})

    async def test_worker_prefers_native_when_available(self, forward_chain):
        bot = FakeBot()
        p = make_plugin({"rules": [make_rule()]})
        p._bot_of_platform = lambda pid: bot
        item = {
            "target": "qq-main:GroupMessage:222",
            "result": None,
            "uid": "u1",
            "header_text": "",
            "native_forward": {"platform_id": "qq-main", "message_id": "777"},
        }
        assert await p._send_queued_native(item) is True
        assert native_calls(bot) == ["forward_group_single_msg"]

    async def test_worker_falls_back_when_native_fails(self, forward_chain):
        bot = FakeBot(fail=True)
        p = make_plugin({"rules": [make_rule()]})
        p._bot_of_platform = lambda pid: bot
        item = {
            "target": "qq-main:GroupMessage:222",
            "result": None,
            "uid": "u1",
            "header_text": "",
            "native_forward": {"platform_id": "qq-main", "message_id": "777"},
        }
        assert await p._send_queued_native(item) is False

    async def test_worker_skips_native_for_other_instance(self):
        p = make_plugin({"rules": [make_rule()]})
        p._bot_of_platform = lambda pid: FakeBot()
        item = {
            "target": "qq-other:GroupMessage:222",
            "result": None,
            "uid": "u1",
            "header_text": "",
            "native_forward": {"platform_id": "qq-main", "message_id": "777"},
        }
        assert await p._send_queued_native(item) is False

    async def test_worker_respects_switch_off(self):
        p = make_plugin(
            {"rules": [make_rule()], "forward": {"forward_native_relay": False}}
        )
        p._bot_of_platform = lambda pid: FakeBot()
        item = {
            "target": "qq-main:GroupMessage:222",
            "result": None,
            "uid": "u1",
            "header_text": "",
            "native_forward": {"platform_id": "qq-main", "message_id": "777"},
        }
        assert await p._send_queued_native(item) is False

    async def test_worker_header_failure_falls_back(self):
        bot = FakeBot()
        p = make_plugin({"rules": [make_rule()]}, ctx=FakeCtx(fail_first=1))
        p._bot_of_platform = lambda pid: bot
        item = {
            "target": "qq-main:GroupMessage:222",
            "result": None,
            "uid": "u1",
            "header_text": "[转发] someone",
            "native_forward": {"platform_id": "qq-main", "message_id": "777"},
        }
        assert await p._send_queued_native(item) is False
        assert native_calls(bot) == []

    async def test_send_queued_item_uses_native_before_send(self):
        """锁住 worker 侧的调用点：_send_queued_item 必须先试原生转发。"""
        bot = FakeBot()
        p = make_plugin({"rules": [make_rule()]})
        p._bot_of_platform = lambda pid: bot
        sent = []
        p.context.send_message = lambda t, r: sent.append((t, r)) or _noop()

        item = {
            "target": "qq-main:GroupMessage:222",
            "result": FakeEvent([], FakeBot()).chain_result([Plain(text="x")]),
            "uid": "u1",
            "header_text": "",
            "native_forward": {"platform_id": "qq-main", "message_id": "777"},
            "sanitized_chain": [],
        }
        p._remove_persisted_item = lambda uid: None
        await p._send_queued_item(item)

        assert native_calls(bot) == ["forward_group_single_msg"]
        assert sent == []


class TestCardBlockedForcesRebuild:
    """记录含卡片而放弃原生转发时，重建必须连带把卡片还原。

    这是之前漏掉的：只让原生转发「放弃」，却没打开 rebuild_card，
    卡片仍按 xml 原样发出，目标端照样显示 [XML消息] 点不开。
    """

    @staticmethod
    def _plugin(rebuild_card: bool = False):
        return make_plugin(
            {
                "rules": [make_rule()],
                "forward": {
                    "forward_native_relay": True,
                    "forward_rebuild_card": rebuild_card,
                },
            }
        )

    async def test_card_blocked_sets_flag(self, forward_chain):
        bot = FakeBot()
        p = self._plugin()
        assert await p._send_native_forward(FakeEvent([], bot=bot, mid="1"), "qq-main:GroupMessage:2") is True
        # 无卡片时不该置位
        assert p._native_card_blocked is False

    async def test_card_blocked_rebuild_uses_rebuild_card_true(self, forward_chain):
        """被卡片拦下而改走重建时，必须用 rebuild_card=True 展开。

        配置里 forward_rebuild_card 保持 false，验证的是「强制」而非「继承配置」。
        """
        import main as m

        calls = []
        original = m._expand_forward_components

        async def fake_expand(*args, **kwargs):
            calls.append(kwargs.get("rebuild_card"))
            return list(forward_chain)

        class CardBot(FakeBot):
            """get_msg 返回 forward 段；get_forward_msg 返回含卡片的记录。"""

            async def call_action(self, action, **params):
                self.calls.append((action, params))
                if action == "get_msg":
                    return {"data": {"message": [
                        {"type": "forward", "data": {"id": "fid"}}
                    ]}}
                if action == "get_forward_msg":
                    return {"data": {"messages": [{"message": [
                        {"type": "xml", "data": {"data":
                            '<msg action="viewMultiMsg" m_resid="x">聊天记录</msg>'}}
                    ]}]}}
                return {"data": {}}

        m._expand_forward_components = fake_expand
        try:
            p = self._plugin(rebuild_card=False)
            await p.forward_message(FakeEvent(forward_chain, CardBot()))
        finally:
            m._expand_forward_components = original

        assert True in calls, f"应强制 rebuild_card=True，实际 {calls}"
