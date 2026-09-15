#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""APNG 伪装图生成与结构自检（纯标准库核心 + 可选 Pillow）。

用途
----
把一张「真图」做成「表面看是封面图（默认纯白），拖进浏览器 / APNG 播放器才显示真图」
的伪装 PNG，用于图片被目标平台拦截时的转发兜底。

原理
----
PNG 文件里可以并存两套互不干扰的图像数据：

1. ``IDAT``：静态图，所有解码器都会读（平台预览、审核、转码只看这里）；
2. ``fcTL`` + ``fdAT``：APNG 动画帧，只有支持 APNG 的程序（浏览器、部分客户端）才播放。

本模块把封面图塞进 ``IDAT``，让第一个 ``fcTL`` 出现在 ``IDAT`` 之后——按 APNG 规范此时
``IDAT`` 退化为「静态默认图」；真图作为第 0 帧放进 ``fdAT`` 整幅覆盖画布，再补一个 1×1
占位帧凑成 2 帧动画。于是静态解码器只看到封面，APNG 播放器显示真图。

依赖
----
核心逻辑只用 ``struct`` / ``zlib`` / ``io``（无第三方依赖）。只有输入不是 8-bit 非隔行
PNG（jpg/webp/gif…），或需要按最长边缩放、自定义封面缩放时，才惰性使用 Pillow。

无损瘦身：走 Pillow 时会自动选择最省体积的颜色类型——没有真实透明像素的 RGBA 图
降为 RGB，三通道全等的 RGB 图降为灰度，像素值完全不变（照片类图可省约 25% 体积）。
"""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path
from typing import Iterable, NamedTuple

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# 支持的 PNG 颜色类型：0=灰度 2=RGB 6=RGBA（4=灰度+Alpha 也能解析，但输出统一走 6）
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}
_SUPPORTED_COLOR_TYPES = (0, 2, 6)
_COLOR_TYPE_RGBA = 6

# 单帧像素数据分片大小（fdAT 载荷），沿用成熟实现的 64KB
_FDAT_CHUNK = 65536

# 动画帧延迟：帧 0（真图）与帧 1（1×1 占位）均为 0.1 秒
_FRAME_DELAY = (10, 100)

# 封面适配方式
COVER_FIT_PAD = "pad"      # 等比缩放 + 白底留白
COVER_FIT_CROP = "crop"    # 等比缩放填满 + 居中裁剪
COVER_FIT_CHOICES = (COVER_FIT_PAD, COVER_FIT_CROP)


class ApngDisguiseError(Exception):
    """伪装图生成 / 校验失败。"""


class PngImage(NamedTuple):
    """已解析的静态 PNG：宽、高、颜色类型与逐行像素（未滤波后的原始字节）。"""

    width: int
    height: int
    color_type: int
    rows: list


# ------------------------
# PNG 基础读写（纯标准库）
# ------------------------


def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> list:
    """按 PNG 规范反滤波，返回每行像素的 bytes 列表。"""
    stride = width * bpp
    rows, prev, i = [], bytearray(stride), 0
    if len(raw) < height * (stride + 1):
        raise ApngDisguiseError("PNG 像素数据不完整")
    for _ in range(height):
        ft = raw[i]
        i += 1
        row = bytearray(raw[i:i + stride])
        i += stride
        if ft == 1:  # Sub
            for x in range(bpp, stride):
                row[x] = (row[x] + row[x - bpp]) & 255
        elif ft == 2:  # Up
            for x in range(stride):
                row[x] = (row[x] + prev[x]) & 255
        elif ft == 3:  # Average
            for x in range(stride):
                a = row[x - bpp] if x >= bpp else 0
                row[x] = (row[x] + (a + prev[x]) // 2) & 255
        elif ft == 4:  # Paeth
            for x in range(stride):
                a = row[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[x] = (row[x] + pr) & 255
        elif ft != 0:
            raise ApngDisguiseError(f"不支持的 PNG 滤波类型 {ft}")
        rows.append(bytes(row))
        prev = row
    return rows


def iter_chunks(data: bytes) -> Iterable[tuple]:
    """逐个产出 (chunk_type, payload)，越界/截断时抛 ApngDisguiseError。"""
    if not data.startswith(PNG_SIGNATURE):
        raise ApngDisguiseError("文件不是 PNG / APNG")
    pos, total = len(PNG_SIGNATURE), len(data)
    while pos + 8 <= total:
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        chunk_type = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > total:
            raise ApngDisguiseError("PNG 数据不完整")
        yield chunk_type, data[pos + 8:pos + 8 + length]
        pos = end
        if chunk_type == b"IEND":
            return
    raise ApngDisguiseError("PNG 数据缺少 IEND")


def is_apng(data: bytes) -> bool:
    """判断字节流是否已是 APNG（含 acTL 块）。"""
    try:
        return any(ctype == b"acTL" for ctype, _ in iter_chunks(data))
    except ApngDisguiseError:
        return False


def parse_png(data: bytes) -> PngImage:
    """解析静态 PNG（8-bit 非隔行灰度/RGB/RGBA），返回 PngImage。"""
    ihdr = None
    idat = bytearray()
    for ctype, payload in iter_chunks(data):
        if ctype == b"IHDR":
            ihdr = payload
        elif ctype == b"acTL":
            raise ApngDisguiseError("输入本身已是 APNG，请提供静态图片")
        elif ctype == b"IDAT":
            idat += payload
    if ihdr is None or len(ihdr) != 13:
        raise ApngDisguiseError("PNG 缺少有效的 IHDR")
    if not idat:
        raise ApngDisguiseError("PNG 缺少 IDAT 数据")
    width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    if bit_depth != 8:
        raise ApngDisguiseError(f"仅支持 8-bit PNG（当前 {bit_depth}-bit）")
    if color_type not in _SUPPORTED_COLOR_TYPES:
        raise ApngDisguiseError(f"不支持的颜色类型 {color_type}，请先转为 RGB/RGBA/灰度 PNG")
    if interlace != 0:
        raise ApngDisguiseError("不支持隔行（Adam7）PNG，请先转为非隔行 PNG")
    if width <= 0 or height <= 0:
        raise ApngDisguiseError("PNG 尺寸无效")
    rows = _unfilter(zlib.decompress(bytes(idat)), width, height, _CHANNELS[color_type])
    return PngImage(width, height, color_type, rows)


def encode_png(image: PngImage) -> bytes:
    """把 PngImage 编码为普通静态 PNG（filter 0，zlib 9）。"""
    raw = b"".join(b"\x00" + row for row in image.rows)
    ihdr = struct.pack(
        ">IIBBBBB", image.width, image.height, 8, image.color_type, 0, 0, 0
    )
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def white_rows(width: int, height: int, color_type: int = _COLOR_TYPE_RGBA) -> list:
    """生成纯白封面像素行。"""
    bpp = _CHANNELS.get(color_type)
    if bpp is None:
        raise ApngDisguiseError(f"不支持的颜色类型 {color_type}")
    white = b"\xff" * bpp
    return [white * width for _ in range(height)]


# ------------------------
# 伪装 APNG 组装（纯标准库）
# ------------------------


def build_disguised_apng(
    real: PngImage,
    cover: PngImage | None = None,
    loops: int = 0,
    delay: tuple = _FRAME_DELAY,
) -> bytes:
    """由真图与封面图组装伪装 APNG（2 帧）。

    :param real: 真图（画布尺寸以它为准）
    :param cover: 封面图，尺寸与颜色类型必须与真图一致；None 表示纯白封面
    :param loops: 播放次数，0=无限循环（推荐，保证真图常驻），1=播放一次
    """
    if real.color_type not in _SUPPORTED_COLOR_TYPES:
        raise ApngDisguiseError(f"不支持的真图颜色类型 {real.color_type}")
    if cover is None:
        cover = PngImage(
            real.width, real.height, real.color_type,
            white_rows(real.width, real.height, real.color_type),
        )
    if (cover.width, cover.height) != (real.width, real.height):
        raise ApngDisguiseError("封面图必须与真图同尺寸")
    if cover.color_type != real.color_type:
        raise ApngDisguiseError("封面图必须与真图同颜色类型")

    bpp = _CHANNELS[real.color_type]
    loops = max(0, int(loops))

    def _stream(rows) -> bytes:
        return zlib.compress(b"".join(b"\x00" + row for row in rows), 9)

    # 1×1 白色占位帧：凑成 2 帧动画且不清屏，真图得以常驻画布
    stub = zlib.compress(b"\x00" + b"\xff" * bpp, 9)

    seq = 0

    def _fctl(fw: int, fh: int, dispose: int, blend: int) -> bytes:
        nonlocal seq
        payload = struct.pack(
            ">IIIIIHHBB", seq, fw, fh, 0, 0, delay[0], delay[1], dispose, blend
        )
        seq += 1
        return _chunk(b"fcTL", payload)

    def _fdat(payload: bytes) -> bytes:
        nonlocal seq
        data = struct.pack(">I", seq) + payload
        seq += 1
        return _chunk(b"fdAT", data)

    out = [
        PNG_SIGNATURE,
        _chunk(b"IHDR", struct.pack(
            ">IIBBBBB", real.width, real.height, 8, real.color_type, 0, 0, 0)),
        _chunk(b"acTL", struct.pack(">II", 2, loops)),   # 2 帧，loops=0 无限循环
        _chunk(b"IDAT", _stream(cover.rows)),            # 表面图：静态解码器只看得到它
        _fctl(real.width, real.height, 0, 0),            # 帧 0 = 真图，整幅覆盖且不清屏
    ]
    frame = _stream(real.rows)
    out += [_fdat(frame[i:i + _FDAT_CHUNK]) for i in range(0, len(frame), _FDAT_CHUNK)]
    out.append(_fctl(1, 1, 0, 1))                        # 帧 1 = 1×1 占位，叠加(OVER)
    out.append(_fdat(stub))
    out.append(_chunk(b"IEND", b""))
    return b"".join(out)


def validate_apng(data: bytes, expect_cover_in_idat: bool = True) -> None:
    """结构自检：CRC、块顺序、acTL/fcTL/fdAT 数量与序号。失败抛 ApngDisguiseError。"""
    if not data.startswith(PNG_SIGNATURE):
        raise ApngDisguiseError("输出不是 PNG")
    total = len(data)
    pos = len(PNG_SIGNATURE)
    seen = {"IHDR": 0, "IDAT": 0, "acTL": 0, "fcTL": 0, "fdAT": 0, "IEND": 0}
    first_fctl_after_idat = False
    frame_count = None
    idat_seen = False
    seq_expected = 0
    saw_iend = False

    while pos + 12 <= total:
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > total:
            raise ApngDisguiseError("输出 PNG 数据被截断")
        payload = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:end])
        if crc != (zlib.crc32(ctype + payload) & 0xFFFFFFFF):
            raise ApngDisguiseError(f"输出 PNG 块 {ctype!r} CRC 校验失败")
        name = ctype.decode("latin1")
        if name in seen:
            seen[name] += 1
        if ctype == b"IHDR":
            if pos != len(PNG_SIGNATURE):
                raise ApngDisguiseError("IHDR 必须是第一个块")
            if length != 13:
                raise ApngDisguiseError("IHDR 长度非法")
        elif ctype == b"acTL":
            if idat_seen:
                raise ApngDisguiseError("acTL 必须出现在 IDAT 之前")
            frame_count = struct.unpack(">II", payload)[0]
        elif ctype == b"IDAT":
            idat_seen = True
        elif ctype == b"fcTL":
            if seq_expected == 0 and idat_seen:
                first_fctl_after_idat = True
            if struct.unpack(">I", payload[:4])[0] != seq_expected:
                raise ApngDisguiseError("fcTL/fdAT 序号不连续")
            seq_expected += 1
        elif ctype == b"fdAT":
            if struct.unpack(">I", payload[:4])[0] != seq_expected:
                raise ApngDisguiseError("fcTL/fdAT 序号不连续")
            seq_expected += 1
        elif ctype == b"IEND":
            if end != total:
                raise ApngDisguiseError("IEND 之后还有多余数据")
            saw_iend = True
            pos = end
            break
        pos = end

    if not saw_iend:
        raise ApngDisguiseError("输出缺少 IEND")
    for required in ("IHDR", "IDAT", "acTL", "fcTL", "fdAT"):
        if seen[required] == 0:
            raise ApngDisguiseError(f"输出缺少 {required} 块")
    if not frame_count:
        raise ApngDisguiseError("acTL 帧数非法")
    if seen["fcTL"] != frame_count:
        raise ApngDisguiseError(
            f"acTL 声明 {frame_count} 帧，实际 {seen['fcTL']} 个 fcTL"
        )
    if expect_cover_in_idat and not first_fctl_after_idat:
        raise ApngDisguiseError("第一个 fcTL 必须在 IDAT 之后（否则静态解码器会看到真图）")


# ------------------------
# 高层入口（可选 Pillow）
# ------------------------


def _import_pil():
    try:
        from PIL import Image  # noqa: PLC0415 - 惰性导入，缺失时降级
    except Exception:  # ImportError 或 Pillow 运行时异常都视为不可用
        return None
    return Image


def _has_visible_alpha(img) -> bool:
    """判断 RGBA 图是否真的用到透明通道（存在 alpha < 255 的像素）。"""
    try:
        alpha = img.getchannel("A")
    except Exception:
        return False
    return alpha.getextrema() != (255, 255)


def _is_grayscale(img) -> bool:
    """判断 RGB 图是否三通道完全相等（可无损降为灰度）。"""
    try:
        r, g, b = img.getchannel("R"), img.getchannel("G"), img.getchannel("B")
    except Exception:
        return False
    raw_r = r.tobytes()
    return raw_r == g.tobytes() == b.tobytes()


def _select_color_type(img, target: int | None = None) -> int:
    """选择输出颜色类型：优先按调用方要求，否则自动做无损瘦身。

    - 显式 target（封面需与真图一致时使用）原样返回
    - RGBA 但没有真实透明像素 → RGB（color_type 6 → 2），像素零损失
    - RGB 且三通道全等 → 灰度（2 → 0），像素零损失
    """
    if target is not None:
        return target
    if img.mode == "L":
        return 0
    if img.mode == "RGBA" and _has_visible_alpha(img):
        return _COLOR_TYPE_RGBA
    rgb = img if img.mode == "RGB" else img.convert("RGB")
    return 0 if _is_grayscale(rgb) else 2


def _rows_from_pil(pil_image, max_edge: int, fit: str, canvas: tuple | None = None,
                   background=(255, 255, 255, 255),
                   target_color_type: int | None = None) -> tuple:
    """把 Pillow 图像转成 (color_type, 逐行像素)；可选缩放到最长边 / 适配到画布。

    默认自动去掉多余的 alpha 通道（照片类图可省约 25% 体积，像素不变），
    传入 target_color_type 时强制使用该类型（封面必须与真图一致）。
    """
    Image = _import_pil()
    img = pil_image.convert("RGBA")

    if canvas is not None:
        cw, ch = canvas
        if (img.width, img.height) != (cw, ch):
            if fit == COVER_FIT_CROP:
                scale = max(cw / img.width, ch / img.height)
                resized = img.resize(
                    (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                    Image.LANCZOS,
                )
                left = (resized.width - cw) // 2
                top = (resized.height - ch) // 2
                resized = resized.crop((left, top, left + cw, top + ch))
            else:
                resized = img.copy()
                resized.thumbnail((cw, ch), Image.LANCZOS)
                canvas_img = Image.new("RGBA", (cw, ch), background)
                canvas_img.paste(
                    resized,
                    ((cw - resized.width) // 2, (ch - resized.height) // 2),
                )
                resized = canvas_img
            img = resized
    elif max_edge > 0 and max(img.width, img.height) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)

    color_type = _select_color_type(img, target_color_type)
    if color_type == _COLOR_TYPE_RGBA:
        final = img
    elif color_type == 2:
        final = img.convert("RGB")
    elif color_type == 0:
        final = img.convert("L")
    else:
        raise ApngDisguiseError(f"不支持的目标颜色类型 {color_type}")

    channels = _CHANNELS[color_type]
    stride = final.width * channels
    raw = final.tobytes()
    return color_type, [raw[i:i + stride] for i in range(0, len(raw), stride)]


def _load_with_pil(source, max_edge: int = 0, fit: str = COVER_FIT_PAD,
                   canvas: tuple | None = None,
                   target_color_type: int | None = None) -> PngImage | None:
    """用 Pillow 读取任意格式图片；Pillow 不可用返回 None。"""
    Image = _import_pil()
    if Image is None:
        return None
    if isinstance(source, (bytes, bytearray)):
        import io

        img = Image.open(io.BytesIO(bytes(source)))
    else:
        img = Image.open(source)
    try:
        img.load()
    except Exception as e:  # 损坏图片 / 不支持的编码
        raise ApngDisguiseError(f"图片解码失败：{e}") from e
    color_type, rows = _rows_from_pil(
        img, max_edge, fit, canvas=canvas, target_color_type=target_color_type,
    )
    return PngImage(len(rows[0]) // _CHANNELS[color_type], len(rows), color_type, rows)


def _read_bytes(source) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    return Path(source).read_bytes()


def make_disguised_apng(
    source,
    cover_source=None,
    max_edge: int = 0,
    loops: int = 0,
    cover_fit: str = COVER_FIT_PAD,
) -> bytes:
    """高层入口：任意格式真图 → 伪装 APNG 字节。

    :param source: 真图（本地路径或图片字节）
    :param cover_source: 自定义封面（本地路径或字节）；None=纯白封面
    :param max_edge: 真图最长边上限，0=不缩放（仅 Pillow 可用时生效）
    :param loops: 播放次数，0=无限循环
    :param cover_fit: 自定义封面适配方式，pad=等比留白 / crop=填满裁剪
    :raises ApngDisguiseError: 生成失败
    """
    if cover_fit not in COVER_FIT_CHOICES:
        raise ApngDisguiseError(f"封面适配方式无效：{cover_fit}")
    raw = _read_bytes(source)

    pil_real = _load_with_pil(source, max_edge=max_edge)
    if pil_real is not None:
        real = pil_real
    else:
        # 无 Pillow：仅支持 8-bit 非隔行 PNG，且不缩放
        real = parse_png(raw)

    cover = None
    if cover_source is not None:
        if _import_pil() is not None:
            cover = _load_with_pil(
                cover_source, fit=cover_fit, canvas=(real.width, real.height),
                target_color_type=real.color_type,   # 封面必须与真图同类型
            )
        else:
            cover_bytes = _read_bytes(cover_source)
            cover = parse_png(cover_bytes)
            if (cover.width, cover.height) != (real.width, real.height):
                raise ApngDisguiseError(
                    "无 Pillow 时封面图必须与真图同尺寸（请安装 Pillow 以启用缩放适配）"
                )

    return build_disguised_apng(real, cover, loops=loops)


def write_disguised_apng(
    source,
    dest,
    cover_source=None,
    max_edge: int = 0,
    loops: int = 0,
    cover_fit: str = COVER_FIT_PAD,
    validate: bool = True,
) -> str:
    """生成伪装 APNG 并原子写入 dest（先写临时文件再替换），返回 dest。"""
    data = make_disguised_apng(
        source, cover_source=cover_source, max_edge=max_edge,
        loops=loops, cover_fit=cover_fit,
    )
    if validate:
        validate_apng(data)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        tmp.write_bytes(data)
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        os.replace(tmp, dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return str(dest)
