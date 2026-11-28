"""队列媒体本地化策略：不该本地化的别下载。

- 原生转发的聊天记录：发送时由协议端搬运，入队前那份重建链根本用不到；
- 自带 base64 / data URI 的媒体：本身就是内容，落盘只会多一份临时文件；
- 已经在插件媒体缓存里的文件：重启恢复 / 重复入队时不必再拷一份。
"""

from __future__ import annotations

import main as m
from astrbot.core.message.components import Forward, Image, Plain

from conftest import FakeBot, FakeEvent, make_plugin, make_rule


def _queue_plugin(**rule_overrides):
    return make_plugin(
        {
            "rules": [make_rule(queue_interval_seconds=5, **rule_overrides)],
            "queue": {"queue_enabled": True},
        }
    )


def _capture_enqueue(p):
    captured: dict = {}

    def _enqueue(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    p._enqueue_send = _enqueue
    return captured


class TestQueueLocalizeMode:
    async def test_native_record_skips_localization(self, monkeypatch):
        prepared = []

        async def fake_prepare(chain, use_proxy=False, proxy_url=None):
            prepared.append(chain)
            return chain

        monkeypatch.setattr(m, "_prepare_chain_for_queue", fake_prepare)
        p = _queue_plugin()
        captured = _capture_enqueue(p)

        await p.forward_message(FakeEvent([Forward(id="abc")], FakeBot()))

        assert captured["kwargs"]["native_forward"], "应命中原生转发"
        assert prepared == [], "原生转发的记录不该在入队前本地化媒体"
        # 入队用的链就是没本地化的源链（原生失败时也走这条，与重启恢复后一致）
        assert captured["args"][1] == captured["args"][3]

    async def test_always_mode_localizes_native_record(self, monkeypatch):
        prepared = []

        async def fake_prepare(chain, use_proxy=False, proxy_url=None):
            prepared.append(chain)
            return chain

        monkeypatch.setattr(m, "_prepare_chain_for_queue", fake_prepare)
        p = _queue_plugin()
        p.config["queue"]["queue_localize_media"] = "always"
        _capture_enqueue(p)

        await p.forward_message(FakeEvent([Forward(id="abc")], FakeBot()))

        assert len(prepared) == 1

    async def test_off_mode_skips_localization_everywhere(self, monkeypatch):
        prepared = []

        async def fake_prepare(chain, use_proxy=False, proxy_url=None):
            prepared.append(chain)
            return chain

        monkeypatch.setattr(m, "_prepare_chain_for_queue", fake_prepare)
        p = _queue_plugin()
        p.config["queue"]["queue_localize_media"] = "off"
        _capture_enqueue(p)

        # 混排消息不走原生转发，off 下同样不本地化
        await p.forward_message(
            FakeEvent([Plain(text="看这个"), Forward(id="abc")], FakeBot())
        )

        assert prepared == []

    async def test_auto_mode_localizes_non_native_media(self, monkeypatch):
        prepared = []

        async def fake_prepare(chain, use_proxy=False, proxy_url=None):
            prepared.append(chain)
            return chain

        monkeypatch.setattr(m, "_prepare_chain_for_queue", fake_prepare)
        p = _queue_plugin()
        _capture_enqueue(p)

        # 混排消息不满足原生转发前提 → auto 仍按旧逻辑本地化
        await p.forward_message(
            FakeEvent([Plain(text="看这个"), Forward(id="abc")], FakeBot())
        )

        assert len(prepared) == 1


class TestLocalizeSkips:
    async def test_base64_image_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "_MEDIA_CACHE_DIR", tmp_path)
        img = Image(file="base64://QUJD")

        out = await m._prepare_chain_for_queue([img])

        assert out[0] is img
        assert out[0].file == "base64://QUJD"
        assert list(tmp_path.iterdir()) == []

    async def test_cached_file_not_copied(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "_MEDIA_CACHE_DIR", tmp_path)
        src = tmp_path / "cached.jpg"
        src.write_bytes(b"x")
        img = Image.fromFileSystem(str(src))

        out = await m._prepare_chain_for_queue([img])

        assert out[0] is img
        assert list(tmp_path.iterdir()) == [src]

    async def test_temp_file_is_copied(self, tmp_path, monkeypatch):
        cache = tmp_path / "cache"
        cache.mkdir()
        monkeypatch.setattr(m, "_MEDIA_CACHE_DIR", cache)
        src = tmp_path / "temp.jpg"
        src.write_bytes(b"x")
        img = Image.fromFileSystem(str(src))

        out = await m._prepare_chain_for_queue([img])

        assert out[0] is not img
        copied = list(cache.iterdir())
        assert len(copied) == 1 and copied[0].read_bytes() == b"x"
