"""plottter-daemon — a small networked plot server for AxiDraw-compatible plotters.

Receives plot jobs (SVG + settings) over HTTP and drives the plotter with
pyaxidraw, so the designing machine is free during long plots. Implements the
remote-plotter contract used by the Plottter app.
"""

from .executor import (
    AxiDrawExecutor,
    FakeExecutor,
    PlotResult,
    PlotterError,
    PlotterExecutor,
)
from .jobs import Busy, JobManager
from .server import make_handler, make_server

__all__ = [
    "AxiDrawExecutor",
    "FakeExecutor",
    "PlotResult",
    "PlotterError",
    "PlotterExecutor",
    "Busy",
    "JobManager",
    "make_handler",
    "make_server",
]
