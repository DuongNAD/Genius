import os
import re


def test_gitignore_ignores_sensitive_files():
    gitignore_path = ".gitignore"
    assert os.path.exists(gitignore_path), ".gitignore file is missing!"

    with open(gitignore_path, "r", encoding="utf-8") as f:
        content = f.read()

    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    # Assert key patterns are present
    assert ".env" in lines or ".env*" in lines, ".env files are not ignored!"
    assert any(
        pat in lines for pat in [".venv/", "venv/", ".venv", "venv"]
    ), "Virtual environments are not ignored!"
    assert (
        "__pycache__/" in lines or "__pycache__" in lines
    ), "__pycache__ is not ignored!"


def test_requirements_contains_dependencies():
    req_path = "requirements.txt"
    assert os.path.exists(req_path), "requirements.txt file is missing!"

    with open(req_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Check for httpx>=0.27.0
    httpx_match = re.search(r"httpx\s*(>=|==)\s*([0-9.]+)", content)
    assert httpx_match, "httpx is missing from requirements.txt!"

    # Check for python-dotenv>=1.0.1
    dotenv_match = re.search(r"python-dotenv\s*(>=|==)\s*([0-9.]+)", content)
    assert dotenv_match, "python-dotenv is missing from requirements.txt!"


def test_jwt_encode_decode():
    import time
    from ag_core.utils.jwt import encode_jwt, decode_jwt
    import pytest

    secret = "test-secret-key"
    payload = {"sub": "user123", "role": "admin"}

    # Test normal encode & decode
    token = encode_jwt(payload, secret)
    decoded = decode_jwt(token, secret)
    assert decoded.pop("jti", None) is not None
    assert decoded == payload

    # Test invalid signature
    with pytest.raises(ValueError, match="Invalid signature"):
        decode_jwt(token, "wrong-secret")

    # Test expired token (well past the default clock-skew leeway)
    expired_payload = {"sub": "user123", "exp": time.time() - 120}
    expired_token = encode_jwt(expired_payload, secret)
    with pytest.raises(ValueError, match="Token has expired"):
        decode_jwt(expired_token, secret)

    # Test valid non-expired token with exp claim
    valid_payload = {"sub": "user123", "exp": time.time() + 60}
    valid_token = encode_jwt(valid_payload, secret)
    decoded_valid = decode_jwt(valid_token, secret)
    assert decoded_valid.pop("jti", None) is not None
    assert decoded_valid == valid_payload

    # Test malformed token
    with pytest.raises(ValueError, match="Invalid token format"):
        decode_jwt("header.payload", secret)
