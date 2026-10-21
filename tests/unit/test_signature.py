import hashlib
import hmac

from core.github.signature import verify_signature

SECRET = b"test-secret"
BODY = b'{"action":"opened"}'
GOOD = "sha256=" + hmac.new(SECRET, BODY, hashlib.sha256).hexdigest()


def test_valid_signature():
    assert verify_signature(SECRET, BODY, GOOD)


def test_tampered_body_rejected():
    assert not verify_signature(SECRET, BODY + b" ", GOOD)


def test_missing_or_wrong_scheme_rejected():
    assert not verify_signature(SECRET, BODY, None)
    assert not verify_signature(SECRET, BODY, GOOD.replace("sha256=", "sha1="))