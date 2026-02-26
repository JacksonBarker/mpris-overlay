# MPRIS OBS Overlay

This starts a local web server that polls MPRIS metadata in a loop and serves:

- `GET /overlay` (or `/`): transparent browser overlay page for OBS.
- `GET /now`: JSON with current track metadata.

## Setup (virtual environment)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

System prerequisite: `gdbus` must be available on your system (usually provided by GLib).

## Run

```bash
./scripts/start.sh
```

If you run inside distrobox and DBus is blocked, force host-side DBus calls:

```bash
source .venv/bin/activate
python overlay_server.py --host 127.0.0.1 --port 8765 --dbus-mode host
```

Open this URL in OBS Browser Source:

`http://127.0.0.1:8765/overlay`

## Run in background

```bash
./scripts/start-bg.sh
```

Stop it later:

```bash
./scripts/stop-bg.sh
```

All helper scripts use `--dbus-mode auto`, so they work both inside and outside distrobox.
