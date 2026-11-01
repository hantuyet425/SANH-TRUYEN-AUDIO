# SANH-TRUYEN-AUDIO

Public GitHub Actions worker for the SẢNH TRUYỆN Windows desktop app.

- Receives an **AES-256-GCM encrypted** job file from a temporary branch.
- Runs Vietnamese Edge TTS on a GitHub-hosted Ubuntu runner.
- Creates one MP3 per detected chapter (or one MP3 if no chapter headings are found).
- Returns an **AES-256-GCM encrypted ZIP** as a workflow artifact.
- Deletes the temporary input branch after the job.
- Keeps plaintext story text and plaintext MP3 files off GitHub branches, artifacts, and logs.

> This repository is public by design. Do not weaken the encryption flow. Both the upload payload and output archive must remain encrypted at rest in GitHub. Workflow logs must never print source text, chapter titles, encryption keys, or audio data.

## One-time setup

1. In **Settings → Secrets and variables → Actions → New repository secret**, add:
   - Name: `AUDIO_SHARED_KEY`
   - Value: a 32-byte key encoded as Base64 (generate it with `python tools/generate_shared_key.py`).
2. In Settings → Actions → General, allow GitHub Actions for the repository. The workflow has explicit `contents: write` permission because it deletes its temporary input branch.
3. Create a fine-grained personal access token limited to this repository with:
   - Contents: Read and write
   - Actions: Read and write
4. The desktop app must keep the same Base64 key locally, protected with Windows DPAPI, and keep the token in Windows Credential Manager/DPAPI. Never paste either value into ChatGPT, source code, a commit, or an issue.

## Wire protocol

The desktop app creates a random branch such as `audio-job-<uuid>`, based on `main`, and uploads a single file:

`audio_jobs/<uuid>/payload.json.enc`

The file is JSON with:

```json
{
  "version": 1,
  "nonce": "BASE64_12_BYTE_NONCE",
  "ciphertext": "BASE64_AES_GCM_CIPHERTEXT_AND_TAG"
}
```

The decrypted payload is UTF-8 JSON:

```json
{
  "job_id": "uuid",
  "text": "Vietnamese novel text",
  "voice": "vi-VN-NamMinhNeural",
  "rate": 1.0
}
```

The app then dispatches `.github/workflows/generate-audio.yml` on `main`, with `job_id` and `payload_branch` inputs. The workflow reads the encrypted file from that branch, decrypts it using `AUDIO_SHARED_KEY`, and synthesizes the audio.

The artifact is named `audio-<job_id>` and contains only `audio-result.enc`. After decryption, that file is a ZIP containing chapter MP3 files and `manifest.json`. The output encryption uses the same AES-256-GCM key and a fresh nonce.

## Important privacy and retention notes

- The temporary Git branch contains ciphertext only. Branch deletion removes the reference, but Git objects may remain reachable for some time; ciphertext remains encrypted.
- The Actions artifact contains ciphertext only and expires after **1 day**.
- Workflow metadata, run timestamps, status, and job IDs are visible in this public repository. Do not use story titles or chapter titles in branch names, job IDs, workflow run names, or logs.
- Anyone with read access to a public repository can access its Actions artifacts. Encryption is therefore mandatory even though artifact retention is short.
- Anyone who obtains `AUDIO_SHARED_KEY` can decrypt both the uploaded source and returned audio. Protect it as a password.

## Local development

```bash
python -m pip install -r requirements.txt
python tools/generate_shared_key.py
```

`worker/run_job.py` is intended to run inside GitHub Actions; it requires `ffmpeg`, `AUDIO_SHARED_KEY`, and a valid encrypted input file.
