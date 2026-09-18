import asyncio
import json
import os
import re
import secrets
import shutil
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
    tmp_dir = _get_media_cache_dir()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=suffix, dir=str(tmp_dir))
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _rebuild_from_local_path(comp, local_path: str, keep_local: bool = False):
    """按本地文件路径重建组件（fromFileSystem）。

    注意：File/Video 重建时优先使用原始 URL（NapCat 通过 URL 下载），而非本地路径
    （NapCat 读不到本地路径会报 retcode=1200 '路径不存在'）。
    本地文件仅作为下载缓存保留在磁盘上。
    队列场景（keep_local=True）：保留本地路径（fromFileSystem / file=本地路径），
    由发送前的 _prepare_chain_for_media_urls 注册 AstrBot 文件服务 token，
    生成短时稳定 URL（跨容器可下载、发送时实时注册不过期）。"""
    if isinstance(comp, Image):
        return Image.fromFileSystem(local_path)
    if isinstance(comp, Record):
        return Record.fromFileSystem(local_path)
    if isinstance(comp, Video):
        # 清空 cover：源端封面通常是源平台临时路径，跨进程不可达
        if keep_local:
            # 保留本地路径（path 字段），发送前注册文件服务 URL
            return Video.fromFileSystem(local_path)
        # 优先保留原始 URL，让目标端（NapCat）通过 URL 下载
        url = getattr(comp, "url", None) or ""
        if url:
            return Video(file=url, url=url)
        return Video.fromFileSystem(local_path)
    if isinstance(comp, File):
        # File 组件优先使用 URL（NapCat 通过 URL 下载），
        # 无 URL 时降级为本地路径（从 local_path 缓存重建）
        name = getattr(comp, "name", None) or ""
        if keep_local:
            # 保留本地路径（file_ 字段），发送前注册文件服务 URL
            return File(name=name, file=local_path)
        url = getattr(comp, "url", None) or ""
        if url:
            return File(name=name, url=url)
        return File(name=name, file=local_path)
    return comp


async def _rebuild_media_component(comp, use_proxy: bool = False, proxy_url: str | None = None):
    """把媒体组件重新下载到本进程临时目录并以 fromFileSystem 重建。

    解决跨会话转发时，组件内嵌的 file/cover 是源端临时路径、目标端不可达，导致 ENOENT / FileNotFoundError 的问题。
    注意：File 组件没有 convert_to_file_path()/fromFileSystem()，等价方法是 get_file() 与构造函数 File(name=..., file=...)。
    如果本地文件不可用（如群文件未下载到本地），尝试从远程 URL 下载。
    全部失败时退化为 Plain 占位文本，绝不向上抛异常影响整条转发。"""
    comp_type = _comp_type_name(comp)
    # 1) 先尝试获取本地文件路径（Image/Record/Video 用 convert_to_file_path；File 用 get_file）
    try:
        if isinstance(comp, File):
            # File 组件没有 convert_to_file_path()，等价方法是 get_file()（异步下载并返回本地路径）
            local_path = await comp.get_file()
        else:
            local_path = await comp.convert_to_file_path()
        if local_path:
            try:
                return _rebuild_from_local_path(comp, local_path)
            except Exception as e:
                logger.warning(f"⚠️ 按本地路径重建媒体失败（{comp_type}），尝试远程 URL 下载：{e}")
        else:
            logger.warning(f"⚠️ 获取文件路径返回空（{comp_type}），尝试远程 URL 下载")
    except Exception as e:
        logger.warning(f"⚠️ 获取文件路径失败（{comp_type}），尝试远程 URL 下载：{e}")

    # 2) 本地文件不可用，尝试从远程 URL 下载后重建
    remote_url = _extract_remote_url(comp)
    if remote_url:
        try:
            local_path = await _download_url_to_local(comp, remote_url, use_proxy=use_proxy, proxy_url=proxy_url)
            try:
                return _rebuild_from_local_path(comp, local_path)
            except Exception as e:
                logger.warning(f"⚠️ 按下载路径重建媒体失败（{comp_type}），将以占位文本代替：{e}")
        except Exception as e:
            logger.warning(f"⚠️ 转发时重下载媒体失败（{comp_type}，本地文件与远程下载均失败），将以占位文本代替：{e}")
    else:
        logger.warning(f"⚠️ 转发时重下载媒体失败（{comp_type}，无远程 URL 且本地文件不可用），将以占位文本代替")
    return Plain(text=f"[{comp_type}转发失败：源文件不可达]")


async def _prepare_chain_for_forward(chain, use_proxy: bool = False, proxy_url: str | None = None):
    """转发前对消息链做「本地化」预处理，返回新的可安全跨会话发送的链。"""
    if not chain:
        return chain
    prepared = []
    for comp in chain:
        if isinstance(comp, (Image, Record, Video, File)):
            prepared.append(await _rebuild_media_component(comp, use_proxy=use_proxy, proxy_url=proxy_url))
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
        if isinstance(comp, (Image, Record, Video, File)):
            remote_url = _extract_remote_url(comp)
            if remote_url:
                comp_type = _comp_type_name(comp)
                try:
                    local_path = await _download_url_to_local(comp, remote_url, use_proxy=use_proxy, proxy_url=proxy_url)
                    prepared.append(_rebuild_from_local_path(comp, local_path))
                except Exception as e:
                    logger.warning(f"⚠️ 转发失败后本地化媒体失败（{comp_type}），将以占位文本代替：{e}")
                    prepared.append(Plain(text=f"[{comp_type}转发失败：源文件不可达]"))
            else:
                # 无远程 URL，尝试本地文件路径兜底（File 用 get_file，其余用 convert_to_file_path）
                try:
                    if isinstance(comp, File):
                        local_path = await comp.get_file()
                    else:
                        local_path = await comp.convert_to_file_path()
                    if local_path:
                        prepared.append(_rebuild_from_local_path(comp, local_path))
                    else:
                        prepared.append(comp)
                except Exception:
                    prepared.append(comp)
        else:
            prepared.append(comp)
    return prepared


async def _prepare_chain_for_queue(chain, use_proxy: bool = False, proxy_url: str | None = None):
    """将消息链中的所有媒体本地化到插件自有临时目录，防止队列延迟后源端文件被清理。

    优先用 AstrBot 核心 convert_to_file_path() 获取本地缓存路径（适配器层已缓存，
    通常瞬间返回）；失败后回退到远程 URL 下载。所有文件复制到 AstrBot data 目录下的
    msg_forward_cc_media 自有目录（持久化，重启不丢），确保队列延迟后仍可访问。
    """
    if not chain:
        return chain
    prepared = []
    tmp_dir = _get_media_cache_dir()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for comp in chain:
        if not isinstance(comp, (Image, Record, Video, File)):
            prepared.append(comp)
            continue
        comp_type = _comp_type_name(comp)
        try:
            # 用 AstrBot 核心获取本地路径（适配器层已缓存，通常很快）
            # 注意：File 组件没有 convert_to_file_path()，等价方法是 get_file()
            if isinstance(comp, File):
                local_path = await comp.get_file()
            else:
                local_path = await comp.convert_to_file_path()
            if local_path and os.path.isfile(local_path):
                suffix = Path(local_path).suffix or ""
                fd, dest = tempfile.mkstemp(suffix=suffix, dir=str(tmp_dir))
                os.close(fd)
                shutil.copy2(local_path, dest)
                # keep_local=True：保留本地路径，发送前由 _prepare_chain_for_media_urls
                # 注册 AstrBot 文件服务 token 生成跨容器可下载的 URL
                prepared.append(_rebuild_from_local_path(comp, dest, keep_local=True))
                continue
        except Exception:
            pass  # convert_to_file_path 失败，尝试远程 URL

        # 回退：远程 URL 下载
        remote_url = _extract_remote_url(comp)
        if remote_url:
            try:
                local_path = await _download_url_to_local(comp, remote_url, use_proxy, proxy_url)
                prepared.append(_rebuild_from_local_path(comp, local_path, keep_local=True))
                continue
            except Exception as e:
                logger.warning(f"⚠️ 队列本地化媒体失败（{comp_type}），将以占位文本代替：{e}")
                prepared.append(Plain(text=f"[{comp_type}转发失败：源文件不可达]"))
                continue

        logger.warning(f"⚠️ 队列本地化媒体失败（{comp_type}），无法获取文件，将以占位文本代替")
        prepared.append(Plain(text=f"[{comp_type}转发失败：源文件不可达]"))
    return prepared


# ------------------------
# AstrBot 内置文件服务（跨容器媒体 URL）
# ------------------------

# 队列媒体缓存目录：AstrBot data 目录下持久化子目录（重启/重载不丢文件）
# 实际路径由插件 __init__ 按 data_dir 设置（StarTools.get_data_dir）
_MEDIA_CACHE_DIR: Path | None = None


def _get_media_cache_dir() -> Path:
    """返回队列媒体缓存目录（AstrBot data 目录下，持久化）。"""
    global _MEDIA_CACHE_DIR
    if _MEDIA_CACHE_DIR is not None:
        return _MEDIA_CACHE_DIR
    return Path(tempfile.gettempdir()) / "msg_forward_cc_media"


def _get_file_service_base_url() -> str:
    """读取 AstrBot 全局配置 callback_api_base（外部访问 AstrBot 的地址）。

    供文件服务 URL 拼接：{callback_api_base}/api/file/{token}。
    未配置时返回空串（文件服务不可用）。"""
    try:
        from astrbot.core import astrbot_config
        base = (astrbot_config.get("callback_api_base") or "").strip().rstrip("/")
        return base
    except Exception:
        return ""


def _file_service_available() -> bool:
    """AstrBot 内置文件服务是否可用（需配置 callback_api_base）。"""
    return bool(_get_file_service_base_url())


async def _register_file_with_service(local_path: str, timeout: float = 3600) -> str | None:
    """把本地文件注册到 AstrBot 文件服务，返回可下载的 URL。

    返回 {callback_api_base}/api/file/{token}；token 单次使用、默认 300s 过期，
    因此需在每次发送前重新注册（本实现用较长 timeout + 发送前注册保证有效）。
    未配置 callback_api_base 或注册失败时返回 None。"""
    base = _get_file_service_base_url()
    if not base:
        return None
    try:
        from astrbot.core import file_token_service
        token = await file_token_service.register_file(local_path, timeout=timeout)
        return f"{base}/api/file/{token}"
    except Exception as e:
        logger.warning(f"⚠️ 注册媒体文件服务失败（{local_path}）：{e}")
        return None


def _local_media_path_of(comp) -> str | None:
    """提取媒体组件本地化的文件路径；无本地文件返回 None（序列化时存的是本地路径）。"""
    try:
        if isinstance(comp, Video):
            # keep_local 场景下 file 是 file:// URI 或本地路径
            cand = getattr(comp, "file", "") or ""
        elif isinstance(comp, File):
            cand = getattr(comp, "file_", "") or ""
        elif isinstance(comp, (Image, Record)):
            cand = getattr(comp, "file", "") or ""
        else:
            return None
    except Exception:
        return None
    if not cand:
        return None
    # file:// URI → 本地路径
    if cand.startswith("file://"):
        from urllib.parse import unquote, urlparse
        try:
            p = urlparse(cand)
            return unquote(p.path)
        except Exception:
            return None
    # base64/data URI / http(s) URL 不算本地路径
    if cand.startswith(("base64://", "data:", "http://", "https://")):
        return None
    p = Path(cand)
    if p.is_absolute() and p.is_file():
        return str(p)
    # 兼容相对路径（在媒体缓存目录内查找）
    try:
        rel = _get_media_cache_dir() / cand
        if rel.is_file():
            return str(rel)
    except Exception:
        pass
    return None


async def _prepare_chain_for_media_urls(chain) -> list:
    """发送前把链中媒体组件注册到 AstrBot 文件服务，生成可下载的 URL。

    队列场景下媒体已本地化到 AstrBot data 目录（持久化），但由于 NapCat 在另一容器
    读不到本地路径、源端短效 URL 又会在队列延迟后过期，这里在「发送前」实时注册
    新 token（每次发送、每个目标都重新注册，避免单次 token 与 300s 过期问题）。
    Image/Record 由 aiocqhttp 转 base64 内嵌，无需文件服务；仅处理 Video/File。
    callback_api_base 未配置时返回原链（回退原始 URL 行为）。"""
    if not chain or not _file_service_available():
        return chain
    new_chain = []
    for comp in chain:
        if isinstance(comp, (Video, File)):
            local_path = _local_media_path_of(comp)
            if local_path:
                url = await _register_file_with_service(local_path)
                if url:
                    if isinstance(comp, Video):
                        comp = Video(file=url, url=url)
                    else:
                        comp = File(name=getattr(comp, "name", "") or "", url=url)
        new_chain.append(comp)
    return new_chain


# ------------------------
# 消息链序列化（队列持久化）
# ------------------------

def _serialize_chain(chain) -> list:
    """把消息链组件序列化为可 JSON 存储的 dict 列表（供队列持久化）。

    仅覆盖队列转发涉及的组件类型（Plain/Image/Record/Video/File/At）；
    其余组件类型原样保留（无法序列化时跳过，避免阻塞入队）。"""
    out = []
    for comp in chain:
        try:
            if isinstance(comp, Plain):
                out.append({"t": "plain", "text": comp.text})
            elif isinstance(comp, Image):
                out.append({"t": "image", "file": comp.file, "url": comp.url or ""})
            elif isinstance(comp, Record):
                out.append({"t": "record", "file": comp.file, "url": comp.url or ""})
            elif isinstance(comp, Video):
                out.append({"t": "video", "file": comp.file, "url": comp.url or "", "cover": getattr(comp, "cover", None) or ""})
            elif isinstance(comp, File):
                out.append({"t": "file", "name": comp.name or "", "file_": comp.file_ or "", "url": comp.url or ""})
            elif isinstance(comp, At):
                out.append({"t": "at", "qq": str(comp.qq), "name": comp.name or ""})
            else:
                # 无法序列化的组件类型：跳过（队列持久化不覆盖）
                continue
        except Exception:
            continue
    return out


def _deserialize_chain(data: list) -> list:
    """把序列化的 dict 列表还原为消息链组件列表。"""
    chain = []
    for item in data or []:
        try:
            t = item.get("t")
            if t == "plain":
                chain.append(Plain(text=item.get("text", "")))
            elif t == "image":
                chain.append(Image(file=item.get("file", ""), url=item.get("url", "")))
            elif t == "record":
                chain.append(Record(file=item.get("file", ""), url=item.get("url", "")))
            elif t == "video":
                chain.append(Video(file=item.get("file", ""), url=item.get("url", ""), cover=item.get("cover", "")))
            elif t == "file":
                chain.append(File(name=item.get("name", ""), file=item.get("file_", ""), url=item.get("url", "")))
            elif t == "at":
                chain.append(At(qq=item.get("qq", ""), name=item.get("name", "")))
        except Exception:
            continue
    return chain


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


def _sanitize_file_chain_for_forward(chain):
    """清洗 File/Video 组件的本地路径引用，只保留 URL。

    OneBot/NapCat 的 File/Video 组件 `file_`/`file` 常是源端容器内本地路径
    （如 `/app/llbot/data/temp/...`），透传给目标端后 NapCat 读不到该路径，
    报 retcode=1200 'rich media transfer failed'；仅保留 `url` 字段让 NapCat 走 URL 下载。
    无 URL 的本地文件（如 base64/纯路径）原样保留。"""
    if not chain:
        return chain
    cleaned = []
    for comp in chain:
        if isinstance(comp, File):
            url = getattr(comp, "url", None) or ""
            if url and url.startswith(("http://", "https://")):
                name = getattr(comp, "name", None) or ""
                cleaned.append(File(name=name, url=url))
                continue
            if not url:
                name = getattr(comp, "name", None) or ""
                file_ = getattr(comp, "file_", None) or ""
                logger.warning(f"⚠️ 群文件 {name!r} 无远程 URL（file_={file_!r}），尝试 API 获取")
        elif isinstance(comp, Video):
            url = getattr(comp, "url", None) or ""
            if url and url.startswith(("http://", "https://")):
                # 保留 URL，清空本地路径（file 字段设为 url）
                cleaned.append(Video(file=url, url=url))
                continue
            if not url:
                logger.info(f"ℹ️ 视频无远程 URL（file={getattr(comp, 'file', '')!r}），NapCat 将尝试读取本地路径")
        cleaned.append(comp)
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
        self.queue_file = self.data_dir / "queue.json"

        # 队列媒体缓存目录：AstrBot data 目录下持久化子目录（重启/重载不丢文件）
        global _MEDIA_CACHE_DIR
        _MEDIA_CACHE_DIR = self.data_dir / "media"
        _MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        self.store = MsgForwardStore(self.pending_file)

        # 冷却计时器：key = "source_umo|target_umo"，value = 冷却结束时间戳
        self._cooldowns: dict[str, float] = {}

        # 发送队列：FIFO 队列，队列间隔 > 0 时消息不立即转发，而是由后台 worker
        # 每隔设定秒数依次发送一条（与「冷却」的丢弃语义互补）
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._queue_worker_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        # 队列暂停标志：pause 后 worker 停止消费（积压消息暂不发送），resume 恢复
        self._queue_paused: bool = False

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

    def _add_rule(self, source_umo: str, target_umo: str, hide_header: bool) -> int:
        """新增一条转发规则并持久化，返回新规则的编号（1-based）。

        统一 bind / bindraw 的建规则逻辑：构造带 __template_key 的规则 dict、
        自动生成 remark 编号、追加到 rules 并保存。"""
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
        return len(rules)

    async def initialize(self):
        # 恢复上次未发送完的持久化队列（重启/重载不丢消息）
        self._restore_persisted_queue()
        self._queue_worker_task = asyncio.create_task(self._queue_worker())
        # 启动时清理所有队列媒体缓存
        self._cleanup_old_media()
        # 启动定期清理任务
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
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
            "mf\n"
            "├── 🔗 绑定\n"
            "│   ├── add: 创建一则转发绑定请求\n"
            "│   ├── bind (code(str)): 接受一则转发绑定请求\n"
            "│   └── bindraw ([源平台] 源ID [目标平台] 目标ID): 直接创建转发绑定\n"
            "│       平台简写 df/qq/wx/tg/dc，加s为私聊，省略平台=default\n"
            "│       例：/mf bindraw 654321 wx 123456\n"
            "│       例：/mf bindraw dfs 114514 wx 123456s（私聊）\n"
            "├── 📋 规则\n"
            "│   ├── list: 列出当前会话的转发规则（含群号）\n"
            "│   ├── listall: 列出所有转发规则\n"
            "│   ├── del (编号(str)): 删除一条转发规则\n"
            "│   ├── hide (编号(str)): 切换规则来源信息显示/隐藏\n"
            "│   ├── toggle (编号(str)): 启用/停用一条转发规则\n"
            "│   └── remark (编号(str), 备注(str)): 设置规则备注名称，留空清除备注\n"
            "├── ❄ 冷却\n"
            "│   ├── cooldown: 查看当前冷却配置\n"
            "│   ├── cooldown default <秒>: 设置全局默认冷却（0=关闭）\n"
            "│   ├── cooldown <编号> <秒>: 设置某条规则冷却（0=关闭该规则冷却）\n"
            "│   └── cooldown <编号> inherit: 重置为继承全局默认\n"
            "├── 🎯 过滤\n"
            "│   └── filter: 查看当前过滤配置\n"
            "├── ⏳ 发送队列\n"
            "│   ├── queue status: 查看发送队列状态与配置\n"
            "│   ├── queue on / queue off: 启用/停用发送队列总开关\n"
            "│   ├── queue interval <秒>: 设置全局默认发送队列间隔（0=关闭）\n"
            "│   ├── queue maxsize <条数>: 设置发送队列最大长度（0=不限制）\n"
            "│   ├── queue retention <小时>: 队列媒体缓存保留时长（0=默认24小时）\n"
            "│   ├── queue set <编号> <秒>: 设置某条规则队列间隔（0=关闭该规则队列）\n"
            "│   ├── queue set <编号> inherit: 重置为继承全局默认\n"
            "│   ├── queue clear: 清空积压队列与媒体缓存\n"
            "│   └── queue pause / queue resume: 暂停/恢复队列消费\n"
            "└── help: 显示此帮助\n\n"
            "冷却：转发一次后在该时间内不会再次转发，避免刷屏。\n"
            "队列：规则设置 queue_interval_seconds > 0 时进入队列，\n"
            "每隔该秒数转发一条。"
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

            idx = self._add_rule(source_umo, target_umo, hide_header)
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

            idx = self._add_rule(source_umo, target_umo, hide_header)
            yield event.plain_result(f"✅ 已绑定 #{idx}\n{source_umo} → {target_umo}")
        except Exception as e:
            yield event.plain_result(f"❌ 直接绑定失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("del")
    async def cmd_del(self, event: AstrMessageEvent, rid: str):
        """删除一条转发规则（规则编号从 /mf list 查看）"""
        rules, idx, rule = self._get_rule(rid)
        if rule is None:
            yield event.plain_result(f"❌ 规则 #{rid} 不存在")
            return
        removed = rules.pop(idx)
        self.config["rules"] = rules
        self.config.save_config()
        yield event.plain_result(
            f"🗑️ 已删除规则 #{rid}（{self._rule_name(removed)}）"
        )

    def _get_rule(self, rid: str):
        """按编号解析规则，返回 (rules, idx, rule)。

        编号非法或越界时返回 (None, None, None)，由调用方统一提示「规则不存在」。
        rules 是可变副本，调用方修改后需自行 self.config["rules"] = rules 并保存。"""
        try:
            idx = int(rid) - 1
        except (TypeError, ValueError):
            return None, None, None
        rules = list(self.config.get("rules", []))
        if idx < 0 or idx >= len(rules):
            return None, None, None
        return rules, idx, rules[idx]

    def _toggle_rule_field(self, rid: str, field: str, on_text: str, off_text: str) -> str | None:
        """切换规则某个布尔字段并持久化，返回成功提示文案；失败返回 None。

        供 /mf hide（hide_header）与 /mf toggle（enabled）复用。"""
        rules, idx, rule = self._get_rule(rid)
        if rule is None:
            return None
        current = bool(rule.get(field, False))
        rule[field] = not current
        self.config["rules"] = rules
        self.config.save_config()
        status = on_text if not current else off_text
        return f"✅ 规则 #{rid}（{self._rule_name(rule)}）{status}"

    def _format_rules(self, items) -> list:
        """把 (规则编号, 规则) 对列表格式化为展示行（启用/隐藏/冷却/队列状态）。

        供 /mf list 与 /mf listall 复用，避免两处重复；编号由调用方决定
        （list 传原始编号，listall 传 1..n 递增编号）。"""
        lines = []
        for idx, r in items:
            en_status = "🟢" if r.get("enabled", True) else "⛔"
            hide_status = "🔒" if r.get("hide_header", False) else "🔓"
            cd = r.get("cooldown_seconds") or self.config.get("default_cooldown_seconds", 0)
            cd_str = f"❄{cd}s" if int(cd) > 0 else ""
            qi = self._queue_interval_for(r)
            qi_str = f"⏳{qi}s" if qi > 0 else ""
            lines.append(f"{en_status} #{idx} {self._rule_name(r)} {hide_status} {cd_str} {qi_str}".strip())
        return lines

    @filter.permission_type(filter.PermissionType.ADMIN)
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
        lines.extend(self._format_rules(matched))
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("hide")
    async def cmd_hide_header(self, event: AstrMessageEvent, rid: str):
        """切换规则的来源信息显示状态（隐藏/显示）"""
        msg = self._toggle_rule_field(rid, "hide_header", "来源信息已隐藏", "来源信息已显示")
        if msg is None:
            yield event.plain_result(f"❌ 规则 #{rid} 不存在")
            return
        yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("toggle")
    async def cmd_toggle(self, event: AstrMessageEvent, rid: str):
        """切换规则的启用状态（启用/停用）"""
        msg = self._toggle_rule_field(rid, "enabled", "已启用", "已停用")
        if msg is None:
            yield event.plain_result(f"❌ 规则 #{rid} 不存在")
            return
        yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("remark")
    async def cmd_remark(self, event: AstrMessageEvent, rid: str, remark: str = ""):
        """设置规则的备注名称（留空清除备注，恢复默认显示）"""
        rules, idx, rule = self._get_rule(rid)
        if rule is None:
            yield event.plain_result(f"❌ 规则 #{rid} 不存在")
            return
        remark = (remark or "").strip()
        if remark:
            rule["remark"] = remark
            self.config["rules"] = rules
            self.config.save_config()
            yield event.plain_result(f"✅ 规则 #{rid} 备注已设为：{remark}")
        else:
            rule.pop("remark", None)
            self.config["rules"] = rules
            self.config.save_config()
            yield event.plain_result(f"✅ 规则 #{rid} 备注已清除（恢复为默认显示）")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("cooldown")
    async def cmd_cooldown(self, event: AstrMessageEvent, args: str = ""):
        """查看/设置转发冷却时间。

        用法：
          /mf cooldown                查看当前冷却配置
          /mf cooldown default <秒>   设置全局默认冷却（0=关闭）
          /mf cooldown <编号> <秒>    设置某条规则冷却（0=关闭，inherit=继承全局）
        """
        args = (args or "").strip()
        # 无参数：查看当前冷却配置
        if not args:
            default_cd = int(self.config.get("default_cooldown_seconds", 0) or 0)
            lines = [f"📋 转发冷却：全局默认 ❄{default_cd}s" if default_cd > 0 else "📋 转发冷却：全局默认 ❄关闭"]
            rules = self.config.get("rules", [])
            has_per_rule = False
            for idx, r in enumerate(rules, start=1):
                cd = r.get("cooldown_seconds")
                if cd is None:
                    continue
                cd = int(cd)
                if not has_per_rule:
                    lines.append("规则级冷却：")
                    has_per_rule = True
                lines.append(f"  #{idx} | {self._rule_name(r)} | ❄{cd}s" if cd > 0 else f"  #{idx} | {self._rule_name(r)} | ❄关闭")
            if not has_per_rule:
                lines.append("（所有规则使用全局默认冷却）")
            lines.append("\n用法：/mf cooldown default <秒> 设全局默认；/mf cooldown <编号> <秒|inherit> 设规则级")
            yield event.plain_result("\n".join(lines))
            return

        parts = args.split()
        # 首参数为 default：设置全局默认冷却
        if parts[0].lower() == "default":
            if len(parts) != 2:
                yield event.plain_result("❌ 用法：/mf cooldown default <秒>（非负整数）")
                return
            try:
                val = int(parts[1])
                if val < 0:
                    raise ValueError
            except ValueError:
                yield event.plain_result("❌ 用法：/mf cooldown default <秒>（非负整数）")
                return
            self.config["default_cooldown_seconds"] = val
            self.config.save_config()
            desc = f"❄{val}s" if val > 0 else "❄关闭"
            yield event.plain_result(f"✅ 全局默认冷却已设为 {desc}")
            return

        # 单参数（纯数字）：兼容旧用法，设置全局默认冷却
        if len(parts) == 1:
            try:
                val = int(parts[0])
                if val < 0:
                    raise ValueError
            except ValueError:
                yield event.plain_result("❌ 用法：/mf cooldown default <秒> 设全局默认；/mf cooldown <编号> <秒|inherit> 设规则级")
                return
            self.config["default_cooldown_seconds"] = val
            self.config.save_config()
            desc = f"❄{val}s" if val > 0 else "❄关闭"
            yield event.plain_result(f"✅ 全局默认冷却已设为 {desc}")
            return

        # 双参数：设置规则级冷却
        rid, arg = parts[0], parts[1].strip().lower()
        rules, idx, rule = self._get_rule(rid)
        if rule is None:
            yield event.plain_result(f"❌ 规则 #{rid} 不存在")
            return
        if arg == "inherit":
            rule.pop("cooldown_seconds", None)
            self.config["rules"] = rules
            self.config.save_config()
            yield event.plain_result(f"✅ 规则 #{rid}（{self._rule_name(rule)}）冷却已重置为继承全局默认")
            return
        try:
            val = int(arg)
            if val < 0:
                raise ValueError
        except ValueError:
            yield event.plain_result("❌ 用法：/mf cooldown <编号> <秒|inherit>，秒必须是非负整数")
            return
        rule["cooldown_seconds"] = val
        self.config["rules"] = rules
        self.config.save_config()
        desc = f"❄{val}s" if val > 0 else "❄关闭"
        yield event.plain_result(f"✅ 规则 #{rid}（{self._rule_name(rule)}）冷却已设为 {desc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("listall")
    async def cmd_list_all(self, event: AstrMessageEvent):
        """列出所有转发规则"""
        rules = self.config.get("rules", [])
        if not rules:
            yield event.plain_result("📭 暂无规则")
            return

        lines = ["📜 所有转发规则："]
        lines.extend(self._format_rules(list(enumerate(rules, start=1))))
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
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

        yield event.plain_result("\n".join(lines))

    @mf.group("queue")
    def queue(self):
        """queue 命令组：发送队列配置"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @queue.command("status")
    async def cmd_queue_status(self, event: AstrMessageEvent):
        """查看发送队列状态与配置"""
        enabled = bool(self.config.get("queue_enabled", False))
        default_qi = int(self.config.get("default_queue_interval_seconds", 0) or 0)
        max_size = int(self.config.get("queue_max_size", 0) or 0)
        retention = int(self.config.get("queue_media_retention_hours", 0) or 0)
        if retention <= 0:
            retention = 24
        pending = self._send_queue.qsize()

        lines = [
            "📋 发送队列状态：",
            f"  总开关：{'🟢 已启用' if enabled else '⛔ 已停用'}",
            f"  消费状态：{'⏸️ 已暂停' if self._queue_paused else '▶️ 运行中'}",
            f"  默认间隔：{'⏳' + str(default_qi) + 's' if default_qi > 0 else '关闭'}",
            f"  队列上限：{'无限制' if max_size <= 0 else str(max_size) + ' 条'}",
            f"  媒体缓存保留：{retention} 小时",
            f"  当前积压：{pending} 条",
        ]
        if not enabled:
            lines.append("  ⚠️ 总开关已停用，即使规则设置了间隔也不会进入队列")
        if self._queue_paused:
            lines.append("  ⚠️ 队列已暂停，积压消息暂不发送（/mf queue resume 恢复）")
        lines.append("\n📋 各规则队列间隔：")
        rules = self.config.get("rules", [])
        has_rule = False
        for idx, r in enumerate(rules, start=1):
            qi = self._queue_interval_for(r)
            if qi > 0:
                has_rule = True
                lines.append(f"  #{idx} | {self._rule_name(r)} | ⏳{qi}s")
        if not has_rule:
            lines.append("  （无规则启用队列，全部立即转发）")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @queue.command("on")
    async def cmd_queue_on(self, event: AstrMessageEvent):
        """启用发送队列总开关"""
        self.config["queue_enabled"] = True
        self.config.save_config()
        yield event.plain_result("✅ 发送队列已启用")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @queue.command("off")
    async def cmd_queue_off(self, event: AstrMessageEvent):
        """停用发送队列总开关"""
        self.config["queue_enabled"] = False
        self.config.save_config()
        yield event.plain_result("✅ 发送队列已停用（规则设置的间隔将不再生效）")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @queue.command("interval")
    async def cmd_queue_interval(self, event: AstrMessageEvent, seconds: str):
        """设置全局默认发送队列间隔（秒），0=关闭"""
        try:
            val = int(seconds)
            if val < 0:
                raise ValueError
        except ValueError:
            yield event.plain_result("❌ 间隔必须是非负整数（秒），如 /mf queue interval 5")
            return
        self.config["default_queue_interval_seconds"] = val
        self.config.save_config()
        desc = f"⏳{val}s" if val > 0 else "关闭"
        yield event.plain_result(f"✅ 全局默认队列间隔已设为 {desc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @queue.command("maxsize")
    async def cmd_queue_maxsize(self, event: AstrMessageEvent, size: str):
        """设置发送队列最大长度，0=不限制"""
        try:
            val = int(size)
            if val < 0:
                raise ValueError
        except ValueError:
            yield event.plain_result("❌ 队列上限必须是非负整数（条），如 /mf queue maxsize 100")
            return
        self.config["queue_max_size"] = val
        self.config.save_config()
        desc = "不限制" if val == 0 else f"{val} 条"
        yield event.plain_result(f"✅ 发送队列上限已设为 {desc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @queue.command("retention")
    async def cmd_queue_retention(self, event: AstrMessageEvent, hours: str):
        """设置队列媒体缓存保留时长（小时），0=默认24小时"""
        try:
            val = int(hours)
            if val < 0:
                raise ValueError
        except ValueError:
            yield event.plain_result("❌ 保留时长必须是非负整数（小时），如 /mf queue retention 48")
            return
        self.config["queue_media_retention_hours"] = val
        self.config.save_config()
        desc = f"{val} 小时" if val > 0 else "默认 24 小时"
        yield event.plain_result(f"✅ 队列媒体缓存保留时长已设为 {desc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @queue.command("set")
    async def cmd_queue_set(self, event: AstrMessageEvent, rid: str, seconds: str):
        """设置某条规则的队列间隔（秒），0=关闭该规则队列，inherit=重置为继承全局"""
        try:
            idx = int(rid) - 1
        except ValueError:
            yield event.plain_result("❌ 用法：/mf queue set <规则编号> <秒|inherit>，如 /mf queue set 2 5 或 /mf queue set 2 inherit")
            return
        rules = list(self.config.get("rules", []))
        if idx < 0 or idx >= len(rules):
            yield event.plain_result(f"❌ 规则 #{rid} 不存在")
            return

        arg = (seconds or "").strip().lower()
        if arg == "inherit":
            # 删除键 → 恢复为继承全局默认间隔
            rules[idx].pop("queue_interval_seconds", None)
            self.config["rules"] = rules
            self.config.save_config()
            yield event.plain_result(f"✅ 规则 #{rid}（{self._rule_name(rules[idx])}）队列间隔已重置为继承全局默认")
            return

        try:
            val = int(arg)
            if val < 0:
                raise ValueError
        except ValueError:
            yield event.plain_result("❌ 用法：/mf queue set <规则编号> <秒|inherit>，秒必须是非负整数，如 /mf queue set 2 5")
            return
        rules[idx]["queue_interval_seconds"] = val
        self.config["rules"] = rules
        self.config.save_config()
        desc = f"⏳{val}s" if val > 0 else "关闭（立即转发）"
        yield event.plain_result(f"✅ 规则 #{rid}（{self._rule_name(rules[idx])}）队列间隔已设为 {desc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @queue.command("clear")
    async def cmd_queue_clear(self, event: AstrMessageEvent):
        """清空当前积压的发送队列，并清理队列媒体缓存"""
        n = self._send_queue.qsize()
        while not self._send_queue.empty():
            try:
                self._send_queue.get_nowait()
                self._send_queue.task_done()
            except asyncio.QueueEmpty:
                break
        # 同步清空磁盘持久化队列
        try:
            self._save_persisted_queue([])
        except Exception:
            pass
        cache_deleted = self._clear_media_cache()
        msg = f"🗑️ 已清空发送队列（丢弃 {n} 条积压消息）"
        if cache_deleted is not None:
            msg += f"\n🧹 已清理 {cache_deleted} 个队列媒体缓存文件"
        yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @queue.command("pause")
    async def cmd_queue_pause(self, event: AstrMessageEvent):
        """暂停发送队列消费：已入队的消息暂不发送，新消息仍可入队"""
        if self._queue_paused:
            yield event.plain_result("⏸️ 发送队列已处于暂停状态")
            return
        self._queue_paused = True
        yield event.plain_result(f"⏸️ 发送队列已暂停（当前积压 {self._send_queue.qsize()} 条暂不发送）")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @queue.command("resume")
    async def cmd_queue_resume(self, event: AstrMessageEvent):
        """恢复发送队列消费"""
        if not self._queue_paused:
            yield event.plain_result("▶️ 发送队列未处于暂停状态")
            return
        self._queue_paused = False
        yield event.plain_result(f"▶️ 发送队列已恢复（继续发送积压的 {self._send_queue.qsize()} 条消息）")

    def _clear_media_cache(self) -> int | None:
        """清空队列媒体缓存目录（AstrBot data 目录下 media/）下的所有文件。

        返回删除的文件数；目录不存在时返回 None（表示无缓存可清理）。"""
        tmp_dir = _get_media_cache_dir()
        if not tmp_dir.exists():
            return None
        deleted = 0
        for f in tmp_dir.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                    deleted += 1
                except OSError:
                    pass
        return deleted

    async def _resolve_file_urls(self, event: AstrMessageEvent, chain):
        """为链中无 URL 的 File 组件尝试从原始 OneBot 消息获取下载 URL。

        通过 `event.message_obj.raw_message` 获取原始 OneBot 消息段中的 `file_id`，
        调用 `get_group_file_url` API 获取下载 URL，使 File 组件能通过 URL 转发
        （而非本地路径，NapCat 读不到跨进程的容器内本地路径）。"""
        umo = str(event.unified_msg_origin)
        raw_event = getattr(event.message_obj, "raw_message", None)
        if not raw_event or not isinstance(raw_event, dict):
            return chain

        raw_segments = raw_event.get("message")
        if not isinstance(raw_segments, list):
            return chain

        # 从原始消息段中提取 file_path → url 映射
        file_url_map = {}
        for seg in raw_segments:
            if seg.get("type") != "file":
                continue
            data = seg.get("data", {})
            file_path = data.get("file", "")
            url = data.get("url", "")
            if url and url.startswith(("http://", "https://")):
                file_url_map[file_path] = url
            elif data.get("file_id"):
                # 尝试调用 OneBot API 获取下载 URL
                try:
                    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
                    parts = umo.split(":")
                    group_id = parts[2] if len(parts) >= 3 and parts[1] == "GroupMessage" else None
                    if not group_id:
                        continue
                    for platform in self.context.platform_manager.get_insts():
                        if isinstance(platform, AiocqhttpAdapter):
                            ret = await platform.bot.call_action(
                                action="get_group_file_url",
                                file_id=data["file_id"],
                                group_id=group_id,
                            )
                            if ret and "url" in ret:
                                file_url_map[file_path] = ret["url"]
                                logger.info(f"ℹ️ 通过 get_group_file_url 获取到文件下载 URL")
                except ImportError:
                    pass
                except Exception as e:
                    logger.warning(f"⚠️ 调用 get_group_file_url 失败: {e}")

        if not file_url_map:
            return chain

        # 替换链中无 URL 的 File 组件（有 URL 的用最新获取的 URL 替换，可能更有效/未过期）
        cleaned = []
        for comp in chain:
            if isinstance(comp, File):
                name = getattr(comp, "name", None) or ""
                file_ = getattr(comp, "file_", None) or ""
                url = getattr(comp, "url", None) or ""
                if url and url.startswith(("http://", "https://")):
                    new_url = file_url_map.get(name) or file_url_map.get(file_)
                    if new_url and new_url != url:
                        cleaned.append(File(name=name, url=new_url))
                        continue
                    cleaned.append(comp)
                    continue
                # 用文件名或 file_ 路径匹配（OneBot 消息段 file 字段可能是文件名或路径）
                matched_url = file_url_map.get(name) or file_url_map.get(file_)
                if matched_url:
                    cleaned.append(File(name=name, url=matched_url))
                    continue
            cleaned.append(comp)
        return cleaned

    def _should_forward(self, event: AstrMessageEvent, rule: dict = None) -> bool:
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
                      sanitized_chain, header_text: str, has_media: bool,
                      use_proxy: bool, proxy_url):
        """把一次转发任务加入发送队列，交由后台 worker 按间隔依次发送。

        同时保存兜底所需的信息，供发送失败时在 worker 内本地化媒体后重试。
        若队列已达上限（queue_max_size > 0），则拒绝入队并记录警告。
        入队后同步持久化到磁盘（queue.json），重启/重载后由 initialize 恢复。"""
        max_size = self.config.get("queue_max_size", 0)
        if max_size > 0 and self._send_queue.qsize() >= max_size:
            logger.error(
                f"❌ 发送队列已满（上限 {max_size} 条），本条消息被丢弃 → {target}。"
                f"请调大 queue_max_size 或降低发送频率。"
            )
            return
        uid = secrets.token_hex(8)
        item = {
            "uid": uid,
            "target": target,
            "result": result,
            "interval": max(0, interval),
            "sanitized_chain": sanitized_chain,
            "header_text": header_text,
            "has_media": has_media,
            "use_proxy": use_proxy,
            "proxy_url": proxy_url,
        }
        self._send_queue.put_nowait(item)
        # 持久化：入队即写盘（序列化链组件），发送成功后由 worker 按 uid 移除
        try:
            persisted = self._load_persisted_queue()
            persisted.append({
                "uid": uid,
                "target": target,
                "interval": max(0, interval),
                "chain": _serialize_chain(sanitized_chain),
                "header_text": header_text,
                "has_media": has_media,
                "use_proxy": use_proxy,
                "proxy_url": proxy_url,
            })
            self._save_persisted_queue(persisted)
        except Exception as e:
            logger.warning(f"⚠️ 队列持久化失败（不影响本次入队）：{e}")

    def _load_persisted_queue(self) -> list:
        """读取磁盘上未发送完的队列条目（queue.json）。"""
        try:
            if self.queue_file.exists():
                data = load_json(self.queue_file)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and isinstance(data.get("items"), list):
                    return data["items"]
            return []
        except Exception as e:
            logger.warning(f"⚠️ 读取持久化队列失败：{e}")
            return []

    def _save_persisted_queue(self, items: list):
        """把未发送完的队列条目写回磁盘（queue.json）。"""
        try:
            save_json(self.queue_file, {"items": items})
        except Exception as e:
            logger.warning(f"⚠️ 写入持久化队列失败：{e}")

    def _remove_persisted_item(self, uid: str):
        """发送成功后从磁盘队列移除对应条目（按 uid）。"""
        if not uid:
            return
        try:
            items = self._load_persisted_queue()
            before = len(items)
            items = [it for it in items if it.get("uid") != uid]
            if len(items) != before:
                self._save_persisted_queue(items)
        except Exception as e:
            logger.warning(f"⚠️ 更新持久化队列失败：{e}")

    def _restore_persisted_queue(self):
        """重启/重载后把磁盘上未发送完的队列条目重新入队。

        媒体组件保持序列化时的 URL/路径；若本地化媒体已随清理被删除，
        仍按原样入队，由 worker 发送失败时走媒体兜底降级。"""
        items = self._load_persisted_queue()
        if not items:
            return
        restored = 0
        for it in items:
            try:
                chain = _deserialize_chain(it.get("chain", []))
                if not chain:
                    continue
                header_text = it.get("header_text", "") or ""
                # sanitized_chain 不含来源头；result 才前置来源头（与入队时一致）
                result_chain = ([Plain(text=header_text)] + chain) if header_text else chain
                result = MessageEventResult(chain=result_chain)
                self._send_queue.put_nowait({
                    "uid": it.get("uid", ""),
                    "target": it.get("target", ""),
                    "result": result,
                    "interval": int(it.get("interval", 0) or 0),
                    "sanitized_chain": chain,
                    "header_text": header_text,
                    "has_media": bool(it.get("has_media", False)),
                    "use_proxy": bool(it.get("use_proxy", False)),
                    "proxy_url": it.get("proxy_url", None),
                })
                restored += 1
            except Exception as e:
                logger.warning(f"⚠️ 恢复队列条目失败：{e}")
        if restored:
            logger.info(f"✅ 已从磁盘恢复 {restored} 条未发送完的队列消息")

    async def _queue_worker(self):
        """后台发送队列消费者：逐条发送，每发一条后按该条间隔休眠再发下一条。

        单条消息发送失败（含异常）只记录日志、不影响后续消息；整体用外层兜底，
        确保 worker 永不因单条消息或意外异常而退出，避免队列永久卡住。"""
        while True:
            try:
                # 暂停时阻塞等待，直到 resume 唤醒；不消费队列中的消息
                while self._queue_paused:
                    await asyncio.sleep(0.5)
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
        """发送队列中的单条消息，失败时先尝试重新本地化媒体再重试。

        第一层降级：用 AstrBot 核心 downloader（convert_to_file_path）重新本地化所有
        媒体组件，覆盖因队列延迟导致源端临时文件路径/短效 URL 过期的问题；
        超时保护（45s）防止 downloader 挂死阻塞队列；
        第二层降级：对远程 URL 媒体用裸 aiohttp 下载（_prepare_chain_fallback）兜底。
        任一成功路径都会从磁盘持久化队列移除该条（uid）。
        发送前会为链中 Video/File 媒体注册 AstrBot 文件服务 token（每次发送实时注册，
        避免单次 token 与 300s 过期问题），使跨容器目标端（NapCat）能通过 URL 下载。"""
        target = item["target"]
        result = item["result"]
        uid = item.get("uid", "")
        try:
            # 发送前：为 Video/File 注册文件服务 URL（Image/Record 由 aiocqhttp 转 base64）
            outgoing = await _prepare_chain_for_media_urls(result.chain)
            await self.context.send_message(target, MessageEventResult(chain=outgoing))
            self._remove_persisted_item(uid)
        except ValueError as e:
            logger.error(f"❌ 不合法的 session 字符串，转发失败: {e}")
        except Exception as e:
            # 第一层降级：AstrBot 核心重新本地化所有媒体（覆盖本地临时路径过期）
            try:
                prepared = await asyncio.wait_for(
                    _prepare_chain_for_forward(item["sanitized_chain"]),
                    timeout=45,
                )
                fb_chain = prepared if not item["header_text"] else \
                    [Plain(text=item["header_text"])] + prepared
                # 重试前同样注册文件服务 URL
                fb_chain = await _prepare_chain_for_media_urls(fb_chain)
                await self.context.send_message(target, MessageEventResult(chain=fb_chain))
                self._remove_persisted_item(uid)
                logger.warning(f"⚠️ 队列转发首次失败（{e}），已重新本地化媒体后重试成功")
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ 队列转发：媒体本地化超时（45s），进入远程 URL 兜底")
                # 第二层降级：远程 URL 媒体
                if item.get("has_media"):
                    try:
                        localized = await _prepare_chain_fallback(
                            item["sanitized_chain"],
                            use_proxy=item["use_proxy"],
                            proxy_url=item["proxy_url"],
                        )
                        fb_chain = localized if not item["header_text"] else \
                            [Plain(text=item["header_text"])] + localized
                        fb_chain = await _prepare_chain_for_media_urls(fb_chain)
                        await self.context.send_message(target, MessageEventResult(chain=fb_chain))
                        self._remove_persisted_item(uid)
                        logger.warning(f"⚠️ 队列转发二次降级，已通过远程 URL 本地化后重试成功")
                    except Exception as e3:
                        logger.error(f"❌ 队列转发失败（本地化重试后仍失败）: {e3}")
                else:
                    logger.error(f"❌ 队列转发失败: 媒体本地化超时且无媒体可兜底")
            except Exception as e2:
                # 第二层降级：原始兜底，仅处理远程 URL 媒体
                if item.get("has_media"):
                    try:
                        localized = await _prepare_chain_fallback(
                            item["sanitized_chain"],
                            use_proxy=item["use_proxy"],
                            proxy_url=item["proxy_url"],
                        )
                        fb_chain = localized if not item["header_text"] else \
                            [Plain(text=item["header_text"])] + localized
                        fb_chain = await _prepare_chain_for_media_urls(fb_chain)
                        await self.context.send_message(target, MessageEventResult(chain=fb_chain))
                        self._remove_persisted_item(uid)
                        logger.warning(f"⚠️ 队列转发二次降级，已通过远程 URL 本地化后重试成功")
                    except Exception as e3:
                        logger.error(f"❌ 队列转发失败（本地化重试后仍失败）: {e3}")
                else:
                    logger.error(f"❌ 队列转发失败: {e2}")

    def _cleanup_old_media(self):
        """清理超过保留时间的媒体缓存文件。"""
        retention_hours = int(self.config.get("queue_media_retention_hours", 0) or 0)
        if retention_hours <= 0:
            retention_hours = 24
        tmp_dir = _get_media_cache_dir()
        if not tmp_dir.exists():
            return
        cutoff = time.time() - retention_hours * 3600
        deleted = 0
        for f in tmp_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                    deleted += 1
                except OSError:
                    pass
        if deleted:
            logger.info(f"🧹 已清理 {deleted} 个过期队列媒体缓存文件")

    async def _periodic_cleanup(self):
        """每小时清理一次过期媒体缓存。"""
        while True:
            try:
                await asyncio.sleep(3600)
                self._cleanup_old_media()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

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
            # 清洗 File 组件的本地路径：NapCat 读不到源端本地路径，仅保留 URL 走下载
            sanitized_chain = _sanitize_file_chain_for_forward(sanitized_chain)
            # 为无 URL 的 File 组件尝试从原始 OneBot 消息获取下载 URL（get_group_file_url）
            sanitized_chain = await self._resolve_file_urls(event, sanitized_chain)
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
                if queue_interval > 0 and not self.config.get("queue_enabled", False):
                    queue_interval = 0

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

                # 是否含媒体组件（决定失败时是否值得本地化后重试）
                has_media = any(isinstance(c, (Image, Record, Video, File)) for c in sanitized_chain)
                # 规则级代理三态：use_proxy 关→直连；开且 proxy_url 空→系统代理；开且非空→该地址
                use_proxy = bool(rule.get("use_proxy", False))
                proxy_url = (rule.get("proxy_url") or "").strip() or None
                fallback_chain = None

                # 逐目标发送：一个目标失败不影响其他目标（冷却按 源|目标 对记录）
                for target in targets:
                    # 队列模式：入队前本地化媒体到自有目录，再交给后台 worker 按间隔依次转发
                    if queue_interval > 0:
                        # 队列模式下必须本地化媒体，防止源端临时文件延迟后被清理
                        if prepared_chain is None:
                            prepared_chain = await _prepare_chain_for_queue(
                                sanitized_chain, use_proxy=use_proxy, proxy_url=proxy_url,
                            )
                        queue_chain = prepared_chain if not header_text else [Plain(text=header_text)] + prepared_chain
                        self._enqueue_send(
                            target, event.chain_result(queue_chain), queue_interval,
                            prepared_chain, header_text, has_media,
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
                        # 第一层降级：AstrBot 核心重新本地化所有媒体
                        try:
                            if fallback_chain is None:
                                prepared = await _prepare_chain_for_forward(sanitized_chain)
                                fallback_chain = prepared if not header_text else [Plain(text=header_text)] + prepared
                            await self.context.send_message(target, event.chain_result(fallback_chain))
                            logger.warning(f"⚠️ 转发首次失败（{e}），已重新本地化媒体后重试成功")
                            if cooldown_sec > 0:
                                self._cooldowns[cd_key] = now + cooldown_sec
                        except Exception as e2:
                            # 第二层降级：远程 URL 媒体
                            if has_media:
                                try:
                                    localized = await _prepare_chain_fallback(sanitized_chain, use_proxy=use_proxy, proxy_url=proxy_url)
                                    fb_chain = localized if not header_text else [Plain(text=header_text)] + localized
                                    await self.context.send_message(target, event.chain_result(fb_chain))
                                    logger.warning(f"⚠️ 转发二次降级，已通过远程 URL 本地化后重试成功")
                                    if cooldown_sec > 0:
                                        self._cooldowns[cd_key] = now + cooldown_sec
                                except Exception as e3:
                                    logger.error(f"❌ 转发失败（本地化重试后仍失败）: {e3}")
                            else:
                                logger.error(f"❌ 转发失败: {e2}")

        except Exception as e:
            logger.error(f"❌ 转发逻辑异常: {e}")

    async def terminate(self):
        if self._queue_worker_task is not None:
            self._queue_worker_task.cancel()
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
        logger.info("MsgForward plugin terminated")
