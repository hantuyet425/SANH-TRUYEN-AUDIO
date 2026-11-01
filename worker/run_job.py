from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import edge_tts
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"SANHAUDIO1"
CHAPTER_RE = re.compile(
    r"(?im)^\\s*((?:chương|chapter|chap\\.?|hồi)\\s+[^\\n]{1,100}|"
    r"第[0-9０-９零〇○一二两兩三四五六七八九十百千万萬亿億]+[章节回节節集话話篇卷部][^\\n]{0,100})\\s*$"
)


def load_key() -> bytes:
    encoded = os.environ.get("AUDIO_SHARED_KEY", "").strip()
    if not encoded:
        raise RuntimeError("Repository secret AUDIO_SHARED_KEY is missing.")
    try:
        key = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise RuntimeError("AUDIO_SHARED_KEY is not valid Base64.") from exc
    if len(key) != 32:
        raise RuntimeError("AUDIO_SHARED_KEY must decode to exactly 32 bytes.")
    return key


def read_encrypted_payload(path: Path, key: bytes) -> dict[str, Any]:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if envelope.get("version") != 1:
        raise RuntimeError("Unsupported encrypted payload version.")
    nonce = base64.b64decode(envelope["nonce"], validate=True)
    ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
    clear = AESGCM(key).decrypt(nonce, ciphertext, MAGIC)
    payload = json.loads(clear.decode("utf-8"))
    if not isinstance(payload.get("text"), str) or not payload["text"].strip():
        raise RuntimeError("The decrypted job contains no text.")
    return payload


def split_chapters(text: str) -> list[tuple[str, str]]:
    normalized = text.replace("\\r\\n", "\\n").replace("\\r", "\\n").strip()
    matches = list(CHAPTER_RE.finditer(normalized))
    if not matches:
        return [("Toàn bộ truyện", normalized)] if normalized else []

    chapters: list[tuple[str, str]] = []
    if normalized[:matches[0].start()].strip():
        chapters.append(("Phần mở đầu", normalized[:matches[0].start()].strip()))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        body = normalized[match.end():end].strip()
        if body:
            chapters.append((match.group(1).strip(), body))
    return chapters


def split_text(text: str, max_chars: int = 2800) -> list[str]:
    text = re.sub(r"\\s+", " ", text).strip()
    parts: list[str] = []
    while len(text) > max_chars:
        window = text[:max_chars]
        cut = max(window.rfind(mark) for mark in ("。", "！", "？", "!", "?", ".", "；", ";", "，", ",", " "))
        if cut < max_chars // 2:
            cut = max_chars
        else:
            cut += 1
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return [part for part in parts if part]


def rate_string(rate: float) -> str:
    value = max(0.5, min(2.0, float(rate)))
    delta = round((value - 1.0) * 100)
    return f"{delta:+d}%"


def safe_filename(value: str, index: int) -> str:
    cleaned = re.sub(r"[^\\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    cleaned = cleaned[:48] or f"Chapter_{index:03d}"
    return f"{index:03d}_{cleaned}.mp3"


async def synthesize_chunk(text: str, voice: str, rate: str, output: Path) -> None:
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
    await communicate.save(str(output))


def merge_mp3(parts: list[Path], output: Path, work_dir: Path) -> None:
    if not parts:
        raise RuntimeError("A chapter produced no speech segments.")
    concat_list = work_dir / "concat.txt"
    lines = []
    for part in parts:
        escaped = str(part.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_list.write_text("\\n".join(lines), encoding="utf-8")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(output)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


async def create_audio(payload: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    text = payload["text"]
    voice = str(payload.get("voice") or "vi-VN-NamMinhNeural")
    rate = rate_string(float(payload.get("rate", 1.0)))
    if voice not in {"vi-VN-NamMinhNeural", "vi-VN-HoaiMyNeural"}:
        raise RuntimeError("Unsupported Vietnamese voice selected.")

    chapters = split_chapters(text)
    if not chapters:
        raise RuntimeError("No audio content could be extracted.")

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="sanh-audio-") as temp_name:
        temp_root = Path(temp_name)
        for chapter_index, (title, body) in enumerate(chapters, start=1):
            chapter_dir = temp_root / f"chapter_{chapter_index:04d}"
            chapter_dir.mkdir()
            chunks = split_text(body)
            part_paths: list[Path] = []
            for part_index, chunk in enumerate(chunks, start=1):
                part_path = chapter_dir / f"part_{part_index:04d}.mp3"
                await synthesize_chunk(chunk, voice, rate, part_path)
                part_paths.append(part_path)
            filename = safe_filename(title, chapter_index)
            target = output_dir / filename
            merge_mp3(part_paths, target, chapter_dir)
            manifest.append({
                "index": chapter_index,
                "file": filename,
                "characters": len(body),
                "segments": len(chunks),
            })

    (output_dir / "manifest.json").write_text(
        json.dumps({"voice": voice, "rate": rate, "chapters": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"chapters": len(manifest), "voice": voice, "rate": rate}


def encrypt_result(output_dir: Path, destination: Path, key: bytes) -> None:
    zip_path = destination.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(output_dir.iterdir()):
            if path.is_file():
                archive.write(path, arcname=path.name)
    plaintext = zip_path.read_bytes()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, MAGIC)
    envelope = {
        "version": 1,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    destination.write_text(json.dumps(envelope, separators=(",", ":")), encoding="utf-8")
    zip_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    key = load_key()
    payload = read_encrypted_payload(args.payload, key)
    expected_job_id = os.environ.get("JOB_ID", "")
    if expected_job_id and payload.get("job_id") != expected_job_id:
        raise RuntimeError("Job ID mismatch.")

    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sanh-mp3-") as output_name:
        audio_dir = Path(output_name)
        summary = asyncio.run(create_audio(payload, audio_dir))
        encrypt_result(audio_dir, args.output / "audio-result.enc", key)

    # Only non-sensitive summary fields are printed. Do not add source text or titles.
    print(f"Audio generation completed: {summary['chapters']} track(s).")


if __name__ == "__main__":
    main()
