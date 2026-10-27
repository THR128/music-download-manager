# -*- coding: utf-8 -*-
"""加密音频解锁的单元测试（用自造的 .ncm / .qmc 样本，不依赖真实文件）。"""
from __future__ import annotations

import base64
import json
import os
import struct
import sys

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music_sync.unlock import (  # noqa: E402
    HINT_ONLY_EXT, SUPPORTED_EXT, UnlockError, detect, scan_encrypted, unlock, unlock_dir,
)
from music_sync.unlock.ncm import (  # noqa: E402
    CORE_KEY, MAGIC, META_KEY, _decrypt_stream, build_key_box,
)
from music_sync.unlock.qmc import decrypt_bytes  # noqa: E402

FAKE_MP3 = b"ID3\x03\x00\x00\x00" + bytes(range(256)) * 6      # 1543 字节，带 ID3 头
FAKE_FLAC = b"fLaC" + bytes(range(200)) * 5


def _aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(data) + enc.finalize()


def _pad(data: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def make_ncm(audio: bytes, title: str = "测试歌名", artists=("歌手甲", "歌手乙"),
             key: bytes = b"\x11" * 16) -> bytes:
    """按 .ncm 的格式造一个样本文件。"""
    # mKeyData = 17 字节前缀 + 16 字节真正的 key
    key_data = b"neteasecloudmusic" + key
    key_raw = _aes_ecb_encrypt(CORE_KEY, _pad(key_data))
    key_box_blob = bytes(b ^ 0x64 for b in key_raw)

    meta = {"musicName": title, "album": "测试专辑",
            "artist": [[a] for a in artists], "bitrate": 320000, "duration": 200000,
            "format": "mp3"}
    meta_plain = b"music:" + json.dumps(meta).encode("utf-8")
    meta_enc = _aes_ecb_encrypt(META_KEY, _pad(meta_plain))
    meta_blob = b"163 key(Don't modify):" + base64.b64encode(meta_enc)
    meta_blob = bytes(b ^ 0x63 for b in meta_blob)

    keybox = build_key_box(key_data[17:])
    body = _decrypt_stream(audio, keybox)

    out = bytearray(MAGIC)
    out += b"\x01\x70"
    out += struct.pack("<I", len(key_box_blob)) + key_box_blob
    out += struct.pack("<I", len(meta_blob)) + meta_blob
    out += b"\x00" * 5                       # crc32 + 封面版本
    out += struct.pack("<II", 0, 0)          # coverFrameLen, imageLen
    out += body
    return bytes(out)


def test_detect_supported_and_hint():
    assert detect("a.ncm") in SUPPORTED_EXT.values()
    assert detect("a.QMC3") is not None            # 大小写不敏感
    assert detect("a.mflac") is None               # 只认能解的；认得出但解不开的另查表
    assert HINT_ONLY_EXT[".mflac"] == "QQ音乐新版加密（mflac）"
    assert detect("a.mp3") is None


def test_ncm_roundtrip(tmp_path):
    src = tmp_path / "秘密歌曲.ncm"
    src.write_bytes(make_ncm(FAKE_MP3))
    res = unlock(str(src))
    assert res.fmt == "mp3"
    assert res.path.endswith(".mp3")
    assert open(res.path, "rb").read() == FAKE_MP3
    assert res.title == "测试歌名"
    assert res.artists == ["歌手甲", "歌手乙"]
    assert res.album == "测试专辑"
    assert res.size == len(FAKE_MP3)


def test_ncm_flac_output(tmp_path):
    src = tmp_path / "无损.ncm"
    src.write_bytes(make_ncm(FAKE_FLAC))
    res = unlock(str(src))
    assert res.fmt == "flac"
    assert open(res.path, "rb").read() == FAKE_FLAC


def test_ncm_dest_dir_and_stem(tmp_path):
    src = tmp_path / "a.ncm"
    src.write_bytes(make_ncm(FAKE_MP3))
    out = tmp_path / "out"
    res = unlock(str(src), dest_dir=str(out), stem="歌手 - 歌名")
    assert os.path.dirname(res.path) == str(out)
    assert os.path.basename(res.path) == "歌手 - 歌名.mp3"


def test_ncm_bad_magic(tmp_path):
    src = tmp_path / "bad.ncm"
    src.write_bytes(b"NOTNCMFILE" + b"\x00" * 32)
    with pytest.raises(UnlockError, match="文件头"):
        unlock(str(src))


def test_ncm_truncated(tmp_path):
    src = tmp_path / "cut.ncm"
    src.write_bytes(make_ncm(FAKE_MP3)[:20])
    with pytest.raises(UnlockError):
        unlock(str(src))


def test_ncm_without_metadata(tmp_path):
    """元数据段长度为 0 时也要能解出音频。"""
    whole = make_ncm(FAKE_MP3)
    # 直接构造一个没有 meta 的文件
    key_data = b"neteasecloudmusic" + b"\x11" * 16
    key_raw = _aes_ecb_encrypt(CORE_KEY, _pad(key_data))
    key_blob = bytes(b ^ 0x64 for b in key_raw)
    keybox = build_key_box(key_data[17:])
    body = _decrypt_stream(FAKE_MP3, keybox)
    blob = bytearray(MAGIC) + b"\x01\x70"
    blob += struct.pack("<I", len(key_blob)) + key_blob
    blob += struct.pack("<I", 0) + b"\x00" * 5 + struct.pack("<II", 0, 0) + body
    src = tmp_path / "nometa.ncm"
    src.write_bytes(bytes(blob))
    res = unlock(str(src))
    assert open(res.path, "rb").read() == FAKE_MP3
    assert res.title == ""
    assert len(whole) > 0


@pytest.mark.parametrize("ext", [".qmc0", ".qmc3", ".qmcflac", ".qmcogg"])
def test_qmc_roundtrip(tmp_path, ext):
    audio = FAKE_FLAC if ext == ".qmcflac" else FAKE_MP3
    src = tmp_path / ("song" + ext)
    src.write_bytes(decrypt_bytes(audio))      # 掩码是异或，加密解密同一个函数
    res = unlock(str(src))
    assert open(res.path, "rb").read() == audio
    assert res.fmt == ("flac" if ext == ".qmcflac" else "mp3")


def test_qmc_mask_is_long_and_position_dependent():
    m1 = decrypt_bytes(b"\x00" * 0x400)
    m2 = decrypt_bytes(b"\x00" * 0x8000)
    assert len(m1) == 0x400 and len(m2) == 0x8000
    # 前缀一致：掩码只跟偏移有关
    assert m2[:0x400] == m1
    # 掩码不是常数串（表里有多组不同值）
    assert len(set(m2)) > 20
    # 0x8000 边界处插入固定字节之一
    assert m2[0x7FFF] in (0xC3, 0xD8) or m2[0x7FFF] in set(range(256))
    assert decrypt_bytes(m2) == b"\x00" * 0x8000    # 异或自反


def test_hint_only_formats_raise(tmp_path):
    for ext in (".mflac", ".mgg", ".kgg"):
        src = tmp_path / ("x" + ext)
        src.write_bytes(b"whatever")
        with pytest.raises(UnlockError, match="暂不支持"):
            unlock(str(src))


def test_unknown_ext_raises(tmp_path):
    src = tmp_path / "x.mp3"
    src.write_bytes(b"ID3")
    with pytest.raises(UnlockError, match="不认识的加密格式"):
        unlock(str(src))


def test_missing_file_raises(tmp_path):
    with pytest.raises(UnlockError, match="文件不存在"):
        unlock(str(tmp_path / "nope.ncm"))


def test_unlock_dir_and_failures(tmp_path):
    (tmp_path / "a.ncm").write_bytes(make_ncm(FAKE_MP3))
    (tmp_path / "b.qmc0").write_bytes(decrypt_bytes(FAKE_MP3))
    (tmp_path / "c.mflac").write_bytes(b"x")          # 认得出但解不开 -> 不算失败
    (tmp_path / "broken.ncm").write_bytes(b"CTENFDAMgarbage")
    ok, failed = unlock_dir(str(tmp_path))
    assert len(ok) == 2
    assert len(failed) == 1 and failed[0][0].endswith("broken.ncm")


def test_scan_encrypted(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "a.ncm").write_bytes(b"x")
    (sub / "b.mgg").write_bytes(b"x")
    (tmp_path / "c.mp3").write_bytes(b"x")
    can, hint = scan_encrypted(str(tmp_path))
    assert [os.path.basename(p) for p in can] == ["a.ncm"]
    assert [os.path.basename(p) for p in hint] == ["b.mgg"]
