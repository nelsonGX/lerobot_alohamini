# AlohaMini Panel

A web panel for recording teleoperation datasets and reviewing them. Run it on the machine
the leader arms are plugged into.

```bash
./panel              # from the repo root; builds the UI the first time
# open http://<this-machine>:8000
./panel --rebuild    # after changing the UI
```

The robot host must still be running on the Jetson (`./host`). Set the Jetson IP under
**Settings** (gear icon). Settings apply to everyone using the panel.

## What it does

- **Record**: pre-flight checks (leader arms, calibration, ports already in use, Jetson host,
  disk space), then a session runs `examples/alohamini/record_bi.py`. The buttons send the
  same keys as the terminal (N = next, R = re-record, Q = stop). Optional voice cues.
- **Datasets**: browse local datasets in `~/.cache/huggingface/lerobot`. Watch any episode
  with synced cameras and state/action plots, mark bad episodes and delete them
  (uses `lerobot-edit-dataset`; a `<name>_old` backup is kept), or append more episodes.

## Layout

- `server/`: FastAPI backend. It runs the recorder in a pseudo-terminal and parses its output
  (`recorder.py`), and also handles dataset reading (`dataset_store.py`), checks
  (`preflight.py`) and routes (`main.py`).
- `app/`, `components/`, `lib/`: Next.js UI, exported as static files to `out/` and served
  by the backend.
- `.data/`: shared settings and session history (git-ignored).

UI development: start the backend (`./panel`), then run `npm run dev` here. `/api` is proxied to
`PANEL_API_ORIGIN` (default `http://127.0.0.1:8000`).
