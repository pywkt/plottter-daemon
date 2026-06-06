"""Plotter executors — the only part that touches the hardware.

``AxiDrawExecutor`` drives the plotter via ``pyaxidraw`` and mirrors, line for
line, the logic in Plottter's ``export/axidraw.py`` so the daemon plots exactly
as the laptop would over USB. ``FakeExecutor`` simulates a plot so the job
manager and HTTP server can be tested with no hardware and no ``pyaxidraw``.

Pause contract: ``plot()`` calls ``on_ready(handle)`` once plotting is
configured, where ``handle`` exposes ``transmit_pause_request()``. The job
manager stores the handle and calls it to pause/stop — identical to how the
Plottter GUI workers drive the USB ``AxiDraw`` object. This is the same
duck-typed handle the Plottter ``NetworkTransport`` relies on, end to end.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class PlotResult:
    """Outcome of a plot: paused early (with resume data) or completed."""

    paused: bool
    resume_svg: Optional[str] = None


class PlotterError(RuntimeError):
    """Hardware / driver error surfaced with a user-facing message."""


class PlotterExecutor(ABC):
    @abstractmethod
    def is_available(self) -> bool:
        """True if the executor can drive a plotter (e.g. pyaxidraw installed)."""

    @abstractmethod
    def device_info(self) -> dict:
        """Static device description for /health (name, firmware if known)."""

    @abstractmethod
    def plot(
        self,
        svg: str,
        settings: dict,
        progress_cb: Optional[Callable[[float], None]] = None,
        on_ready: Optional[Callable[[Any], None]] = None,
        resume: bool = False,
    ) -> PlotResult:
        ...

    @abstractmethod
    def manual(self, command: str, settings: dict) -> None:
        """Run a one-off manual command (raise_pen / lower_pen / disable_xy / …)."""


# ---------------------------------------------------------------------------
# Real executor — pyaxidraw (mirrors Plottter export/axidraw.py)
# ---------------------------------------------------------------------------


class AxiDrawExecutor(PlotterExecutor):
    """Drive the USB-connected plotter with ``pyaxidraw``.

    A faithful copy of Plottter's ``plot_svg_string`` / ``run_manual_command``
    so the plotted output is identical to the laptop's USB path. Works with the
    iDraw H SE (AxiDraw-compatible EBB); the A2 bed is the SE/A2 model, passed in
    each job's ``settings["model"]``.
    """

    def __init__(self, device_name: str = "AxiDraw-compatible plotter") -> None:
        self._device_name = device_name

    @staticmethod
    def _axidraw():
        try:
            from pyaxidraw import axidraw  # type: ignore[import-untyped]

            return axidraw
        except ImportError as exc:  # pragma: no cover - depends on host
            raise PlotterError(
                "pyaxidraw is not installed on this host.\n"
                "Install it with:\n"
                "  pip install https://cdn.evilmadscientist.com/dl/ad/public/AxiDraw_API.zip"
            ) from exc

    def is_available(self) -> bool:
        try:
            self._axidraw()
            return True
        except PlotterError:
            return False

    def device_info(self) -> dict:
        return {"device": self._device_name, "firmware": None}

    def plot(self, svg, settings, progress_cb=None, on_ready=None, resume=False) -> PlotResult:
        axidraw = self._axidraw()
        ad = axidraw.AxiDraw()
        ad.plot_setup(svg)

        ad.options.speed_pendown = int(settings.get("speed_pendown", 25))
        ad.options.speed_penup = int(settings.get("speed_penup", 75))
        ad.options.pen_pos_down = int(settings.get("pen_pos_down", 40))
        ad.options.pen_pos_up = int(settings.get("pen_pos_up", 60))
        ad.options.pen_delay_down = int(settings.get("pen_delay_down", 0))
        ad.options.pen_delay_up = int(settings.get("pen_delay_up", 0))
        ad.options.const_speed = bool(settings.get("const_speed", False))
        ad.options.report_time = bool(settings.get("report_time", False))
        ad.options.model = int(settings.get("model", 2))
        # Never re-sort paths — the laptop already optimized the order.
        ad.options.reordering = 0

        if resume:
            ad.options.mode = "resume"
            ad.options.resume_type = "plot"

        port = settings.get("port")
        if port:
            ad.options.port = port
        if bool(settings.get("preview", False)):
            ad.options.preview = True

        if progress_cb:
            progress_cb(10.0)
        if on_ready is not None:
            on_ready(ad)

        try:
            output_svg = ad.plot_run(True)
        except Exception as exc:
            msg = str(exc)
            if "unable to find" in msg.lower() or "no axidraw" in msg.lower():
                raise PlotterError(
                    "Plotter not found. Check the USB cable and power."
                ) from exc
            raise PlotterError(msg) from exc

        try:
            stopped = int(getattr(ad.plot_status, "stopped", 0) or 0)
        except (TypeError, ValueError):
            stopped = 0
        paused = stopped > 0
        if not paused and progress_cb:
            progress_cb(100.0)
        return PlotResult(paused=paused, resume_svg=output_svg if paused else None)

    def manual(self, command, settings) -> None:
        axidraw = self._axidraw()
        ad = axidraw.AxiDraw()
        ad.plot_setup()
        ad.options.mode = "manual"
        ad.options.manual_cmd = command
        ad.options.model = int(settings.get("model", 2))
        ad.options.pen_pos_up = int(settings.get("pen_pos_up", 60))
        ad.options.pen_pos_down = int(settings.get("pen_pos_down", 40))
        port = settings.get("port")
        if port:
            ad.options.port = port
        if bool(settings.get("preview", False)):
            ad.options.preview = True
        try:
            ad.plot_run()
        except Exception as exc:
            msg = str(exc)
            if "unable to find" in msg.lower() or "no axidraw" in msg.lower():
                raise PlotterError("Plotter not found. Check the USB cable and power.") from exc
            raise PlotterError(msg) from exc


# ---------------------------------------------------------------------------
# Fake executor — for tests / dev with no hardware
# ---------------------------------------------------------------------------


class _FakeHandle:
    def __init__(self) -> None:
        self.paused = False

    def transmit_pause_request(self) -> None:
        self.paused = True


class FakeExecutor(PlotterExecutor):
    """Simulate a plot (no hardware) so the server/job logic can be tested."""

    def __init__(self, duration: float = 0.5, device_name: str = "Fake Plotter") -> None:
        self.duration = duration
        self._device_name = device_name
        self.manual_calls: list[tuple[str, dict]] = []

    def is_available(self) -> bool:
        return True

    def device_info(self) -> dict:
        return {"device": self._device_name, "firmware": "fake-1.0"}

    def plot(self, svg, settings, progress_cb=None, on_ready=None, resume=False) -> PlotResult:
        handle = _FakeHandle()
        if on_ready is not None:
            on_ready(handle)
        steps = max(1, int(self.duration / 0.05))
        for i in range(steps):
            if handle.paused:
                return PlotResult(paused=True, resume_svg="<fake-resume>")
            if progress_cb:
                progress_cb((i + 1) / steps * 100.0)
            time.sleep(0.05)
        if progress_cb:
            progress_cb(100.0)
        return PlotResult(paused=False, resume_svg=None)

    def manual(self, command, settings) -> None:
        self.manual_calls.append((command, dict(settings)))
