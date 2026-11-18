"""合并转发的协议端原生转发：前提判定与 action 调用参数。"""

from __future__ import annotations

import main as m
import pytest
from astrbot.core.message.components import Forward, Plain

from conftest import FakeBot, FakeEvent, make_plugin


class TestSingleForwardChain:
    @pytest.mark.parametrize(
        ("chain", "expected"),
        [
            ([Forward(id="abc")], True),
            ([Forward(id="abc"), Plain(text="x")], False),
            ([Plain(text="x")], False),
            ([], False),
        ],
    )
    def test_only_lone_forward_qualifies(self, chain, expected):
        assert m._is_single_forward_chain(chain) is expected


class TestMessageId:
    def test_reads_message_obj_id(self):
        assert m._native_forward_message_id(FakeEvent([], mid="999")) == "999"

    def test_missing_id_is_empty(self):
        assert m._native_forward_message_id(FakeEvent([], mid="")) == ""


class TestNativeForwardReady:
    TARGET = "qq-main:GroupMessage:111"

    def test_default_enabled_same_instance(self, forward_chain):
        p = make_plugin({})
        assert p._native_forward_ready(FakeEvent([]), forward_chain, self.TARGET)

    def test_disabled_by_config(self, forward_chain):
        p = make_plugin({"forward": {"forward_native_relay": False}})
        assert not p._native_forward_ready(FakeEvent([]), forward_chain, self.TARGET)

    def test_disabled_by_string_value(self, forward_chain):
        p = make_plugin({"forward": {"forward_native_relay": "off"}})
        assert not p._native_forward_ready(FakeEvent([]), forward_chain, self.TARGET)

    def test_cross_instance_rejected(self, forward_chain):
        p = make_plugin({})
        assert not p._native_forward_ready(
            FakeEvent([]), forward_chain, "qq-other:GroupMessage:111"
        )

    def test_mixed_chain_rejected(self, mixed_chain):
        p = make_plugin({})
        assert not p._native_forward_ready(FakeEvent([]), mixed_chain, self.TARGET)

    def test_missing_message_id_rejected(self, forward_chain):
        p = make_plugin({})
        assert not p._native_forward_ready(
            FakeEvent([], mid=""), forward_chain, self.TARGET
        )

    def test_malformed_umo_rejected(self, forward_chain):
        p = make_plugin({})
        assert not p._native_forward_ready(
            FakeEvent([]), forward_chain, "qq-main:GroupMessage"
        )


class TestSendNativeForward:
    async def test_group_uses_forward_group_single_msg(self, forward_chain):
        bot = FakeBot()
        # 探测记录里有没有卡片：get_msg → res_id，get_forward_msg → 节点（这里为空，
        # 表示记录里没有卡片，可以走原生转发）
        async def _call(action, **params):
            bot.calls.append((action, params))
            if action == "get_msg":
                return {"data": {"message": [{"type": "forward", "data": {"id": "fid"}}]}}
            if action == "get_forward_msg":
                return {"data": {"messages": [{"message": [{"type": "text", "data": {"text": "hi"}}]}]}}
            return {"data": {}}
        bot.call_action = _call
        p = make_plugin({})
        sent = await p._send_native_forward(
            FakeEvent([], bot=bot, mid="777"), "qq-main:GroupMessage:222"
        )
        assert sent is True
        # 先探测记录里有没有卡片，再调原生转发
        assert bot.calls[-1] == (
            "forward_group_single_msg",
            {"message_id": "777", "group_id": 222},
        )
        # 探测顺序：get_msg 取 res_id → get_forward_msg 查卡片 → 原生转发
        assert bot.actions()[:2] == ["get_msg", "get_forward_msg"]

    async def test_private_uses_forward_friend_single_msg(self, forward_chain):
        bot = FakeBot()
        p = make_plugin({})
        sent = await p._send_native_forward(
            FakeEvent([], bot=bot, mid="778"), "qq-main:FriendMessage:333"
        )
        assert sent is True
        assert bot.calls[-1] == (
            "forward_friend_single_msg",
            {"message_id": "778", "user_id": 333},
        )

    async def test_protocol_error_returns_false(self, forward_chain):
        bot = FakeBot(fail=True)
        p = make_plugin({})
        sent = await p._send_native_forward(
            FakeEvent([], bot=bot, mid="779"), "qq-main:GroupMessage:222"
        )
        assert sent is False

    async def test_non_numeric_conversation_id_skips_api(self, forward_chain):
        bot = FakeBot()
        p = make_plugin({})
        sent = await p._send_native_forward(
            FakeEvent([], bot=bot, mid="780"), "qq-main:GroupMessage:abc"
        )
        assert sent is False
        assert bot.calls == []


class TestRecordWithCard:
    """记录里含「聊天记录」卡片时必须改走重建。

    协议端原生转发会把 xml 段原样搬到目标群，QQ 客户端不解析记录里的 xml，
    只显示 `[XML消息]` 且点不开；重建链路能按 m_resid 还原成可点开的嵌套记录。
    """

    CARD_XML = (
        '<?xml version="1.0" encoding="utf-8"?><msg serviceID="35" templateID="1" '
        'action="viewMultiMsg" brief="[聊天记录]" m_resid="abc" m_fileName="x" tSum="1">'
        "<item><title>某人的聊天记录</title></item></msg>"
    )

    @staticmethod
    def _bot_with(messages, *, forward_id: str = "fid"):
        """造一个能回应 get_msg / get_forward_msg 的协议端。"""

        class CardBot(FakeBot):
            async def call_action(self, action, **params):
                self.calls.append((action, params))
                if action == "get_msg":
                    return {"data": {"message": [
                        {"type": "forward", "data": {"id": forward_id}}
                    ]}}
                if action == "get_forward_msg":
                    return {"data": {"messages": messages}}
                return {"data": {}}

        return CardBot()

    def test_detects_viewmultimsg_card(self):
        seg = {"type": "xml", "data": {"data": self.CARD_XML}}
        assert m._segment_is_forward_card(seg) is True

    def test_plain_xml_is_not_card(self):
        seg = {"type": "xml", "data": {"data": "<msg>普通 xml</msg>"}}
        assert m._segment_is_forward_card(seg) is False

    def test_text_segment_is_not_card(self):
        assert m._segment_is_forward_card({"type": "text", "data": {"text": "x"}}) is False

    def test_record_with_card_detected(self):
        messages = [
            {"message": [{"type": "text", "data": {"text": "hi"}}]},
            {"message": [{"type": "xml", "data": {"data": self.CARD_XML}}]},
        ]
        assert m._record_has_forward_card(messages) is True

    def test_record_without_card(self):
        messages = [{"message": [{"type": "text", "data": {"text": "hi"}}]}]
        assert m._record_has_forward_card(messages) is False

    async def test_native_forward_skipped_when_record_has_card(self, forward_chain):
        bot = self._bot_with(
            [
                {"message": [{"type": "text", "data": {"text": "外层文字"}}]},
                {"message": [{"type": "xml", "data": {"data": self.CARD_XML}}]},
            ]
        )
        p = make_plugin({})
        sent = await p._send_native_forward(
            FakeEvent([], bot=bot, mid="777"), "qq-main:GroupMessage:222"
        )
        assert sent is False
        assert "forward_group_single_msg" not in bot.actions()

    async def test_native_forward_proceeds_without_card(self, forward_chain):
        bot = self._bot_with([{"message": [{"type": "text", "data": {"text": "hi"}}]}])
        p = make_plugin({})
        sent = await p._send_native_forward(
            FakeEvent([], bot=bot, mid="777"), "qq-main:GroupMessage:222"
        )
        assert sent is True
        assert "forward_group_single_msg" in bot.actions()

    async def test_probe_failure_does_not_block_native(self, forward_chain):
        """探测拉不到记录时不该阻断快路径（否则记录失效就永远走不了原生转发）。"""
        bot = FakeBot(fail=True)
        p = make_plugin({})
        assert await p._record_has_card(bot, "777") is False


class TestCardAutoRebuildSwitch:
    """forward_card_auto_rebuild 开关：默认开启，允许用户关掉换回纯原生转发。"""

    CARD_XML = (
        '<msg serviceID="35" action="viewMultiMsg" brief="[聊天记录]" '
        'm_resid="abc" m_fileName="x" tSum="1">聊天记录</msg>'
    )

    @staticmethod
    def _bot_with_card():
        class CardBot(FakeBot):
            async def call_action(self, action, **params):
                self.calls.append((action, params))
                if action == "get_msg":
                    return {"data": {"message": [
                        {"type": "forward", "data": {"id": "fid"}}
                    ]}}
                if action == "get_forward_msg":
                    return {"data": {"messages": [{"message": [
                        {"type": "xml",
                         "data": {"data": TestCardAutoRebuildSwitch.CARD_XML}}
                    ]}]}}
                return {"data": {}}

        return CardBot()

    def test_default_enabled(self):
        assert make_plugin({})._forward_card_auto_rebuild() is True

    def test_can_be_disabled(self):
        p = make_plugin({"forward": {"forward_card_auto_rebuild": False}})
        assert p._forward_card_auto_rebuild() is False

    def test_string_false_disables(self):
        p = make_plugin({"forward": {"forward_card_auto_rebuild": "false"}})
        assert p._forward_card_auto_rebuild() is False

    async def test_enabled_detects_card(self):
        p = make_plugin({})
        assert await p._record_has_card(self._bot_with_card(), "1") is True

    async def test_disabled_skips_detection(self):
        """关掉后不再探测卡片，一律走原生转发。"""
        p = make_plugin({"forward": {"forward_card_auto_rebuild": False}})
        bot = self._bot_with_card()
        assert await p._record_has_card(bot, "1") is False
        # 连探测请求都不该发
        assert bot.actions() == []

    async def test_disabled_lets_native_forward_proceed(self):
        p = make_plugin({"forward": {"forward_card_auto_rebuild": False}})
        bot = self._bot_with_card()
        sent = await p._send_native_forward(
            FakeEvent([], bot=bot, mid="777"), "qq-main:GroupMessage:222"
        )
        assert sent is True
        assert "forward_group_single_msg" in bot.actions()
