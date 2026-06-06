"""Run the plot daemon.

    python -m plottter_daemon [--host H] [--port P]
                             [--token TOK | --no-auth]
                             [--device-name NAME] [--serial-port PORT]
                             [--fake]

Auth: if neither --token nor --no-auth is given, a random token is generated and
printed once (secure-by-default). Paste it into Plottter's "Remote Plotter" Token
field. --no-auth runs fully open (fine on a trusted home network).

--fake uses a simulated plotter (no pyaxidraw / no hardware) — handy for testing
the install and the Plottter connection before wiring up the real machine.
"""

from __future__ import annotations

import argparse
import secrets

from .executor import AxiDrawExecutor, FakeExecutor
from .jobs import JobManager
from .server import API_BASE, make_server


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="plottter_daemon",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--token", default=None, help="require this bearer token on control endpoints")
    ap.add_argument("--no-auth", action="store_true", help="run with no token (trusted network)")
    ap.add_argument("--device-name", default="iDraw H SE A2")
    ap.add_argument("--serial-port", default=None, help="force a serial port (default: auto-detect)")
    ap.add_argument("--fake", action="store_true", help="simulate a plotter (no hardware)")
    args = ap.parse_args()

    if args.no_auth:
        token = None
    elif args.token:
        token = args.token
    else:
        token = secrets.token_hex(16)  # secure-by-default

    if args.fake:
        executor = FakeExecutor(device_name=args.device_name + " (FAKE)")
    else:
        executor = AxiDrawExecutor(device_name=args.device_name)

    # The serial port, if forced, is injected into every job's settings via a
    # default; the laptop's per-job settings still win if it sets one.
    manager = JobManager(executor)
    if args.serial_port:
        _wrap_default_port(manager, args.serial_port)

    server = make_server(args.host, args.port, manager, token)
    print(f"[plottter-daemon] listening on http://{args.host}:{args.port}{API_BASE}")
    print(f"[plottter-daemon] device: {args.device_name}{'  (FAKE)' if args.fake else ''}")
    print(f"[plottter-daemon] auth: {'OPEN (no token)' if not token else 'token = ' + token}")
    if not executor.is_available() and not args.fake:
        print("[plottter-daemon] WARNING: pyaxidraw not importable — plots will fail until installed.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[plottter-daemon] shutting down")
        server.shutdown()


def _wrap_default_port(manager: JobManager, serial_port: str) -> None:
    """Inject a default serial port into submit/manual settings."""
    orig_submit = manager.submit
    orig_manual = manager.manual

    def submit(svg, settings):
        return orig_submit(svg, {**{"port": serial_port}, **settings})

    def manual(command, settings):
        return orig_manual(command, {**{"port": serial_port}, **settings})

    manager.submit = submit  # type: ignore[method-assign]
    manager.manual = manual  # type: ignore[method-assign]


if __name__ == "__main__":
    main()
