"""End-to-end tests: the real daemon server backed by FakeExecutor (no hardware).

Boots make_server() on an OS-assigned port and drives it over HTTP, exercising
the same contract the Plottter NetworkTransport speaks.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from plottter_daemon.executor import FakeExecutor
from plottter_daemon.jobs import JobManager
from plottter_daemon.server import make_server

SVG = '<svg xmlns="http://www.w3.org/2000/svg"></svg>'


@pytest.fixture
def server():
    manager = JobManager(FakeExecutor(duration=0.4))
    srv = make_server("127.0.0.1", 0, manager, token="tok")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}/api/v1", "tok"
    finally:
        srv.shutdown()
        srv.server_close()


def call(base, method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "{}")


def test_health_open_and_reports_auth(server):
    base, _ = server
    code, data = call(base, "GET", "/health")  # no token
    assert code == 200
    assert data["ok"] is True
    assert data["auth_required"] is True
    assert data["state"] == "idle"


def test_version_open(server):
    base, _ = server
    code, data = call(base, "GET", "/version")
    assert code == 200
    assert data["api_version"]


def test_auth_required_on_control_endpoints(server):
    base, _ = server
    assert call(base, "POST", "/jobs", {"svg": SVG, "settings": {}})[0] == 401
    assert call(base, "GET", "/jobs/job_0001")[0] == 401


def test_job_completes(server):
    base, tok = server
    code, data = call(base, "POST", "/jobs", {"svg": SVG, "settings": {}}, tok)
    assert code == 200
    job_id = data["job_id"]
    last = 0.0
    for _ in range(50):
        code, st = call(base, "GET", f"/jobs/{job_id}", token=tok)
        assert code == 200
        assert st["percent"] >= last
        last = st["percent"]
        if st["state"] == "done":
            break
        time.sleep(0.05)
    assert st["state"] == "done"
    assert st["percent"] == 100.0


def test_busy_rejects_second_job_and_manual(server):
    base, tok = server
    assert call(base, "POST", "/jobs", {"svg": SVG, "settings": {}}, tok)[0] == 200
    assert call(base, "POST", "/jobs", {"svg": SVG, "settings": {}}, tok)[0] == 409
    assert call(base, "POST", "/manual", {"command": "raise_pen"}, tok)[0] == 409


def test_pause_then_resume(server):
    base, tok = server
    job_id = call(base, "POST", "/jobs", {"svg": SVG, "settings": {}}, tok)[1]["job_id"]
    time.sleep(0.1)
    assert call(base, "POST", f"/jobs/{job_id}/control", {"action": "pause"}, tok)[0] == 200
    # reaches paused
    for _ in range(40):
        st = call(base, "GET", f"/jobs/{job_id}", token=tok)[1]
        if st["state"] == "paused":
            break
        time.sleep(0.05)
    assert st["state"] == "paused"
    # resume -> completes
    assert call(base, "POST", f"/jobs/{job_id}/control", {"action": "resume"}, tok)[0] == 200
    for _ in range(50):
        st = call(base, "GET", f"/jobs/{job_id}", token=tok)[1]
        if st["state"] == "done":
            break
        time.sleep(0.05)
    assert st["state"] == "done"


def test_stop_while_paused_frees_device(server):
    """A paused job must be stoppable so the daemon doesn't stay busy forever."""
    base, tok = server
    job_id = call(base, "POST", "/jobs", {"svg": SVG, "settings": {}}, tok)[1]["job_id"]
    time.sleep(0.1)
    call(base, "POST", f"/jobs/{job_id}/control", {"action": "pause"}, tok)
    for _ in range(40):
        st = call(base, "GET", f"/jobs/{job_id}", token=tok)[1]
        if st["state"] == "paused":
            break
        time.sleep(0.05)
    assert st["state"] == "paused"
    # Health reports busy while paused.
    assert call(base, "GET", "/health")[1]["state"] == "paused"

    # Stop the paused job -> it becomes "stopped" and the device frees up.
    assert call(base, "POST", f"/jobs/{job_id}/control", {"action": "stop"}, tok)[0] == 200
    st = call(base, "GET", f"/jobs/{job_id}", token=tok)[1]
    assert st["state"] == "stopped"
    assert call(base, "GET", "/health")[1]["state"] == "idle"

    # A fresh job is now accepted instead of 409'ing.
    assert call(base, "POST", "/jobs", {"svg": SVG, "settings": {}}, tok)[0] == 200


def test_manual_validation_and_success(server):
    base, tok = server
    assert call(base, "POST", "/manual", {"command": "nope"}, tok)[0] == 400
    assert call(base, "POST", "/manual", {"command": "lower_pen", "settings": {}}, tok)[0] == 200


def test_control_unknown_job_404(server):
    base, tok = server
    assert call(base, "POST", "/jobs/nope/control", {"action": "pause"}, tok)[0] == 404
