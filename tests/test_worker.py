from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from worker import run_job
from worker.run_job import (
    MAGIC,
    MAX_CONCURRENT_SEGMENTS,
    create_audio,
    encrypt_result,
    rate_string,
    split_chapters,
    split_text,
)


class AudioWorkerTests(unittest.TestCase):
    def test_split_vietnamese_chapters(self):
        source = "Chương 1: Mở đầu\nNội dung một.\n\nChương 2: Tiếp theo\nNội dung hai."
        chapters = split_chapters(source)
        self.assertEqual([item[0] for item in chapters], ["Chương 1: Mở đầu", "Chương 2: Tiếp theo"])
        self.assertEqual([item[1] for item in chapters], ["Nội dung một.", "Nội dung hai."])

    def test_split_chinese_chapters(self):
        source = "第一章 初入江湖\n正文一\n\n第二章 风云再起\n正文二"
        chapters = split_chapters(source)
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0][0], "第一章 初入江湖")
        self.assertEqual(chapters[1][1], "正文二")

    def test_no_heading_keeps_one_whole_story(self):
        source = "Một câu chuyện chưa có tiêu đề chương."
        self.assertEqual(split_chapters(source), [("Toàn bộ truyện", source)])

    def test_split_long_text_respects_limit(self):
        chunks = split_text("a" * 7000, max_chars=1000)
        self.assertTrue(chunks)
        self.assertTrue(all(len(part) <= 1000 for part in chunks))
        self.assertEqual("".join(chunks), "a" * 7000)

    def test_default_segments_are_at_most_5000_characters(self):
        source = "a" * 12001
        chunks = split_text(source)
        self.assertTrue(chunks)
        self.assertTrue(all(len(part) <= 5000 for part in chunks))
        self.assertEqual([len(part) for part in chunks], [5000, 5000, 2001])
        self.assertEqual("".join(chunks), source)

    def test_default_segment_size_constant(self):
        from worker.run_job import MAX_SEGMENT_CHARACTERS
        self.assertEqual(MAX_SEGMENT_CHARACTERS, 5000)

    def test_rate(self):
        self.assertEqual(rate_string(1.25), "+25%")
        self.assertEqual(rate_string(0.25), "-50%")

    def test_parallel_synthesis_is_bounded_and_returns_source_order(self):
        async def scenario():
            active = 0
            peak = 0

            async def fake_synthesize(text, voice, rate, destination):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                # Deliberately finish later segments earlier.
                await asyncio.sleep(0.03 if text == "first" else 0.005)
                destination.write_text(text, encoding="utf-8")
                active -= 1

            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                jobs = [
                    ("first", root / "1.mp3"),
                    ("second", root / "2.mp3"),
                    ("third", root / "3.mp3"),
                    ("fourth", root / "4.mp3"),
                    ("fifth", root / "5.mp3"),
                ]
                paths = await run_job.synthesize_segments(
                    jobs, "voice", "+0%", max_concurrency=4,
                    synthesize_fn=fake_synthesize,
                )
                self.assertLessEqual(peak, 4)
                self.assertEqual(peak, 4)
                self.assertEqual(paths, [item[1] for item in jobs])
                self.assertEqual([p.read_text(encoding="utf-8") for p in paths],
                                 ["first", "second", "third", "fourth", "fifth"])

        asyncio.run(scenario())
        self.assertEqual(MAX_CONCURRENT_SEGMENTS, 4)

    def test_create_audio_outputs_one_ordered_audiobook(self):
        async def fake_synthesize(text, voice, rate, destination):
            await asyncio.sleep(0)
            destination.write_text(text, encoding="utf-8")

        def fake_merge(parts, output, work_dir):
            output.write_text("|".join(path.read_text(encoding="utf-8") for path in parts),
                              encoding="utf-8")

        payload = {
            "text": "Chương 1: Đầu\nNỘI_DUNG_1\n\nChương 2: Sau\nNỘI_DUNG_2",
            "voice": "vi-VN-NamMinhNeural",
            "rate": 1.0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "result"
            with patch.object(run_job, "split_text",
                              side_effect=lambda body: [body + "_A", body + "_B"]), \
                 patch.object(run_job, "synthesize_chunk", side_effect=fake_synthesize), \
                 patch.object(run_job, "merge_mp3", side_effect=fake_merge):
                summary = asyncio.run(create_audio(payload, out))

            self.assertEqual(summary["chapters"], 2)
            self.assertEqual(summary["segments"], 4)
            self.assertEqual(summary["parallel_segments"], 4)
            self.assertEqual(summary["segment_character_limit"], 5000)
            self.assertEqual(sorted(p.name for p in out.iterdir()),
                             ["audiobook.mp3", "manifest.json"])
            merged = (out / "audiobook.mp3").read_text(encoding="utf-8")
            self.assertEqual(
                merged,
                "NỘI_DUNG_1_A|NỘI_DUNG_1_B|NỘI_DUNG_2_A|NỘI_DUNG_2_B",
            )
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["audio_file"], "audiobook.mp3")
            self.assertEqual(manifest["total_segments"], 4)
            self.assertEqual(
                [(chapter["first_segment"], chapter["last_segment"])
                 for chapter in manifest["chapters"]],
                [(1, 2), (3, 4)],
            )

    def test_result_is_encrypted_and_contains_one_audiobook(self):
        key = bytes(range(32))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "tracks"
            output_dir.mkdir()
            (output_dir / "audiobook.mp3").write_bytes(b"fake-full-audiobook")
            (output_dir / "manifest.json").write_text('{"audio_file": "audiobook.mp3"}',
                                                       encoding="utf-8")
            encrypted = root / "audio-result.enc"

            encrypt_result(output_dir, encrypted, key)
            envelope = json.loads(encrypted.read_text(encoding="utf-8"))
            self.assertEqual(envelope["version"], 1)
            nonce = base64.b64decode(envelope["nonce"])
            ciphertext = base64.b64decode(envelope["ciphertext"])
            plaintext_zip = AESGCM(key).decrypt(nonce, ciphertext, MAGIC)

            with zipfile.ZipFile(__import__("io").BytesIO(plaintext_zip)) as archive:
                self.assertEqual(set(archive.namelist()), {"audiobook.mp3", "manifest.json"})
                self.assertEqual(archive.read("audiobook.mp3"), b"fake-full-audiobook")


if __name__ == "__main__":
    unittest.main()
