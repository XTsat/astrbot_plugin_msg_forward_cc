import asyncio
import json
import os
import re
import secrets
import ssl
import string
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import astrbot.api.star as star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api import AstrBotConfig

from astrbot.core.message.components import At, Plain, Image, Record, Video, File
from astrbot.core.message.message_event_result import MessageEventResult


# ------------------------
# 工具与数据路径
# ------------------------


# 远程媒体 Content-Type → 落盘后缀映射（自定义下载器据此确定临时文件后缀）
_MIME_EXT_MAP = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "audio/amr": ".amr",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/silk": ".silk",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "video/mpeg": ".mpg",
    "video/quicktime": ".mov",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
}

# 兜底：按组件类型确定后缀
_DEFAULT_MEDIA_EXT = {
    Image: ".jpg",
    Record: ".amr",
    Video: ".mp4",
    File: ".bin",
}

_VALID_URL_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg",
    ".amr", ".mp3", ".wav", ".silk", ".ogg", ".flac", ".m4a",
    ".mp4", ".mov", ".mpg", ".mpeg", ".pdf", ".zip", ".bin",
}


def _comp_type_name(comp) -> str:
    """返回组件的可读类型名，用于日志与占位文本。"""
    return getattr(getattr(comp, "type", None), "value", None) or type(comp).__name__


def _extract_remote_url(comp) -> str | None:
    """返回组件引用的远程 http(s) URL；本地文件 / base64 / data URI 返回 None。

    注意：File 组件的 `.file` 是 property，在异步上下文访问会触发同步下载并报
    警告，因此对 File 只检查 `.url` 与 `.file_`。
    """
    url = getattr(comp, "url", None)
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    if isinstance(comp, File):
        file_ref = getattr(comp, "file_", None)
    else:
        file_ref = getattr(comp, "file", None)
    if isinstance(file_ref, str) and file_ref.startswith(("http://", "https://")):
        return file_ref
    return None


def _guess_media_ext(comp, url: str, content_type: str) -> str:
    """根据 Content-Type / URL 后缀 / 组件类型确定临时文件后缀。"""
    mime = (content_type or "").split(";")[0].strip().lower()
    ext = _MIME_EXT_MAP.get(mime)
    if ext:
        return ext
    url_ext = Path(urlparse(url).path).suffix.lower()
    if url_ext in _VALID_URL_EXT:
        return url_ext
    for comp_type, default in _DEFAULT_MEDIA_EXT.items():
        if isinstance(comp, comp_type):
            return default
    return ".bin"


async def _download_url_to_local(comp, url: str, use_proxy: bool = False, proxy_url: str | None = None) -> str:
    """把远程媒体下载到本地临时目录，返回本地路径。

    先用正常网络（aiohttp 默认 AF_UNSPEC / happy eyeballs）尝试，失败后改用强制
    IPv4（AF_INET）重试，规避宿主机 IPv6 无默认路由 / DNS no-data 时 aiohttp 报
    `Cannot connect ... ssl:default [None]`（aio-libs/aiohttp#9447）的问题。
    代理三态：use_proxy=False 直连；use_proxy=True 且 proxy_url 空走系统代理（环境变量）；
    use_proxy=True 且 proxy_url 非空走该代理地址。两次都失败则抛异常，由调用方降级为占位文本。
    """
    try:
        import aiohttp
    except ImportError as e:
        raise RuntimeError("aiohttp 不可用，无法本地化媒体") from e

    try:
        import certifi
        ssl_context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ssl_context = ssl.create_default_context()

    import socket

    # 代理三态：关→直连；开且无地址→系统代理（读环境变量）；开且有地址→指定代理
    if not use_proxy:
        trust_env, proxy = False, None
    elif proxy_url:
        trust_env, proxy = False, proxy_url
    else:
        trust_env, proxy = True, None

    async def _fetch(connector):
        async with aiohttp.ClientSession(trust_env=trust_env, connector=connector) as session:
            async with session.get(url, proxy=proxy, timeout=120) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "")
                return await resp.read(), content_type

    # 先正常网络（默认 AF_UNSPEC），失败再强制 IPv4
    try:
        data, content_type = await _fetch(aiohttp.TCPConnector(ssl=ssl_context))
    except Exception as e:
        logger.warning(f"⚠️ 正常网络下载媒体失败（{e}），改用强制 IPv4 重试")
        data, content_type = await _fetch(
            aiohttp.TCPConnector(ssl=ssl_context, family=socket.AF_INET)
        )

    suffix = _guess_media_ext(comp, url, content_type)
    tmp_dir = Path(tempfile.gettempdir()) / "msg_forward_cc_media"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=suffix, dir=str(tmp_dir))
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _rebuild_from_local_path(comp, local_path: str):
    """按本地文件路径重建组件（fromFileSystem）。"""
    if isinstance(comp, Image):
        return Image.fromFileSystem(local_path)
    if isinstance(comp, Record):
        return Record.fromFileSystem(local_path)
    if isinstance(comp, Video):
        # 清空 cover：源端封面通常是源平台临时路径，跨进程不可达
        return Video.fromFileSystem(local_path)
    if isinstance(comp, File):
        # File 组件无 fromFileSystem 静态方法，直接用构造函数按本地路径重建，
        # 保留原文件名方便目标平台显示。
        name = getattr(comp, "name", None) or ""
        return File(name=name, file=local_path)
    return comp


async def _rebuild_media_component(comp):
    """把媒体组件重新下载到本进程临时目录并以 fromFileSystem 重建。

    解决跨会话转发时，组件内嵌的 file/cover 是源端临时路径、目标端不可达，导致 ENOENT / FileNotFoundError 的问题。失败时退化为 Plain 占位文本。"""
    try:
        local_path = await comp.convert_to_file_path()
        if not local_path:
            return comp
        if isinstance(comp, Image):
            return Image.fromFileSystem(local_path)
        if isinstance(comp, Record):
            return Record.fromFileSystem(local_path)
        if isinstance(comp, Video):
            # 清空 cover：源端封面通常是源平台临时路径，跨进程不可达
            return Video.fromFileSystem(local_path)
        if isinstance(comp, File):
            # 保留原文件名（如存在），方便目标平台显示
            name = getattr(comp, "name", None) or ""
            try:
                return File.fromFileSystem(local_path, name=name) if name else File.fromFileSystem(local_path)
            except TypeError:
                # 旧版本 File.fromFileSystem 不接受 name 关键字
                return File.fromFileSystem(local_path)
        return comp
    except Exception as e:
        comp_type = getattr(getattr(comp, "type", None), "value", None) or type(comp).__name__
        logger.warning(f"⚠️ 转发时重下载媒体失败（{comp_type}），将以占位文本代替：{e}")
        return Plain(text=f"[{comp_type}转发失败：源文件不可达]")


async def _prepare_chain_for_forward(chain):
    """转发前对消息链做「本地化」预处理，返回新的可安全跨会话发送的链。"""
    if not chain:
        return chain
    prepared = []
    for comp in chain:
        if isinstance(comp, (Image, Record, Video, File)):
            prepared.append(await _rebuild_media_component(comp))
        else:
            prepared.append(comp)
    return prepared


async def _prepare_chain_fallback(chain, use_proxy: bool = False, proxy_url: str | None = None):
    """把远程 URL 媒体下载到本地（内部先正常网络、失败再 IPv4），作为转发失败后的兜底链。

    仅处理引用远程 http(s) URL 的媒体组件（图片/语音/视频/文件），下载失败降级为
    Plain 占位文本；本地文件/base64 与非媒体组件原样保留。与 _prepare_chain_for_forward
    的区别：后者走 AstrBot 核心 download_file，本函数自带「正常网络 → 强制 IPv4」的
    兜底下载器，用于规避宿主机 IPv6 无默认路由 / DNS no-data 时核心 download_file
    连接远程源报 `Cannot connect ... ssl:default [None]`（aio-libs/aiohttp#9447）的问题。
    代理三态：use_proxy=False 直连；use_proxy=True 且 proxy_url 空走系统代理；非空走该地址。
    """
    if not chain:
        return chain
    prepared = []
    for comp in chain:
        remote_url = _extract_remote_url(comp) if isinstance(comp, (Image, Record, Video, File)) else None
        if remote_url:
            comp_type = _comp_type_name(comp)
            try:
                local_path = await _download_url_to_local(comp, remote_url, use_proxy=use_proxy, proxy_url=proxy_url)
                prepared.append(_rebuild_from_local_path(comp, local_path))
            except Exception as e:
                logger.warning(f"⚠️ 转发失败后本地化媒体失败（{comp_type}），将以占位文本代替：{e}")
                prepared.append(Plain(text=f"[{comp_type}转发失败：源文件不可达]"))
        else:
            prepared.append(comp)
    return prepared


def _sanitize_chain_for_forward(chain):
    """转发前清洗 @ 提及组件：只保留 @全体（all）与纯数字目标。

    跨会话转发时，源会话的 @ 目标（QQ号 / openid / uid）在目标会话通常无法解析；
    若原样透传，目标平台（如 OneBot/NapCat）会用空 uid 查询群成员，
    内核调用超时导致整个转发失败（retcode=1200 invoke timeout）。
    空目标直接丢弃；非数字目标（如 openid、"qq_official"）降级为纯文本 @昵称。"""
    if not chain:
        return chain
    cleaned = []
    for comp in chain:
        if not isinstance(comp, At):
            cleaned.append(comp)
            continue
        qq = getattr(comp, "qq", None)
        qq_str = str(qq).strip() if qq is not None else ""
        if qq_str == "all" or (qq_str.isdigit() and qq_str != "0"):
            cleaned.append(comp)
            continue
        name = (getattr(comp, "name", "") or "").strip()
        if name:
            cleaned.append(Plain(text=f"@{name}"))
            logger.info(f"⚠️ 转发时 @ 目标({qq_str!r})无法解析，已降级为文本 @{name}")
        else:
            logger.warning(f"⚠️ 转发时丢弃无效的 @ 目标: {qq_str!r}")
    return cleaned


def load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("❌ 文件不存在！本次创建空 JSON！")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"❌ 文件 {path} 不是有效 JSON: {e}")
        raise ValueError(f"❌ 文件 {path} 不是有效 JSON: {e}") from e
    except OSError as e:
        logger.error(f"❌ 读取文件 {path} 失败: {e}")
        raise RuntimeError(f"❌ 读取文件 {path} 失败: {e}") from e
    except Exception as e:
        logger.error(f"❌ 发生预期外的 JSON 读取错误: {e}！")
        raise RuntimeError(f"❌ 发生预期外的 JSON 读取错误: {e}！")


def save_json(path: Path, data: dict):
    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except OSError as e:
        logger.error(f"❌ 写入文件 {path} 失败: {e}")
        raise RuntimeError(f"❌ 写入文件 {path} 失败: {e}") from e
    except TypeError as e:
        logger.error(f"❌ 数据无法序列化为 JSON: {e}")
        raise ValueError(f"❌ 数据无法序列化为 JSON: {e}") from e
    except Exception as e:
        logger.error(f"❌ 发生预期外的 JSON 写入错误: {e}")
        raise RuntimeError(f"❌ 发生预期外的 JSON 写入错误: {e}") from e


def gen_code(n=6):
    # 使用 secrets 模块生成更安全的随机字符串
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(n))



# ------------------------
# 存储层（无锁简化）
# ------------------------
class MsgForwardStore:
    def __init__(self, pending_file: Path):
        self.pending_file = pending_file
        self._ensure_files()

    def _ensure_files(self):
        if not self.pending_file.exists():
            self.pending_file.write_text("{}", encoding="utf-8")

    # ----- pending -----
    def load_pending(self):
        return load_json(self.pending_file)

    def save_pending(self, data: dict):
        save_json(self.pending_file, data)

    def add_pending(self, code: str, source_umo: str):
        p = self.load_pending()
        p[code] = source_umo
        self.save_pending(p)

    def pop_pending(self, code: str):
        p = self.load_pending()
        if code not in p:
            raise KeyError("绑定码不存在或已使用")
        source_umo = p.pop(code)
        self.save_pending(p)
        return source_umo


# ------------------------
# 插件主体
# ------------------------
class MsgForward(star.Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        self.data_dir = star.StarTools.get_data_dir("msg_forward_cc")
        self.pending_file = self.data_dir / "pending.json"

        self.store = MsgForwardStore(self.pending_file)

        # 冷却计时器：key = "source_umo|target_umo"，value = 冷却结束时间戳
        self._cooldowns: dict[str, float] = {}

        # 发送队列：FIFO 队列，队列间隔 > 0 时消息不立即转发，而是由后台 worker
        # 每隔设定秒数依次发送一条（与「冷却」的丢弃语义互补）
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._queue_worker_task: asyncio.Task | None = None

        # 迁移旧版 list 存储的 UMO 字段 → 每行一条的文本（修复 WebUI 校验失败）
        self._migrate_legacy_umo_lists()

    def _migrate_legacy_umo_lists(self):
        """把旧版 list 类型存储的 source_umo / target_umo 迁移为每行一条的 text。

        旧版 schema 中这两个字段是 list 类型，存量规则里可能是 ["umo1", "umo2"] 数组；
        现 schema 为 text（每行一条），AstrBot 在 WebUI 保存时校验会因 list 值报
        「期望是 string, 得到了 list」导致无法保存。这里在启动时一次性转换并持久化。
        """
        try:
            rules = self.config.get("rules", [])
            if not isinstance(rules, list) or not rules:
                return
            changed = False
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                for key in ("source_umo", "target_umo"):
                    val = rule.get(key)
                    if isinstance(val, list):
                        rule[key] = "\n".join(str(x).strip() for x in val if str(x).strip())
                        changed = True
            if changed:
                self.config.save_config()
                logger.info("✅ 已将旧版列表格式的 source_umo/target_umo 迁移为每行一条的文本格式")
        except Exception as e:
            logger.warning(f"⚠️ UMO 字段迁移失败（不影响正常运行）：{e}")

    def _format_origin_header(self, event: AstrMessageEvent, umo: str) -> str:
        try:
            _, msg_type, conversation_id = umo.split(":", 2)
        except ValueError:
            msg_type = "Unknown"
            conversation_id = "Unknown"

        source_platform = event.get_platform_name()
        sender_name = event.get_sender_name()
        sender_id = event.get_sender_id()

        # 平台友好名称（从配置读取，合并默认值）
        default_map = {
            "default": "默认",
            "aiocqhttp": "QQ",
            "qq_official": "QQ官方机器人",
            "qq_official_webhook": "QQ官方机器人(Webhook)",
            "telegram": "Telegram",
            "weixin_oc": "个人微信",
            "wecom": "企业微信",
            "weixin_official_account": "微信公众号",
            "lark": "飞书",
            "dingtalk": "钉钉",
            "discord": "Discord",
            "kook": "KOOK",
            "slack": "Slack",
            "vocechat": "VoceChat",
            "line": "LINE",
            "satori": "Satori",
            "matrix": "Matrix",
            "mattermost": "Mattermost",
            "misskey": "Misskey",
            "wecom_ai_bot": "企微AI机器人",
        }
        platform_map = self.config.get("platform_name_map", {}) or {}
        default_map.update(platform_map)
        source_platform_human = default_map.get(source_platform, source_platform)

        # 消息类型友好名称
        if msg_type == "GroupMessage":
            msg_type_human = "群组"
        elif msg_type == "FriendMessage":
            msg_type_human = "私聊"
        else:
            msg_type_human = "未知类型"

        # 使用配置中的模板
        template = self.config.get("header_template", "").strip()
        if template:
            header = template.format(
                sender_name=sender_name,
                sender_id=sender_id,
                platform=source_platform_human,
                msg_type=msg_type_human,
                conversation_id=conversation_id,
            )
        else:
            header = (
                f"[转发] {sender_name} ({sender_id})\n"
                f"来自 {source_platform_human} 的 {msg_type_human}（ID: {conversation_id}）消息"
            )

        return header

    @staticmethod
    def _umo_list(rule: dict, key: str) -> list:
        """把规则中的 UMO 字段统一归一化为列表。

        兼容三种存储格式：text 按行拆分（每行一条）、list 列表、单字符串。"""
        val = rule.get(key)
        if not val:
            return []
        if isinstance(val, str):
            # 单字符串或多行 text 均按行拆分（单个 UMO 无换行，拆出单项）
            return [x.strip() for x in val.splitlines() if x.strip()]
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
        return []

    @staticmethod
    def _rule_name(rule: dict) -> str:
        """规则展示名称：优先取自定义备注 remark，留空则回退为 source_umo → target_umo。"""
        remark = (rule.get("remark") or "").strip()
        if remark:
            return remark
        src = ", ".join(MsgForward._umo_list(rule, "source_umo")) or "?"
        dst = ", ".join(MsgForward._umo_list(rule, "target_umo")) or "?"
        return f"{src} → {dst}"

    async def initialize(self):
        self._queue_worker_task = asyncio.create_task(self._queue_worker())
        logger.info("MsgForward plugin init OK")

    @filter.command_group("mf")
    def mf(self):
        """mf 命令组"""
        pass

    @mf.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        yield event.plain_result(
            "📋 MsgForward 帮助\n\n"
            "/mf add           创建一则转发绑定请求\n"
            "/mf bind <绑定码>     接受一则转发绑定请求\n"
            "/mf bindraw [源平台] <源ID> [目标平台] <目标ID>\n"
            "                  直接创建转发绑定，省略默认平台为default。平台简写：df/qq/wx/tg/dc，加s为私聊\n"
            "                  例：/mf bindraw 654321 wx 123456\n"
            "                  例：/mf bindraw dfs 114514 wx 123456s（私聊）\n"
            "/mf del <编号>    删除一条转发规则\n"
            "/mf list          列出当前会话的转发规则（含群号）\n"
            "/mf listall       列出所有转发规则\n"
            "/mf hide <编号>   切换规则来源信息显示/隐藏\n"
            "/mf toggle <编号>  启用/停用一条转发规则\n"
            "/mf hidelist      列出当前会话规则的来源信息状态\n"
            "/mf hidelistall   列出所有规则的来源信息状态\n"
            "/mf filter        查看当前过滤与冷却配置\n"
            "/mf help          显示此帮助\n\n"
            "冷却转发：在规则配置中设置 cooldown_seconds > 0\n"
            "转发一次后在该时间内不会再次转发，避免刷屏。\n\n"
            "发送队列：在规则配置中设置 queue_interval_seconds > 0\n"
            "匹配的消息进入队列，每隔该秒数转发一条。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("add")
    async def cmd_add(self, event: AstrMessageEvent):
        """创建一则消息转发绑定的请求"""
        code = gen_code()
        source_umo = str(event.unified_msg_origin)
        self.store.add_pending(code, source_umo)

        yield event.plain_result(
            f"📌 已创建绑定请求\n"
            f"请在目标会话执行：/mf bind {code}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("bind")
    async def cmd_bind(self, event: AstrMessageEvent, code: str):
        """接受一则消息转发绑定的请求"""
        try:
            target_umo = str(event.unified_msg_origin)
            source_umo = self.store.pop_pending(code)
            hide_header = self.config.get("default_hide_header", False)

            rules = list(self.config.get("rules", []))
            rules.append({
                "__template_key": "rule",
                "remark": f"规则 #{len(rules) + 1}",
                "source_umo": source_umo,
                "target_umo": target_umo,
                "hide_header": hide_header,
                "enabled": True,
            })
            self.config["rules"] = rules
            self.config.save_config()

            idx = len(rules)
            yield event.plain_result(f"✅ 已绑定 #{idx}\n{source_umo} → {target_umo}")
        except Exception as e:
            yield event.plain_result(f"❌ 绑定失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("bindraw")
    async def cmd_bindraw(self, event: AstrMessageEvent, args: str = ""):
        """直接创建转发绑定（格式：/mf bindraw 平台 群号 平台 群号）"""
        PLATFORM_MAP = {
            "df": "default",
            "qq": "aiocqhttp",
            "wx": "weixin_oc",
            "tg": "telegram",
            "dc": "discord",
        }

        def build_umo(plat: str, uid: str) -> str:
            plat_lower = plat.lower()
            msg_type = "FriendMessage" if plat_lower.endswith("s") else "GroupMessage"
            plat_key = plat_lower[:-1] if plat_lower.endswith("s") else plat_lower
            if not plat_key or plat_key == "default":
                plat_key = "default"
            # 大于 3 个字母的平台名直接作为完整平台标识使用（如 aiocqhttp、weixin_oc）
            if len(plat_key) > 3:
                platform = plat_key
            else:
                platform = PLATFORM_MAP.get(plat_key, plat_key)
            # 兼容在 ID 末尾加 s 表示私聊（如 /mf bindraw 654321 123456s）
            if uid.endswith("s") and msg_type == "GroupMessage" and plat_lower == plat_key:
                msg_type = "FriendMessage"
                uid = uid[:-1]
            return f"{platform}:{msg_type}:{uid}"

        try:
            raw = (event.message_str or "").strip()
            idx = raw.lower().find("bindraw")
            args_str = raw[idx + len("bindraw"):].strip() if idx != -1 else (args or "")
            parts = args_str.split()
            if len(parts) == 2:
                src_plat, dst_plat = "default", "default"
                src_id, dst_id = parts[0], parts[1]
            elif len(parts) == 3:
                if parts[0].isdigit():
                    src_plat = "default"
                    src_id, dst_plat, dst_id = parts
                else:
                    src_plat, src_id, dst_id = parts
                    dst_plat = "default"
            elif len(parts) == 4:
                src_plat, src_id, dst_plat, dst_id = parts
            else:
                yield event.plain_result("❌ 格式错误，用法：/mf bindraw [源平台] 源ID [目标平台] 目标ID\n例：/mf bindraw 654321 wx 123456（省略源平台=default）")
                return
            source_umo = build_umo(src_plat, src_id)
            target_umo = build_umo(dst_plat, dst_id)
            hide_header = self.config.get("default_hide_header", False)

            rules = list(self.config.get("rules", []))
            rules.append({
                "__template_key": "rule",
                "remark": f"规则 #{len(rules) + 1}",
                "source_umo": source_umo,
                "target_umo": target_umo,
                "hide_header": hide_header,
                "enabled": True,
            })
            self.config["rules"] = rules
            self.config.save_config()

            idx = len(rules)
            yield event.plain_result(f"✅ 已绑定 #{idx}\n{source_umo} → {target_umo}")
        except Exception as e:
            yield event.plain_result(f"❌ 直接绑定失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("del")
    async def cmd_del(self, event: AstrMessageEvent, rid: str):
        """删除一条转发规则（规则编号从 /mf list 查看）"""
        try:
            rules = list(self.config.get("rules", []))
            idx = int(rid) - 1
            if idx < 0 or idx >= len(rules):
                yield event.plain_result(f"❌ 规则 #{rid} 不存在")
                return
            removed = rules.pop(idx)
            self.config["rules"] = rules
            self.config.save_config()
            yield event.plain_result(
                f"🗑️ 已删除规则 #{rid}（{self._rule_name(removed)}）"
            )
        except Exception as e:
            yield event.plain_result(f"❌ 删除失败: {e}")

    @mf.command("list")
    async def cmd_list(self, event: AstrMessageEvent):
        """列出与当前会话相关的所有转发规则"""
        source_umo = str(event.unified_msg_origin)
        rules = self.config.get("rules", [])
        matched = [(idx, r) for idx, r in enumerate(rules, start=1)
                   if source_umo in MsgForward._umo_list(r, "source_umo")]
        if not matched:
            yield event.plain_result(f"📭 当前会话 {source_umo} 没有规则")
            return

        lines = [f"📜 当前会话({source_umo}) 的规则："]
        for idx, r in matched:
            en_status = "🟢" if r.get("enabled", True) else "⛔"
            hide_status = "🔒" if r.get("hide_header", False) else "🔓"
            cd = r.get("cooldown_seconds") or self.config.get("default_cooldown_seconds", 0)
            cd_str = f"❄{cd}s" if int(cd) > 0 else ""
            qi = self._queue_interval_for(r)
            qi_str = f"⏳{qi}s" if qi > 0 else ""
            lines.append(f"{en_status} #{idx} {self._rule_name(r)} {hide_status} {cd_str} {qi_str}".strip())
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("hide")
    async def cmd_hide_header(self, event: AstrMessageEvent, rid: str):
        """切换规则的来源信息显示状态（隐藏/显示）"""
        try:
            rules = list(self.config.get("rules", []))
            idx = int(rid) - 1
            if idx < 0 or idx >= len(rules):
                yield event.plain_result(f"❌ 规则 #{rid} 不存在")
                return

            current = rules[idx].get("hide_header", False)
            rules[idx]["hide_header"] = not current
            self.config["rules"] = rules
            self.config.save_config()

            status = "隐藏" if not current else "显示"
            yield event.plain_result(f"✅ 规则 #{rid}（{self._rule_name(rules[idx])}）来源信息已{status}")
        except Exception as e:
            yield event.plain_result(f"❌ 操作失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("toggle")
    async def cmd_toggle(self, event: AstrMessageEvent, rid: str):
        """切换规则的启用状态（启用/停用）"""
        try:
            rules = list(self.config.get("rules", []))
            idx = int(rid) - 1
            if idx < 0 or idx >= len(rules):
                yield event.plain_result(f"❌ 规则 #{rid} 不存在")
                return

            current = rules[idx].get("enabled", True)
            rules[idx]["enabled"] = not current
            self.config["rules"] = rules
            self.config.save_config()

            status = "已启用" if not current else "已停用"
            yield event.plain_result(f"✅ 规则 #{rid}（{self._rule_name(rules[idx])}）{status}")
        except Exception as e:
            yield event.plain_result(f"❌ 操作失败：{e}")

    @mf.command("hidelist")
    async def cmd_header_status(self, event: AstrMessageEvent):
        """列出当前会话规则的来源信息显示状态（允许：显示来源，禁止：隐藏来源）"""
        source_umo = str(event.unified_msg_origin)
        rules = self.config.get("rules", [])
        matched = [(idx, r) for idx, r in enumerate(rules, start=1)
                   if source_umo in MsgForward._umo_list(r, "source_umo")]
        if not matched:
            yield event.plain_result("📭 当前会话没有规则")
            return

        allowed = []
        blocked = []

        for idx, r in matched:
            if r.get("hide_header", False):
                blocked.append(f"#{idx} {self._rule_name(r)}")
            else:
                allowed.append(f"#{idx} {self._rule_name(r)}")

        lines = [f"📋 当前会话({source_umo}) 来源信息状态："]
        if allowed:
            lines.append("\n✅ 允许显示来源：")
            lines.extend(allowed)
        if blocked:
            lines.append("\n🔒 禁止显示来源：")
            lines.extend(blocked)

        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("hidelistall")
    async def cmd_header_status_all(self, event: AstrMessageEvent):
        """查看所有规则的来源信息显示状态（允许：显示来源，禁止：隐藏来源）"""
        rules = self.config.get("rules", [])
        if not rules:
            yield event.plain_result("📭 暂无规则")
            return

        allowed = []
        blocked = []

        for idx, r in enumerate(rules, start=1):
            if r.get("hide_header", False):
                blocked.append(f"#{idx} {self._rule_name(r)}")
            else:
                allowed.append(f"#{idx} {self._rule_name(r)}")

        lines = ["📋 所有规则来源信息状态："]
        if allowed:
            lines.append("\n✅ 允许显示来源：")
            lines.extend(allowed)
        if blocked:
            lines.append("\n🔒 禁止显示来源：")
            lines.extend(blocked)

        yield event.plain_result("\n".join(lines))

    @mf.command("listall")
    async def cmd_list_all(self, event: AstrMessageEvent):
        """列出所有转发规则"""
        rules = self.config.get("rules", [])
        if not rules:
            yield event.plain_result("📭 暂无规则")
            return

        lines = ["📜 所有转发规则："]
        for idx, r in enumerate(rules, start=1):
            en_status = "🟢" if r.get("enabled", True) else "⛔"
            hide_status = "🔒" if r.get("hide_header", False) else "🔓"
            cd = r.get("cooldown_seconds") or self.config.get("default_cooldown_seconds", 0)
            cd_str = f"❄{cd}s" if int(cd) > 0 else ""
            qi = self._queue_interval_for(r)
            qi_str = f"⏳{qi}s" if qi > 0 else ""
            lines.append(
                f"{en_status} #{idx} {self._rule_name(r)} {hide_status} {cd_str} {qi_str}".strip()
            )
        yield event.plain_result("\n".join(lines))

    @mf.command("filter")
    async def cmd_filter_list(self, event: AstrMessageEvent):
        """查看当前的过滤配置"""
        filter_mode = self.config.get("filter_mode", "off")
        patterns_data = MsgForward._unwrap_patterns(self.config.get("filter_patterns"))

        mode_text = {"off": "关闭", "blacklist": "黑名单", "whitelist": "白名单"}.get(filter_mode, filter_mode)
        lines = [f"📋 全局过滤：{mode_text}" + (f"（共 {len(patterns_data)} 条）" if patterns_data else "")]

        if filter_mode == "off":
            lines.append("      （关闭，未启用过滤）")
        elif not patterns_data:
            lines.append(f"      （已启用但未配置过滤规则）")
        else:
            for i, item in enumerate(patterns_data, start=1):
                tp, val = MsgForward._parse_filter_item(item)
                tag = "[正]" if tp == "regex" else "[关]"
                lines.append(f"      {tag} {i}. {val}")

        # 显示各规则的单独过滤配置
        rules = self.config.get("rules", [])
        has_per_rule = False
        for idx, r in enumerate(rules, start=1):
            rfm = r.get("filter_mode", "inherit")
            rfp = r.get("filter_patterns", [])
            if rfm != "inherit" or (rfp and len(rfp) > 0):
                if not has_per_rule:
                    lines.append(f"\n📋 规则级过滤（共 {len(rules)} 条规则）：")
                    has_per_rule = True
                rm_text = {"off": "关闭", "blacklist": "黑名单", "whitelist": "白名单"}.get(rfm, "继承全局") if rfm != "inherit" else "继承全局"
                lines.append(f"  #{idx} | {self._rule_name(r)} | {rm_text}")
                if rfp:
                    for j, item in enumerate(rfp, start=1):
                        tp, val = MsgForward._parse_filter_item(str(item))
                        tag = "[正]" if tp == "regex" else "[关]"
                        lines.append(f"      {tag} {j}. {val}")

        if not has_per_rule:
            lines.append("（所有规则使用全局过滤配置）")

        # 显示冷却配置
        default_cd = self.config.get("default_cooldown_seconds", 0)
        cd_desc = f"{default_cd}s" if int(default_cd) > 0 else "关闭"
        lines.append(f"\n📋 转发冷却：全局默认 ❄{cd_desc}")
        for idx, r in enumerate(rules, start=1):
            cd = r.get("cooldown_seconds")
            if cd is not None and int(cd) > 0:
                lines.append(f"  #{idx} | {self._rule_name(r)} | ❄{cd}s")
            elif cd is not None and int(cd) == 0:
                lines.append(f"  #{idx} | {self._rule_name(r)} | ❄关闭")

        # 显示发送队列配置
        default_qi = self.config.get("default_queue_interval_seconds", 0)
        qi_desc = f"{default_qi}s" if int(default_qi) > 0 else "关闭"
        max_size = int(self.config.get("queue_max_size", 0) or 0)
        max_desc = f"（上限 {max_size} 条）" if max_size > 0 else "（无上限）"
        lines.append(f"\n📋 发送队列：全局默认 ⏳{qi_desc}{max_desc}")
        for idx, r in enumerate(rules, start=1):
            qi = r.get("queue_interval_seconds")
            if qi is not None and int(qi) > 0:
                lines.append(f"  #{idx} | {self._rule_name(r)} | ⏳{qi}s")
            elif qi is not None and int(qi) == 0:
                lines.append(f"  #{idx} | {self._rule_name(r)} | ⏳关闭")

        yield event.plain_result("\n".join(lines))

    def _should_forward(self, event: AstrMessageEvent, rule: dict = None) -> bool:
        """根据过滤规则判断是否应该转发此消息，优先使用规则级配置"""
        # 确定生效的过滤模式和规则列表
        if rule:
            fm = rule.get("filter_mode", "inherit")
            if fm == "inherit":
                fm = self.config.get("filter_mode", "off")
            rfp = rule.get("filter_patterns")
            if rfp and len(rfp) > 0:
                fp = rfp
            else:
                fp = MsgForward._unwrap_patterns(self.config.get("filter_patterns"))
        else:
            fm = self.config.get("filter_mode", "off")
            fp = MsgForward._unwrap_patterns(self.config.get("filter_patterns"))

        if fm == "off":
            return True

        fp = [x.strip() for x in fp if x.strip()]
        if not fp:
            return True

        msg_text = event.message_str
        msg_lower = msg_text.lower()

        for item in fp:
            item_type, item_val = self._parse_filter_item(item)
            if not item_val:
                continue
            if item_type == "keyword":
                if item_val.lower() in msg_lower:
                    return fm == "whitelist"
            else:
                if re.search(item_val, msg_text):
                    return fm == "whitelist"

        return fm == "blacklist"

    @staticmethod
    def _parse_filter_item(item: str):
        """解析一条过滤规则，返回 (type, value)"""
        s = item.strip()
        if s.startswith("regex:"):
            return "regex", s[6:].strip()
        return "keyword", s

    @staticmethod
    def _unwrap_patterns(patterns):
        """将全局 filter_patterns 统一转为字符串列表（兼容 text 和 template_list 格式）"""
        if not patterns:
            return []
        if isinstance(patterns, str):
            return [x.strip() for x in patterns.splitlines() if x.strip()]
        if isinstance(patterns, list):
            return [item.get("rule", "").strip() for item in patterns
                    if isinstance(item, dict) and item.get("rule", "").strip()]
        return []

    def _should_download_media(self, rule: dict) -> bool:
        """判断某条规则是否需要在发送前先把媒体下载到本地。

        规则显式设置为 true/false 时按规则值决定；inherit（或未设置）时继承全局配置。
        兼容旧版 bool 存储（True/False）。"""
        val = rule.get("download_media_before_send")
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            if val == "true":
                return True
            if val == "false":
                return False
            # "inherit" 或其他值 → 继承全局
        return bool(self.config.get("download_media_before_send", False))

    def _queue_interval_for(self, rule: dict) -> int:
        """解析某条规则生效的发送队列间隔（秒）。

        规则未设置（键不存在）时继承全局 default_queue_interval_seconds；
        显式设置为 0 时关闭队列（立即转发），语义与冷却字段一致。"""
        val = rule.get("queue_interval_seconds")
        if val is None:
            val = self.config.get("default_queue_interval_seconds", 0)
        try:
            return int(val) if val else 0
        except (TypeError, ValueError):
            return 0

    def _enqueue_send(self, target: str, result: MessageEventResult, interval: int,
                      sanitized_chain, header_text: str, has_remote_media: bool,
                      use_proxy: bool, proxy_url):
        """把一次转发任务加入发送队列，交由后台 worker 按间隔依次发送。

        同时保存兜底所需的信息，供发送失败时在 worker 内本地化媒体后重试。
        若队列已达上限（queue_max_size > 0），则拒绝入队并记录警告。"""
        max_size = self.config.get("queue_max_size", 0)
        if max_size > 0 and self._send_queue.qsize() >= max_size:
            logger.warning(
                f"⚠️ 发送队列已满（上限 {max_size}），丢弃消息 → {target}"
            )
            return
        self._send_queue.put_nowait({
            "target": target,
            "result": result,
            "interval": max(0, interval),
            "sanitized_chain": sanitized_chain,
            "header_text": header_text,
            "has_remote_media": has_remote_media,
            "use_proxy": use_proxy,
            "proxy_url": proxy_url,
        })

    async def _queue_worker(self):
        """后台发送队列消费者：逐条发送，每发一条后按该条间隔休眠再发下一条。

        单条消息发送失败（含异常）只记录日志、不影响后续消息；整体用外层兜底，
        确保 worker 永不因单条消息或意外异常而退出，避免队列永久卡住。"""
        while True:
            try:
                item = await self._send_queue.get()
                try:
                    await self._send_queued_item(item)
                except Exception as e:
                    logger.error(f"❌ 队列发送异常: {e}")
                finally:
                    self._send_queue.task_done()
                interval = item.get("interval", 0)
                if interval > 0:
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                # 插件终止（terminate 调用 task.cancel()）时正常退出，其余情况不让 worker 挂掉
                raise
            except Exception as e:
                logger.error(f"❌ 队列 worker 异常，已恢复继续运行: {e!r}")
                await asyncio.sleep(1)

    async def _send_queued_item(self, item: dict):
        """发送队列中的单条消息，失败时复用与立即转发一致的本地化兜底逻辑。"""
        target = item["target"]
        result = item["result"]
        try:
            await self.context.send_message(target, result)
        except ValueError as e:
            logger.error(f"❌ 不合法的 session 字符串，转发失败: {e}")
        except Exception as e:
            if item.get("has_remote_media"):
                try:
                    localized = await _prepare_chain_fallback(
                        item["sanitized_chain"],
                        use_proxy=item["use_proxy"],
                        proxy_url=item["proxy_url"],
                    )
                    fb_chain = localized if not item["header_text"] else \
                        [Plain(text=item["header_text"])] + localized
                    await self.context.send_message(target, MessageEventResult(chain=fb_chain))
                    logger.warning(f"⚠️ 队列转发首次失败（{e}），已本地化媒体后重试成功")
                except Exception as e2:
                    logger.error(f"❌ 队列转发失败（本地化重试后仍失败）: {e2}")
            else:
                logger.error(f"❌ 队列转发失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def forward_message(self, event: AstrMessageEvent):
        """主转发逻辑"""
        try:
            source_umo = str(event.unified_msg_origin)
            rules = [r for r in self.config.get("rules", [])
                     if source_umo in MsgForward._umo_list(r, "source_umo")]
            if not rules:
                return

            raw_chain = event.get_messages()
            # 清洗无效的 @ 提及，避免目标平台用空 uid 查询群成员导致超时（retcode=1200）
            sanitized_chain = _sanitize_chain_for_forward(raw_chain)
            # 开启 download_media_before_send 的规则本地化链，惰性构建一次供多条规则复用
            prepared_chain = None
            now = time.time()

            for idx, rule in enumerate(rules):
                targets = MsgForward._umo_list(rule, "target_umo")
                if not targets:
                    continue
                # 规则启用开关：关闭的规则直接跳过（默认启用，兼容旧版规则）
                if not rule.get("enabled", True):
                    continue
                # 逐规则过滤检查
                if not self._should_forward(event, rule):
                    continue

                # 冷却检查
                cooldown_sec = rule.get("cooldown_seconds")
                if cooldown_sec is None:
                    cooldown_sec = self.config.get("default_cooldown_seconds", 0)
                cooldown_sec = int(cooldown_sec) if cooldown_sec else 0

                # 发送队列间隔：> 0 时消息进入队列，由后台 worker 每隔该秒数发送一条
                queue_interval = self._queue_interval_for(rule)

                # 主消息链：默认透传（正常网络，媒体交给目标端自行处理）；
                # 开启 download_media_before_send 时，发送前先本地化（原逻辑不变）。
                if self._should_download_media(rule):
                    if prepared_chain is None:
                        prepared_chain = await _prepare_chain_for_forward(sanitized_chain)
                    message_chain = prepared_chain
                else:
                    message_chain = sanitized_chain

                # 来源头（hide_header 时为空，不前置）
                header_text = ""
                if not rule.get("hide_header", False):
                    header_text = self._format_origin_header(event, source_umo) + "\n\n\u200b"
                new_chain = message_chain if not header_text else [Plain(text=header_text)] + message_chain

                # 是否含远程 URL 媒体（决定失败时是否值得本地化后重试）
                has_remote_media = any(_extract_remote_url(c) for c in sanitized_chain)
                # 规则级代理三态：use_proxy 关→直连；开且 proxy_url 空→系统代理；开且非空→该地址
                use_proxy = bool(rule.get("use_proxy", False))
                proxy_url = (rule.get("proxy_url") or "").strip() or None
                fallback_chain = None

                # 逐目标发送：一个目标失败不影响其他目标（冷却按 源|目标 对记录）
                for target in targets:
                    # 队列模式：不立即发送，也不做冷却，统一交给后台 worker 按间隔依次转发
                    if queue_interval > 0:
                        self._enqueue_send(
                            target, event.chain_result(new_chain), queue_interval,
                            sanitized_chain, header_text, has_remote_media,
                            use_proxy, proxy_url,
                        )
                        continue
                    if cooldown_sec > 0:
                        cd_key = f"{source_umo}|{target}"
                        cd_end = self._cooldowns.get(cd_key, 0)
                        if now < cd_end:
                            continue
                    try:
                        await self.context.send_message(target, event.chain_result(new_chain))
                        # 转发成功后设置冷却
                        if cooldown_sec > 0:
                            self._cooldowns[cd_key] = now + cooldown_sec
                    except ValueError as e:
                        logger.error(f"❌ 不合法的 session 字符串，转发失败: {e}")
                    except Exception as e:
                        # 自动降级：先用正常网络（上面已尝试），失败后把远程媒体下载到
                        # 本地（下载器内部也是先正常网络、失败再 IPv4）再重试一次；仍失败才记录错误。
                        if has_remote_media:
                            if fallback_chain is None:
                                localized = await _prepare_chain_fallback(sanitized_chain, use_proxy=use_proxy, proxy_url=proxy_url)
                                fallback_chain = localized if not header_text else [Plain(text=header_text)] + localized
                            try:
                                await self.context.send_message(target, event.chain_result(fallback_chain))
                                logger.warning(f"⚠️ 转发首次失败（{e}），已本地化媒体后重试成功")
                                if cooldown_sec > 0:
                                    self._cooldowns[cd_key] = now + cooldown_sec
                            except Exception as e2:
                                logger.error(f"❌ 转发失败（本地化重试后仍失败）: {e2}")
                        else:
                            logger.error(f"❌ 转发失败: {e}")

        except Exception as e:
            logger.error(f"❌ 转发逻辑异常: {e}")

    async def terminate(self):
        if self._queue_worker_task is not None:
            self._queue_worker_task.cancel()
        logger.info("MsgForward plugin terminated")
