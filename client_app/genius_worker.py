#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import sys
import time
import uuid
import websockets

# Add project root to sys.path
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from ag_core.utils.jwt import encode_jwt

from ag_core.distributed.worker import ClientWorker


def generate_jwt(worker_id: str) -> str:
    # Kept for backward compatibility or direct imports if needed
    import sys

    secret = os.getenv(
        "SKILL_API_KEY",
        "" if ("pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST")) else "",
    )
    payload = {"sub": worker_id, "exp": int(time.time() + 300)}
    return encode_jwt(payload, secret)


async def run_worker(hub_ip: str, hub_port: int, roles: list, worker_id: str):
    worker = ClientWorker(worker_id, roles)
    await worker.run_production_loop(hub_ip, hub_port)


def main():
    parser = argparse.ArgumentParser(description="Genius Distributed Worker Node")
    parser.add_argument("--hub-ip", default="127.0.0.1", help="Central hub IP address")
    parser.add_argument("--hub-port", type=int, default=8000, help="Central hub port")
    parser.add_argument(
        "--roles", default="grok", help="Comma-separated roles this worker supports"
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help="Unique worker ID (generates UUID if not specified)",
    )
    args = parser.parse_args()

    worker_id = args.worker_id
    if not worker_id:
        worker_id = f"worker_{uuid.uuid4().hex[:8]}"

    roles = [r.strip() for r in args.roles.split(",") if r.strip()]

    try:
        asyncio.run(run_worker(args.hub_ip, args.hub_port, roles, worker_id))
    except KeyboardInterrupt:
        print("\n[Worker] Stopped by user.")


if __name__ == "__main__":
    main()
