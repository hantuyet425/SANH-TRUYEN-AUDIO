# SANH-TRUYEN-AUDIO

Public GitHub Actions worker for the SẢNH TRUYỆN Windows desktop app.

- Receives a plain UTF-8 JSON job payload from a temporary branch.
- Creates exactly one audio job and one MP3 file per detected story chapter; chapters are not split into separate 5,000-character jobs.
- Combines any introductory narration before the first chapter with that first chapter, so the job count matches the number of chapters.
- Sends each whole chapter to Vietnamese Edge TTS in one synthesis stream. Edge TTS handles its own protocol-sized text messages inside that stream; SẢNH TRUYỆN does not create extra MP3 files for those internal messages.
- Creates one visible GitHub Actions matrix job per chapter, named `TTS 001/015`, `TTS 002/015`, and so on.
- Uploads one chapter MP3 per job, then merges all chapter files in source order with FFmpeg into exactly one final `auN.mp3` file (`au1.mp3`, `au2.mp3`, `au3.mp3`, ...).
- Publishes `auN.mp3` and `manifest.json` as the final artifact named `auN`.
- Deletes the temporary input branch after the workflow.

**No AES key, encryption secret, or decryption step is required.** The desktop app needs only a GitHub Fine-grained Personal Access Token.

## Workflow settings

Open **Actions → Generate Audio MP3 → Run workflow**.

| Workflow input | Default | Purpose |
|---|---:|---|
| `max_parallel` | `50` | Maximum number of chapter jobs requested to run simultaneously; actual concurrency depends on GitHub runner/account limits |

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

The app dispatches `.github/workflows/generate-audio-mp3.yml` on `main`, passing `job_id` and `payload_branch`. The preparation job creates one matrix entry per detected chapter. Each matrix job downloads the payload, synthesizes one whole chapter into a single MP3, and uploads a file such as `tts 001-015.mp3`. The workflow requests up to 50 chapter jobs at once, subject to GitHub runner and account concurrency limits. The merge job downloads all chapter artifacts, validates that every expected chapter exists exactly once, and concatenates them in source order.

The final artifact is named `auN` and contains the ordinary files `auN.mp3` and `manifest.json`; the desktop app downloads the artifact ZIP and saves the MP3 locally as `au1.mp3`, `au2.mp3`, and so on.

## Privacy and retention

**This repository is public and this workflow does not encrypt story text or audio.** While a job is running, the source text is present in its temporary public Git branch and in the short-lived payload artifact. The generated chapter MP3 artifacts and final audiobook artifact are also ordinary, unencrypted files. Anyone who can read this public repository may be able to access branch contents while published and workflow artifacts while they remain available.

The cleanup job deletes the temporary branch reference, but deleting a branch is not a guarantee that the underlying Git objects are immediately erased. Artifacts expire after **1 day**. Do not use this workflow for private, confidential, or copyrighted text/audio that must not be publicly accessible. To reduce exposure without AES, make this worker repository private before sending story text to it.

During synthesis, each segment is sent to the online Edge TTS service. Do not use this workflow for content that must not be processed by a third-party TTS provider.

## Local development

```bash
python -m pip install -r requirements.txt
```

`worker/run_job.py` runs inside GitHub Actions and needs `ffmpeg` for audiobook merging. GitHub Actions packages artifacts as ZIP downloads; the files inside those ZIPs are plain `.mp3` files.
