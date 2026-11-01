"""Small client-side encryption helpers shared with SẢNH TRUYỆN.

The desktop app should protect the Base64 key using Windows DPAPI and should
never store it in settings.json or commit it to Git.
"""
from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"SANHAUDIO1"


def _key_from_b64(shared_key_b64: str) -> bytes:
    try:
        key = base64.b64decode(shared_key_b64.strip(), validate=True)
    except Exception as exc:
        raise ValueError("AUDIO_SHARED_KEY phải là chuỗi Base64 hợp lệ.") from exc
    if len(key) != 32:
        raise ValueError("AUDIO_SHARED_KEY phải giải mã thành đúng 32 byte (AES-256).")
    return key


def encrypt_job(payload: dict[str, Any], shared_key_b64: str) -> bytes:
    """Return the JSON envelope to upload as payload.json.enc."""
    required = {"job_id", "text", "voice", "rate"}
    missing = required.difference(payload)
    if missing:
        raise ValueError("Thiếu trường trong payload: " + ", ".join(sorted(missing)))
    if not isinstance(payload["text"], str) or not payload["text"].strip():
        raise ValueError("Nội dung audio đang trống.")

    key = _key_from_b64(shared_key_b64)
    nonce = secrets.token_bytes(12)
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, MAGIC)
    envelope = {
        "version": 1,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def decrypt_result(encrypted_file: str | Path, destination_zip: str | Path, shared_key_b64: str) -> Path:
    """Decrypt audio-result.enc to a ZIP file on the local computer."""
    source = Path(encrypted_file)
    output = Path(destination_zip)
    envelope = json.loads(source.read_text(encoding="utf-8"))
    if envelope.get("version") != 1:
        raise ValueError("Phiên bản file audio mã hóa không được hỗ trợ.")
    nonce = base64.b64decode(envelope["nonce"], validate=True)
    ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
    plaintext = AESGCM(_key_from_b64(shared_key_b64)).decrypt(nonce, ciphertext, MAGIC)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(plaintext)
    return output
