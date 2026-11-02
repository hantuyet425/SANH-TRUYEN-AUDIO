from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Awaitable, Callable

import edge_tts

MAX_CONCURRENT_SEGMENTS = 4  # Legacy in-process path
DEFAULT_MAX_PARALLEL_JOBS = 30  # GitHub Actions per-segment matrix
MAX_SEGMENT_CHARACTERS = 7000
TTS_RETRY_ATTEMPTS = 3
CHAPTER_RE = re.compile(
    r"(?im)^\s*((?:chương|chapter|chap\.?|hồi)\s+[^\n]{1,100}|"
    r"第[0-9０-９零〇○一二两兩三四五六七八九十百千万萬亿億]+[章节回节節集话話篇卷部][^\n]{0,100})\s*$"
)


def read_payload(path: Path) -> dict[str, Any]:
    """Read a plain JSON job payload from the temporary GitHub branch."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("The audio job payload is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("The audio job payload must be a JSON object.")
    if not isinstance(payload.get("text"), str) or not payload["text"].strip():
        raise RuntimeError("The audio job contains no text.")
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
    if max_chars < 1:
        raise ValueError("max_chars must be a positive integer.")
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


def build_audio_plan(
    text: str, max_chars: int = MAX_SEGMENT_CHARACTERS
) -> tuple[list[dict[str, Any]], list[str]]:
    """Create a deterministic chapter/segment plan for all visible matrix jobs."""
    if max_chars < 1:
        raise ValueError("max_chars must be a positive integer.")
    chapters = split_chapters(text)
    chapter_manifest: list[dict[str, Any]] = []
    segment_texts: list[str] = []
    for chapter_index, (title, body) in enumerate(chapters, start=1):
        # Chapter headings are removed from the body by split_chapters().
        # Put real source headings back at the start of the spoken chapter so
        # the audiobook does not jump straight into the chapter body.
        # Synthetic labels are metadata only and must not be narrated.
        spoken_text = body
        if title not in {"Phần mở đầu", "Toàn bộ truyện"}:
            spoken_text = f"{title}. {body}"

        chunks = (
            split_text(spoken_text)
            if max_chars == MAX_SEGMENT_CHARACTERS
            else split_text(spoken_text, max_chars=max_chars)
        )
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


def build_segment_matrix(text: str, max_chars: int = MAX_SEGMENT_CHARACTERS) -> list[dict[str, Any]]:
    """Build one visible GitHub Actions matrix job for every audio segment."""
    _chapters, segments = build_audio_plan(text, max_chars=max_chars)
    if len(segments) > 256:
        raise RuntimeError(
            f"This story creates {len(segments)} segments. GitHub Actions supports at most "
            "256 matrix jobs per workflow run. Increase segment_characters or split the book "
            "into smaller input files."
        )
    return {
        "include": [
            {"segment_index": index, "segment_label": f"{index:03d}"}
            for index in range(1, len(segments) + 1)
        ]
    }


def write_segment_matrix_outputs(
    text: str,
    max_chars: int,
    github_output: str | Path | None,
) -> dict[str, Any]:
    matrix = build_segment_matrix(text, max_chars=max_chars)
    result = {
        "segment_count": len(matrix["include"]),
        "segment_matrix": matrix,
    }
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as stream:
            stream.write(
                "segment_matrix="
                + json.dumps(matrix, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
            stream.write(f"segment_count={len(matrix['include'])}\n")
    return result


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


async def create_audio(
    payload: dict[str, Any],
    output_dir: Path,
    max_chars: int = MAX_SEGMENT_CHARACTERS,
) -> dict[str, Any]:
    """Legacy single-run path, retaining in-process concurrency for local testing."""
    text = payload["text"]
    voice = str(payload.get("voice") or "vi-VN-NamMinhNeural")
    rate = rate_string(float(payload.get("rate", 1.0)))
    if voice not in {"vi-VN-NamMinhNeural", "vi-VN-HoaiMyNeural"}:
        raise RuntimeError("Unsupported Vietnamese voice selected.")

    chapter_manifest, segment_texts = build_audio_plan(text, max_chars=max_chars)
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
        "segment_character_limit": max_chars,
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
        "segment_character_limit": max_chars,
        "voice": voice,
        "rate": rate,
    }


async def create_audio_shard(
    payload: dict[str, Any],
    output_dir: Path,
    shard_index: int,
    shard_count: int = MAX_CONCURRENT_SEGMENTS,
) -> dict[str, Any]:
    """Synthesize this worker's share and package plain MP3 segments in a ZIP."""
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
            partial_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1
        ) as archive:
            for path in sorted(temp_root.iterdir()):
                if path.is_file():
                    archive.write(path, arcname=path.name)

    return {
        "shard_index": shard_index,
        "shard_count": shard_count,
        "assigned_segments": len(assigned),
        "total_segments": len(segment_texts),
    }


async def create_audio_segment(
    payload: dict[str, Any],
    output_dir: Path,
    segment_index: int,
    max_chars: int = MAX_SEGMENT_CHARACTERS,
) -> dict[str, Any]:
    """Synthesize exactly one planned segment and save it as an ordinary MP3."""
    voice = str(payload.get("voice") or "vi-VN-NamMinhNeural")
    rate = rate_string(float(payload.get("rate", 1.0)))
    if voice not in {"vi-VN-NamMinhNeural", "vi-VN-HoaiMyNeural"}:
        raise RuntimeError("Unsupported Vietnamese voice selected.")

    _chapters, segment_texts = build_audio_plan(payload["text"], max_chars=max_chars)
    if segment_index < 1 or segment_index > len(segment_texts):
        raise RuntimeError(
            f"Segment index {segment_index} is outside 1..{len(segment_texts)}."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"tts {segment_index:03d}-{len(segment_texts):03d}.mp3"
    await synthesize_chunk(segment_texts[segment_index - 1], voice, rate, destination)
    return {
        "segment_index": segment_index,
        "total_segments": len(segment_texts),
        "characters": len(segment_texts[segment_index - 1]),
        "file": destination.name,
    }


def merge_audio_segments(
    payload: dict[str, Any],
    segments_dir: Path,
    output_dir: Path,
    max_chars: int = MAX_SEGMENT_CHARACTERS,
    max_parallel: int = DEFAULT_MAX_PARALLEL_JOBS,
    output_name: str = "au1.mp3",
) -> dict[str, Any]:
    """Merge plain MP3 segment files into one ordered auN.mp3 audiobook."""
    voice = str(payload.get("voice") or "vi-VN-NamMinhNeural")
    rate = rate_string(float(payload.get("rate", 1.0)))
    if voice not in {"vi-VN-NamMinhNeural", "vi-VN-HoaiMyNeural"}:
        raise RuntimeError("Unsupported Vietnamese voice selected.")
    if max_parallel not in {1, 2, 3, 4, 6, 8, 10, 20, 30}:
        raise RuntimeError("max_parallel must be one of: 1, 2, 3, 4, 6, 8, 10, 20, 30.")
    if not re.fullmatch(r"au[1-9][0-9]*\.mp3", output_name):
        raise RuntimeError("output_name must use the au1.mp3, au2.mp3, ... naming format.")

    chapter_manifest, segment_texts = build_audio_plan(payload["text"], max_chars=max_chars)
    expected_indices = list(range(1, len(segment_texts) + 1))
    actual_files = {
        path.name for path in segments_dir.glob("tts *.mp3") if path.is_file()
    }
    expected_files = {
        f"tts {index:03d}-{len(segment_texts):03d}.mp3"
        for index in expected_indices
    }
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise RuntimeError(
            f"Segment artifacts are incomplete (missing={len(missing)}, unexpected={len(extra)})."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sanh-audio-merge-") as temp_name:
        temp_root = Path(temp_name)
        ordered_paths: list[Path] = []
        for index in expected_indices:
            source = segments_dir / f"tts {index:03d}-{len(segment_texts):03d}.mp3"
            destination = temp_root / f"segment_{index:06d}.mp3"
            destination.write_bytes(source.read_bytes())
            ordered_paths.append(destination)

        audiobook = output_dir / output_name
        merge_mp3(ordered_paths, audiobook, temp_root)

    manifest = {
        "audio_file": output_name,
        "voice": voice,
        "rate": rate,
        "parallel_segments": max_parallel,
        "segment_character_limit": max_chars,
        "total_segments": len(segment_texts),
        "chapters": chapter_manifest,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "segments": len(segment_texts),
        "segment_character_limit": max_chars,
        "parallel_segments": max_parallel,
        "voice": voice,
        "rate": rate,
        "audio_file": output_name,
    }


def merge_audio_shards(partials_dir: Path, output_dir: Path) -> dict[str, Any]:
    """Merge four ZIP packages containing ordinary MP3 segments."""
    partial_archives = sorted(partials_dir.glob("partial-*.zip"))
    if len(partial_archives) != MAX_CONCURRENT_SEGMENTS:
        raise RuntimeError(
            f"Expected {MAX_CONCURRENT_SEGMENTS} segment ZIP artifacts; "
            f"found {len(partial_archives)}."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sanh-audio-merge-") as temp_name:
        temp_root = Path(temp_name)
        segment_root = temp_root / "segments"
        segment_root.mkdir()
        reference_manifest: dict[str, Any] | None = None
        found_indices: set[int] = set()

        for partial_archive in partial_archives:
            try:
                with zipfile.ZipFile(partial_archive) as archive:
                    manifest = json.loads(archive.read("partial-manifest.json").decode("utf-8"))
                    if reference_manifest is None:
                        reference_manifest = manifest
                    else:
                        for field in (
                            "voice", "rate", "parallel_segments",
                            "segment_character_limit", "total_segments",
                            "chapters", "shard_count",
                        ):
                            if manifest.get(field) != reference_manifest.get(field):
                                raise RuntimeError("Audio shard metadata does not match.")
                    for index in manifest.get("assigned_indices", []):
                        index = int(index)
                        if index in found_indices:
                            raise RuntimeError("Duplicate audio segment received from matrix workers.")
                        name = f"segment_{index:06d}.mp3"
                        if name not in archive.namelist():
                            raise RuntimeError(f"Audio shard is missing segment {index}.")
                        (segment_root / name).write_bytes(archive.read(name))
                        found_indices.add(index)
            except (zipfile.BadZipFile, KeyError, ValueError, TypeError) as exc:
                raise RuntimeError(f"Invalid audio segment archive: {partial_archive.name}") from exc

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

    return {
        "segments": total_segments,
        "parallel_workers": MAX_CONCURRENT_SEGMENTS,
        "voice": final_manifest["voice"],
        "rate": final_manifest["rate"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--shard-index", type=int)
    parser.add_argument("--shard-count", type=int, default=MAX_CONCURRENT_SEGMENTS)
    parser.add_argument("--merge-partials", type=Path)
    parser.add_argument("--prepare-matrix", action="store_true")
    parser.add_argument("--segment-index", type=int)
    parser.add_argument("--merge-segments", type=Path)
    parser.add_argument("--segment-limit", type=int, default=MAX_SEGMENT_CHARACTERS)
    parser.add_argument("--max-parallel", type=int, choices=(1, 2, 3, 4, 6, 8, 10, 20, 30), default=DEFAULT_MAX_PARALLEL_JOBS)
    parser.add_argument("--output-name", default="au1.mp3")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)

    if args.merge_partials is not None:
        summary = merge_audio_shards(args.merge_partials, args.output)
        print(
            "Audio merge completed: "
            f"{summary['segments']} segment(s), "
            f"{summary['parallel_workers']} parallel worker(s)."
        )
        return

    if args.payload is None:
        parser.error("--payload is required for matrix preparation, segment synthesis, and audiobook merge.")

    payload = read_payload(args.payload)
    expected_job_id = os.environ.get("JOB_ID", "")
    if expected_job_id and payload.get("job_id") != expected_job_id:
        raise RuntimeError("Job ID mismatch.")

    if args.prepare_matrix:
        output_path = os.environ.get("GITHUB_OUTPUT")
        summary = write_segment_matrix_outputs(
            payload["text"], args.segment_limit, output_path
        )
        print(
            f"Prepared {summary['segment_count']} visible TTS segment job(s) "
            f"at a maximum of {args.segment_limit} characters each."
        )
        return

    if args.segment_index is not None:
        summary = asyncio.run(
            create_audio_segment(
                payload, args.output, args.segment_index, args.segment_limit
            )
        )
        print(
            f"TTS {summary['segment_index']:03d}/{summary['total_segments']:03d} "
            f"saved as {summary['file']} "
            f"({summary['characters']} characters)."
        )
        return

    if args.merge_segments is not None:
        summary = merge_audio_segments(
            payload, args.merge_segments, args.output,
            args.segment_limit, args.max_parallel, args.output_name
        )
        print(
            f"Merged {summary['segments']} segment(s) into {summary['audio_file']}."
        )
        return

    if args.shard_index is not None:
        summary = asyncio.run(
            create_audio_shard(
                payload, args.output, args.shard_index, args.shard_count
            )
        )
        print(
            f"Audio shard {summary['shard_index'] + 1}/{summary['shard_count']} completed: "
            f"{summary['assigned_segments']} of {summary['total_segments']} segment(s)."
        )
        return

    summary = asyncio.run(create_audio(payload, args.output, max_chars=args.segment_limit))
    print(
        "Audio generation completed: "
        f"{summary['chapters']} chapter(s), "
        f"{summary['segments']} segment(s), "
        f"up to {summary['parallel_segments']} concurrent TTS requests."
    )

if __name__ == "__main__":
    main()
