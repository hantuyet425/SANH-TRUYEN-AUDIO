# SANH-TRUYEN-AUDIO

Public GitHub Actions worker for the SẢNH TRUYỆN Windows desktop app.

- Receives an **AES-256-GCM encrypted** job file from a temporary branch.
- Uses a preparation job to split chapter text into speech segments of at most the configured character limit.
- Creates **one visible GitHub Actions matrix job per segment**. In the run's **All jobs** list, each item is named \`TTS segment 001 / N\`, \`TTS segment 002 / N\`, and so on; each has its own queued, in-progress, success, or failure status.
- Limits simultaneous segment jobs with the \`max_parallel\` workflow input. The default is **4**: when one segment job completes, GitHub starts the next queued segment automatically.
- Synthesizes each segment with Vietnamese Edge TTS, retries failed TTS requests up to three times, and uploads that segment's MP3 only as an AES-256-GCM encrypted artifact.
- Runs a final merge job that verifies every segment artifact exists exactly once, restores original source order, and produces exactly **one \`audiobook.mp3\`** using FFmpeg.
- Returns one **AES-256-GCM encrypted ZIP** containing \`audiobook.mp3\` and \`manifest.json\` as the final workflow artifact.
- Deletes the temporary encrypted input branch after the workflow.
- Keeps plaintext story text and plaintext MP3 files off GitHub branches, artifacts, and logs.

## Easy workflow settings

Open **Actions → Generate Audio → Run workflow**. The two controls are also declared near the top of \`.github/workflows/generate-audio.yml\`:

| Workflow input | Default | Purpose |
|---|---:|---|
| \`segment_characters\` | \`7000\` | Maximum character count sent to one TTS segment job |
| \`max_parallel\` | \`100\` | Maximum number of segment jobs allowed to run at the same time |

To change the defaults permanently, edit the \`default\` value for these two inputs in \`.github/workflows/generate-audio.yml\`. To change settings for one manual run, select different values in the **Run workflow** form. The desktop app sends only the required \`job_id\` and \`payload_branch\`; GitHub applies these workflow defaults automatically.

The \`max_parallel\` choice currently offers 1, 2, 3, 4, 6, 8, 10, 20, 50, or 100. The default is 100. This is the workflow's requested ceiling, not a guarantee that 100 GitHub-hosted runners can start simultaneously: GitHub applies an account-wide concurrency limit based on its plan, and excess matrix jobs remain queued until runner capacity is available.

**GitHub matrix limit:** one workflow run can contain at most 256 matrix jobs. The preparation job stops with a clear error before synthesis if the selected segment size would create more than 256 segments. For a very long book, increase \`segment_characters\` or divide the source into multiple book files.

## One-time setup

1. In **Settings → Secrets and variables → Actions → New repository secret**, add:
   - Name: \`AUDIO_SHARED_KEY\`
   - Value: a 32-byte key encoded as Base64 (generate it with \`python tools/generate_shared_key.py\`).
2. In Settings → Actions → General, allow GitHub Actions for the repository. The workflow has explicit \`contents: write\` permission because it deletes its temporary input branch.
3. The desktop app uses GitHub CLI authentication and stores the shared key in Windows Credential Manager. Never paste either value into ChatGPT, source code, a commit, or an issue.

## Wire protocol

The desktop app creates a random branch such as \`audio-job-<uuid>\`, based on \`main\`, and uploads a single file:

\`audio_jobs/<uuid>/payload.json.enc\`

The file is JSON with:

\`\`\`json
{
  "version": 1,
  "nonce": "BASE64_12_BYTE_NONCE",
  "ciphertext": "BASE64_AES_GCM_CIPHERTEXT_AND_TAG"
}
\`\`\`

The decrypted payload is UTF-8 JSON:

\`\`\`json
{
  "job_id": "uuid",
  "text": "Vietnamese novel text",
  "voice": "vi-VN-NamMinhNeural",
  "rate": 1.0
}
\`\`\`

The app dispatches \`.github/workflows/generate-audio.yml\` on \`main\`, with \`job_id\` and \`payload_branch\` inputs. The workflow decrypts the payload in runner memory and prepares the segment matrix. The encrypted input is uploaded as a short-lived encrypted artifact so each matrix job can download it independently; no plaintext story text is stored in artifacts.

Each matrix job handles exactly one segment. Its name and status are visible separately in **All jobs**, and the \`max_parallel\` setting controls how many of those jobs run concurrently. Each job independently decrypts the source in memory, synthesizes only its assigned segment, encrypts the resulting MP3, and uploads that encrypted segment artifact.

After all segment jobs succeed, the merge job downloads all encrypted segment artifacts, verifies the exact expected segment index set, decrypts them in runner memory, and concatenates them in source order. The final artifact is named \`audio-<job_id>\` and contains only \`audio-result.enc\`; after decryption it is a ZIP containing exactly one complete \`audiobook.mp3\` plus \`manifest.json\`.

## Important privacy and retention notes

- The temporary Git branch contains ciphertext only. Branch deletion removes the reference, but Git objects may remain reachable for some time; ciphertext remains encrypted.
- The encrypted input artifact and all encrypted segment artifacts expire after **1 day**.
- Workflow metadata, segment numbers, run timestamps, status, and job IDs are visible in this public repository. Do not use story titles or chapter titles in branch names, job IDs, workflow run names, or logs.
- Anyone with read access to a public repository can access its Actions artifacts. Encryption is therefore mandatory even though artifact retention is short.
- Anyone who obtains \`AUDIO_SHARED_KEY\` can decrypt both the uploaded source and returned audio. Protect it as a password.
- During synthesis, the GitHub runner decrypts the text in memory and sends each segment to the online Edge TTS service over the network. Do not use this workflow for content that must not be processed by a third-party TTS provider.

## Local development

\`\`\`bash
python -m pip install -r requirements.txt
python tools/generate_shared_key.py
\`\`\`

\`worker/run_job.py\` is intended to run inside GitHub Actions; it requires \`ffmpeg\`, \`AUDIO_SHARED_KEY\`, and a valid encrypted input file.
