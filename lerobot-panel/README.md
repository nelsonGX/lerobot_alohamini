# AlohaMini Panel

A web panel for recording teleoperation datasets and reviewing them. Run it on the machine
the leader arms are plugged into.

```bash
./panel              # from the repo root; builds the UI the first time
# open http://<this-machine>:8000
./panel --rebuild    # after changing the UI
```

Set the Jetson IP under **Settings** (gear icon). Settings apply to everyone using the panel.
No SSH: the Jetson runs a small agent that the panel talks to (HTTP for commands, a WebSocket
for live logs and health). On the Jetson, once:

```bash
./agent              # from the repo root; prints a token on first run
```

Then open **Robot → Jetson** and paste the token. To keep the agent running across reboots,
use a systemd unit like:

```ini
[Unit]
Description=LeRobot panel agent
After=network-online.target
[Service]
User=<jetson user>
WorkingDirectory=/home/<jetson user>/lerobot_alohamini
ExecStart=/home/<jetson user>/lerobot_alohamini/agent
Restart=on-failure
[Install]
WantedBy=multi-user.target
```

The agent runs the robot host, so the host keeps running if the panel restarts; the panel picks it up again.

## What it does

- **Record** (teammates: see [`RECORDING.md`](./RECORDING.md)): pre-flight checks, then a session runs
  `examples/alohamini/record_panel.py`, one shared dataset (`examples/alohamini/collection.yaml`). For every episode the panel
  shows a random 3-object layout and a task chosen from the fixed list (the one furthest behind its target is suggested), then
  the operator **saves, discards or re-records**; nothing partial is ever written. Live stream health (FPS, latency, frame gaps),
  automatic flags for bad episodes, operator name and layout stored per episode, and an **Upload** button (private Hugging Face
  repo or rsync). `PANEL_SIMULATE=1 ./panel` runs all of it on fake hardware.
- **Robot**: no terminal needed for the rest.
  - Start/stop the robot host on the Jetson through the agent (what `./host` does), with or without cameras.
    A host started from a terminal is detected and can be stopped too.
  - Teleoperate without recording (`./client`).
  - Calibrate the leader arms (`./lcalibrate`) or the follower arms on the Jetson (`./fcalibrate`). Prompts
    such as "press ENTER" become buttons.
  - Pre-flight problems on the Record page have fix buttons, e.g. **Start robot host**.
  - **Jetson monitor**: live CPU, memory, disk, temperature and robot device links (`/dev/am_*`).
  - Stopping the panel stops the programs it runs locally; the host on the Jetson keeps running.
- **Datasets**: browse local datasets in `~/.cache/huggingface/lerobot`. Watch any episode
  with synced cameras and state/action plots, mark bad episodes and delete them
  (uses `lerobot-edit-dataset`; a `<name>_old` backup is kept), or append more episodes.

## Layout

- `server/`: FastAPI backend. It runs the recording engine as a child process, sends it JSON commands and tails its
  event file (`recorder.py`), starts uploads (`upload.py`), and also handles dataset reading (`dataset_store.py`), checks
  (`preflight.py`), the host/teleop/calibration programs (`procs.py`, shown via the tiny
  terminal emulator in `term.py`) and routes (`main.py`).
- `app/`, `components/`, `lib/`: Next.js UI, exported as static files to `out/` and served
  by the backend.
- `.data/`: shared settings and session history (git-ignored).

UI development: start the backend (`./panel`), then run `npm run dev` here. `/api` is proxied to
`PANEL_API_ORIGIN` (default `http://127.0.0.1:8000`).
