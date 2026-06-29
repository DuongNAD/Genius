import base64
import json
import hmac
import hashlib
import time

def base64url_encode(data: bytes) -> str:
    """Encode bytes to base64url format string."""
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('utf-8')

def base64url_decode(data_str: str) -> bytes:
    """Decode base64url format string to bytes."""
    rem = len(data_str) % 4
    if rem > 0:
        data_str += '=' * (4 - rem)
    return base64.urlsafe_b64decode(data_str.encode('utf-8'))

import uuid
import threading

_seen_jtis = {}
_jtis_lock = threading.Lock()

def encode_jwt(payload: dict, secret: str) -> str:
    """
    Encode a JWT token with HS256 algorithm.
    """
    payload = dict(payload)
    if "jti" not in payload:
        payload["jti"] = str(uuid.uuid4())
        
    header = {"alg": "HS256", "typ": "JWT"}
    header_json = json.dumps(header, separators=(',', ':')).encode('utf-8')
    payload_json = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    
    header_b64 = base64url_encode(header_json)
    payload_b64 = base64url_encode(payload_json)
    
    signing_input = f"{header_b64}.{payload_b64}".encode('utf-8')
    secret_bytes = secret if isinstance(secret, bytes) else secret.encode('utf-8')
    
    signature = hmac.new(secret_bytes, signing_input, hashlib.sha256).digest()
    signature_b64 = base64url_encode(signature)
    
    return f"{header_b64}.{payload_b64}.{signature_b64}"

def decode_jwt(token: str, secret: str) -> dict:
    """
    Decode and verify a JWT token with HS256 algorithm.
    Raises ValueError on any parsing, verification or expiration error.
    """
    parts = token.split('.')
    if len(parts) != 3:
        raise ValueError("Invalid token format")
        
    header_b64, payload_b64, signature_b64 = parts
    
    try:
        header_json = base64url_decode(header_b64)
        header = json.loads(header_json)
    except Exception as e:
        raise ValueError("Invalid header") from e
        
    if header.get("alg") != "HS256":
        raise ValueError("Unsupported algorithm")
        
    signing_input = f"{header_b64}.{payload_b64}".encode('utf-8')
    secret_bytes = secret if isinstance(secret, bytes) else secret.encode('utf-8')
    
    expected_signature = hmac.new(secret_bytes, signing_input, hashlib.sha256).digest()
    expected_signature_b64 = base64url_encode(expected_signature)
    
    if not hmac.compare_digest(signature_b64.encode('utf-8'), expected_signature_b64.encode('utf-8')):
        raise ValueError("Invalid signature")
        
    try:
        payload_json = base64url_decode(payload_b64)
        payload = json.loads(payload_json)
    except Exception as e:
        raise ValueError("Invalid payload") from e
        
    if "exp" in payload:
        exp = payload["exp"]
        if not isinstance(exp, (int, float)):
            raise ValueError("Invalid exp claim type")
        if time.time() > exp:
            raise ValueError("Token has expired")
            
    jti = payload.get("jti")
    if not jti:
        raise ValueError("Missing jti claim")
        
    from ag_core.config import load_config
    import sqlite3
    import os
    
    config = load_config()
    db_path = config.memory.db_path
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
        
    with _jtis_lock:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_jtis (
                    jti TEXT PRIMARY KEY,
                    exp REAL
                )
            """)
            conn.commit()
            
            now = time.time()
            conn.execute("DELETE FROM seen_jtis WHERE exp IS NOT NULL AND ? > exp", (now,))
            conn.commit()
            
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM seen_jtis WHERE jti = ?", (jti,))
            if cursor.fetchone():
                raise ValueError("Token replay detected")
                
            conn.execute("INSERT INTO seen_jtis (jti, exp) VALUES (?, ?)", (jti, payload.get("exp")))
            conn.commit()
        finally:
            conn.close()
            
    return payload
