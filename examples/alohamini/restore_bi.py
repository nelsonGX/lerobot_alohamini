"""Move the follower arms to the leader arms' current pose, slowly, then exit.

Run this before teleoperation or recording when the follower is somewhere else than the leader
(the follower otherwise stays limp until the leader is moved close to its pose).
"""

import argparse
import time

from lerobot.robots.alohamini import AlohaMiniClient, AlohaMiniClientConfig
from lerobot.teleoperators.bi_so_leader import BiSOLeader, BiSOLeaderConfig
from lerobot.teleoperators.so_leader import SOLeaderConfig
from lerobot.utils.robot_utils import precise_sleep

parser = argparse.ArgumentParser()
parser.add_argument("--fps", type=int, default=50, help="Command frequency")
parser.add_argument("--duration", type=float, default=3.0, help="Seconds to take moving to the leader pose")
parser.add_argument("--robot.remote_ip", "--remote_ip", dest="remote_ip", type=str, default="127.0.0.1")
parser.add_argument("--robot.id", "--robot_id", dest="robot_id", type=str, default="my_alohamini")
parser.add_argument(
    "--robot.robot_model",
    "--robot_model",
    dest="robot_model",
    type=str,
    default="alohamini1",
    choices=["alohamini1", "alohamini2", "alohamini2pro"],
)
parser.add_argument("--teleop.id", "--leader_id", dest="leader_id", type=str, default="so101_leader_bi")
parser.add_argument(
    "--teleop.arm_profile",
    "--arm_profile",
    dest="arm_profile",
    type=str,
    default="so-arm-5dof",
    choices=["so-arm-5dof", "am-leader-6dof"],
)
args = parser.parse_args()
if args.fps <= 0 or args.duration <= 0:
    parser.error("--fps and --duration must be positive")

robot = AlohaMiniClient(
    AlohaMiniClientConfig(remote_ip=args.remote_ip, id=args.robot_id, robot_model=args.robot_model)
)
leader = BiSOLeader(
    BiSOLeaderConfig(
        left_arm_config=SOLeaderConfig(
            port="/dev/am_arm_leader_left", arm_profile=args.arm_profile, use_degrees=False
        ),
        right_arm_config=SOLeaderConfig(
            port="/dev/am_arm_leader_right", arm_profile=args.arm_profile, use_degrees=False
        ),
        id=args.leader_id,
    )
)

robot.connect()
leader.connect()
try:
    arm_keys = [k for k in robot.action_features if k.startswith("arm_")]
    start = None
    t_start = time.perf_counter()
    hold_until = None
    print("Moving the follower arms to the leader pose…", flush=True)
    while True:
        t0 = time.perf_counter()
        obs = robot.get_observation(include_cameras=False)
        if start is None:
            if not all(k in obs for k in arm_keys):
                if t0 - t_start > 5:
                    raise SystemExit("No follower arm state received from the robot host.")
                precise_sleep(1.0 / args.fps)
                continue
            start = {k: float(obs[k]) for k in arm_keys}
            t_start = t0
        target = {f"arm_{k}": v for k, v in leader.get_action().items()}
        alpha = min(1.0, (t0 - t_start) / args.duration)
        alpha = alpha * alpha * (3 - 2 * alpha)  # smoothstep: gentle start and stop
        action = {k: start[k] + alpha * (target[k] - start[k]) for k in arm_keys if k in target}
        robot.send_action(action)
        if alpha >= 1.0:
            hold_until = hold_until or t0 + 0.5  # keep commanding briefly so the final pose is reached
            if t0 >= hold_until:
                break
        precise_sleep(max(1.0 / args.fps - (time.perf_counter() - t0), 0.0))
    print("Done: the follower arms match the leader arms.", flush=True)
finally:
    leader.disconnect()
    robot.disconnect()
