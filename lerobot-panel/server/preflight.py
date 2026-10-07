"""Hardware and environment checks shown before a teammate starts recording."""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
from pathlib import Path

from config import LEADER_PORTS, SIMULATE, calibration_home, lerobot_home, Settings

# status: ok | warn | fail. "fail" blocks the Start button unless the user overrides.


def _check(id: str, label: str, status: str, detail: str, hint: str = "", action: str = "") -> dict:
    # action: a fix the UI can offer as a button (start_host, calibrate_leader, stop_teleop, setup_jetson)
    return {"id": id, "label": label, "status": status, "detail": detail, "hint": hint, "action": action}


def _port_users(device: str, exclude_pids: set[int]) -> list[str]:
    """Processes holding a serial device open (e.g. a teleop left running in a terminal)."""
    try:
        target = os.path.realpath(device)
    except OSError:
        return []
    users = []
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit() or int(pid_dir.name) in exclude_pids:
            continue
        try:
            for fd in (pid_dir / "fd").iterdir():
                if os.path.realpath(fd) == target:
                    cmdline = (pid_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                    users.append(f"PID {pid_dir.name}: {cmdline.strip()[:120]}")
                    break
        except (PermissionError, FileNotFoundError, ProcessLookupError, NotADirectoryError):
            continue
    return users


def _child_pids(root_pid: int) -> set[int]:
    pids = {root_pid}
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            stat = (pid_dir / "stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if ppid in pids:
            pids.add(int(pid_dir.name))
    return pids


def _disk_check() -> dict:
    home = lerobot_home()
    probe = home if home.exists() else home.parent if home.parent.exists() else Path.home()
    free_gb = shutil.disk_usage(probe).free / 1e9
    status = "ok" if free_gb > 20 else "warn" if free_gb > 5 else "fail"
    return _check("disk", "Free disk space", status, f"{free_gb:.0f} GB free in {probe}",
                  "" if status == "ok" else "Free up space or move old datasets off this machine.")  # fmt: skip


def run_checks(settings: Settings, recorder_pid: int | None = None, panel_users: list[str] | None = None,
               panel_pids: set[int] | None = None) -> list[dict]:
    """panel_users/panel_pids: programs the panel itself runs on the leader arms (teleop, calibration)."""
    checks = []
    if SIMULATE:  # PANEL_SIMULATE=1: no robot, no arms; only disk space matters
        checks.append(_check("sim", "Simulation mode", "ok", "Fake robot and leader arms - nothing will move",
                             "Unset PANEL_SIMULATE and restart ./panel to record with the real robot."))  # fmt: skip
        checks.append(_disk_check())
        return checks
    exclude = _child_pids(recorder_pid) if recorder_pid else set()
    for pid in panel_pids or ():
        exclude |= _child_pids(pid)
    exclude.add(os.getpid())

    # Leader arms
    for side, port in LEADER_PORTS.items():
        label = f"Leader arm ({side})"
        if not os.path.exists(port):
            checks.append(_check(f"leader_{side}", label, "fail", f"{port} not found",
                                 "Plug in the leader arm USB cable and power it on."))  # fmt: skip
            continue
        users = _port_users(port, exclude)
        if panel_users:
            checks.append(_check(f"leader_{side}", label, "fail", f"{panel_users[0]} is using the leader arms",
                                 "Stop it on the Robot page first.", "stop_teleop" if panel_users[0] == "Teleoperation" else ""))  # fmt: skip
        elif users:
            checks.append(_check(f"leader_{side}", label, "fail", f"{port} is in use",
                                 "Close the other program first: " + "; ".join(users)))  # fmt: skip
        else:
            checks.append(_check(f"leader_{side}", label, "ok", f"{port} → {os.path.realpath(port)}"))

    # Leader calibration (missing calibration makes record_bi.py wait on an interactive prompt)
    cal_dir = calibration_home() / "teleoperators" / "so_leader"
    missing = [s for s in ("left", "right") if not (cal_dir / f"{settings.teleop_id}_{s}.json").exists()]
    if missing:
        checks.append(_check("calibration", "Leader calibration", "fail",
                             f"Missing for {', '.join(missing)} arm ({settings.teleop_id})",
                             "Calibrate the leader arms on the Robot page.", "calibrate_leader"))  # fmt: skip
    else:
        checks.append(_check("calibration", "Leader calibration", "ok", f"{settings.teleop_id}"))

    # Robot host on the Jetson (ZMQ observation socket)
    label = "Robot host (Jetson)"
    try:
        with socket.create_connection((settings.jetson_ip, settings.obs_port), timeout=0.8):
            checks.append(_check("jetson", label, "ok", f"{settings.jetson_ip}:{settings.obs_port} is reachable"))
    except ConnectionRefusedError:
        checks.append(_check("jetson", label, "fail", f"{settings.jetson_ip} is up but the host is not running",
                             "Start the robot host on the Jetson.", "start_host"))  # fmt: skip
    except OSError as e:
        reason = "timed out" if isinstance(e, TimeoutError | socket.timeout) else str(e)
        checks.append(_check("jetson", label, "fail", f"Cannot reach {settings.jetson_ip} ({reason})",
                             "Check the Jetson is powered on, on the network, and the IP in Settings is right."))  # fmt: skip

    checks.append(_disk_check())

    # Python deps for recording
    missing_deps = [m for m in ("datasets", "av", "pyarrow") if importlib.util.find_spec(m) is None]
    if missing_deps:
        checks.append(_check("deps", "Recording dependencies", "fail", f"Missing: {', '.join(missing_deps)}",
                             "Run: uv sync --locked --extra dataset --extra hardware --extra feetech --extra lekiwi"))  # fmt: skip
    return checks
