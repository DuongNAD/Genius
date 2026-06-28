import sys
import os
import time
import json
import hashlib
import hmac
import base64
import unittest
from unittest.mock import patch, MagicMock

# Add current directory to path if not present
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import genius_client
from genius_client import (
    ChecksumMismatchError,
    TaskFailedError,
    generate_jwt,
    get_endpoints,
    verify_response,
    make_request_with_retry,
    run_client,
    main
)

class TestGeniusClient(unittest.TestCase):

    def test_jwt_generation(self):
        api_key = "test-secret-key-12345"
        token = generate_jwt(api_key)
        
        # Verify token format header.payload.signature
        parts = token.split(".")
        self.assertEqual(len(parts), 3)
        
        header_b64, payload_b64, signature_b64 = parts
        
        # Decode and verify header
        # Add padding back if necessary, but standard base64url decode handles it or we can manually pad
        def b64url_decode(s):
            padding = '=' * (4 - len(s) % 4)
            return base64.urlsafe_b64decode(s + padding).decode('utf-8')
            
        header = json.loads(b64url_decode(header_b64))
        self.assertEqual(header.get("alg"), "HS256")
        self.assertEqual(header.get("typ"), "JWT")
        
        # Decode and verify payload
        payload = json.loads(b64url_decode(payload_b64))
        self.assertEqual(payload.get("sub"), "orchestrator")
        self.assertGreater(payload.get("exp"), time.time())
        self.assertLessEqual(payload.get("exp"), time.time() + 301)
        
        # Re-sign and verify signature matches
        signing_input = f"{header_b64}.{payload_b64}"
        expected_sig = hmac.new(
            api_key.encode('utf-8'),
            signing_input.encode('utf-8'),
            hashlib.sha256
        ).digest()
        expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode('utf-8').rstrip('=')
        self.assertEqual(signature_b64, expected_sig_b64)

    def test_get_endpoints(self):
        # Port 8005 (security)
        self.assertEqual(get_endpoints(8005), ("/security/run", "/security/status/{task_id}"))
        # Port 8006 (devops)
        self.assertEqual(get_endpoints(8006), ("/devops/run", "/devops/status/{task_id}"))
        # Port 8001 (grok)
        self.assertEqual(get_endpoints(8001), ("/run", "/status/{task_id}"))
        # Port 8002 (claude)
        self.assertEqual(get_endpoints(8002), ("/run", "/status/{task_id}"))

    def test_verify_response_valid(self):
        mock_resp = MagicMock()
        content = b'{"status":"completed","result":"ok"}'
        mock_resp.content = content
        mock_resp.headers = {"X-Payload-SHA256": hashlib.sha256(content).hexdigest()}
        
        # Should not raise exception
        try:
            verify_response(mock_resp)
        except ChecksumMismatchError:
            self.fail("verify_response raised ChecksumMismatchError unexpectedly!")

    def test_verify_response_missing_header(self):
        mock_resp = MagicMock()
        mock_resp.content = b'{"status":"completed"}'
        mock_resp.headers = {}
        
        with self.assertRaises(ChecksumMismatchError):
            verify_response(mock_resp)

    def test_verify_response_mismatch(self):
        mock_resp = MagicMock()
        mock_resp.content = b'{"status":"completed"}'
        mock_resp.headers = {"X-Payload-SHA256": "badchecksum"}
        
        with self.assertRaises(ChecksumMismatchError):
            verify_response(mock_resp)

    @patch("time.sleep", return_value=None)
    @patch("requests.post")
    def test_make_request_with_retry_transient_failure_then_success(self, mock_post, mock_sleep):
        # 1st call: 500 Internal Server Error
        # 2nd call: Checksum mismatch (valid status but wrong checksum)
        # 3rd call: 200 OK with correct checksum
        
        response1 = MagicMock()
        response1.status_code = 500
        # raise_for_status raises HTTPError on 500
        import requests
        response1.raise_for_status.side_effect = requests.exceptions.HTTPError("Internal Server Error", response=response1)
        response1.headers = {}
        
        response2 = MagicMock()
        response2.status_code = 200
        response2.content = b"content2"
        response2.headers = {"X-Payload-SHA256": "wrong_checksum"}
        
        response3 = MagicMock()
        response3.status_code = 200
        response3.content = b"content3"
        response3.headers = {"X-Payload-SHA256": hashlib.sha256(b"content3").hexdigest()}
        
        mock_post.side_effect = [response1, response2, response3]
        
        res = make_request_with_retry("POST", "http://test", {}, b"", max_retries=5)
        self.assertEqual(res, response3)
        self.assertEqual(mock_post.call_count, 3)

    @patch("time.sleep", return_value=None)
    @patch("requests.post")
    def test_make_request_with_retry_non_transient_failure(self, mock_post, mock_sleep):
        # 401 Unauthorized should not be retried
        import requests
        response = MagicMock()
        response.status_code = 401
        response.raise_for_status.side_effect = requests.exceptions.HTTPError("Unauthorized", response=response)
        
        mock_post.return_value = response
        
        with self.assertRaises(requests.exceptions.HTTPError):
            make_request_with_retry("POST", "http://test", {}, b"", max_retries=5)
            
        self.assertEqual(mock_post.call_count, 1)

    @patch("time.sleep", return_value=None)
    @patch("requests.post")
    def test_make_request_with_retry_max_retries_exceeded(self, mock_post, mock_sleep):
        import requests
        response = MagicMock()
        response.status_code = 502
        response.raise_for_status.side_effect = requests.exceptions.HTTPError("Bad Gateway", response=response)
        
        mock_post.return_value = response
        
        with self.assertRaises(requests.exceptions.HTTPError):
            make_request_with_retry("POST", "http://test", {}, b"", max_retries=3)
            
        self.assertEqual(mock_post.call_count, 3)

    @patch("time.sleep", return_value=None)
    @patch("genius_client.make_request_with_retry")
    def test_run_client_success(self, mock_retry_request, mock_sleep):
        # Mock run response (POST)
        run_content = b'{"status":"processing","task_id":"task-999"}'
        run_response = MagicMock()
        run_response.status_code = 200
        run_response.json.return_value = {"status": "processing", "task_id": "task-999"}
        run_response.content = run_content
        run_response.headers = {"X-Payload-SHA256": hashlib.sha256(run_content).hexdigest()}
        
        # Mock status responses (GET)
        # 1. processing
        status_content1 = b'{"status":"processing"}'
        status_response1 = MagicMock()
        status_response1.json.return_value = {"status": "processing"}
        status_response1.content = status_content1
        status_response1.headers = {"X-Payload-SHA256": hashlib.sha256(status_content1).hexdigest()}
        
        # 2. completed
        status_content2 = b'{"status":"completed","result":"my final result text"}'
        status_response2 = MagicMock()
        status_response2.json.return_value = {"status": "completed", "result": "my final result text"}
        status_response2.content = status_content2
        status_response2.headers = {"X-Payload-SHA256": hashlib.sha256(status_content2).hexdigest()}
        
        mock_retry_request.side_effect = [run_response, status_response1, status_response2]
        
        res = run_client(
            ip="127.0.0.1",
            port=8001,
            api_key="my-key",
            prompt="do research",
            poll_interval=0.1,
            timeout=5.0
        )
        self.assertEqual(res, "my final result text")
        self.assertEqual(mock_retry_request.call_count, 3)

    @patch("time.sleep", return_value=None)
    @patch("genius_client.make_request_with_retry")
    def test_run_client_task_failed(self, mock_retry_request, mock_sleep):
        # Mock run response (POST)
        run_content = b'{"status":"processing","task_id":"task-999"}'
        run_response = MagicMock()
        run_response.json.return_value = {"status": "processing", "task_id": "task-999"}
        run_response.content = run_content
        run_response.headers = {"X-Payload-SHA256": hashlib.sha256(run_content).hexdigest()}
        
        # Mock status response (GET) - failed
        status_content = b'{"status":"failed","error":"something went wrong"}'
        status_response = MagicMock()
        status_response.json.return_value = {"status": "failed", "error": "something went wrong"}
        status_response.content = status_content
        status_response.headers = {"X-Payload-SHA256": hashlib.sha256(status_content).hexdigest()}
        
        mock_retry_request.side_effect = [run_response, status_response]
        
        with self.assertRaises(TaskFailedError) as context:
            run_client(
                ip="127.0.0.1",
                port=8002,
                api_key="my-key",
                prompt="test failures",
                poll_interval=0.1,
                timeout=5.0
            )
        self.assertIn("something went wrong", str(context.exception))

    @patch("time.sleep", return_value=None)
    @patch("genius_client.make_request_with_retry")
    def test_run_client_timeout(self, mock_retry_request, mock_sleep):
        # Mock run response (POST)
        run_content = b'{"status":"processing","task_id":"task-999"}'
        run_response = MagicMock()
        run_response.json.return_value = {"status": "processing", "task_id": "task-999"}
        run_response.content = run_content
        run_response.headers = {"X-Payload-SHA256": hashlib.sha256(run_content).hexdigest()}
        
        # Mock status response (GET) - keeps processing
        status_content = b'{"status":"processing"}'
        status_response = MagicMock()
        status_response.json.return_value = {"status": "processing"}
        status_response.content = status_content
        status_response.headers = {"X-Payload-SHA256": hashlib.sha256(status_content).hexdigest()}
        
        mock_retry_request.side_effect = [run_response, status_response, status_response, status_response]
        
        # Set a very low timeout to force timeout
        with patch("time.time", side_effect=[100, 100, 101, 102, 103, 104]):
            with self.assertRaises(TimeoutError):
                run_client(
                    ip="127.0.0.1",
                    port=8003,
                    api_key="my-key",
                    prompt="test timeout",
                    poll_interval=0.1,
                    timeout=2.0
                )

    @patch("genius_client.run_client", return_value="CLI output result")
    @patch("sys.exit")
    def test_cli_args_provided(self, mock_exit, mock_run_client):
        test_args = [
            "genius_client.py",
            "--ip", "10.0.0.5",
            "--port", "8005",
            "--api-key", "secret1",
            "--prompt", "do security audit",
            "--poll-interval", "0.5",
            "--timeout", "10"
        ]
        with patch("sys.argv", test_args):
            main()
            
        mock_run_client.assert_called_once_with(
            ip="10.0.0.5",
            port=8005,
            api_key="secret1",
            prompt="do security audit",
            poll_interval=0.5,
            timeout=10.0,
            max_retries=5
        )
        mock_exit.assert_called_once_with(0)

    @patch("builtins.input")
    @patch("genius_client.run_client", return_value="CLI interactive result")
    @patch("sys.exit")
    def test_cli_args_interactive_fallback(self, mock_exit, mock_run_client, mock_input):
        mock_input.side_effect = ["192.168.1.100", "8006", "interactivekey", "deploy application"]
        
        # No arguments passed to CLI
        with patch("sys.argv", ["genius_client.py"]):
            main()
            
        self.assertEqual(mock_input.call_count, 4)
        mock_run_client.assert_called_once_with(
            ip="192.168.1.100",
            port=8006,
            api_key="interactivekey",
            prompt="deploy application",
            poll_interval=1.0,
            timeout=120.0,
            max_retries=5
        )
        mock_exit.assert_called_once_with(0)

if __name__ == "__main__":
    unittest.main()
