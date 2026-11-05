from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from worker import run_job
from worker.run_job import (
    DEFAULT_MAX_PARALLEL_JOBS,
    MAX_CONCURRENT_SEGMENTS,
    MAX_SEGMENT_CHARACTERS,
    TTS_RETRY_ATTEMPTS,
    TTS_RETRY_BASE_DELAY_SECONDS,
    build_audio_plan,
    build_segment_matrix,
    create_audio,
    create_audio_segment,
    create_audio_shard,
    merge_audio_segments,
    merge_audio_shards,
    rate_string,
    read_payload,
    split_chapters,
    split_text,
    write_segment_matrix_outputs,
)


class AudioWorkerTests(unittest.TestCase):
    def test_default_max_parallel_jobs_is_30(self):
        self.assertEqual(DEFAULT_MAX_PARALLEL_JOBS, 30)

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

    def test_audio_plan_speaks_original_chapter_headings(self):
        source = "Chương 1: Mở đầu\nNội dung một.\n\nChương 2: Tiếp theo\nNội dung hai."
        chapters, segments = build_audio_plan(source)
        self.assertEqual([chapter["title"] for chapter in chapters],
                         ["Chương 1: Mở đầu", "Chương 2: Tiếp theo"])
        self.assertEqual(segments, [
            "Chương 1: Mở đầu. Nội dung một.",
            "Chương 2: Tiếp theo. Nội dung hai.",
        ])

    def test_audio_plan_does_not_speak_synthetic_titles(self):
        source = "Một câu chuyện chưa có tiêu đề chương."
        self.assertEqual(build_audio_plan(source)[1], [source])

    def test_no_heading_keeps_one_whole_story(self):
        source = "Một câu chuyện chưa có tiêu đề chương."
        self.assertEqual(split_chapters(source), [("Toàn bộ truyện", source)])

    def test_split_long_text_respects_limit(self):
        chunks = split_text("a" * 7000, max_chars=1000)
        self.assertTrue(all(len(part) <= 1000 for part in chunks))
        self.assertEqual("".join(chunks), "a" * 7000)

    def test_default_segments_are_at_most_5000_characters(self):
        source = "a" * 10001
        chunks = split_text(source)
        self.assertEqual([len(part) for part in chunks], [5000, 5000, 1] )
        self.assertEqual("".join(chunks), source)

    def test_default_segment_size_constant(self):
        self.assertEqual(MAX_SEGMENT_CHARACTERS, 5000)

    def test_split_text_rebalances_punctuation_only_tail(self):
        source = "a" * 4700 + " " + "b" * 296 + " " + "…… ……"
        chunks = split_text(source)
        normalized = " ".join(source.split())

        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(part) <= MAX_SEGMENT_CHARACTERS for part in chunks))
        self.assertTrue(all(any(char.isalnum() for char in part) for part in chunks))
        self.assertTrue(chunks[-1].endswith("…… ……"))
        self.assertEqual("".join(chunks).replace(" ", ""), normalized.replace(" ", ""))

    def test_split_text_skips_input_with_no_speakable_content(self):
        self.assertEqual(split_text("…… ……"), [])

    def test_tts_retry_policy_uses_five_attempts(self):
        self.assertEqual(TTS_RETRY_ATTEMPTS, 5)
        self.assertEqual(TTS_RETRY_BASE_DELAY_SECONDS, 3)

    def test_synthesize_chunk_retries_transient_failures_with_backoff(self):
        async def scenario():
            attempts = 0
            delays = []

            class FakeCommunicate:
                def __init__(self, text, voice, rate):
                    self.text = text

                async def save(self, destination):
                    nonlocal attempts
                    attempts += 1
                    if attempts < TTS_RETRY_ATTEMPTS:
                        raise RuntimeError("temporary provider failure")
                    Path(destination).write_bytes(b"mp3-data")

            async def fake_sleep(delay):
                delays.append(delay)

            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "segment.mp3"
                with patch.object(run_job.edge_tts, "Communicate", FakeCommunicate):
                    with patch.object(run_job.asyncio, "sleep", side_effect=fake_sleep):
                        await run_job.synthesize_chunk(
                            "spoken text", "vi-VN-NamMinhNeural", "+25%", output
                        )

                self.assertEqual(attempts, TTS_RETRY_ATTEMPTS)
                self.assertEqual(
                    delays,
                    [TTS_RETRY_BASE_DELAY_SECONDS * (2 ** index)
                     for index in range(TTS_RETRY_ATTEMPTS - 1)],
                )
                self.assertEqual(output.read_bytes(), b"mp3-data")

    def test_rate(self):
        self.assertEqual(rate_string(1.25), "+25%")
        self.assertEqual(rate_string(0.25), "-50%")

    def test_plain_json_payload_is_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "payload.json"
            path.write_text(json.dumps({
                "job_id": "job-1", "text": "Nội dung truyện",
            }, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(read_payload(path)["text"], "Nội dung truyện")

    def test_parallel_synthesis_is_bounded_and_returns_source_order(self):
        async def scenario():
            active = 0
            peak = 0

            async def fake_synthesize(text, voice, rate, destination):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02 if text == "first" else 0.003)
                destination.write_text(text, encoding="utf-8")
                active -= 1

            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                jobs = [(label, root / f"{index}.mp3")
                        for index, label in enumerate(
                            ("first", "second", "third", "fourth", "fifth"), start=1)]
                paths = await run_job.synthesize_segments(
                    jobs, "voice", "+0%", max_concurrency=4,
                    synthesize_fn=fake_synthesize,
                )
                self.assertEqual(peak, 4)
                self.assertEqual(paths, [item[1] for item in jobs])
                self.assertEqual([p.read_text(encoding="utf-8") for p in paths],
                                 ["first", "second", "third", "fourth", "fifth"])
        asyncio.run(scenario())
        self.assertEqual(MAX_CONCURRENT_SEGMENTS, 4)

    def test_matrix_contains_one_job_per_segment_and_supports_custom_size(self):
        self.assertEqual(build_segment_matrix("ABCDEFGHIJK", max_chars=5)["include"], [
            {"segment_index": 1, "segment_label": "001"},
            {"segment_index": 2, "segment_label": "002"},
            {"segment_index": 3, "segment_label": "003"},
        ])

    def test_matrix_rejects_more_than_githubs_256_job_limit(self):
        with self.assertRaisesRegex(RuntimeError, "at most 256 matrix jobs"):
            build_segment_matrix("x" * 257, max_chars=1)

    def test_prepare_matrix_writes_only_segment_ids_to_github_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "github-output.txt"
            summary = write_segment_matrix_outputs("ABCDEFGHIJ", 5, output)
            self.assertEqual(summary["segment_count"], 2)
            values = dict(line.split("=", 1)
                          for line in output.read_text(encoding="utf-8").splitlines())
            self.assertEqual(json.loads(values["segment_matrix"]), {
                "include": [
                    {"segment_index": 1, "segment_label": "001"},
                    {"segment_index": 2, "segment_label": "002"},
                ]
            })
            self.assertEqual(values["segment_count"], "2")
            self.assertNotIn("ABCDEFGHIJ", output.read_text(encoding="utf-8"))

    def test_one_segment_job_saves_plain_mp3(self):
        async def fake_synthesize(text, voice, rate, destination):
            destination.write_bytes(("tts:" + text).encode("utf-8"))
        payload = {"text": "ABCDEFGHIJ", "voice": "vi-VN-NamMinhNeural", "rate": 1.0}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "segment"
            with patch.object(run_job, "synthesize_chunk", side_effect=fake_synthesize):
                summary = asyncio.run(create_audio_segment(payload, output, 2, max_chars=5))
            self.assertEqual(summary["segment_index"], 2)
            self.assertEqual(summary["total_segments"], 2)
            self.assertEqual(summary["characters"], 5)
            self.assertEqual([p.name for p in output.iterdir()], ["tts 002-002.mp3"])
            self.assertEqual((output / "tts 002-002.mp3").read_bytes(), b"tts:FGHIJ")

    def test_individual_plain_segments_merge_in_original_order(self):
        def fake_merge(parts, output, work_dir):
            output.write_bytes(b"|".join(path.read_bytes() for path in parts))
        payload = {"text": "ABCDEF", "voice": "vi-VN-NamMinhNeural", "rate": 1.0}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            segments = root / "segments"
            segments.mkdir()
            for index, value in enumerate((b"audio-AB", b"audio-CD", b"audio-EF"), start=1):
                (segments / f"tts {index:03d}-003.mp3").write_bytes(value)
            with patch.object(run_job, "merge_mp3", side_effect=fake_merge):
                summary = merge_audio_segments(
                    payload, segments, root / "result", max_chars=2, max_parallel=30,
                    output_name="au7.mp3",
                )
            self.assertEqual(summary["segments"], 3)
            self.assertEqual((root / "result" / "au7.mp3").read_bytes(),
                             b"audio-AB|audio-CD|audio-EF")
            manifest = json.loads((root / "result" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["total_segments"], 3)
            self.assertEqual(manifest["audio_file"], "au7.mp3")
            self.assertEqual(manifest["segment_character_limit"], 2)
            self.assertEqual(manifest["parallel_segments"], 30)

    def test_merge_rejects_a_missing_individual_segment(self):
        payload = {"text": "ABCDEF", "voice": "vi-VN-NamMinhNeural", "rate": 1.0}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            segments = root / "segments"
            segments.mkdir()
            (segments / "tts 001-003.mp3").write_bytes(b"audio")
            with self.assertRaisesRegex(RuntimeError, "Segment artifacts are incomplete"):
                merge_audio_segments(payload, segments, root / "result", max_chars=2)

    def test_four_matrix_shards_merge_plain_mp3_segments(self):
        async def fake_synthesize(text, voice, rate, destination):
            destination.write_text(text, encoding="utf-8")
        def fake_merge(parts, output, work_dir):
            output.write_text("|".join(path.read_text(encoding="utf-8") for path in parts),
                              encoding="utf-8")
        payload = {
            "text": "Chương 1: Mở đầu\nNỘI_DUNG",
            "voice": "vi-VN-NamMinhNeural",
            "rate": 1.0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partials = root / "partials"
            partials.mkdir()
            with patch.object(run_job, "split_text", return_value=[
                "ĐOẠN_1", "ĐOẠN_2", "ĐOẠN_3", "ĐOẠN_4", "ĐOẠN_5",
            ]), patch.object(run_job, "synthesize_chunk", side_effect=fake_synthesize):
                summaries = [
                    asyncio.run(create_audio_shard(payload, root / f"shard-{slot}", slot, 4))
                    for slot in range(4)
                ]
            for slot in range(4):
                source = root / f"shard-{slot}" / f"partial-{slot}.zip"
                (partials / source.name).write_bytes(source.read_bytes())
            self.assertEqual([item["assigned_segments"] for item in summaries], [2, 1, 1, 1])
            with patch.object(run_job, "merge_mp3", side_effect=fake_merge):
                summary = merge_audio_shards(partials, root / "result")
            self.assertEqual(summary["segments"], 5)
            self.assertEqual(summary["parallel_workers"], 4)
            self.assertEqual(
                (root / "result" / "audiobook.mp3").read_text(encoding="utf-8"),
                "ĐOẠN_1|ĐOẠN_2|ĐOẠN_3|ĐOẠN_4|ĐOẠN_5",
            )
            manifest = json.loads((root / "result" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["parallel_segments"], 4)
            self.assertEqual(manifest["segment_character_limit"], 5000)
            self.assertEqual(manifest["total_segments"], 5)

    def test_merge_rejects_missing_matrix_shard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partials = root / "partials"
            partials.mkdir()
            (partials / "partial-0.zip").write_bytes(b"not-a-valid-zip")
            with self.assertRaisesRegex(RuntimeError, "Expected 4 segment ZIP artifacts"):
                merge_audio_shards(partials, root / "result")

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
            self.assertEqual(summary["segment_character_limit"], 5000)
            self.assertEqual(sorted(p.name for p in out.iterdir()),
                             ["audiobook.mp3", "manifest.json"])
            self.assertEqual(
                (out / "audiobook.mp3").read_text(encoding="utf-8"),
                "Chương 1: Đầu. NỘI_DUNG_1_A|"
                "Chương 1: Đầu. NỘI_DUNG_1_B|"
                "Chương 2: Sau. NỘI_DUNG_2_A|"
                "Chương 2: Sau. NỘI_DUNG_2_B",
            )


if __name__ == "__main__":
    unittest.main()
