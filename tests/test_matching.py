# -*- coding: utf-8 -*-
"""对账算法测试：不碰网络、不碰真实文件，全部用内存数据。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music_sync import matching                                    # noqa: E402
from music_sync.platforms.base import Track                        # noqa: E402


def track(tid, name, artists, platform="netease"):
    return Track(platform=platform, id=str(tid), name=name, artists=artists)


def local(name_stem):
    """模拟一个「歌手 - 歌名」命名的本地文件。"""
    artist_text, _, title = name_stem.partition(" - ")
    return matching.LocalFile(
        path=f"X:\\lib\\{name_stem}.mp3", name=name_stem,
        artists=matching.split_artists(artist_text),
        core=matching.core_title(title),
        tags=matching.version_tags(title, " ".join(matching.brackets(title)), artist_text))


class TextHelpersTest(unittest.TestCase):
    def test_core_title_keeps_version_in_tags_only(self):
        self.assertEqual(matching.core_title("泡沫 (Live)"), matching.normalize("泡沫"))
        self.assertEqual(matching.version_tags("泡沫 (Live)"), {"live"})

    def test_split_artists_handles_multi_separators(self):
        self.assertEqual(matching.split_artists("周杰伦; 费玉清"), {"周杰伦", "费玉清"})
        self.assertEqual(matching.split_artists("A & B"), {"a", "b"})

    def test_similarity_prefers_containment(self):
        self.assertGreaterEqual(matching.similarity("告白气球", "告白气球钢琴版"), 0.86)


class MatchTest(unittest.TestCase):
    def test_exact_match(self):
        tracks = [track(1, "晴天", ["周杰伦"])]
        files = [local("周杰伦 - 晴天")]
        report = matching.match(tracks, files)
        self.assertEqual(report.matched_count, 1)
        self.assertEqual(report.missing, [])

    def test_version_mismatch_is_not_matched(self):
        # 本地只有「重生版」，歌单要的是原版 —— 不能算已下载
        tracks = [track(1, "泡沫", ["邓紫棋"])]
        files = [local("邓紫棋 - 泡沫 (重生版)")]
        report = matching.match(tracks, files)
        self.assertEqual(report.matched_count, 0)
        self.assertEqual(len(report.missing), 1)

    def test_artist_mismatch_same_title(self):
        tracks = [track(1, "传奇", ["李健"])]
        files = [local("凤凰传奇 - 传奇")]
        report = matching.match(tracks, files)
        self.assertEqual(report.matched_count, 0)

    def test_duplicate_entries_grouped(self):
        # 歌单里同一首歌有两个 id，本地只有一个文件：两条都算已下载
        tracks = [track(1, "情歌", ["梁静茹"]), track(2, "情歌", ["梁静茹"])]
        files = [local("梁静茹 - 情歌")]
        report = matching.match(tracks, files)
        self.assertEqual(report.missing, [])

    def test_ignore_ids_are_respected(self):
        tracks = [track(99, "不存在的歌", ["某人"])]
        report = matching.match(tracks, [], ignore_ids={"99"})
        self.assertEqual(report.missing, [])

    def test_one_file_not_stolen_twice(self):
        tracks = [track(1, "晴天", ["周杰伦"]), track(2, "晴天", ["周杰伦"])]
        self.assertEqual(len(matching.match(tracks, [local("周杰伦 - 晴天")]).pairs), 1)

    def test_extras_reported(self):
        report = matching.match([track(1, "晴天", ["周杰伦"])],
                                [local("周杰伦 - 晴天"), local("群星 - 走进新时代")])
        self.assertEqual([f.name for f in report.extras], ["群星 - 走进新时代"])

    def test_empty_library(self):
        report = matching.match([track(1, "晴天", ["周杰伦"])], [])
        self.assertEqual(report.matched_count, 0)
        self.assertEqual(len(report.missing), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
