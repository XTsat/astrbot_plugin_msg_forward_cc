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


class TestCardRebuildFollowsSingleSwitch:
    """卡片是否还原只由 forward_card_auto_rebuild 决定，不做任何强制覆盖。

    A 方案：一个开关统一管「含卡片时是否改走重建」与「重建时是否还原卡片」。
    开着 → 改走重建且还原；关掉 → 走原生转发且卡片原样，插件不得自作主张。
    """

    CARD_XML = (
        '<msg action="viewMultiMsg" brief="[聊天记录]" m_resid="x">聊天记录</msg>'
    )

    @classmethod
    def _card_bot(cls, native_ok: bool = True):
        """能回应卡片探测的协议端；native_ok=False 让原生转发失败以强制走重建。"""

        class CardBot(FakeBot):
            async def call_action(self, action, **params):
                self.calls.append((action, params))
                if action == "get_msg":
                    return {"data": {"message": [
                        {"type": "forward", "data": {"id": "fid"}}
                    ]}}
                if action == "get_forward_msg":
                    return {"data": {"messages": [{"message": [
                        {"type": "xml", "data": {"data": cls.CARD_XML}}
                    ]}]}}
                if action.startswith("forward_") and not native_ok:
                    raise RuntimeError("native forward disabled for test")
                return {"data": {}}

        return CardBot()

    @staticmethod
    def _plugin(enabled: bool):
        return make_plugin(
            {
                "rules": [make_rule()],
                "forward": {
                    "forward_native_relay": True,
                    "forward_card_auto_rebuild": enabled,
                },
            }
        )

    async def _capture_rebuild_flag(self, enabled: bool, native_ok: bool = True):
        """跑一次 forward_message，捕获展开时传入的 rebuild_card。"""
        import main as m

        calls = []
        original = m._expand_forward_components

        async def fake_expand(*args, **kwargs):
            calls.append(kwargs.get("rebuild_card"))
            return list(args[1]) if len(args) > 1 else []

        m._expand_forward_components = fake_expand
        try:
            await self._plugin(enabled).forward_message(
                FakeEvent([m.Forward(id="x")], self._card_bot(native_ok))
            )
        finally:
            m._expand_forward_components = original
        return calls

    async def test_enabled_rebuilds_card(self):
        """开启时：探测到卡片 → 改走重建 → 以 rebuild_card=True 展开。"""
        calls = await self._capture_rebuild_flag(True)
        assert calls, "开启且含卡片时应当触发重建"
        assert True in calls, f"开启时应以 rebuild_card=True 展开，实际 {calls}"

    async def test_disabled_skips_card_detection(self):
        """关闭时：不探测卡片，直接走原生转发。"""
        bot = self._card_bot()
        p = self._plugin(False)
        await p.forward_message(FakeEvent([__import__("main").Forward(id="x")], bot))
        assert "forward_group_single_msg" in bot.actions()
        # 关掉开关后连探测都不该发
        assert "get_forward_msg" not in bot.actions()

    async def test_disabled_never_forces_rebuild(self):
        """关闭时即使被迫走重建（原生失败），也不得强制还原卡片。"""
        calls = await self._capture_rebuild_flag(False, native_ok=False)
        assert calls, "原生转发失败时应回退到重建"
        assert True not in calls, f"关闭时不应强制 rebuild_card=True，实际 {calls}"


class TestQueueMasterSwitchDefault:
    """发送队列总开关默认开启：规则设了间隔就直接生效。

    默认关闭会让「设了间隔却不生效」成为默认陷阱，故改为默认开启，
    总开关退居紧急刹车（关闭后所有规则立即转发）。
    """

    async def test_interval_alone_enters_queue(self, forward_chain):
        """未显式配置 queue_enabled 时，规则间隔 > 0 即入队。"""
        bot = FakeBot()
        p = make_plugin({"rules": [make_rule(queue_interval_seconds=5)]})
        p._send_queue = asyncio.Queue()
        p._enqueue_send = lambda *a, **k: p._send_queue.put_nowait((a, k))

        await p.forward_message(FakeEvent(forward_chain, bot))
        assert p._send_queue.qsize() == 1

    async def test_switch_off_forces_immediate(self):
        """显式关闭总开关后，即使规则设了间隔也立即转发（发送已发生、未入队）。"""
        ctx = FakeCtx()
        p = make_plugin(
            {
                "rules": [make_rule(queue_interval_seconds=5)],
                "queue": {"queue_enabled": False},
            },
            ctx=ctx,
        )
        p._send_queue = asyncio.Queue()
        p._enqueue_send = lambda *a, **k: p._send_queue.put_nowait((a, k))

        await p.forward_message(FakeEvent([Plain(text="hi")], FakeBot()))

        assert p._send_queue.qsize() == 0, "总开关关闭时不应入队"
        assert ctx.sent, "应立即转发"


class _SaveableDict(dict):
    """带 save_config 的配置字典，模拟 AstrBotConfig 的持久化接口。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.saves = 0

    def save_config(self):
        self.saves += 1


class TestConfigGroupMigration:
    """media / at / platform 已合并进 enhance，需把旧值搬过去。

    旧分组在 schema 里保留为 invisible——AstrBot 会在插件初始化前裁掉
    schema 未声明的键，删了 schema 就永远读不到存量值。
    """

    def _plugin(self, conf):
        return make_plugin(conf)

    def test_moves_legacy_values(self):
        conf = _SaveableDict(
            {
                "media": {"download_media_before_send": True},
                "at": {
                    "at_nickname_lookup": True,
                    "at_nickname_lookup_cache_ttl": 600,
                    "at_passthrough_extra_platforms": ["myqq"],
                },
                "platform": {"platform_names": ["aiocqhttp=扣扣"]},
            }
        )
        self._plugin(conf)._migrate_config_groups()

        e = conf["enhance"]
        assert e["download_media_before_send"] is True
        assert e["at_nickname_lookup"] is True
        assert e["at_nickname_lookup_cache_ttl"] == 600
        assert e["at_passthrough_extra_platforms"] == ["myqq"]
        assert e["platform_names"] == ["aiocqhttp=扣扣"]
        assert conf.saves == 1

    def test_does_not_overwrite_new_value(self):
        """新分组被改过（非默认值）时，不用旧分组的值覆盖用户当前设置。"""
        conf = _SaveableDict(
            {
                "enhance": {"at_nickname_lookup_cache_ttl": 900},
                "at": {"at_nickname_lookup_cache_ttl": 600},
            }
        )
        self._plugin(conf)._migrate_config_groups()

        assert conf["enhance"]["at_nickname_lookup_cache_ttl"] == 900

    def test_legacy_wins_when_new_is_default(self):
        """新分组仍是默认值（AstrBot 自动填充）时，采用旧分组的用户设置。

        默认值无法区分「用户显式设为默认」与「AstrBot 铺的默认」，此时优先
        保留旧分组里那个明显被改过的值，避免升级丢配置。"""
        conf = _SaveableDict(
            {
                "enhance": {"at_nickname_lookup_cache_ttl": 300},
                "at": {"at_nickname_lookup_cache_ttl": 600},
            }
        )
        self._plugin(conf)._migrate_config_groups()

        assert conf["enhance"]["at_nickname_lookup_cache_ttl"] == 600

    def test_legacy_default_value_not_copied(self):
        """旧值等于默认值时无需迁移（避免平白写入）。"""
        conf = _SaveableDict(
            {"at": {"at_nickname_lookup": False, "at_nickname_lookup_cache_ttl": 300}}
        )
        self._plugin(conf)._migrate_config_groups()
        assert conf.saves == 0

    def test_no_legacy_groups_is_noop(self):
        conf = _SaveableDict({"enhance": {"at_nickname_lookup": True}})
        self._plugin(conf)._migrate_config_groups()
        assert conf.saves == 0

    def test_second_run_is_idempotent(self):
        """迁移过一遍后再跑不应重复保存。"""
        conf = _SaveableDict({"at": {"at_nickname_lookup": True}})
        p = self._plugin(conf)
        p._migrate_config_groups()
        saves = conf.saves
        p._migrate_config_groups()
        assert conf.saves == saves

    def test_read_falls_back_to_legacy_group(self):
        """迁移未执行时，读取仍能从旧分组取到值（兜底）。"""
        p = self._plugin({"at": {"at_nickname_lookup": True}})
        assert p._conf_get("at_nickname_lookup", False) is True


class TestAtPassthroughPlatforms:
    """额外 @ 透传平台：配置存在分组里，读取必须走分组否则永远取不到。"""

    def test_builtin_qq_platforms_present(self):
        import main as m

        assert "aiocqhttp" in m._at_passthrough_platforms(None)

    def test_extra_platform_appended(self):
        import main as m

        assert "myqq" in m._at_passthrough_platforms(["myqq"])

    def test_read_from_grouped_config(self):
        """分组配置里的追加平台能被读到（回归：曾直读扁平键导致失效）。"""
        p = make_plugin({"enhance": {"at_passthrough_extra_platforms": ["myqq"]}})
        import main as m

        extra = p._conf_get("at_passthrough_extra_platforms", [])
        assert "myqq" in m._at_passthrough_platforms(extra)


class TestCardFallbackKeepsRawXml:
    """卡片内容取不回时，重建保留原样 XML 卡片，而不是降级成文字占位。

    SnowLuma 对只剩 m_fileName 的卡片一律 retcode=100；旧行为会把它变成
    「[聊天记录] 标题（N条）· 预览行」文字，卡片外观与点开都丢了。
    """

    CARD_XML = (
        '<?xml version="1.0" encoding="UTF-8"?><msg serviceID="35" templateID="1" '
        'action="viewMultiMsg" brief="[聊天记录]" m_fileName="deadbeef" tSum="2">'
        "<item><title>某群聊天记录</title></item></msg>"
    )

    @classmethod
    def _bot(cls):
        class Bot(FakeBot):
            async def call_action(self, action, **params):
                self.calls.append((action, params))
                if action == "get_forward_msg":
                    if str(params.get("id", "")) == "fid":
                        return {"data": {"messages": [{"message": [
                            {"type": "xml", "data": {"data": cls.CARD_XML}}
                        ]}]}}
                    return {"status": "failed", "retcode": 100, "data": None}
                return {"data": {}}

        return Bot()

    async def test_rebuild_keeps_raw_xml_card(self):
        import main as m

        ev = FakeEvent([m.Forward(id="fid")], self._bot())
        chain = await m._expand_forward_components(
            ev, [m.Forward(id="fid")], keep_time=True, depth=2, rebuild_card=True
        )
        content = chain[0].nodes[0].content
        raw = [c for c in content if isinstance(c, m._RawXmlCard)]
        assert raw and raw[0].raw == self.CARD_XML
        assert not any(
            isinstance(c, Plain) and "[聊天记录]" in str(c.text) for c in content
        ), "取不回内容时不应再降级成文字占位"

    async def test_resolvable_card_becomes_nested_nodes(self):
        import main as m

        class Bot(FakeBot):
            async def call_action(self, action, **params):
                self.calls.append((action, params))
                if action == "get_forward_msg":
                    if str(params.get("id", "")) == "fid":
                        return {"data": {"messages": [{"message": [
                            {"type": "xml", "data": {"data": TestCardFallbackKeepsRawXml.CARD_XML}}
                        ]}]}}
                    # 卡片 resid / fileName 能取到内层记录
                    return {"data": {"messages": [{"message": [
                        {"type": "text", "data": {"text": "内层消息"}}
                    ]}]}}
                return {"data": {}}

        ev = FakeEvent([m.Forward(id="fid")], Bot())
        chain = await m._expand_forward_components(
            ev, [m.Forward(id="fid")], keep_time=True, depth=2, rebuild_card=True
        )
        content = chain[0].nodes[0].content
        assert any(isinstance(c, m.Nodes) for c in content)
        assert not any(isinstance(c, m._RawXmlCard) for c in content)

    def test_raw_xml_card_survives_queue_serialization(self):
        import main as m

        data = m._serialize_chain([m._RawXmlCard(self.CARD_XML)])
        assert data and data[0]["t"] == "raw_xml"
        chain = m._deserialize_chain(data)
        assert len(chain) == 1 and isinstance(chain[0], m._RawXmlCard)
        assert chain[0].raw == self.CARD_XML
