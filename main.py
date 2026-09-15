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

from astrbot.core.message.components import (
    At, Plain, Image, Record, Video, File, Forward, Node, Nodes,
)
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

_FORWARD_MAX_DEPTH = 3
_FORWARD_MAX_NODES = 50
_FORWARD_MAX_CONTENT_CHARS = 20000
_IMAGE_BATCH_MAX_MESSAGES = 20

# APNG 伪装图兜底：图片转发失败后，把图片换成「静态解码器看到封面、APNG 播放器看到真图」
# 的伪装 PNG 再重发一次（平台若按静态图审核/转码即可放行）。
_APNG_DISGUISE_MODES = ("off", "on_failure")


def _load_apng_disguise_module():
    """加载同目录的 apng_disguise 模块（兼容包内导入 / 同目录导入 / 按路径加载）。"""
    try:
        from . import apng_disguise as module  # AstrBot 以包形式加载插件时
        return module
    except Exception:
        pass
    try:
        import apng_disguise as module  # 插件目录已在 sys.path 时
        return module
    except Exception:
        pass
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "msg_forward_cc_apng_disguise",
            Path(__file__).resolve().with_name("apng_disguise.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        logger.warning(f"⚠️ APNG 伪装模块加载失败，伪装兜底功能不可用：{e}")
        return None


_apng_disguise = _load_apng_disguise_module()


def _make_disguised_file(src: str, dest: str, cover_path: str = "",
                         max_edge: int = 0, loops: int = 0,
                         cover_fit: str = "pad") -> str:
    """同步生成伪装 APNG，供 asyncio.to_thread 调用。

    返回 dest；当输入本身已是 APNG（无需二次伪装）时返回空字符串表示跳过。
    """
    if _apng_disguise is None:
        return ""
    data = Path(src).read_bytes()
    if _apng_disguise.is_apng(data):
        return ""
    return _apng_disguise.write_disguised_apng(
        src, dest,
        cover_source=(cover_path or None),
        max_edge=max(0, int(max_edge or 0)),
        loops=max(0, int(loops or 0)),
        cover_fit=cover_fit or "pad",
        validate=True,
    )


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


def _ensure_media_cache_dir(cache_dir: Path) -> Path:
    """创建插件可控的共享媒体缓存目录。"""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.chmod(0o755)
    return cache_dir


def _create_media_cache_file(cache_dir: Path, suffix: str) -> tuple[int, str]:
    """在共享缓存中创建 NapCat 可读的媒体文件。"""
    cache_dir = _ensure_media_cache_dir(cache_dir)
    fd, path = tempfile.mkstemp(suffix=suffix, dir=str(cache_dir))
    os.chmod(path, 0o644)
    return fd, path


def _copy_media_to_cache(local_path: str, cache_dir: Path) -> str:
    """将现有媒体复制到共享缓存，避免透传容器私有路径。"""
    suffix = Path(local_path).suffix or ""
    fd, dest = _create_media_cache_file(cache_dir, suffix)
    os.close(fd)
    try:
        shutil.copyfile(local_path, dest)
        os.chmod(dest, 0o644)
    except Exception:
        Path(dest).unlink(missing_ok=True)
        raise
    return dest


def _cleanup_media_cache(cache_dir: Path, retention_hours: int,
                         now: float | None = None) -> int:
    """仅清理受控缓存根目录内的过期普通文件。"""
    cache_dir = Path(cache_dir)
    if not cache_dir.exists():
        return 0
    cutoff = (time.time() if now is None else now) - retention_hours * 3600
    deleted = 0
    for path in cache_dir.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            try:
                path.unlink()
                deleted += 1
            except OSError:
                pass
    return deleted


async def _download_url_to_local(comp, url: str, cache_dir: Path,
                                 use_proxy: bool = False,
                                 proxy_url: str | None = None) -> str:
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
    fd, path = _create_media_cache_file(cache_dir, suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(path, 0o644)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    return path


def _rebuild_from_local_path(comp, local_path: str):
    """按本地文件路径重建组件（fromFileSystem）。

    Video 会清空源端封面并使用本地化后的文件；File 保持原有的 URL 优先策略。"""
    if isinstance(comp, Image):
        return Image.fromFileSystem(local_path)
    if isinstance(comp, Record):
        return Record.fromFileSystem(local_path)
    if isinstance(comp, Video):
        # 源端封面通常是跨进程不可达的临时路径，fromFileSystem 不会复用它。
        return Video.fromFileSystem(local_path)
    if isinstance(comp, File):
        # File 组件优先使用 URL（NapCat 通过 URL 下载），
        # 无 URL 时降级为本地路径（从 local_path 缓存重建）
        name = getattr(comp, "name", None) or ""
        url = getattr(comp, "url", None) or ""
        if url:
            return File(name=name, url=url)
        return File(name=name, file=local_path)
    return comp


async def _rebuild_media_component(comp, cache_dir: Path,
                                   use_proxy: bool = False,
                                   proxy_url: str | None = None):
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
                cached_path = _copy_media_to_cache(local_path, cache_dir)
                return _rebuild_from_local_path(comp, cached_path)
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
            local_path = await _download_url_to_local(
                comp, remote_url, cache_dir,
                use_proxy=use_proxy, proxy_url=proxy_url,
            )
            try:
                return _rebuild_from_local_path(comp, local_path)
            except Exception as e:
                logger.warning(f"⚠️ 按下载路径重建媒体失败（{comp_type}），将以占位文本代替：{e}")
        except Exception as e:
            logger.warning(f"⚠️ 转发时重下载媒体失败（{comp_type}，本地文件与远程下载均失败），将以占位文本代替：{e}")
    else:
        logger.warning(f"⚠️ 转发时重下载媒体失败（{comp_type}，无远程 URL 且本地文件不可用），将以占位文本代替")
    return Plain(text=f"[{comp_type}转发失败：源文件不可达]")


async def _prepare_chain_for_forward(chain, cache_dir: Path,
                                     use_proxy: bool = False,
                                     proxy_url: str | None = None):
    """转发前对消息链做「本地化」预处理，返回新的可安全跨会话发送的链。"""
    if not chain:
        return chain
    prepared = []
    for comp in chain:
        if isinstance(comp, (Image, Record, Video, File)):
            prepared.append(await _rebuild_media_component(
                comp, cache_dir, use_proxy=use_proxy, proxy_url=proxy_url,
            ))
        else:
            prepared.append(comp)
    return prepared


async def _prepare_chain_fallback(chain, cache_dir: Path,
                                  use_proxy: bool = False,
                                  proxy_url: str | None = None):
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
                    local_path = await _download_url_to_local(
                        comp, remote_url, cache_dir,
                        use_proxy=use_proxy, proxy_url=proxy_url,
                    )
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
                        cached_path = _copy_media_to_cache(local_path, cache_dir)
                        prepared.append(_rebuild_from_local_path(comp, cached_path))
                    else:
                        prepared.append(comp)
                except Exception:
                    prepared.append(comp)
        else:
            prepared.append(comp)
    return prepared


async def _prepare_chain_for_queue(chain, cache_dir: Path,
                                   use_proxy: bool = False,
                                   proxy_url: str | None = None):
    """将消息链中的所有媒体本地化到插件自有临时目录，防止队列延迟后源端文件被清理。

    优先用 AstrBot 核心 convert_to_file_path() 获取本地缓存路径（适配器层已缓存，
    通常瞬间返回）；失败后回退到远程 URL 下载。所有文件复制到注入的
    插件共享缓存目录，确保队列延迟后仍可访问。
    """
    if not chain:
        return chain
    prepared = []
    _ensure_media_cache_dir(cache_dir)
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
                dest = _copy_media_to_cache(local_path, cache_dir)
                prepared.append(_rebuild_from_local_path(comp, dest))
                continue
        except Exception:
            pass  # convert_to_file_path 失败，尝试远程 URL

        # 回退：远程 URL 下载
        remote_url = _extract_remote_url(comp)
        if remote_url:
            try:
                local_path = await _download_url_to_local(
                    comp, remote_url, cache_dir, use_proxy, proxy_url,
                )
                prepared.append(_rebuild_from_local_path(comp, local_path))
                continue
            except Exception as e:
                logger.warning(f"⚠️ 队列本地化媒体失败（{comp_type}），将以占位文本代替：{e}")
                prepared.append(Plain(text=f"[{comp_type}转发失败：源文件不可达]"))
                continue

        logger.warning(f"⚠️ 队列本地化媒体失败（{comp_type}），无法获取文件，将以占位文本代替")
        prepared.append(Plain(text=f"[{comp_type}转发失败：源文件不可达]"))
    return prepared


def _onebot_forward_media_url(data: dict) -> str:
    """Only accept a remote media URL from an expanded OneBot node."""
    for key in ("url", "file"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return ""


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
        self.media_cache_dir = self.data_dir / "media_cache"

        self.store = MsgForwardStore(self.pending_file)

        # 冷却计时器：key = "source_umo|target_umo"，value = 冷却结束时间戳
        self._cooldowns: dict[str, float] = {}

        # 发送队列：FIFO 队列，队列间隔 > 0 时消息不立即转发，而是由后台 worker
        # 每隔设定秒数依次发送一条（与「冷却」的丢弃语义互补）
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._queue_worker_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        # Pending QQ merged-image batches, keyed by source/rule/target.
        self._image_batches: dict[str, dict] = {}

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
        _ensure_media_cache_dir(self.media_cache_dir)
        self._queue_worker_task = asyncio.create_task(self._queue_worker())
        # 启动时清理过期的共享媒体缓存
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
                "image_send_mode": "direct",
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
                "image_send_mode": "direct",
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

    def _image_batch_window_for(self, rule: dict) -> int:
        """Return the merged-image collection window in seconds."""
        val = rule.get("image_batch_window_seconds")
        if val is None:
            val = self.config.get("default_image_batch_window_seconds", 5)
        try:
            return max(0, min(int(val), 300)) if val else 0
        except (TypeError, ValueError):
            return 0

    def _is_aiocqhttp_group_target(self, target: str) -> bool:
        """Resolve a UMO platform instance ID before enabling QQ merged forwarding."""
        try:
            platform_id, msg_type, _ = target.split(":", 2)
        except ValueError:
            return False
        if msg_type != "GroupMessage":
            return False
        # Keep compatibility with UMO values that use the adapter type directly.
        if platform_id.lower() == "aiocqhttp":
            return True
        try:
            platform = self.context.get_platform_inst(platform_id)
        except (AttributeError, TypeError):
            return False
        if platform is None:
            return False
        try:
            return str(platform.meta().name).lower() == "aiocqhttp"
        except (AttributeError, TypeError):
            return False

    @staticmethod
    def _has_native_forward(chain) -> bool:
        """Native forwarded messages must stay direct instead of being nested."""
        return any(isinstance(comp, (Forward, Node, Nodes)) for comp in chain)

    def _should_merge_images(self, rule: dict, target: str, message_chain) -> bool:
        """Merged image forwarding is intentionally limited to QQ group targets."""
        return (
            rule.get("image_send_mode", "direct") == "merged"
            and self._is_aiocqhttp_group_target(target)
            and any(isinstance(comp, Image) for comp in message_chain)
            and not self._has_native_forward(message_chain)
        )

    @staticmethod
    def _is_batchable_image_chain(message_chain) -> bool:
        """Batch ordinary image messages while leaving other media immediate."""
        return (
            any(isinstance(comp, Image) for comp in message_chain)
            and all(isinstance(comp, (Plain, Image)) for comp in message_chain)
        )

    @staticmethod
    def _make_merged_chain(message_chain, header_text: str,
                           sender_name: str, sender_id) -> list:
        """Build an AstrBot Nodes component for OneBot's native merged forward."""
        content = list(message_chain)
        if header_text:
            content.insert(0, Plain(text=header_text))
        return [Nodes(nodes=[Node(
            name=str(sender_name or "转发消息"),
            uin=str(sender_id or "0"),
            content=content,
        )])]

    def _build_outbound_chain(self, event: AstrMessageEvent, source_umo: str,
                              rule: dict, target: str, message_chain,
                              direct_header_text: str, merged_header_text: str):
        """Select direct or QQ-native merged forwarding for one target."""
        if self._should_merge_images(rule, target, message_chain):
            return self._make_merged_chain(
                message_chain,
                merged_header_text,
                event.get_sender_name(),
                event.get_sender_id(),
            ), True
        return (
            message_chain if not direct_header_text
            else [Plain(text=direct_header_text)] + message_chain
        ), False

    @staticmethod
    def _build_queued_outbound_chain(item: dict, message_chain):
        if item.get("merge_images"):
            return MsgForward._make_merged_chain(
                message_chain,
                item.get("merged_header_text", ""),
                item.get("merged_sender_name", ""),
                item.get("merged_sender_id", "0"),
            )
        header_text = item.get("header_text", "")
        return message_chain if not header_text else [Plain(text=header_text)] + message_chain

    # ------------------------
    # APNG 伪装图兜底（图片转发失败后自动重发）
    # ------------------------

    def _apng_disguise_mode(self, rule: dict | None) -> str:
        """解析伪装图模式：规则级 inherit 时回退到全局设置。"""
        mode = str((rule or {}).get("apng_disguise_mode", "inherit") or "inherit").lower()
        if mode != "inherit" and mode not in _APNG_DISGUISE_MODES:
            mode = "inherit"
        if mode == "inherit":
            mode = str(self.config.get("apng_disguise_mode", "on_failure") or "off").lower()
        return mode if mode in _APNG_DISGUISE_MODES else "off"

    def _can_disguise(self, rule: dict | None) -> bool:
        """伪装模块可用且该规则开启了失败兜底。"""
        return _apng_disguise is not None and self._apng_disguise_mode(rule) == "on_failure"

    def _apng_disguise_options(self, rule: dict | None = None) -> dict:
        """读取伪装图全局配置（封面 / 缩放 / 播放次数 / 发送形式）。"""
        cover_mode = str(
            self.config.get("apng_disguise_cover_mode", "white") or "white"
        ).lower()
        cover_path = ""
        if cover_mode == "custom":
            cover_path = str(self.config.get("apng_disguise_cover_path", "") or "").strip()
            if cover_path and not Path(cover_path).is_file():
                logger.warning(f"⚠️ 伪装图自定义封面不存在，已回退纯白封面：{cover_path}")
                cover_path = ""
        fit = str(self.config.get("apng_disguise_cover_fit", "pad") or "pad").lower()
        if fit not in ("pad", "crop"):
            fit = "pad"
        try:
            max_edge = int(self.config.get("apng_disguise_max_edge", 0) or 0)
        except (TypeError, ValueError):
            max_edge = 0
        try:
            loops = int(self.config.get("apng_disguise_loop", 0) or 0)
        except (TypeError, ValueError):
            loops = 0
        return {
            "cover_path": cover_path,
            "max_edge": max(0, max_edge),
            "loops": max(0, loops),
            "cover_fit": fit,
            "send_as_file": bool(self.config.get("apng_disguise_send_as_file", False)),
        }

    async def _disguise_image_component(self, comp, opts: dict):
        """把单个 Image 组件替换为伪装 APNG；失败或跳过时返回 None（调用方保留原组件）。"""
        if _apng_disguise is None:
            return None
        try:
            local_path = await comp.convert_to_file_path()
        except Exception as e:
            logger.warning(f"⚠️ 伪装图兜底：获取图片本地文件失败：{e}")
            return None
        if not local_path or not Path(local_path).is_file():
            logger.warning("⚠️ 伪装图兜底：图片本地文件不可用，跳过伪装")
            return None
        fd, dest = _create_media_cache_file(self.media_cache_dir, ".png")
        os.close(fd)
        try:
            result = await asyncio.to_thread(
                _make_disguised_file,
                str(local_path), dest, opts["cover_path"],
                opts["max_edge"], opts["loops"], opts["cover_fit"],
            )
        except Exception as e:
            Path(dest).unlink(missing_ok=True)
            logger.warning(f"⚠️ 伪装图兜底：生成伪装 APNG 失败：{e}")
            return None
        if not result:
            Path(dest).unlink(missing_ok=True)
            logger.info("ℹ️ 伪装图兜底：图片本身已是 APNG，跳过")
            return None
        if opts["send_as_file"]:
            return File(name=Path(str(local_path)).with_suffix(".png").name, file=dest)
        try:
            return Image.fromFileSystem(dest)
        except Exception as e:
            logger.warning(f"⚠️ 伪装图兜底：重建图片组件失败：{e}")
            return None

    async def _disguise_chain_list(self, chain, opts: dict) -> tuple:
        """递归替换链中的图片（含 QQ 合并转发的 Nodes 节点），返回 (新链, 替换数)。"""
        out, replaced = [], 0
        for comp in chain:
            if isinstance(comp, Image):
                new_comp = await self._disguise_image_component(comp, opts)
                if new_comp is not None:
                    out.append(new_comp)
                    replaced += 1
                else:
                    out.append(comp)
                continue
            if isinstance(comp, Nodes):
                nodes, sub_replaced = [], 0
                for node in getattr(comp, "nodes", None) or []:
                    content = getattr(node, "content", None)
                    if not isinstance(content, list):
                        nodes.append(node)
                        continue
                    new_content, count = await self._disguise_chain_list(content, opts)
                    if count:
                        nodes.append(Node(
                            content=new_content,
                            name=getattr(node, "name", "") or "",
                            uin=getattr(node, "uin", "0") or "0",
                        ))
                        sub_replaced += count
                    else:
                        nodes.append(node)
                if sub_replaced:
                    out.append(Nodes(nodes=nodes))
                    replaced += sub_replaced
                else:
                    out.append(comp)
                continue
            out.append(comp)
        return out, replaced

    async def _build_disguised_chain(self, chain, opts: dict):
        """生成伪装链；没有任何图片被替换时返回 None。"""
        if not chain:
            return None
        try:
            new_chain, replaced = await self._disguise_chain_list(chain, opts)
        except Exception as e:
            logger.warning(f"⚠️ 伪装图兜底：处理消息链异常：{e}")
            return None
        return new_chain if replaced else None

    async def _resend_with_disguise(self, target: str, apng_mode: str, chain,
                                    build_result) -> bool:
        """把链中的图片换成伪装 APNG 后重发；成功返回 True。"""
        if _apng_disguise is None or apng_mode != "on_failure":
            return False
        opts = self._apng_disguise_options()
        disguised = await self._build_disguised_chain(chain, opts)
        if not disguised:
            return False
        try:
            await self.context.send_message(target, build_result(disguised))
        except Exception as e:
            logger.error(f"❌ 使用 APNG 伪装图重发仍失败: {e}")
            return False
        logger.warning(
            "⚠️ 图片转发失败，已改用 APNG 伪装图重发成功"
            "（静态看图显示封面，浏览器 / APNG 播放器显示真图）"
        )
        return True

    def _enqueue_image_batch(
        self, key: str, target: str, message_chain, merged_header_text: str,
        sender_name: str, sender_id, window_seconds: int,
        cooldown_key: str | None = None, cooldown_seconds: int = 0,
        apng_mode: str = "off",
    ):
        """Append one source image message and schedule one merged send."""
        batches = getattr(self, "_image_batches", None)
        if batches is None:
            batches = self._image_batches = {}
        batch = batches.get(key)
        if batch is None:
            batch = {
                "target": target,
                "nodes": [],
                "cooldown_key": cooldown_key,
                "cooldown_seconds": cooldown_seconds,
                "apng_mode": apng_mode,
                "task": None,
            }
            batches[key] = batch
            batch["task"] = asyncio.create_task(
                self._flush_image_batch_after(key, window_seconds)
            )

        content = list(message_chain)
        if merged_header_text:
            content.insert(0, Plain(text=merged_header_text))
        batch["nodes"].append(Node(
            name=str(sender_name or "转发消息"),
            uin=str(sender_id or "0"),
            content=content,
        ))

        if len(batch["nodes"]) >= _IMAGE_BATCH_MAX_MESSAGES:
            task = batch.get("task")
            if task is not None:
                task.cancel()
            batch["task"] = asyncio.create_task(self._flush_image_batch(key))

    async def _flush_image_batch_after(self, key: str, window_seconds: int):
        try:
            await asyncio.sleep(window_seconds)
            await self._flush_image_batch(key)
        except asyncio.CancelledError:
            return

    async def _flush_image_batch(self, key: str):
        batch = self._image_batches.pop(key, None)
        if not batch or not batch.get("nodes"):
            return
        try:
            await self.context.send_message(
                batch["target"],
                MessageEventResult(chain=[Nodes(nodes=batch["nodes"])]),
            )
            cooldown_seconds = batch.get("cooldown_seconds", 0)
            cooldown_key = batch.get("cooldown_key")
            if cooldown_seconds > 0 and cooldown_key:
                self._cooldowns[cooldown_key] = time.time() + cooldown_seconds
            logger.info(
                f"🖼️ 图片批次已合并转发（{len(batch['nodes'])} 条）→ {batch['target']}"
            )
        except Exception as e:
            # 最后兜底：批次内图片替换为 APNG 伪装图后重发
            if await self._resend_disguised_batch(batch):
                cooldown_seconds = batch.get("cooldown_seconds", 0)
                cooldown_key = batch.get("cooldown_key")
                if cooldown_seconds > 0 and cooldown_key:
                    self._cooldowns[cooldown_key] = time.time() + cooldown_seconds
                return
            logger.error(f"❌ 图片批次合并转发失败: {e}")

    async def _resend_disguised_batch(self, batch: dict) -> bool:
        """图片合并转发失败后的最后兜底：伪装后重发整个合并消息。"""
        def _build(chain):
            return MessageEventResult(chain=chain)

        return await self._resend_with_disguise(
            batch["target"],
            batch.get("apng_mode", "off"),
            [Nodes(nodes=batch["nodes"])],
            _build,
        )

    async def _expand_onebot_forward_segment(
        self, segment: dict, event: AstrMessageEvent, state: dict,
        use_proxy: bool, proxy_url: str | None,
    ) -> list:
        """Convert one OneBot node segment into shared-cache components."""
        segment_type = str(segment.get("type") or "").lower()
        data = segment.get("data")
        data = data if isinstance(data, dict) else {}
        if segment_type == "text":
            text = str(data.get("text") or "")
            remaining = _FORWARD_MAX_CONTENT_CHARS - state["content_chars"]
            if remaining <= 0:
                return [Plain(text="[合并转发内容过长，已截断]")]
            state["content_chars"] += len(text)
            return [Plain(text=text[:remaining])] if text else []
        if segment_type in {"forward", "forward_msg"}:
            forward_id = str(data.get("id") or data.get("message_id") or "").strip()
            if not forward_id:
                return [Plain(text="[合并转发嵌套消息缺少标识]")]
            state["depth"] += 1
            try:
                return [await self._expand_onebot_forward(
                    event, forward_id, state, use_proxy, proxy_url,
                )]
            finally:
                state["depth"] -= 1

        media_url = _onebot_forward_media_url(data)
        if segment_type == "image":
            component = Image.fromURL(media_url) if media_url else None
        elif segment_type in {"record", "audio"}:
            component = Record.fromURL(media_url) if media_url else None
        elif segment_type == "video":
            component = Video.fromURL(media_url) if media_url else None
        elif segment_type == "file":
            name = str(data.get("name") or "转发文件")
            component = File(name=name, file=media_url, url=media_url) if media_url else None
        else:
            return [Plain(text=f"[不支持的合并转发消息段：{segment_type or '未知'}]")]

        if component is None:
            return [Plain(text=f"[{segment_type}转发失败：媒体地址不可用]")]
        try:
            local_path = await _download_url_to_local(
                component, media_url, self.media_cache_dir,
                use_proxy=use_proxy, proxy_url=proxy_url,
            )
            return [_rebuild_from_local_path(component, local_path)]
        except Exception as e:
            logger.warning(f"⚠️ 合并转发媒体本地化失败（{segment_type}）: {e}")
            return [Plain(text=f"[{segment_type}转发失败：媒体不可达]")]

    async def _expand_onebot_forward(
        self, event: AstrMessageEvent, forward_id: str, state: dict,
        use_proxy: bool, proxy_url: str | None,
    ):
        """Fetch a OneBot merged-forward tree and rebuild it as AstrBot Nodes."""
        if state["depth"] >= _FORWARD_MAX_DEPTH:
            return Plain(text="[合并转发层级过深，已截断]")
        if not forward_id or forward_id in state["seen"]:
            return Plain(text="[合并转发存在循环或重复引用，已截断]")

        platform_id = event.get_platform_id()
        platform = self.context.get_platform_inst(platform_id) if platform_id else None
        client = platform.get_client() if platform is not None else None
        call_action = getattr(client, "call_action", None)
        if not callable(call_action):
            logger.warning("⚠️ 合并转发无法展开：aiocqhttp 客户端不可用")
            return Plain(text="[合并转发无法展开：OneBot API 不可用]")

        state["seen"].add(forward_id)
        try:
            raw = await call_action("get_forward_msg", id=forward_id)
        except Exception as e:
            logger.warning(f"⚠️ 拉取合并转发失败: {e}")
            return Plain(text="[合并转发无法展开：原消息不可获取]")

        if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
            raw = raw["data"]
        messages = raw.get("messages") if isinstance(raw, dict) else raw
        if not isinstance(messages, list):
            return Plain(text="[合并转发无法展开：返回格式无效]")

        nodes = []
        for entry in messages:
            if state["nodes"] >= _FORWARD_MAX_NODES:
                nodes.append(Node(content=[Plain(text="[合并转发节点过多，已截断]")],
                                  name="合并转发", uin="0"))
                break
            if not isinstance(entry, dict):
                continue
            payload = entry.get("data") if entry.get("type") == "node" else entry
            if not isinstance(payload, dict):
                continue
            sender = payload.get("sender")
            sender = sender if isinstance(sender, dict) else {}
            name = str(
                sender.get("nickname") or sender.get("card") or sender.get("name")
                or payload.get("name") or "未知发送者"
            )
            uin = str(
                sender.get("user_id") or sender.get("uin") or sender.get("qq")
                or payload.get("user_id") or payload.get("uin") or "0"
            )
            content = payload.get("content", payload.get("message"))
            if isinstance(content, str):
                content = [{"type": "text", "data": {"text": content}}]
            if not isinstance(content, list):
                node_content = [Plain(text="[合并转发节点内容不可用]")]
            else:
                node_content = []
                for child in content:
                    if isinstance(child, dict):
                        node_content.extend(await self._expand_onebot_forward_segment(
                            child, event, state, use_proxy, proxy_url,
                        ))
                if not node_content:
                    node_content = [Plain(text="[合并转发节点为空]")]
            nodes.append(Node(content=node_content, name=name, uin=uin))
            state["nodes"] += 1

        return Nodes(nodes=nodes) if nodes else Plain(text="[合并转发无法展开：没有可用节点]")

    async def _expand_forward_chain_for_qq(
        self, chain, event: AstrMessageEvent,
        use_proxy: bool = False, proxy_url: str | None = None,
    ):
        """Expand source-session Forward ids only for a QQ group target."""
        if event.get_platform_name() != "aiocqhttp":
            return chain
        state = {"depth": 0, "nodes": 0, "content_chars": 0, "seen": set()}
        expanded = []
        for comp in chain:
            if isinstance(comp, Forward):
                expanded.append(await self._expand_onebot_forward(
                    event, str(comp.id), state, use_proxy, proxy_url,
                ))
            else:
                expanded.append(comp)
        return expanded

    def _enqueue_send(self, target: str, result: MessageEventResult, interval: int,
                      sanitized_chain, header_text: str, has_media: bool,
                      use_proxy: bool, proxy_url, merge_images: bool = False,
                      merged_header_text: str = "", merged_sender_name: str = "",
                      merged_sender_id="0", apng_mode: str = "off"):
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
            "has_media": has_media,
            "use_proxy": use_proxy,
            "proxy_url": proxy_url,
            "merge_images": merge_images,
            "merged_header_text": merged_header_text,
            "merged_sender_name": merged_sender_name,
            "merged_sender_id": merged_sender_id,
            "apng_mode": apng_mode,
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
        """发送队列中的单条消息，失败时先尝试重新本地化媒体再重试。

        第一层降级：用 AstrBot 核心 downloader（convert_to_file_path）重新本地化所有
        媒体组件，覆盖因队列延迟导致源端临时文件路径/短效 URL 过期的问题；
        超时保护（45s）防止 downloader 挂死阻塞队列；
        第二层降级：对远程 URL 媒体用裸 aiohttp 下载（_prepare_chain_fallback）兜底。"""
        target = item["target"]
        result = item["result"]
        try:
            await self.context.send_message(target, result)
        except ValueError as e:
            logger.error(f"❌ 不合法的 session 字符串，转发失败: {e}")
        except Exception as e:
            # 第一层降级：AstrBot 核心重新本地化所有媒体（覆盖本地临时路径过期）
            try:
                prepared = await asyncio.wait_for(
                    _prepare_chain_for_forward(
                        item["sanitized_chain"], self.media_cache_dir,
                    ),
                    timeout=45,
                )
                fb_chain = self._build_queued_outbound_chain(item, prepared)
                await self.context.send_message(target, MessageEventResult(chain=fb_chain))
                logger.warning(f"⚠️ 队列转发首次失败（{e}），已重新本地化媒体后重试成功")
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ 队列转发：媒体本地化超时（45s），进入远程 URL 兜底")
                # 第二层降级：远程 URL 媒体
                if item.get("has_media"):
                    try:
                        localized = await _prepare_chain_fallback(
                            item["sanitized_chain"],
                            self.media_cache_dir,
                            use_proxy=item["use_proxy"],
                            proxy_url=item["proxy_url"],
                        )
                        fb_chain = self._build_queued_outbound_chain(item, localized)
                        await self.context.send_message(target, MessageEventResult(chain=fb_chain))
                        logger.warning(f"⚠️ 队列转发二次降级，已通过远程 URL 本地化后重试成功")
                    except Exception as e3:
                        # 第三层降级：APNG 伪装图重发
                        if await self._try_resend_disguised_queued(item, target):
                            return
                        logger.error(f"❌ 队列转发失败（本地化重试后仍失败）: {e3}")
                else:
                    logger.error(f"❌ 队列转发失败: 媒体本地化超时且无媒体可兜底")
            except Exception as e2:
                # 第二层降级：原始兜底，仅处理远程 URL 媒体
                if item.get("has_media"):
                    try:
                        localized = await _prepare_chain_fallback(
                            item["sanitized_chain"],
                            self.media_cache_dir,
                            use_proxy=item["use_proxy"],
                            proxy_url=item["proxy_url"],
                        )
                        fb_chain = self._build_queued_outbound_chain(item, localized)
                        await self.context.send_message(target, MessageEventResult(chain=fb_chain))
                        logger.warning(f"⚠️ 队列转发二次降级，已通过远程 URL 本地化后重试成功")
                    except Exception as e3:
                        # 第三层降级：APNG 伪装图重发
                        if await self._try_resend_disguised_queued(item, target):
                            return
                        logger.error(f"❌ 队列转发失败（本地化重试后仍失败）: {e3}")
                else:
                    logger.error(f"❌ 队列转发失败: {e2}")

    async def _try_resend_disguised_queued(self, item: dict, target: str) -> bool:
        """队列发送失败后的最后兜底：图片替换为 APNG 伪装图重发。"""
        def _build(chain):
            return MessageEventResult(chain=self._build_queued_outbound_chain(item, chain))

        return await self._resend_with_disguise(
            target, item.get("apng_mode", "off"), item["sanitized_chain"], _build,
        )

    def _cleanup_old_media(self):
        """清理超过保留时间的媒体缓存文件。"""
        retention_hours = int(self.config.get("queue_media_retention_hours", 0) or 0)
        if retention_hours <= 0:
            retention_hours = 24
        deleted = _cleanup_media_cache(self.media_cache_dir, retention_hours)
        if deleted:
            logger.info(f"🧹 已清理 {deleted} 个过期媒体缓存文件")

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
                        prepared_chain = await _prepare_chain_for_forward(
                            sanitized_chain, self.media_cache_dir,
                        )
                    message_chain = prepared_chain
                else:
                    message_chain = sanitized_chain

                # 直发保持既有头部格式；合并转发把同一来源信息放进 QQ 合并节点。
                header_text = ""
                merged_header_text = ""
                if not rule.get("hide_header", False):
                    origin_header = self._format_origin_header(event, source_umo)
                    header_text = origin_header + "\n\n\u200b"
                    merged_header_text = origin_header + "\n\n"

                # 是否含媒体组件（决定失败时是否值得本地化后重试）
                has_media = any(isinstance(c, (Image, Record, Video, File)) for c in sanitized_chain)
                # 规则级代理三态：use_proxy 关→直连；开且 proxy_url 空→系统代理；开且非空→该地址
                use_proxy = bool(rule.get("use_proxy", False))
                proxy_url = (rule.get("proxy_url") or "").strip() or None
                fallback_message_chain = None
                expanded_forward_chain = None
                batch_message_chain = None
                image_batch_window = self._image_batch_window_for(rule)
                # APNG 伪装图兜底模式（规则级 inherit → 全局），供失败时重发使用
                apng_mode = self._apng_disguise_mode(rule)

                # 逐目标发送：一个目标失败不影响其他目标（冷却按 源|目标 对记录）
                for target in targets:
                    # 队列模式：入队前本地化媒体到自有目录，再交给后台 worker 按间隔依次转发
                    if queue_interval > 0:
                        # 队列模式下必须本地化媒体，防止源端临时文件延迟后被清理
                        if prepared_chain is None:
                            prepared_chain = await _prepare_chain_for_queue(
                                sanitized_chain, self.media_cache_dir,
                                use_proxy=use_proxy, proxy_url=proxy_url,
                            )
                        target_chain = prepared_chain
                        expands_forward = (
                            self._is_aiocqhttp_group_target(target)
                            and any(isinstance(comp, Forward) for comp in target_chain)
                        )
                        if expands_forward:
                            if expanded_forward_chain is None:
                                expanded_forward_chain = await self._expand_forward_chain_for_qq(
                                    target_chain, event, use_proxy, proxy_url,
                                )
                            target_chain = expanded_forward_chain
                        queue_chain, merge_images = self._build_outbound_chain(
                            event, source_umo, rule, target, target_chain,
                            header_text, merged_header_text,
                        )
                        self._enqueue_send(
                            target, event.chain_result(queue_chain), queue_interval,
                            target_chain, header_text, has_media,
                            use_proxy, proxy_url, merge_images, merged_header_text,
                            event.get_sender_name(), event.get_sender_id(),
                            apng_mode,
                        )
                        continue
                    if cooldown_sec > 0:
                        cd_key = f"{source_umo}|{target}"
                        cd_end = self._cooldowns.get(cd_key, 0)
                        if now < cd_end:
                            continue
                    else:
                        cd_key = None

                    if (
                        queue_interval == 0
                        and image_batch_window > 0
                        and self._should_merge_images(rule, target, message_chain)
                        and self._is_batchable_image_chain(message_chain)
                    ):
                        if batch_message_chain is None:
                            batch_message_chain = await _prepare_chain_for_queue(
                                sanitized_chain, self.media_cache_dir,
                                use_proxy=use_proxy, proxy_url=proxy_url,
                            )
                        batch_key = f"{source_umo}|{idx}|{target}"
                        self._enqueue_image_batch(
                            batch_key,
                            target,
                            batch_message_chain,
                            merged_header_text,
                            event.get_sender_name(),
                            event.get_sender_id(),
                            image_batch_window,
                            cooldown_key=cd_key,
                            cooldown_seconds=cooldown_sec,
                            apng_mode=apng_mode,
                        )
                        continue
                    target_chain = message_chain
                    expands_forward = (
                        self._is_aiocqhttp_group_target(target)
                        and any(isinstance(comp, Forward) for comp in target_chain)
                    )
                    if expands_forward:
                        if expanded_forward_chain is None:
                            expanded_forward_chain = await self._expand_forward_chain_for_qq(
                                target_chain, event, use_proxy, proxy_url,
                            )
                        target_chain = expanded_forward_chain
                    try:
                        outbound_chain, _ = self._build_outbound_chain(
                            event, source_umo, rule, target, target_chain,
                            header_text, merged_header_text,
                        )
                        await self.context.send_message(target, event.chain_result(outbound_chain))
                        # 转发成功后设置冷却
                        if cooldown_sec > 0:
                            self._cooldowns[cd_key] = now + cooldown_sec
                    except ValueError as e:
                        logger.error(f"❌ 不合法的 session 字符串，转发失败: {e}")
                    except Exception as e:
                        if expands_forward:
                            logger.error(f"❌ 已展开的合并转发发送失败，不重用源会话标识重试: {e}")
                            continue
                        # 第一层降级：AstrBot 核心重新本地化所有媒体
                        try:
                            if fallback_message_chain is None:
                                fallback_message_chain = await _prepare_chain_for_forward(
                                    sanitized_chain, self.media_cache_dir,
                                )
                            fallback_chain, _ = self._build_outbound_chain(
                                event, source_umo, rule, target, fallback_message_chain,
                                header_text, merged_header_text,
                            )
                            await self.context.send_message(target, event.chain_result(fallback_chain))
                            logger.warning(f"⚠️ 转发首次失败（{e}），已重新本地化媒体后重试成功")
                            if cooldown_sec > 0:
                                self._cooldowns[cd_key] = now + cooldown_sec
                        except Exception as e2:
                            # 第二层降级：远程 URL 媒体
                            if has_media:
                                try:
                                    localized = await _prepare_chain_fallback(
                                        sanitized_chain, self.media_cache_dir,
                                        use_proxy=use_proxy, proxy_url=proxy_url,
                                    )
                                    fb_chain, _ = self._build_outbound_chain(
                                        event, source_umo, rule, target, localized,
                                        header_text, merged_header_text,
                                    )
                                    await self.context.send_message(target, event.chain_result(fb_chain))
                                    logger.warning(f"⚠️ 转发二次降级，已通过远程 URL 本地化后重试成功")
                                    if cooldown_sec > 0:
                                        self._cooldowns[cd_key] = now + cooldown_sec
                                except Exception as e3:
                                    # 第三层降级：图片替换为 APNG 伪装图后重发
                                    def _build_disguised_result(chain):
                                        fb, _ = self._build_outbound_chain(
                                            event, source_umo, rule, target, chain,
                                            header_text, merged_header_text,
                                        )
                                        return event.chain_result(fb)

                                    if await self._resend_with_disguise(
                                        target, apng_mode,
                                        fallback_message_chain or sanitized_chain,
                                        _build_disguised_result,
                                    ):
                                        if cooldown_sec > 0:
                                            self._cooldowns[cd_key] = now + cooldown_sec
                                    else:
                                        logger.error(f"❌ 转发失败（本地化重试后仍失败）: {e3}")
                            else:
                                logger.error(f"❌ 转发失败: {e2}")

        except Exception as e:
            logger.error(f"❌ 转发逻辑异常: {e}")

    async def terminate(self):
        for batch in getattr(self, "_image_batches", {}).values():
            task = batch.get("task")
            if task is not None:
                task.cancel()
        getattr(self, "_image_batches", {}).clear()
        if self._queue_worker_task is not None:
            self._queue_worker_task.cancel()
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
        logger.info("MsgForward plugin terminated")
