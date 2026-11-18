"""回归：常规消息路径、跨平台降级、内容类型与冷却不受原生转发影响。"""

from __future__ import annotations

import asyncio

from astrbot.core.message.components import At, Forward, Image, Plain

from conftest import FakeBot, FakeCtx, FakeEvent, make_plugin, make_rule


def forward_api_calls(bot: FakeBot) -> list[str]:
    return [a for a in bot.actions() if "forward" in a]


def native_calls(bot: FakeBot) -> list[str]:
    return [a for a in bot.actions() if a.startswith("forward_")]


class TestOrdinaryMessages:
    async def test_plain_text_untouched(self):
        bot = FakeBot()
        p = make_plugin({"rules": [make_rule()]})
        await p.forward_message(FakeEvent([Plain(text="hello")], bot))

        assert len(p.context.sent) == 1
        assert forward_api_calls(bot) == []

    async def test_image_untouched(self):
        bot = FakeBot()
        p = make_plugin({"rules": [make_rule()]})
        await p.forward_message(
            FakeEvent([Image(file="http://x/y.png")], bot)
        )

        assert len(p.context.sent) == 1
        assert forward_api_calls(bot) == []

    async def test_at_is_textified(self):
        bot = FakeBot()
        p = make_plugin({"rules": [make_rule()]})
        await p.forward_message(
            FakeEvent([Plain(text="hi "), At(qq="12345", name="bob")], bot)
        )

        chain = p.context.sent[0][1]
        assert any(getattr(c, "text", "") == "@bob" for c in chain)


class TestCrossPlatformTarget:
    async def test_native_skipped_and_degraded_to_text(self, forward_chain):
        bot = FakeBot()
        p = make_plugin(
            {"rules": [make_rule(target_umo="telegram:GroupMessage:-100")]}
        )
        await p.forward_message(FakeEvent(forward_chain, bot))

        assert "forward_group_single_msg" not in bot.actions()
        assert len(p.context.sent) == 1
        chain = p.context.sent[0][1]
        assert any(getattr(c, "text", "") == "[聊天记录]" for c in chain)


class TestContentTypeFilter:
    async def test_forward_dropped_when_only_plain_selected(self, forward_chain):
        """只勾「文字」时聊天记录不转发。

        注意展开成功时原始链里的 Forward（归「其他」）被筛掉，所以不发；这条断言
        只覆盖成功展开的情形，展开失败时的占位文本行为由
        TestContentTypeWithForward 覆盖（那是改造前就有的既有行为）。
        """
        bot = FakeBot(fail=True)  # 展开失败 → 占位文本，才轮到「文字」类型判定
        p = make_plugin(
            {
                "rules": [make_rule(content_types=["plain"])],
                "forward": {"forward_native_relay": False},
            }
        )
        await p.forward_message(FakeEvent(forward_chain, bot))

        # 展开失败时原始行为是发「[聊天记录]」占位；展开成功时 Forward 归「其他」被筛掉
        assert p.context.sent == [] or [
            getattr(c, "text", "") for c in p.context.sent[0][1]
        ] == ["[聊天记录]"]


class TestMultiTarget:
    async def test_same_instance_native_cross_platform_rebuilt(self, forward_chain):
        bot = FakeBot()
        rule = make_rule(
            target_umo="qq-main:GroupMessage:222\ntelegram:GroupMessage:-100"
        )
        p = make_plugin({"rules": [rule]})
        await p.forward_message(FakeEvent(forward_chain, bot))

        assert native_calls(bot) == ["forward_group_single_msg"]
        assert len(p.context.sent) == 1


class TestCooldown:
    async def test_second_message_skipped(self, forward_chain):
        bot = FakeBot()
        p = make_plugin({"rules": [make_rule(cooldown_seconds=60)]})

        await p.forward_message(FakeEvent(forward_chain, bot))
        first = len(native_calls(bot))
        await p.forward_message(FakeEvent(forward_chain, bot))
        second = len(native_calls(bot))

        assert first == 1
        assert second == 1


class TestHasMediaUsesPreLocalizationChain:
    """has_media 必须按本地化**之前**的链判断。

    媒体本地化失败会被替换成占位文本；若用本地化后的链判断，会把「本来有媒体」
    误判成没有，从而跳过第二层（远程 URL）兜底重试。
    """

    async def test_placeholder_localization_keeps_media_flag(self):
        """先固化不变量：本地化前后 has_media 结论确实会不同。"""
        import main as m

        filtered = [Image(file="http://example.com/a.png")]
        assert m._chain_has_media(filtered) is True

        localized = [Plain(text="[图片]")]
        assert m._chain_has_media(localized) is False

    async def test_fallback_runs_when_localization_loses_media(self):
        """本地化把媒体变成占位后，失败仍应触发远程 URL 兜底。"""
        import main as m

        # 前两次发送都失败：首次发送 + 第一层（本地化）兜底，才能走到第二层兜底
        ctx = FakeCtx(fail_first=2)
        bot = FakeBot(fail=True)
        # 必须开启「发送前下载媒体」，否则主链不会本地化，也就测不到本地化丢媒体
        p = make_plugin(
            {"rules": [make_rule(download_media_before_send="true")]}, ctx=ctx
        )

        original_forward = m._prepare_chain_for_forward
        original_fallback = m._prepare_chain_fallback
        fallback_called = {"count": 0}

        async def fake_forward(_chain, **_kwargs):
            # 模拟本地化失败：媒体被替换成占位文本
            return [Plain(text="[图片]")]

        async def fake_fallback(_chain, **_kwargs):
            fallback_called["count"] += 1
            return [Plain(text="[图片-远程URL]")]

        m._prepare_chain_for_forward = fake_forward
        m._prepare_chain_fallback = fake_fallback
        try:
            await p.forward_message(
                FakeEvent([Image(file="http://example.com/a.png")], bot)
            )
        finally:
            m._prepare_chain_for_forward = original_forward
            m._prepare_chain_fallback = original_fallback

        assert fallback_called["count"] == 1


class TestContentTypeWithForward:
    """合并转发的内容类型筛选语义（与改造前逐格对齐）。

    关键点：原始链里的一条 Forward 归入「其他」，展开失败后会变成「文字」占位。
    两者命中不同类型，所以必须等展开之后再筛，不能提前按原始链判定。
    """

    @staticmethod
    async def _run(types: list[str], expand_ok: bool) -> list:
        bot = FakeBot(fail=not expand_ok)
        if expand_ok:
            bot.payload = {
                "data": {
                    "messages": [
                        {
                            "sender": {"user_id": "1", "nickname": "a"},
                            "message": [{"type": "text", "data": {"text": "hi"}}],
                            "time": 1,
                        }
                    ]
                }
            }
        conf = {
            "rules": [make_rule(content_types=types)],
            "forward": {"forward_native_relay": False},
        }
        p = make_plugin(conf)
        await p.forward_message(FakeEvent([Forward(id="abc")], bot))
        return [c for _, chain in p.context.sent for c in chain]

    async def test_plain_only_sends_placeholder_on_failure(self):
        chain = await self._run(["plain"], expand_ok=False)
        assert [getattr(c, "text", None) for c in chain] == ["[聊天记录]"]

    async def test_plain_only_sends_nothing_on_success(self):
        assert await self._run(["plain"], expand_ok=True) == []

    async def test_other_only_sends_record_on_success(self):
        chain = await self._run(["other"], expand_ok=True)
        assert len(chain) == 1
        assert type(chain[0]).__name__ == "_ForwardNodes"

    async def test_other_only_sends_nothing_on_failure(self):
        assert await self._run(["other"], expand_ok=False) == []

    async def test_both_types_covers_both_outcomes(self):
        assert len(await self._run(["plain", "other"], expand_ok=True)) == 1
        chain = await self._run(["plain", "other"], expand_ok=False)
        assert [getattr(c, "text", None) for c in chain] == ["[聊天记录]"]


class TestBehaviorParityWithPreRefactor:
    """与改造前逐场景对齐：原生转发关闭时，行为必须完全一致。

    合并转发展开被改成按需计算（惰性），这条链路牵动内容类型筛选、来源头、
    队列与兜底重试，故用参数化把关键场景固化下来。
    """

    @staticmethod
    def _conf(**rule_kw):
        return {
            "rules": [make_rule(**rule_kw)],
            "forward": {"forward_native_relay": False},
        }

    async def test_plain_text(self):
        p = make_plugin(self._conf())
        await p.forward_message(FakeEvent([Plain(text="hi")], FakeBot()))
        assert [[getattr(c, "text", "") for c in ch] for _, ch in p.context.sent] == [
            ["hi"]
        ]

    async def test_header_prepended(self):
        p = make_plugin(self._conf(hide_header=False))
        await p.forward_message(FakeEvent([Plain(text="hi")], FakeBot()))
        first = p.context.sent[0][1][0]
        assert getattr(first, "text", "").startswith("[转发] tester (10000)")
        assert getattr(p.context.sent[0][1][1], "text", "") == "hi"

    async def test_image_passthrough(self):
        p = make_plugin(self._conf())
        await p.forward_message(
            FakeEvent([Image(file="http://e/a.png")], FakeBot())
        )
        assert type(p.context.sent[0][1][0]).__name__ == "Image"

    async def test_multi_target_sends_once_each(self):
        p = make_plugin(
            self._conf(target_umo="qq-main:GroupMessage:222\ntelegram:GroupMessage:-1")
        )
        await p.forward_message(FakeEvent([Plain(text="hi")], FakeBot()))
        assert len(p.context.sent) == 2

    async def test_queue_mode_enqueues(self):
        p = make_plugin(
            {
                "rules": [make_rule(queue_interval_seconds=5)],
                "queue": {"queue_enabled": True},
                "forward": {"forward_native_relay": False},
            }
        )
        p._send_queue = asyncio.Queue()
        p._enqueue_send = lambda *a, **k: p._send_queue.put_nowait((a, k))
        await p.forward_message(FakeEvent([Plain(text="hi")], FakeBot()))
        assert p.context.sent == []
        assert p._send_queue.qsize() == 1

    async def test_at_textified(self):
        p = make_plugin(self._conf())
        await p.forward_message(
            FakeEvent([Plain(text="x "), At(qq="123", name="bob")], FakeBot())
        )
        texts = [getattr(c, "text", "") for c in p.context.sent[0][1]]
        assert texts == ["x ", "@bob"]
