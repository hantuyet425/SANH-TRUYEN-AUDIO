# SANH-TRUYEN-AUDIO

Public GitHub Actions worker for the SẢNH TRUYỆN Windows desktop app.

- Receives a plain UTF-8 JSON job payload from a temporary branch.
- Splits the story into speech segments, with a default maximum of 7,000 characters per segment.
- Creates one visible GitHub Actions matrix job per segment, named `TTS 001/207`, `TTS 002/207`, and so on.
- Synthesizes each segment with Vietnamese Edge TTS and uploads it as a normal MP3 file such as `tts 001-207.mp3` (the first number is the segment and the second is the total).
- Merges every segment in source order with FFmpeg into exactly one final `auN.mp3` file (`au1.mp3`, `au2.mp3`, `au3.mp3`, ...).
- Publishes `auN.mp3` and `manifest.json` as the final artifact named `auN`.
- Deletes the temporary input branch after the workflow.

**No AES key, encryption secret, or decryption step is required.** The desktop app needs only a GitHub Fine-grained Personal Access Token.

## Workflow settings

Open **Actions → Generate Audio MP3 → Run workflow**.

| Workflow input | Default | Purpose |
|---|---:|---|
| `segment_characters` | `7000` | Maximum character count sent to one TTS segment job |
| `max_parallel` | `100` | Maximum number of segment jobs requested to run simultaneously |

GitHub may queue jobs when runner capacity or account concurrency limits are reached. A workflow run can contain at most 256 matrix jobs; the preparation step stops with an error if the story would exceed that limit.

## One-time setup

1. Create a Fine-grained Personal Access Token for this repository only.
2. Grant **Contents: Read and write** and **Actions: Read and write**.
3. In the desktop app, open **Phòng thu audio → CẤU HÌNH GITHUB**, paste the token, then click **KIỂM TRA & LƯU TOKEN**.
4. Ensure GitHub Actions are enabled for this repository. The workflow has `contents: write` permission so its cleanup job can delete the temporary input branch.

There is no `AUDIO_SHARED_KEY` repository secret to create.

## Wire protocol

The desktop app creates a random branch named `audio-job-<uuid>` and uploads:

`audio_jobs/<uuid>/payload.json`

The file is plain UTF-8 JSON:

```json
{
  "job_id": "uuid",
  "text": "Vietnamese novel text",
  "voice": "vi-VN-NamMinhNeural",
  "rate": 1.0
}
```

The app dispatches `.github/workflows/generate-audio-mp3.yml` on `main`, passing `job_id` and `payload_branch`. The preparation job creates the matrix. Each matrix job downloads the payload, synthesizes one segment, and uploads a normal MP3 such as `tts 001-207.mp3`. The merge job downloads all segment artifacts, validates that every expected segment exists exactly once, and concatenates them in source order.

The final artifact is named `auN` and contains the ordinary files `auN.mp3` and `manifest.json`; the desktop app downloads the artifact ZIP and saves the MP3 as `au1.mp3`, `au2.mp3`, and so on.

## Privacy and retention

**This repository is public and this workflow does not encrypt story text or audio.** While a job is running, the source text is present in its temporary public Git branch and in the short-lived payload artifact. The generated MP3 segment artifacts and final audiobook artifact are also ordinary, unencrypted files. Anyone who can read this public repository may be able to access branch contents while published and workflow artifacts while they remain available.

The cleanup job deletes the temporary branch reference, but deleting a branch is not a guarantee that the underlying Git objects are immediately erased. Artifacts expire after **1 day**. Do not use this workflow for private, confidential, or copyrighted text/audio that must not be publicly accessible. To reduce exposure without AES, make this worker repository private before sending story text to it.

During synthesis, each segment is sent to the online Edge TTS service. Do not use this workflow for content that must not be processed by a third-party TTS provider.

## Local development

```bash
python -m pip install -r requirements.txt
```

`worker/run_job.py` runs inside GitHub Actions and needs `ffmpeg` for audiobook merging. GitHub Actions packages artifacts as ZIP downloads; the files inside those ZIPs are plain `.mp3` files.
