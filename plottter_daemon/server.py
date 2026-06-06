"""HTTP server exposing the remote-plotter contract (stdlib only).

Implements the endpoints from the Plottter remote-plotter spec §6:

    GET  /api/v1/health                 open
    GET  /api/v1/version                open
    POST /api/v1/jobs                   {svg, settings} -> {job_id}; 409 if busy
    GET  /api/v1/jobs/{id}              status / progress
    POST /api/v1/jobs/{id}/control      {action: pause|resume|stop}
    POST /api/v1/manual                 {command, settings}; 409 if a job is plotting

Control endpoints require ``Authorization: Bearer <token>`` when a token is
configured; ``/health`` and ``/version`` stay open for discovery.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .jobs import Busy, JobManager

API_BASE = "/api/v1"
API_VERSION = "1.0"
_MANUAL_COMMANDS = {"raise_pen", "lower_pen", "disable_xy", "enable_xy", "walk_home"}


def make_handler(manager: JobManager, token: Optional[str]):
    """Build a request-handler class bound to a manager + optional token."""

    class _Handler(BaseHTTPRequestHandler):
        server_version = "PlottterDaemon/1.0"

        def log_message(self, fmt, *args):
            print(f"[plottter-daemon] {self.address_string()} {fmt % args}")

        # -- helpers --

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode() or "{}")
            except json.JSONDecodeError:
                return {}

        def _authed(self) -> bool:
            if not token:
                return True
            return self.headers.get("Authorization", "") == f"Bearer {token}"

        def _require_auth(self) -> bool:
            if self._authed():
                return True
            self._send(401, {"error": "missing or invalid token"})
            return False

        # -- routing --

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == f"{API_BASE}/health":
                payload = manager.health()
                payload.update(auth_required=bool(token), api_version=API_VERSION)
                return self._send(200, payload)
            if path == f"{API_BASE}/version":
                return self._send(200, {"daemon": "plottter-daemon", "api_version": API_VERSION})
            m = re.fullmatch(rf"{re.escape(API_BASE)}/jobs/([^/]+)", path)
            if m:
                if not self._require_auth():
                    return
                job = manager.get(m.group(1))
                if job is None:
                    return self._send(404, {"error": "no such job"})
                return self._send(200, job)
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == f"{API_BASE}/jobs":
                return self._create_job()
            m = re.fullmatch(rf"{re.escape(API_BASE)}/jobs/([^/]+)/control", path)
            if m:
                return self._control(m.group(1))
            if path == f"{API_BASE}/manual":
                return self._manual()
            self._send(404, {"error": "not found"})

        # -- endpoints --

        def _create_job(self) -> None:
            if not self._require_auth():
                return
            body = self._body()
            svg = body.get("svg")
            settings = body.get("settings", {})
            if not isinstance(svg, str) or not svg:
                return self._send(400, {"error": "missing 'svg'"})
            if not isinstance(settings, dict):
                return self._send(400, {"error": "'settings' must be an object"})
            try:
                job_id = manager.submit(svg, settings)
            except Busy as exc:
                return self._send(409, {"error": str(exc)})
            self._send(200, {"job_id": job_id})

        def _control(self, job_id: str) -> None:
            if not self._require_auth():
                return
            action = self._body().get("action")
            if action not in ("pause", "resume", "stop"):
                return self._send(400, {"error": "action must be pause|resume|stop"})
            try:
                manager.control(job_id, action)
            except KeyError:
                return self._send(404, {"error": "no such job"})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            self._send(200, {"ok": True, "action": action})

        def _manual(self) -> None:
            if not self._require_auth():
                return
            body = self._body()
            command = body.get("command")
            settings = body.get("settings", {})
            if command not in _MANUAL_COMMANDS:
                return self._send(400, {"error": f"command must be one of {sorted(_MANUAL_COMMANDS)}"})
            if not isinstance(settings, dict):
                return self._send(400, {"error": "'settings' must be an object"})
            try:
                manager.manual(command, settings)
            except Busy as exc:
                return self._send(409, {"error": str(exc)})
            except Exception as exc:
                return self._send(500, {"error": str(exc)})
            self._send(200, {"ok": True, "command": command})

    return _Handler


def make_server(host: str, port: int, manager: JobManager, token: Optional[str]) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(manager, token))
