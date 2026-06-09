"""Single-job plot manager.

Owns the one in-flight plot, runs it on a background thread (so the HTTP request
returns immediately and the plot survives the client disconnecting), and exposes
pause / resume / stop. One job at a time — a second submission while busy raises
``Busy`` (HTTP 409).
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from .executor import PlotterError, PlotterExecutor


class Busy(RuntimeError):
    """Raised when an action is rejected because a plot is in progress."""


class JobManager:
    def __init__(self, executor: PlotterExecutor) -> None:
        self._executor = executor
        self._lock = threading.RLock()
        self._job: Optional[dict] = None
        self._counter = 0

    # -- queries --

    def is_busy(self) -> bool:
        with self._lock:
            return self._job is not None and self._job["state"] in ("plotting", "paused")

    def health(self) -> dict:
        info = self._executor.device_info()
        with self._lock:
            job = self._job
            if job and job["state"] in ("plotting", "paused"):
                state, current = job["state"], job["job_id"]
            else:
                state, current = "idle", None
        return {
            "ok": True,
            "device_connected": self._executor.is_available(),
            "device": info.get("device", "plotter"),
            "firmware": info.get("firmware"),
            "state": state,
            "current_job": current,
        }

    def get(self, job_id: str) -> Optional[dict]:
        with self._lock:
            if self._job is None or self._job["job_id"] != job_id:
                return None
            return _public(self._job)

    # -- actions --

    def submit(self, svg: str, settings: dict) -> str:
        with self._lock:
            if self.is_busy():
                raise Busy("A plot is already in progress.")
            self._counter += 1
            job_id = f"job_{self._counter:04d}"
            self._job = {
                "job_id": job_id,
                "state": "plotting",
                "percent": 0.0,
                "paused_reason": None,
                "elapsed_s": 0,
                "error": None,
                "_svg": svg,
                "_settings": settings,
                "_resume_svg": None,
                "_handle": None,
                "_stop": False,
                "_started": time.monotonic(),
            }
            thread = threading.Thread(target=self._run, args=(job_id, svg, settings, False), daemon=True)
            self._job["_thread"] = thread
            thread.start()
            return job_id

    def manual(self, command: str, settings: dict) -> None:
        with self._lock:
            if self.is_busy():
                raise Busy("The plotter is busy with a plot.")
        self._executor.manual(command, settings)  # outside the lock — talks to hardware

    def control(self, job_id: str, action: str) -> None:
        # If we finalize a stop here (paused job, no live plot thread), lift the
        # pen outside the lock since that talks to hardware.
        lift_settings = None
        with self._lock:
            job = self._job
            if job is None or job["job_id"] != job_id:
                raise KeyError(job_id)
            handle = job["_handle"]
            if action == "pause":
                if handle is not None:
                    _safe_pause(handle)
            elif action == "stop":
                job["_stop"] = True
                if job["state"] == "paused":
                    # A paused job has no live plot thread to reach the post-plot
                    # block in _run, so it would otherwise sit in "paused"
                    # forever — keeping the daemon busy until a restart. Finalize
                    # the stop right here so the device is freed.
                    job["state"] = "stopped"
                    job["_resume_svg"] = None
                    lift_settings = job["_settings"]
                elif handle is not None:
                    # Still plotting: ask it to stop at the next safe point; _run
                    # sees _stop and transitions the job to "stopped".
                    _safe_pause(handle)
            elif action == "resume":
                if job["state"] == "paused" and job["_resume_svg"] is not None:
                    settings = job["_settings"]
                    resume_svg = job["_resume_svg"]
                    job["state"] = "plotting"
                    job["paused_reason"] = None
                    thread = threading.Thread(
                        target=self._run, args=(job_id, resume_svg, settings, True), daemon=True
                    )
                    job["_thread"] = thread
                    thread.start()
            else:
                raise ValueError(f"unknown action: {action}")
        if lift_settings is not None:
            self._safety_lift(lift_settings)

    # -- worker --

    def _run(self, job_id: str, svg: str, settings: dict, resume: bool) -> None:
        def progress_cb(pct: float) -> None:
            with self._lock:
                if self._job and self._job["job_id"] == job_id:
                    self._job["percent"] = float(pct)
                    self._job["elapsed_s"] = int(time.monotonic() - self._job["_started"])

        def on_ready(handle) -> None:
            with self._lock:
                if self._job and self._job["job_id"] == job_id:
                    self._job["_handle"] = handle

        try:
            result = self._executor.plot(svg, settings, progress_cb, on_ready, resume)
        except PlotterError as exc:
            with self._lock:
                if self._job and self._job["job_id"] == job_id:
                    self._job["state"] = "error"
                    self._job["error"] = str(exc)
            self._safety_lift(settings)
            return
        except Exception as exc:  # pragma: no cover - defensive
            with self._lock:
                if self._job and self._job["job_id"] == job_id:
                    self._job["state"] = "error"
                    self._job["error"] = str(exc)
            self._safety_lift(settings)
            return

        with self._lock:
            job = self._job
            if job is None or job["job_id"] != job_id:
                return
            stopped = job["_stop"]
            if stopped:
                job["state"] = "stopped"
                job["_resume_svg"] = None
            elif result.paused:
                job["state"] = "paused"
                job["paused_reason"] = "user"
                job["_resume_svg"] = result.resume_svg
            else:
                job["state"] = "done"
                job["percent"] = 100.0
                job["_resume_svg"] = None

        if stopped:
            self._safety_lift(settings)

    def _safety_lift(self, settings: dict) -> None:
        """On stop/error, raise the pen and release motors so nothing bleeds ink."""
        for cmd in ("raise_pen", "disable_xy"):
            try:
                self._executor.manual(cmd, settings)
            except Exception:
                pass


def _safe_pause(handle) -> None:
    try:
        handle.transmit_pause_request()
    except Exception:
        pass


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}
