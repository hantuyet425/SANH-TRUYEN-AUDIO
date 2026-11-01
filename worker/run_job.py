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
from typing import Any, Awaitable, Callable

import edge_tts
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"SANHAUDIO1"
MAX_CONCURRENT_SEGMENTS = 4
MAX_SEGMENT_CHARACTERS = 5000
TTS_RETRY_ATTEMPTS = 3
CHAPTER_RE = re.compile(
    r"(?im)^\s*((?:chương|chapter|chap\.?|hồi)\s+[^\n]{1,100}|"
    r"第[0-9０-９零〇○一二两兩三四五六七八九十百千万萬亿億]+[章节回节節集话話篇卷部][^\n]{0,100})\s*$"
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
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
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


def split_text(text: str, max_chars: int = MAX_SEGMENT_CHARACTERS) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
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


def build_audio_plan(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Create a deterministic chapter/segment plan shared by all matrix workers."""
    chapters = split_chapters(text)
    chapter_manifest: list[dict[str, Any]] = []
    segment_texts: list[str] = []
    for chapter_index, (title, body) in enumerate(chapters, start=1):
        chunks = split_text(body)
        if not chunks:
            continue
        first_segment = len(segment_texts) + 1
        segment_texts.extend(chunks)
        chapter_manifest.append({
            "index": chapter_index,
            "title": title,
            "characters": len(body),
            "segments": len(chunks),
            "first_segment": first_segment,
            "last_segment": len(segment_texts),
        })
    if not segment_texts:
        raise RuntimeError("No audio segments could be created.")
    return chapter_manifest, segment_texts


def rate_string(rate: float) -> str:
    value = max(0.5, min(2.0, float(rate)))
    delta = round((value - 1.0) * 100)
    return f"{delta:+d}%"


async def synthesize_chunk(text: str, voice: str, rate: str, output: Path) -> None:
    last_error: Exception | None = None
    for attempt in range(TTS_RETRY_ATTEMPTS):
        try:
            communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
            await communicate.save(str(output))
            if not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError("The TTS provider returned an empty audio segment.")
            return
        except Exception as exc:
            last_error = exc
            output.unlink(missing_ok=True)
            if attempt + 1 < TTS_RETRY_ATTEMPTS:
                await asyncio.sleep(2 ** attempt)
    raise RuntimeError(
        f"An audio segment failed after {TTS_RETRY_ATTEMPTS} attempts."
    ) from last_error


async def synthesize_segments(
    segments: list[tuple[str, Path]],
    voice: str,
    rate: str,
    max_concurrency: int = MAX_CONCURRENT_SEGMENTS,
    synthesize_fn: Callable[[str, str, str, Path], Awaitable[None]] | None = None,
) -> list[Path]:
    """Synthesize independent text segments concurrently, preserving source order."""
    if not segments:
        return []
    concurrency = max(1, min(MAX_CONCURRENT_SEGMENTS, int(max_concurrency)))
    semaphore = asyncio.Semaphore(concurrency)
    synthesize = synthesize_fn or synthesize_chunk

    async def run_one(text: str, destination: Path) -> None:
        async with semaphore:
            await synthesize(text, voice, rate, destination)

    # asyncio.gather returns results in input order, not completion order.
    await asyncio.gather(*(run_one(text, path) for text, path in segments))
    return [path for _text, path in segments]


def merge_mp3(parts: list[Path], output: Path, work_dir: Path) -> None:
    """Join MP3 segments in the exact order supplied using ffmpeg's concat demuxer."""
    if not parts:
        raise RuntimeError("No synthesized audio segments are available to merge.")
    concat_list = work_dir / "audiobook-concat.txt"
    lines = []
    for part in parts:
        escaped = str(part.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_list.write_text("\n".join(lines), encoding="utf-8")
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy", str(output),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("ffmpeg did not produce a valid audiobook file.")


async def create_audio(payload: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """Legacy single-run path, retaining in-process concurrency for local testing."""
    text = payload["text"]
    voice = str(payload.get("voice") or "vi-VN-NamMinhNeural")
    rate = rate_string(float(payload.get("rate", 1.0)))
    if voice not in {"vi-VN-NamMinhNeural", "vi-VN-HoaiMyNeural"}:
        raise RuntimeError("Unsupported Vietnamese voice selected.")

    chapter_manifest, segment_texts = build_audio_plan(text)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="sanh-audio-") as temp_name:
        temp_root = Path(temp_name)
        segment_jobs: list[tuple[str, Path]] = []
        for index, chunk in enumerate(segment_texts, start=1):
            segment_jobs.append((chunk, temp_root / f"segment_{index:06d}.mp3"))

        ordered_paths = await synthesize_segments(
            segment_jobs,
            voice,
            rate,
            max_concurrency=MAX_CONCURRENT_SEGMENTS,
        )
        audiobook_path = output_dir / "audiobook.mp3"
        merge_mp3(ordered_paths, audiobook_path, temp_root)

    manifest = {
        "audio_file": "audiobook.mp3",
        "voice": voice,
        "rate": rate,
        "parallel_segments": MAX_CONCURRENT_SEGMENTS,
        "segment_character_limit": MAX_SEGMENT_CHARACTERS,
        "total_segments": len(segment_texts),
        "chapters": chapter_manifest,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "chapters": len(chapter_manifest),
        "segments": len(segment_texts),
        "parallel_segments": MAX_CONCURRENT_SEGMENTS,
        "segment_character_limit": MAX_SEGMENT_CHARACTERS,
        "voice": voice,
        "rate": rate,
    }


async def create_audio_shard(
    payload: dict[str, Any],
    output_dir: Path,
    key: bytes,
    shard_index: int,
    shard_count: int = MAX_CONCURRENT_SEGMENTS,
) -> dict[str, Any]:
    """Synthesize this matrix worker's share, then encrypt its MP3 segments."""
    if shard_count != MAX_CONCURRENT_SEGMENTS:
        raise RuntimeError(f"Exactly {MAX_CONCURRENT_SEGMENTS} worker shards are required.")
    if not 0 <= shard_index < shard_count:
        raise RuntimeError("Invalid audio shard index.")

    voice = str(payload.get("voice") or "vi-VN-NamMinhNeural")
    rate = rate_string(float(payload.get("rate", 1.0)))
    if voice not in {"vi-VN-NamMinhNeural", "vi-VN-HoaiMyNeural"}:
        raise RuntimeError("Unsupported Vietnamese voice selected.")
    chapters, segment_texts = build_audio_plan(payload["text"])
    assigned = [
        (index, chunk)
        for index, chunk in enumerate(segment_texts, start=1)
        if (index - 1) % shard_count == shard_index
    ]

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"sanh-audio-shard-{shard_index}-") as temp_name:
        temp_root = Path(temp_name)
        for index, chunk in assigned:
            destination = temp_root / f"segment_{index:06d}.mp3"
            # One TTS request at a time per runner; the four matrix workers
            # provide the four-way parallelism visible in GitHub Actions.
            await synthesize_chunk(chunk, voice, rate, destination)

        partial_manifest = {
            "voice": voice,
            "rate": rate,
            "parallel_segments": shard_count,
            "segment_character_limit": MAX_SEGMENT_CHARACTERS,
            "total_segments": len(segment_texts),
            "chapters": chapters,
            "shard_index": shard_index,
            "shard_count": shard_count,
            "assigned_indices": [index for index, _chunk in assigned],
        }
        (temp_root / "partial-manifest.json").write_text(
            json.dumps(partial_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        partial_zip = output_dir / f"partial-{shard_index}.zip"
        with zipfile.ZipFile(
            partial_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for path in sorted(temp_root.iterdir()):
                if path.is_file():
                    archive.write(path, arcname=path.name)
        encrypt_file(partial_zip, output_dir / f"partial-{shard_index}.enc", key)
        partial_zip.unlink(missing_ok=True)

    return {
        "shard_index": shard_index,
        "shard_count": shard_count,
        "assigned_segments": len(assigned),
        "total_segments": len(segment_texts),
    }


def decrypt_file(source: Path, key: bytes) -> bytes:
    try:
        envelope = json.loads(source.read_text(encoding="utf-8"))
        if envelope.get("version") != 1:
            raise ValueError("unsupported encrypted envelope")
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        return AESGCM(key).decrypt(nonce, ciphertext, MAGIC)
    except Exception as exc:
        raise RuntimeError(f"Could not decrypt encrypted audio package: {source.name}") from exc


def encrypt_file(source: Path, destination: Path, key: bytes) -> None:
    plaintext = source.read_bytes()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, MAGIC)
    envelope = {
        "version": 1,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(envelope, separators=(",", ":")), encoding="utf-8")


def merge_audio_shards(partials_dir: Path, output_dir: Path, key: bytes) -> dict[str, Any]:
    """Decrypt four shard artifacts, validate completeness, and build one audiobook."""
    encrypted_partials = sorted(partials_dir.glob("partial-*.enc"))
    if len(encrypted_partials) != MAX_CONCURRENT_SEGMENTS:
        raise RuntimeError(
            f"Expected {MAX_CONCURRENT_SEGMENTS} encrypted segment artifacts; "
            f"found {len(encrypted_partials)}."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sanh-audio-merge-") as temp_name:
        temp_root = Path(temp_name)
        segment_root = temp_root / "segments"
        segment_root.mkdir()
        reference_manifest: dict[str, Any] | None = None
        found_indices: set[int] = set()

        for encrypted_partial in encrypted_partials:
            clear_zip = decrypt_file(encrypted_partial, key)
            try:
                with zipfile.ZipFile(__import__("io").BytesIO(clear_zip)) as archive:
                    manifest = json.loads(archive.read("partial-manifest.json").decode("utf-8"))
                    if reference_manifest is None:
                        reference_manifest = manifest
                    else:
                        for field in ("voice", "rate", "parallel_segments",
                                      "segment_character_limit", "total_segments",
                                      "chapters", "shard_count"):
                            if manifest.get(field) != reference_manifest.get(field):
                                raise RuntimeError("Audio shard metadata does not match.")
                    for index in manifest.get("assigned_indices", []):
                        index = int(index)
                        if index in found_indices:
                            raise RuntimeError("Duplicate audio segment received from matrix workers.")
                        name = f"segment_{index:06d}.mp3"
                        if name not in archive.namelist():
                            raise RuntimeError(f"Audio shard is missing segment {index}.")
                        destination = segment_root / name
                        destination.write_bytes(archive.read(name))
                        found_indices.add(index)
            except (zipfile.BadZipFile, KeyError, ValueError, TypeError) as exc:
                raise RuntimeError(f"Invalid encrypted segment archive: {encrypted_partial.name}") from exc

        if reference_manifest is None:
            raise RuntimeError("No audio shard metadata was received.")
        total_segments = int(reference_manifest.get("total_segments", 0))
        expected = set(range(1, total_segments + 1))
        if found_indices != expected:
            missing = len(expected - found_indices)
            extra = len(found_indices - expected)
            raise RuntimeError(
                f"Audio shards are incomplete (missing={missing}, unexpected={extra})."
            )

        ordered_paths = [
            segment_root / f"segment_{index:06d}.mp3"
            for index in range(1, total_segments + 1)
        ]
        audiobook = output_dir / "audiobook.mp3"
        merge_mp3(ordered_paths, audiobook, temp_root)
        final_manifest = {
            "audio_file": "audiobook.mp3",
            "voice": reference_manifest["voice"],
            "rate": reference_manifest["rate"],
            "parallel_segments": MAX_CONCURRENT_SEGMENTS,
            "segment_character_limit": MAX_SEGMENT_CHARACTERS,
            "total_segments": total_segments,
            "chapters": reference_manifest["chapters"],
        }
        (output_dir / "manifest.json").write_text(
            json.dumps(final_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        encrypt_result(output_dir, output_dir / "audio-result.enc", key)

    return {
        "segments": total_segments,
        "parallel_workers": MAX_CONCURRENT_SEGMENTS,
        "voice": final_manifest["voice"],
        "rate": final_manifest["rate"],
    }


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
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--shard-index", type=int)
    parser.add_argument("--shard-count", type=int, default=MAX_CONCURRENT_SEGMENTS)
    parser.add_argument("--merge-partials", type=Path)
    args = parser.parse_args()

    key = load_key()
    args.output.mkdir(parents=True, exist_ok=True)

    if args.merge_partials is not None:
        summary = merge_audio_shards(args.merge_partials, args.output, key)
        print(
            "Audio merge completed: "
            f"{summary['segments']} segment(s), "
            f"{summary['parallel_workers']} parallel worker(s)."
        )
        return

    if args.payload is None:
        parser.error("--payload is required unless --merge-partials is used.")

    payload = read_encrypted_payload(args.payload, key)
    expected_job_id = os.environ.get("JOB_ID", "")
    if expected_job_id and payload.get("job_id") != expected_job_id:
        raise RuntimeError("Job ID mismatch.")

    if args.shard_index is not None:
        summary = asyncio.run(
            create_audio_shard(
                payload, args.output, key, args.shard_index, args.shard_count
            )
        )
        print(
            f"Audio shard {summary['shard_index'] + 1}/{summary['shard_count']} completed: "
            f"{summary['assigned_segments']} of {summary['total_segments']} segment(s)."
        )
        return

    # Legacy single-run mode retained for local development and backward compatibility.
    with tempfile.TemporaryDirectory(prefix="sanh-mp3-") as output_name:
        audio_dir = Path(output_name)
        summary = asyncio.run(create_audio(payload, audio_dir))
        encrypt_result(audio_dir, args.output / "audio-result.enc", key)

    print(
        "Audio generation completed: "
        f"{summary['chapters']} chapter(s), "
        f"{summary['segments']} segment(s), "
        f"up to {summary['parallel_segments']} concurrent TTS requests."
    )


if __name__ == "__main__":
    main()
