
import hashlib
import hmac


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """Check GitHub's X-Hub-Signature-256 header against the raw request body."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    # Constant-time compare so the signature can't be guessed byte by byte from response timing
    return hmac.compare_digest(expected, header.removeprefix("sha256="))

