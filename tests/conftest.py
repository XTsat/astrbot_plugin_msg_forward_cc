"""测试夹具：伪造 AstrBot 事件 / 协议端 / 上下文。

这些测试需要一个能 import `main` 的 AstrBot 运行时（插件真实运行环境），
不在这里伪造整个 astrbot 包；只伪造事件与协议端调用，让转发逻辑可离线验证。
"""

from __future__ import annotations

import main as m
import pytest
from astrbot.core.message.components import Forward, Plain


class FakeBot:
    """假协议端：记录 call_action 调用，可配置为全部失败。"""

    def __init__(self, fail: bool = False, payload: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self.payload = payload if payload is not None else {"data": {}}

    async def call_action(self, action: str, **params):
        self.calls.append((action, params))
        if self.fail:
            raise RuntimeError("protocol error")
        return self.payload

    def actions(self) -> list[str]:
        return [c[0] for c in self.calls]


class FakePlatformMeta:
    def __init__(self, pid: str, name: str = "aiocqhttp") -> None:
        self.id = pid
        self.name = name


class FakeMsgObj:
    def __init__(self, mid: str, messages: list) -> None:
        self.message_id = mid
        self.message = messages
        self.group_id = "111"
        self.type = "GroupMessage"


class FakeEvent:
    """假 AstrMessageEvent：只实现转发逻辑用到的那几个访问器。"""

    def __init__(
        self,
        messages: list,
        bot: FakeBot | None = None,
        *,
        mid: str = "123456",
        pid: str = "qq-main",
        platform_name: str = "aiocqhttp",
        sender_id: str = "10000",
    ) -> None:
        self.platform_meta = FakePlatformMeta(pid, platform_name)
        self.message_obj = FakeMsgObj(mid, messages)
        self.bot = bot if bot is not None else FakeBot()
        self.unified_msg_origin = f"{pid}:GroupMessage:111"
        self._sender_id = sender_id

    def get_platform_id(self) -> str:
        return self.platform_meta.id

    def get_platform_name(self) -> str:
        return self.platform_meta.name

    def get_messages(self) -> list:
        return self.message_obj.message

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return "tester"

    def chain_result(self, chain):
        return chain

    def plain_result(self, text):
        return text


class FakeInst:
    def __init__(self, pid: str = "qq-main", name: str = "aiocqhttp") -> None:
        self._meta = FakePlatformMeta(pid, name)

    def meta(self):
        return self._meta


class FakeCtx:
    """假 Context：记录发出去的消息，可按需让前 N 次发送失败。"""

    def __init__(self, fail_first: int = 0) -> None:
        self.sent: list[tuple[str, list]] = []
        self._fail_remaining = fail_first

    def get_platform_inst(self, pid: str):
        return FakeInst() if pid == "qq-main" else None

    async def send_message(self, target: str, result) -> None:
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise RuntimeError("send failed")
        self.sent.append((target, result))


def make_plugin(conf: dict, ctx: FakeCtx | None = None) -> m.MsgForward:
    """构造一个不跑 __init__ 的插件实例，只装转发逻辑需要的状态。"""
    p = m.MsgForward.__new__(m.MsgForward)
    p.config = conf
    p.context = ctx if ctx is not None else FakeCtx()
    p._cooldowns = {}
    p._cooldown_warned = set()
    p._send_queue = None
    p._queue_rule_counts = {}
    return p


def make_rule(**overrides) -> dict:
    rule = {
        "enabled": True,
        "source_umo": "qq-main:GroupMessage:111",
        "target_umo": "qq-main:GroupMessage:222",
        "hide_header": True,
    }
    rule.update(overrides)
    return rule


@pytest.fixture
def forward_chain() -> list:
    """整条消息就是一条合并转发。"""
    return [Forward(id="abc")]


@pytest.fixture
def mixed_chain() -> list:
    """文字 + 合并转发的混排消息。"""
    return [Plain(text="看这个"), Forward(id="abc")]
