# plottter-daemon

A small networked plot server for AxiDraw-compatible pen plotters (built for the
**Uunatek iDraw H SE A2**). It receives plot jobs (an SVG + settings) over HTTP and
drives the plotter with `pyaxidraw`, so the machine you design on is free during long
plots. It is the device side of the [Plottter](../plottter) app's remote-plotter
feature — Plottter sends jobs; this daemon plots them.

- **Stdlib-only HTTP server** — no FastAPI/Flask. The only thing you install on the Pi
  is `pyaxidraw`.
- **Plots are detached** — a job runs inside this long-lived service, so closing the
  laptop or dropping off the network does not stop the plot.
- **One job at a time**, with pause / resume / stop and live progress.
- **The daemon never changes your design** — it plots the SVG verbatim with
  `reordering=0`; all path optimization happens in Plottter.

## Install (on the Raspberry Pi)

```bash
git clone <this repo> plottter-daemon && cd plottter-daemon
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                 # the daemon itself (stdlib only)
# Driver for the plotter (not a hard dep, install separately):
pip install https://cdn.evilmadscientist.com/dl/ad/public/AxiDraw_API.zip
```

Your user needs serial access (usually already true on Raspberry Pi OS):
`sudo usermod -aG dialout $USER` (then re-login).

## Run

```bash
# Real plotter, auto-generated token (printed once):
python -m plottter_daemon --port 8080 --device-name "iDraw H SE A2"

# Trusted home network, no auth:
python -m plottter_daemon --no-auth

# No hardware yet? Simulate a plotter to test the Plottter connection:
python -m plottter_daemon --fake --no-auth
```

Then in Plottter → **Plot with AxiDraw → Remote Plotter (network)**: enter the URL
(`http://<pi-host>:8080`) and the token, tick **Send to remote device**, and click
**Refresh connection**.

### Run on boot (systemd)

Edit `deploy/plottter-daemon.service` (user, paths, `--token`, `--device-name`), then:

```bash
sudo cp deploy/plottter-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now plottter-daemon
```

## API (v1)

| Method & path | Purpose | Auth |
|---|---|---|
| `GET /api/v1/health` | liveness + device + idle/busy + `auth_required` | open |
| `GET /api/v1/version` | daemon / API version | open |
| `POST /api/v1/jobs` | `{svg, settings}` → `{job_id}`; `409` if busy | token |
| `GET /api/v1/jobs/{id}` | status / progress | token |
| `POST /api/v1/jobs/{id}/control` | `{action: pause\|resume\|stop}` | token |
| `POST /api/v1/manual` | `{command, settings}` (raise/lower pen, motors) | token |

`settings` is exactly the dict Plottter's plot dialog produces (`model`,
`speed_pendown/penup`, `pen_pos_up/down`, `pen_delay_*`, `const_speed`, …). Pen
positions ride with each job/manual call — the daemon keeps no machine state.

## Develop / test (no hardware)

```bash
pip install -e ".[dev]"
pytest -q          # exercises the server + job manager via a FakeExecutor
```

`plottter_daemon/executor.py` has the only hardware code (`AxiDrawExecutor`, a faithful
copy of Plottter's `export/axidraw.py`) and a `FakeExecutor` used by the tests.

## License

MIT.
