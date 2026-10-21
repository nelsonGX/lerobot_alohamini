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
- **Train & Eval**: connect a GPU server, start/stop training, select saved checkpoints,
  run simulation or live Aloha Mini evaluation, and monitor GPU memory, utilization,
  temperature, job history and logs. See the setup below.

## GPU server setup

Use the same repository revision on the panel, GPU server and Jetson. On the GPU
server, install the training dependencies and any policy/environment extras you use:

```bash
# ACT training + Aloha simulation + the live evaluation script's dependencies:
uv sync --locked --extra training --extra evaluation --extra core_scripts --extra aloha
# Add --extra diffusion, --extra smolvla, or --extra pi when using those policies.
# Add --extra pusht, --extra libero, or --extra metaworld for those environments.
uv run --no-sync python -c 'import torch; print(torch.cuda.is_available())'
hf auth login       # if using private datasets/models; authenticate on the GPU server
./gpu_agent         # prints a separate GPU agent token; defaults to port 8766
```

CUDA-enabled PyTorch, a compatible NVIDIA driver and the selected environment's
native dependencies must be installed on that machine. `./gpu_agent` preserves the
installed environment with `--no-sync`. It does not install policy or simulator extras.

On the panel machine, run `./panel --rebuild`, open **Train & Eval**, and enter the GPU
server address, port and printed token. The panel backend must reach that server on
TCP 8766 (override with `GPU_AGENT_PORT=9000 ./gpu_agent`). Use a trusted LAN or VPN,
as with the Jetson agent; the panel/agent connection uses HTTP.

1. **Train**: upload the collection dataset using **Record → Upload dataset**, or copy
   it to the GPU server. Choose its `namespace/name`; optionally provide its directory
   on that server under **Training options**. Choose a policy, steps and batch size.
   Supplying a pretrained model uses that model's policy type and starts a new
   fine-tuning run. VLA policies need the appropriate pretrained weights. Checkpoints
   stay local; training does not publish models to the Hub.
2. **Simulation**: select a saved model from **Models in GPU outputs** or enter a model ID, then choose the
   environment, task and episode count. The policy must match the environment's
   observation/action shapes. A policy trained on real Aloha Mini data does not
   automatically work in the Aloha benchmark. Aggregate evaluation results appear
   under the job's logs; videos and other artifacts remain in its output directory.
   The model picker automatically scans the GPU server's entire `outputs/` directory,
   including runs launched outside the panel. Choose **Last checkpoint per run** or
   **All checkpoints**. Last follows a valid `checkpoints/last` link, falling back to
   the highest numbered checkpoint. The picker lists each physical checkpoint once,
   and requires a readable config and nonempty model weights. It refreshes every
   10 seconds or when you press **Refresh**. Selection fills the concrete checkpoint
   path for fine-tuning, simulation, or live evaluation; it does not start jobs automatically.
3. **Live robot**: start the Jetson host with cameras on the **Robot** page. The GPU
   server must reach the Jetson's command/observation ports (5555/5556 by default).
   Enter the task, a new `namespace/eval_run` dataset name, episode duration and reset
   time. An operator at the robot checks the readiness box and starts evaluation.
   Inference runs on the GPU server through `examples/alohamini/evaluate_bi.py`.
   Its existing engage ramp and safety guard remain in use. The panel blocks conflicting
   recording, teleoperation and calibration operations while live evaluation runs.
   Camera previews require the panel-to-Jetson camera stream (5557).

One GPU job runs at a time. **Stop job** interrupts it, then terminates a hung process.
Closing the browser or restarting the panel does not stop GPU jobs. Stopping the GPU
agent stops its jobs; interrupted jobs are not resumed automatically. The stop control
is a network/software stop, so keep the robot's physical stop available during live use.

Training checkpoints and simulation artifacts are stored under `outputs/panel/` on
the GPU server. Live evaluation datasets use its `HF_LEROBOT_HOME` (by default
`~/.cache/huggingface/lerobot`). The panel shows these paths; it does not copy artifacts
back automatically or assign live-robot success scores. Job history and the most recent
400 console lines persist in `lerobot-panel/.data/gpu_jobs/` across agent restarts.
The agent token lives in `.data/gpu_agent_token`; the panel keeps the connection secret
in its own `.data/gpu_connection.json`, with owner-only permissions.

For persistent hosting, adapt the Jetson systemd example above to execute `gpu_agent`
on the GPU server. Run only one agent process per checkout; do not add uvicorn workers.

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
