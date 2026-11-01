from __future__ import annotations

import base64
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from worker.run_job import (
    MAGIC,
    encrypt_result,
    rate_string,
    safe_filename,
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

    def test_rate_and_safe_filename(self):
        self.assertEqual(rate_string(1.25), "+25%")
        self.assertEqual(rate_string(0.25), "-50%")
        self.assertEqual(safe_filename("../title|with/slashes", 2), "002_title_with_slashes.mp3")

    def test_result_is_encrypted_zip(self):
        key = bytes(range(32))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "tracks"
            output_dir.mkdir()
            (output_dir / "001_Chuong_1.mp3").write_bytes(b"fake-mp3-data")
            (output_dir / "manifest.json").write_text('{"chapters": 1}', encoding="utf-8")
            encrypted = root / "audio-result.enc"

            encrypt_result(output_dir, encrypted, key)
            envelope = json.loads(encrypted.read_text(encoding="utf-8"))
            self.assertEqual(envelope["version"], 1)
            nonce = base64.b64decode(envelope["nonce"])
            ciphertext = base64.b64decode(envelope["ciphertext"])
            plaintext_zip = AESGCM(key).decrypt(nonce, ciphertext, MAGIC)

            with zipfile.ZipFile(__import__("io").BytesIO(plaintext_zip)) as archive:
                self.assertIn("001_Chuong_1.mp3", archive.namelist())
                self.assertEqual(archive.read("001_Chuong_1.mp3"), b"fake-mp3-data")
                self.assertIn("manifest.json", archive.namelist())


if __name__ == "__main__":
    unittest.main()
