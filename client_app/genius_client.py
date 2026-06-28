#!/usr/bin/env python3
import sys
import time
import json
import hmac
import hashlib
import base64
import argparse
import requests

class ChecksumMismatchError(Exception):
    """Raised when the response checksum does not match X-Payload-SHA256."""
    pass

class TaskFailedError(Exception):
    """Raised when the remote task status is 'failed'."""
    pass

def base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')

def generate_jwt(api_key: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": "orchestrator",
        "exp": int(time.time() + 300)
    }
    header_json = json.dumps(header, separators=(',', ':'))
    payload_json = json.dumps(payload, separators=(',', ':'))
    
    header_b64 = base64url_encode(header_json.encode('utf-8'))
    payload_b64 = base64url_encode(payload_json.encode('utf-8'))
    
    signing_input = f"{header_b64}.{payload_b64}"
    signature = hmac.new(
        api_key.encode('utf-8'),
        signing_input.encode('utf-8'),
        hashlib.sha256
    ).digest()
    
    signature_b64 = base64url_encode(signature)
    return f"{signing_input}.{signature_b64}"

def get_endpoints(port: int) -> tuple[str, str]:
    if port == 8005:
        return "/security/run", "/security/status/{task_id}"
    elif port == 8006:
        return "/devops/run", "/devops/status/{task_id}"
    else:
        return "/run", "/status/{task_id}"

def verify_response(response: requests.Response) -> None:
    expected_checksum = response.headers.get("X-Payload-SHA256")
    if not expected_checksum:
        raise ChecksumMismatchError("Response is missing X-Payload-SHA256 header")
    
    calculated = hashlib.sha256(response.content).hexdigest()
    if calculated != expected_checksum:
        raise ChecksumMismatchError(
            f"Response checksum mismatch: expected {expected_checksum}, calculated {calculated}"
        )

def make_request_with_retry(
    method: str,
    url: str,
    headers: dict,
    data: bytes = None,
    max_retries: int = 5,
    initial_backoff: float = 1.0,
    backoff_factor: float = 2.0
) -> requests.Response:
    attempt = 0
    backoff = initial_backoff
    
    while True:
        try:
            attempt += 1
            if method.upper() == "POST":
                response = requests.post(url, headers=headers, data=data, timeout=30)
            else:
                response = requests.get(url, headers=headers, timeout=30)
            
            # Check for transient HTTP status errors (429, 5xx)
            if response.status_code == 429 or response.status_code >= 500:
                retry_after_val = response.headers.get("Retry-After")
                delay = backoff
                if retry_after_val:
                    try:
                        delay = float(retry_after_val)
                    except ValueError:
                        pass
                
                if attempt >= max_retries:
                    response.raise_for_status()
                
                sys.stderr.write(f"[INFO] Transient HTTP error {response.status_code} on attempt {attempt}. Retrying in {delay:.2f}s...\n")
                sys.stderr.flush()
                time.sleep(delay)
                backoff *= backoff_factor
                continue
            
            response.raise_for_status()
            verify_response(response)
            return response
            
        except (requests.exceptions.RequestException, ChecksumMismatchError) as e:
            is_transient = True
            if isinstance(e, requests.exceptions.HTTPError):
                status_code = e.response.status_code if e.response is not None else 0
                is_transient = (status_code == 429 or status_code >= 500)
            
            if not is_transient or attempt >= max_retries:
                raise e
            
            sys.stderr.write(f"[INFO] Transient error ({type(e).__name__}): {e} on attempt {attempt}. Retrying in {backoff:.2f}s...\n")
            sys.stderr.flush()
            time.sleep(backoff)
            backoff *= backoff_factor

def run_client(
    ip: str,
    port: int,
    api_key: str,
    prompt: str,
    poll_interval: float = 1.0,
    timeout: float = 120.0,
    max_retries: int = 5
) -> str:
    base_url = ip.strip()
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        base_url = f"http://{base_url}"
    base_url = base_url.rstrip("/")
    
    parts = base_url.split("://", 1)
    host_part = parts[1] if len(parts) > 1 else parts[0]
    if ":" not in host_part:
        base_url = f"{base_url}:{port}"
        
    run_path, status_path_template = get_endpoints(port)
    run_url = f"{base_url}{run_path}"
    
    jwt_token = generate_jwt(api_key)
    
    payload = {"prompt": prompt}
    payload_bytes = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    post_checksum = hashlib.sha256(payload_bytes).hexdigest()
    
    post_headers = {
        "X-API-Key": jwt_token,
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "X-Payload-SHA256": post_checksum
    }
    
    sys.stderr.write(f"[INFO] Sending task run request to {run_url}...\n")
    sys.stderr.flush()
    
    run_response = make_request_with_retry(
        method="POST",
        url=run_url,
        headers=post_headers,
        data=payload_bytes,
        max_retries=max_retries
    )
    
    run_data = run_response.json()
    task_id = run_data.get("task_id")
    if not task_id:
        raise ValueError(f"No task_id returned from server: {run_data}")
        
    sys.stderr.write(f"[INFO] Task started successfully. Task ID: {task_id}\n")
    sys.stderr.flush()
    
    get_checksum = hashlib.sha256(b"").hexdigest()
    get_headers = {
        "X-API-Key": jwt_token,
        "Authorization": f"Bearer {jwt_token}",
        "X-Payload-SHA256": get_checksum
    }
    
    status_url = f"{base_url}{status_path_template.format(task_id=task_id)}"
    sys.stderr.write(f"[INFO] Polling task status from {status_url}...\n")
    sys.stderr.flush()
    
    poll_start = time.time()
    while True:
        if time.time() - poll_start > timeout:
            raise TimeoutError(f"Task polling timed out after {timeout} seconds")
            
        status_response = make_request_with_retry(
            method="GET",
            url=status_url,
            headers=get_headers,
            max_retries=max_retries
        )
        
        status_data = status_response.json()
        curr_status = status_data.get("status")
        
        if curr_status == "completed":
            return status_data.get("result", "")
        elif curr_status == "failed":
            error_msg = status_data.get("error", "Unknown error occurred on server.")
            raise TaskFailedError(error_msg)
        elif curr_status == "processing":
            sys.stderr.write(f"[INFO] Task status: processing... waiting {poll_interval}s\n")
            sys.stderr.flush()
            time.sleep(poll_interval)
        else:
            raise ValueError(f"Unexpected task status '{curr_status}' returned for task {task_id}")

def main():
    parser = argparse.ArgumentParser(description="Genius CLI Client")
    parser.add_argument("--ip", help="Server IP Address")
    parser.add_argument("--port", type=int, help="Agent Port (Grok=8001, Claude=8002, Codex=8003, Tester=8004, Security=8005, DevOps=8006)")
    parser.add_argument("--api-key", help="API Key")
    parser.add_argument("--prompt", help="Prompt")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Polling interval in seconds")
    parser.add_argument("--timeout", type=float, default=120.0, help="Polling timeout in seconds")
    parser.add_argument("--max-retries", type=int, default=5, help="Max retries for transient errors")
    
    args = parser.parse_args()
    
    ip = args.ip
    if not ip:
        try:
            ip = input("Enter Server IP Address: ").strip()
            while not ip:
                ip = input("Server IP Address cannot be empty. Enter Server IP Address: ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.stderr.write("\nExiting.\n")
            sys.exit(1)
            
    port = args.port
    if not port:
        try:
            port_str = input("Enter Agent Port (Grok=8001, Claude=8002, Codex=8003, Tester=8004, Security=8005, DevOps=8006): ").strip()
            while not port_str:
                port_str = input("Agent Port cannot be empty. Enter Agent Port: ").strip()
            try:
                port = int(port_str)
            except ValueError:
                sys.stderr.write("[ERROR] Port must be an integer.\n")
                sys.exit(1)
        except (KeyboardInterrupt, EOFError):
            sys.stderr.write("\nExiting.\n")
            sys.exit(1)
            
    api_key = args.api_key
    if not api_key:
        try:
            api_key = input("Enter API Key: ").strip()
            while not api_key:
                api_key = input("API Key cannot be empty. Enter API Key: ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.stderr.write("\nExiting.\n")
            sys.exit(1)
            
    prompt = args.prompt
    if not prompt:
        try:
            prompt = input("Enter Prompt: ").strip()
            while not prompt:
                prompt = input("Prompt cannot be empty. Enter Prompt: ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.stderr.write("\nExiting.\n")
            sys.exit(1)
            
    try:
        result = run_client(
            ip=ip,
            port=port,
            api_key=api_key,
            prompt=prompt,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
            max_retries=args.max_retries
        )
        print(result)
        sys.exit(0)
    except TaskFailedError as e:
        sys.stderr.write(f"[ERROR] Task failed: {e}\n")
        sys.exit(1)
    except TimeoutError as e:
        sys.stderr.write(f"[ERROR] Timeout: {e}\n")
        sys.exit(1)
    except Exception as e:
        sys.stderr.write(f"[ERROR] Unexpected error: {e}\n")
        sys.exit(1)

if __name__ == "__main__":
    main()
