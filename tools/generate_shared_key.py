"""Generate a random AES-256 key for GitHub Actions and the desktop app."""
import base64
import secrets

key = secrets.token_bytes(32)
print(base64.b64encode(key).decode("ascii"))
print("\nCopy the Base64 line above into the AUDIO_SHARED_KEY repository secret.")
print("Store the same value in SẢNH TRUYỆN's protected local settings.")
print("Do not commit this key or send it in chat.")
