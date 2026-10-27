# -*- coding: utf-8 -*-
"""酷狗 .kgm / .kgma / .vpr 解锁的单元测试。

没有真实样本可用（本机没装酷狗），所以用「按同一算法加密 → 再解密」的往返
测试，并额外用一个照 C++ 参考实现（unlock-music 的 kgm.hpp）逐行重写的
参考函数做交叉验证，避免我把公式抄错。
"""
from __future__ import annotations

import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music_sync.unlock import UnlockError, detect, unlock  # noqa: E402
from music_sync.unlock import kgm  # noqa: E402

KEY = bytes(range(0x10))
KEY17 = KEY + b"\x00"
PAYLOAD = b"ID3\x03\x00\x00\x00" + bytes((i * 37) % 256 for i in range(4096))


def _build(path: str, payload: bytes = PAYLOAD, key: bytes = KEY,
           header: bytes = kgm.KGM_HEADER, header_len: int = 0x3C) -> str:
    is_vpr = header == kgm.VPR_HEADER
    body = kgm.encrypt_kgm(payload, KEY17, vpr=is_vpr)
    head = bytearray(0x3C)
    head[0:16] = header
    struct.pack_into("<I", head, 0x10, header_len)
    head[0x1C:0x2C] = key
    with open(path, "wb") as fh:
        fh.write(bytes(head[:header_len]) + body)
    return path


def test_mask_matches_reference_formula():
    """_mask_at 与 kgm.hpp 里的 getMask（含 while offset >= 0x11）等价。"""
    def ref_mask(pos: int) -> int:
        offset = pos >> 4
        value = 0
        while offset >= 0x11:
            value ^= kgm.TABLE_1[offset % 272]
            offset >>= 4
            value ^= kgm.TABLE_2[offset % 272]
            offset >>= 4
        return kgm.TABLE_V2[pos % 272] ^ value

    for pos in (0, 1, 15, 16, 17, 255, 272, 273, 0x1000, 0x8000, 0x1_0000, 0x45C453F):
        assert kgm._mask_at(pos) == ref_mask(pos), pos


def test_byte_matches_reference_formula():
    """单字节解密与 kgm.hpp 的 Decrypt() 逐行写法一致。"""
    def ref_byte(byte: int, key: bytes, offset: int, vpr: bool = False) -> int:
        med8 = key[(offset) % 17] ^ byte
        med8 ^= (med8 & 0xF) << 4
        msk8 = kgm._mask_at(offset)
        msk8 ^= (msk8 & 0xF) << 4
        out = med8 ^ msk8
        if vpr:
            out ^= kgm.VPR_KEY[offset % 17]
        return out

    for offset in (0, 1, 7, 16, 17, 0x1234, 0x8000 * 3 + 5):
        for byte in (0x00, 0x7F, 0x80, 0xFF):
            assert kgm._decrypt_byte(byte, KEY17, offset) == \
                ref_byte(byte, KEY17, offset), (offset, byte)


def test_kgm_roundtrip(tmp_path):
    src = _build(str(tmp_path / "song.kgm"))
    assert detect(src) == "酷狗 .kgm"
    res = unlock(src, dest_dir=str(tmp_path / "out"), stem="歌手 - 歌名")
    assert res.path.endswith("歌手 - 歌名.mp3")
    assert res.fmt == "mp3"
    with open(res.path, "rb") as fh:
        assert fh.read() == PAYLOAD


def test_kgma_roundtrip(tmp_path):
    src = _build(str(tmp_path / "song.kgma"))
    out = unlock(src, dest_dir=str(tmp_path / "out"))
    with open(out.path, "rb") as fh:
        assert fh.read() == PAYLOAD


def test_flac_payload_keeps_flac_ext(tmp_path):
    flac = b"fLaC" + bytes(range(256)) * 8
    src = _build(str(tmp_path / "a.kgm"), payload=flac)
    out = unlock(src, dest_dir=str(tmp_path / "out"))
    assert out.path.endswith(".flac")
    assert out.fmt == "flac"
    with open(out.path, "rb") as fh:
        assert fh.read() == flac


def test_vpr_roundtrip(tmp_path):
    src = _build(str(tmp_path / "song.vpr"), header=kgm.VPR_HEADER)
    out = unlock(src, dest_dir=str(tmp_path / "out"))
    assert out.fmt == "mp3"
    with open(out.path, "rb") as fh:
        assert fh.read() == PAYLOAD


def test_bad_header_rejected(tmp_path):
    src = str(tmp_path / "bad.kgm")
    with open(src, "wb") as fh:
        fh.write(b"NOTKUGOU" + b"\x00" * 100)
    with pytest.raises(UnlockError, match="不是酷狗"):
        unlock(src)


def test_bad_header_len_rejected(tmp_path):
    src = _build(str(tmp_path / "bad2.kgm"), header_len=0x10)
    with pytest.raises(UnlockError, match="文件头损坏"):
        unlock(src)


def test_tiny_file_rejected(tmp_path):
    src = str(tmp_path / "tiny.kgm")
    with open(src, "wb") as fh:
        fh.write(b"\x00" * 8)
    with pytest.raises(UnlockError, match="太小"):
        unlock(src)


def test_kgg_still_hint_only(tmp_path):
    src = str(tmp_path / "a.kgg")
    with open(src, "wb") as fh:
        fh.write(b"x")
    assert detect(src) is None
    with pytest.raises(UnlockError, match="暂不支持"):
        unlock(src)
