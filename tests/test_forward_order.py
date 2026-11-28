"""同一来源会话的转发顺序：后到的消息不能插到聊天记录前面。

AstrBot 为每条消息事件单独创建 task 并发处理（`event_bus.dispatch`），聊天记录
要先拉取内容重建、队列模式还要先本地化媒体，比纯文字慢得多；不按来源串行化时，
后到的文字 / 图片会先入队、先发送，「先发的聊天记录最后才转出去」。
"""

from __future__ import annotations

import asyncio

from conftest import FakeBot, FakeEvent, make_plugin, make_rule
from astrbot.core.message.components import Forward, Plain

SOURCE = "qq-main:GroupMessage:111"


class GatedBot(FakeBot):
    """聊天记录拉取会卡在闸门上，模拟「展开比纯文字慢」的真实耗时。"""

    def __init__(self) -> None:
        super().__init__()
        self.reached = asyncio.Event()
        self.gate = asyncio.Event()

    async def call_action(self, action: str, **params):
        if action == "get_forward_msg":
            self.reached.set()
            await self.gate.wait()
        return await super().call_action(action, **params)


async def _wait_until(pred, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.001)


def _text_of(chain: list) -> str:
    return "".join(getattr(c, "text", "") for c in chain)


class TestSameSourceOrder:
    async def test_chat_record_before_text_keeps_order(self):
        bot = GatedBot()
        p = make_plugin(
            {"rules": [make_rule()], "forward": {"forward_native_relay": False}}
        )

        # 第一条：聊天记录，卡在拉取上；第二条：纯文字，后到
        record = asyncio.create_task(
            p.forward_message(FakeEvent([Forward(id="abc")], bot))
        )
        await _wait_until(bot.reached.is_set)
        assert p._forward_order_lock_refs.get(SOURCE) == 1

        text = asyncio.create_task(
            p.forward_message(FakeEvent([Plain(text="第二条")]))
        )
        await asyncio.sleep(0.05)
        assert p.context.sent == [], "后到的文字不应抢在聊天记录前面发出去"

        bot.gate.set()
        await asyncio.gather(record, text)

        assert len(p.context.sent) == 2
        assert _text_of(p.context.sent[0][1]) == "[聊天记录]"
        assert _text_of(p.context.sent[1][1]) == "第二条"

    async def test_queue_enqueue_keeps_arrival_order(self):
        bot = GatedBot()
        p = make_plugin(
            {
                "rules": [make_rule(queue_interval_seconds=5)],
                "queue": {"queue_enabled": True},
                "forward": {"forward_native_relay": False},
            }
        )
        enqueued: list[list] = []
        p._enqueue_send = lambda *args, **kwargs: enqueued.append(args[3])

        record = asyncio.create_task(
            p.forward_message(FakeEvent([Forward(id="abc")], bot))
        )
        await _wait_until(bot.reached.is_set)

        text = asyncio.create_task(
            p.forward_message(FakeEvent([Plain(text="第二条")]))
        )
        await asyncio.sleep(0.05)
        assert enqueued == [], "后到的文字不应先入队（队列 FIFO 会把它排在前面）"

        bot.gate.set()
        await asyncio.gather(record, text)

        assert len(enqueued) == 2
        assert _text_of(enqueued[0]) == "[聊天记录]"
        assert _text_of(enqueued[1]) == "第二条"

    async def test_other_source_not_blocked(self):
        bot = GatedBot()
        p = make_plugin(
            {
                "rules": [
                    make_rule(),
                    make_rule(
                        source_umo="qq-main:GroupMessage:333",
                        target_umo="qq-main:GroupMessage:444",
                    ),
                ],
                "forward": {"forward_native_relay": False},
            }
        )

        record = asyncio.create_task(
            p.forward_message(FakeEvent([Forward(id="abc")], bot))
        )
        await _wait_until(bot.reached.is_set)

        other_event = FakeEvent([Plain(text="另一个群")])
        other_event.unified_msg_origin = "qq-main:GroupMessage:333"
        other = asyncio.create_task(p.forward_message(other_event))
        await _wait_until(lambda: len(p.context.sent) == 1)
        assert _text_of(p.context.sent[0][1]) == "另一个群"

        bot.gate.set()
        await asyncio.gather(record, other)
        assert len(p.context.sent) == 2


class TestOrderLockLifecycle:
    async def test_lock_reclaimed_when_idle(self):
        p = make_plugin({"rules": [make_rule()]})
        await p.forward_message(FakeEvent([Plain(text="hi")]))

        assert p._forward_order_locks == {}
        assert p._forward_order_lock_refs == {}

    async def test_error_does_not_keep_source_locked(self):
        p = make_plugin({"rules": [make_rule()]})

        async def boom(event, chain):
            raise RuntimeError("boom")

        p._resolve_file_urls = boom
        await p.forward_message(FakeEvent([Plain(text="第一条")]))
        assert p._forward_order_locks == {}

        async def passthrough(event, chain):
            return chain

        p._resolve_file_urls = passthrough
        await p.forward_message(FakeEvent([Plain(text="第二条")]))
        assert _text_of(p.context.sent[0][1]) == "第二条"