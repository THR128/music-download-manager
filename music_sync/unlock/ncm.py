# -*- coding: utf-8 -*-
"""网易云音乐 ``.ncm`` 解锁。

文件结构（小端）::

    "CTENFDAM"                 8 字节魔数
    2 字节 空 / 版本
    u32  keyLen + keyLen 字节  （每字节 ^0x64，再 AES-128-ECB 解出，含 17 字节前缀）
    u32  metaLen + metaLen 字节（每字节 ^0x63，去掉 "163 key(Don't modify):" 再 base64
                                + AES-128-ECB，去掉 "music:" 后是 JSON）
    5 字节  crc32(4) + 封面版本(1)
    u32  coverFrameLen
    u32  imageLen + imageLen 字节（封面图 jpg/png）
    剩余部分 = 加密音频流

音频流用一把 256 字节的 keybox 逐字节异或，keybox 由 AES 解出来的
「17 字节前缀 + 16 字节密钥」里的密钥喂进 RC4 变体 KSA 生成。

算法参考 taurusxin/ncmdump（MIT），此处按算法自行实现。
"""
from __future__ import annotations

import base64
import json
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import UnlockError, Unlocked, _out_path, _sniff_ext

CORE_KEY = bytes.fromhex("687A4852416D736F356B496E62617857")   # NeteaseCrypt::sCoreKey
META_KEY = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")   # NeteaseCrypt::sModifyKey
MAGIC = b"CTENFDAM"


def _aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return dec.update(data) + dec.finalize()


def _unpad(data: bytes) -> bytes:
    """ncm 的 key 块是 PKCS7 填充（末字节是填充长度）。"""
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > 16:
        return data
    return data[:-pad]


def build_key_box(key: bytes) -> bytes:
    """RC4 风格的 KSA，产出 256 字节的 keybox。"""
    box = list(range(256))
    key_len = len(key)
    if key_len == 0:
        raise UnlockError("ncm 里的密钥是空的，文件可能已损坏")
    last = 0
    off = 0
    for i in range(256):
        swap = box[i]
        c = (swap + last + key[off]) & 0xFF
        off += 1
        if off >= key_len:
            off = 0
        box[i] = box[c]
        box[c] = swap
        last = c
    return bytes(box)


def _decrypt_stream(data: bytes, keybox: bytes) -> bytes:
    out = bytearray(data)
    for i in range(len(out)):
        j = (i + 1) & 0xFF
        k = (keybox[j] + keybox[(keybox[j] + j) & 0xFF]) & 0xFF
        out[i] ^= keybox[k]
    return bytes(out)


def _parse_meta(raw: bytes) -> dict:
    """解析 ``163 key(Don't modify):`` 那段，失败返回空 dict。"""
    try:
        blob = raw[22:]                      # 跳过 "163 key(Don't modify):"
        plain = _aes_ecb_decrypt(META_KEY, base64.b64decode(blob + b"=" * (-len(blob) % 4)))
        text = _unpad(plain)[6:].decode("utf-8", "ignore")   # 跳过 "music:"
        return json.loads(text) or {}
    except Exception:  # noqa: BLE001 - 元数据只是锦上添花，坏了不影响音频
        return {}


def _artists(meta: dict) -> list[str]:
    raw = meta.get("artist") or []
    out: list[str] = []
    for item in raw:
        if isinstance(item, list) and item:
            out.append(str(item[0]))
        elif isinstance(item, str):
            out.append(item)
    return [a for a in out if a]


def decrypt_ncm(path: str, dest_dir: str | None = None, stem: str = "") -> Unlocked:
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 16 or data[:8] != MAGIC:
        raise UnlockError("不是网易云的 .ncm 文件（文件头不对）")

    pos = 10
    if pos + 4 > len(data):
        raise UnlockError("ncm 文件被截断（缺少密钥段）")
    key_len = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    if key_len == 0 or pos + key_len > len(data):
        raise UnlockError("ncm 文件被截断（密钥段长度异常）")
    key_raw = bytes(b ^ 0x64 for b in data[pos:pos + key_len])
    pos += key_len
    key_data = _unpad(_aes_ecb_decrypt(CORE_KEY, key_raw))
    if len(key_data) <= 17:
        raise UnlockError("ncm 密钥解出来不完整，文件可能已损坏")
    keybox = build_key_box(key_data[17:])

    meta: dict = {}
    if pos + 4 <= len(data):
        meta_len = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if meta_len and pos + meta_len <= len(data):
            meta_raw = bytes(b ^ 0x63 for b in data[pos:pos + meta_len])
            pos += meta_len
            meta = _parse_meta(meta_raw)

    pos += 5                                    # crc32(4) + 封面版本(1)
    cover = b""
    if pos + 8 <= len(data):
        frame_len, img_len = struct.unpack_from("<II", data, pos)
        pos += 8
        if img_len and pos + img_len <= len(data):
            cover = data[pos:pos + img_len]
            pos += img_len
        if frame_len > img_len:
            pos += frame_len - img_len

    audio = _decrypt_stream(data[pos:], keybox)
    if not audio:
        raise UnlockError("ncm 里没有音频数据")

    ext = _sniff_ext(audio[:16])
    out = _out_path(path, dest_dir, stem or os.path.splitext(os.path.basename(path))[0], ext)
    tmp = out + ".part"
    with open(tmp, "wb") as fh:
        fh.write(audio)
    os.replace(tmp, out)

    title = str(meta.get("musicName") or "")
    album = str(meta.get("album") or "")
    return Unlocked(path=out, fmt=ext.lstrip("."), title=title, artists=_artists(meta),
                    album=album, size=len(audio), source=path)


__all__ = ["decrypt_ncm", "build_key_box", "CORE_KEY", "META_KEY", "MAGIC"]
