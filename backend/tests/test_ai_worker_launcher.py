"""The worker launcher must keep its heartbeat after it starts serving.

A real staging run found this the hard way: the launcher booted the agent with
``asyncio.run(main_async(...))`` and then served with ``uvicorn.Server.run()``,
which starts a *second* event loop. Closing the first one cancelled the
supervised heartbeat task, so the node registered, beat once, and then went
silent while still answering jobs. The coordinator dropped it after 120 s and
every completion failed with ``no_eligible_worker`` — exactly the failure the
supervised heartbeat was written to prevent.

This test runs the real launcher in a subprocess against a fake coordinator and
asserts the beats keep arriving after the server is up.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeCoordinator:
    """Minimal coordinator: key bundle, register, and counted heartbeats."""

    def __init__(self) -> None:
        from app.ai.keyring import KeyRing

        self.ring = KeyRing.generate("coordinator-launcher-test")
        self.heartbeats: list[float] = []
        self.registered = threading.Event()
        self.port = _free_port()
        self._server = None
        self._thread = None

    def viewer(self):
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/api/ai/v1/keys/coordinator")
        async def keys():
            return {"bundle": self.ring.public_bundle(), "revocations": {}}

        @app.post("/api/ai/v1/nodes/register")
        async def register(body: dict):
            payload = body["payload"]
            import hashlib

            node_id = (
                "node_"
                + hashlib.sha256(
                    payload["identity_public_key"].encode("utf-8")
                ).hexdigest()[:24]
            )
            self.registered.set()
            return {"node_id": node_id}

        @app.post("/api/ai/v1/nodes/heartbeat")
        async def heartbeat(body: dict):
            self.heartbeats.append(time.time())
            return {"status": "ok"}

        return app

    def __enter__(self):
        import uvicorn

        config = uvicorn.Config(
            self.viewer(), host="127.0.0.1", port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.05)
        raise TimeoutError("fake coordinator did not start")

    def __exit__(self, *_exc):
        if self._server is not None:
            self._server.should_exit = True


@pytest.fixture
def worker_env(tmp_path):
    digest = "a" * 64
    env = {
        "PATH": os.environ.get("PATH", ""),
        "TCDN_AI_COORDINATOR_URL": "",
        "TCDN_AI_WORKER_API_KEY": "tcdn_ai_launcher_test_key",
        "TCDN_AI_WORKER_MODELS": f"qwen2.5:0.5b={digest}",
        "TCDN_AI_BACKEND": "echo",
        "TCDN_AI_IDENTITY_FILE": str(tmp_path / "identity.json"),
        "TCDN_AI_ENVIRONMENT": "development",
        "TCDN_AI_ALLOW_DEV_STUB_BACKEND": "true",
        "TCDN_AI_REGION": "test",
    }
    return env


def test_heartbeats_continue_after_the_worker_starts_serving(worker_env):
    with FakeCoordinator() as coordinator:
        worker_env["TCDN_AI_COORDINATOR_URL"] = f"http://127.0.0.1:{coordinator.port}"
        proc = subprocess.Popen(
            [
                sys.executable,
                "scripts/ai_worker.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(_free_port()),
                "--public-url",
                f"http://127.0.0.1:{_free_port()}",
                "--allow-plaintext-loopback",
                "--heartbeat-interval",
                "1",
            ],
            cwd=BACKEND,
            env=worker_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            assert coordinator.registered.wait(timeout=30), "worker never registered"
            # The bug left exactly zero or one beat. Give the loop time for
            # several beats; a cancelled task produces nothing after the first.
            deadline = time.time() + 20
            while time.time() < deadline and len(coordinator.heartbeats) < 3:
                time.sleep(0.2)
            assert len(coordinator.heartbeats) >= 3, (
                "the heartbeat stopped after the worker started serving "
                f"({len(coordinator.heartbeats)} beat(s)): the task was created on "
                "the loop asyncio.run closes"
            )
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()


def test_the_worker_serves_health_on_the_same_loop(worker_env):
    """Control: the launcher really is serving while the beats land."""
    with FakeCoordinator() as coordinator:
        worker_env["TCDN_AI_COORDINATOR_URL"] = f"http://127.0.0.1:{coordinator.port}"
        port = _free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "scripts/ai_worker.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--public-url",
                f"http://127.0.0.1:{port}",
                "--allow-plaintext-loopback",
                "--heartbeat-interval",
                "1",
            ],
            cwd=BACKEND,
            env=worker_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            import httpx

            deadline = time.time() + 30
            body = None
            while time.time() < deadline:
                try:
                    response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                    if response.status_code == 200:
                        body = response.json()
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
            assert body is not None, "worker never started serving /health"
            assert body["backend"] == "dev-stub"
            assert body["models"] == ["qwen2.5:0.5b"]
            assert json.dumps(body)  # JSON-serialisable, no objects leaked
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
