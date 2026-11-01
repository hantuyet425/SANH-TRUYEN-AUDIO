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
    MAX_SEGMENT_CHARACTERS,
    create_audio,
    create_audio_shard,
    create_audio_segment,
    merge_audio_segments,
    merge_audio_shards,
    encrypt_file,
    decrypt_file,
    encrypt_result,
    build_segment_matrix,
    write_segment_matrix_outputs,
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

    def test_four_matrix_shards_merge_all_segments_in_source_order(self):
        async def fake_synthesize(text, voice, rate, destination):
            destination.write_text(text, encoding="utf-8")

        def fake_merge(parts, output, work_dir):
            output.write_text(
                "|".join(path.read_text(encoding="utf-8") for path in parts),
                encoding="utf-8",
            )

        payload = {
            "text": "Chương 1: Mở đầu\nNỘI_DUNG",
            "voice": "vi-VN-NamMinhNeural",
            "rate": 1.0,
        }
        key = bytes(range(32))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partials = root / "partials"
            partials.mkdir()

            with patch.object(
                run_job, "split_text",
                return_value=["ĐOẠN_1", "ĐOẠN_2", "ĐOẠN_3", "ĐOẠN_4", "ĐOẠN_5"],
            ), patch.object(run_job, "synthesize_chunk", side_effect=fake_synthesize):
                summaries = [
                    asyncio.run(create_audio_shard(
                        payload, root / f"shard-{slot}", key, slot, 4
                    ))
                    for slot in range(4)
                ]

            for slot in range(4):
                source = root / f"shard-{slot}" / f"partial-{slot}.enc"
                (partials / source.name).write_bytes(source.read_bytes())

            self.assertEqual([item["assigned_segments"] for item in summaries], [2, 1, 1, 1])
            self.assertEqual([item["total_segments"] for item in summaries], [5, 5, 5, 5])

            with patch.object(run_job, "merge_mp3", side_effect=fake_merge):
                summary = merge_audio_shards(partials, root / "result", key)

            self.assertEqual(summary["segments"], 5)
            self.assertEqual(summary["parallel_workers"], 4)
            final_envelope = json.loads(
                (root / "result" / "audio-result.enc").read_text(encoding="utf-8")
            )
            clear_zip = AESGCM(key).decrypt(
                base64.b64decode(final_envelope["nonce"]),
                base64.b64decode(final_envelope["ciphertext"]),
                MAGIC,
            )
            with zipfile.ZipFile(__import__("io").BytesIO(clear_zip)) as archive:
                self.assertEqual(set(archive.namelist()), {"audiobook.mp3", "manifest.json"})
                self.assertEqual(
                    archive.read("audiobook.mp3").decode("utf-8"),
                    "ĐOẠN_1|ĐOẠN_2|ĐOẠN_3|ĐOẠN_4|ĐOẠN_5",
                )
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                self.assertEqual(manifest["parallel_segments"], 4)
                self.assertEqual(manifest["segment_character_limit"], 5000)
                self.assertEqual(manifest["total_segments"], 5)

    def test_merge_rejects_missing_matrix_shard(self):
        key = bytes(range(32))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partials = root / "partials"
            partials.mkdir()
            (partials / "partial-0.enc").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Expected 4 encrypted segment artifacts"):
                merge_audio_shards(partials, root / "result", key)


    def test_matrix_contains_one_job_per_segment_and_supports_custom_size(self):
        matrix = build_segment_matrix("ABCDEFGHIJK", max_chars=5)
        self.assertEqual(
            matrix["include"],
            [
                {"segment_index": 1, "segment_label": "001"},
                {"segment_index": 2, "segment_label": "002"},
                {"segment_index": 3, "segment_label": "003"},
            ],
        )

    def test_matrix_rejects_more_than_githubs_256_job_limit(self):
        with self.assertRaisesRegex(RuntimeError, "at most 256 matrix jobs"):
            build_segment_matrix("x" * 257, max_chars=1)

    def test_prepare_matrix_writes_only_segment_ids_to_github_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "github-output.txt"
            summary = write_segment_matrix_outputs("ABCDEFGHIJ", 5, output)
            self.assertEqual(summary["segment_count"], 2)
            lines = output.read_text(encoding="utf-8").splitlines()
            values = dict(line.split("=", 1) for line in lines)
            self.assertEqual(json.loads(values["segment_matrix"]), {
                "include": [
                    {"segment_index": 1, "segment_label": "001"},
                    {"segment_index": 2, "segment_label": "002"},
                ]
            })
            self.assertEqual(values["segment_count"], "2")
            self.assertNotIn("ABCDEFGHIJ", output.read_text(encoding="utf-8"))

    def test_one_segment_job_saves_only_encrypted_mp3(self):
        async def fake_synthesize(text, voice, rate, destination):
            destination.write_bytes(("tts:" + text).encode("utf-8"))

        payload = {
            "text": "ABCDEFGHIJ",
            "voice": "vi-VN-NamMinhNeural",
            "rate": 1.0,
        }
        key = bytes(range(32))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "segment"
            with patch.object(run_job, "synthesize_chunk", side_effect=fake_synthesize):
                summary = asyncio.run(
                    create_audio_segment(payload, output, key, 2, max_chars=5)
                )

            self.assertEqual(summary["segment_index"], 2)
            self.assertEqual(summary["total_segments"], 2)
            self.assertEqual(summary["characters"], 5)
            self.assertEqual([item.name for item in output.iterdir()], ["segment-2.enc"])
            self.assertEqual(
                decrypt_file(output / "segment-2.enc", key),
                b"tts:FGHIJ",
            )

    def test_individual_segment_artifacts_merge_in_original_order(self):
        def fake_merge(parts, output, work_dir):
            output.write_bytes(b"|".join(path.read_bytes() for path in parts))

        payload = {
            "text": "ABCDEF",
            "voice": "vi-VN-NamMinhNeural",
            "rate": 1.0,
        }
        key = bytes(range(32))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            segments = root / "segments"
            segments.mkdir()
            for index, value in enumerate((b"audio-AB", b"audio-CD", b"audio-EF"), start=1):
                clear = root / f"clear-{index}.mp3"
                clear.write_bytes(value)
                encrypt_file(clear, segments / f"segment-{index}.enc", key)

            with patch.object(run_job, "merge_mp3", side_effect=fake_merge):
                summary = merge_audio_segments(
                    payload, segments, root / "result", key,
                    max_chars=2, max_parallel=100,
                )

            self.assertEqual(summary["segments"], 3)
            final_envelope = json.loads(
                (root / "result" / "audio-result.enc").read_text(encoding="utf-8")
            )
            clear_zip = AESGCM(key).decrypt(
                base64.b64decode(final_envelope["nonce"]),
                base64.b64decode(final_envelope["ciphertext"]),
                MAGIC,
            )
            with zipfile.ZipFile(__import__("io").BytesIO(clear_zip)) as archive:
                self.assertEqual(set(archive.namelist()), {"audiobook.mp3", "manifest.json"})
                self.assertEqual(
                    archive.read("audiobook.mp3"),
                    b"audio-AB|audio-CD|audio-EF",
                )
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                self.assertEqual(manifest["total_segments"], 3)
                self.assertEqual(manifest["segment_character_limit"], 2)
                self.assertEqual(manifest["parallel_segments"], 100)

    def test_merge_rejects_a_missing_individual_segment(self):
        payload = {
            "text": "ABCDEF",
            "voice": "vi-VN-NamMinhNeural",
            "rate": 1.0,
        }
        key = bytes(range(32))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            segments = root / "segments"
            segments.mkdir()
            clear = root / "clear.mp3"
            clear.write_bytes(b"audio")
            encrypt_file(clear, segments / "segment-1.enc", key)
            with self.assertRaisesRegex(RuntimeError, "Segment artifacts are incomplete"):
                merge_audio_segments(payload, segments, root / "result", key, max_chars=2)

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
