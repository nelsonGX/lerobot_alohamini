#!/usr/bin/env python3

import argparse
import inspect
import logging
import math
import signal
import time
from contextlib import contextmanager

import lerobot.robots.alohamini  # noqa: F401 — registers alohamini_client robot type
from lerobot.configs.policies import PreTrainedConfig
from lerobot.datasets import aggregate_pipeline_dataset_features, create_initial_features
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from lerobot.policies import get_policy_class, make_pre_post_processors
from lerobot.policies.rtc.configuration_rtc import RTCConfig
from lerobot.processor import make_default_processors
from lerobot.robots.alohamini import AlohaMiniClient, AlohaMiniClientConfig
from lerobot.rollout.inference.factory import (
    RTCInferenceConfig,
    SyncInferenceConfig,
    create_inference_engine,
)
from lerobot.rollout.robot_wrapper import ThreadSafeRobot
from lerobot.utils.action_interpolator import ActionInterpolator
from lerobot.utils.constants import ACTION, OBS_STR
from lerobot.utils.device_utils import auto_select_torch_device
from lerobot.utils.feature_utils import build_dataset_frame, combine_feature_dicts, hw_to_dataset_features
from lerobot.utils.robot_utils import precise_sleep
from lerobot.utils.utils import log_say

try:
    from .evaluation_safety import EngageRamp, EvaluationSafetyGuard, hold_action, stop_inference
except ImportError:
    from evaluation_safety import EngageRamp, EvaluationSafetyGuard, hold_action, stop_inference


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value

    value = value.lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("Expected true or false.")


@contextmanager
def defer_interrupt():
    """Finish writing an episode before honoring Ctrl+C, so stopping never leaves it half-saved."""
    interrupted = []
    previous = signal.signal(signal.SIGINT, lambda *_: interrupted.append(True))
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
    if interrupted:
        raise KeyboardInterrupt


def apply_action_overrides(
    policy_cfg, n_action_steps: int | None, temporal_ensemble_coeff: float | None
) -> None:
    """Make the policy re-plan more (or less) often without retraining.

    A chunking policy predicts ``chunk_size`` (diffusion: ``horizon``) actions per call and
    executes ``n_action_steps`` of them before calling the model again. The chunk length is
    fixed by the trained weights; how much of it is executed is an inference-time choice.
    """
    if temporal_ensemble_coeff is not None:
        if not hasattr(policy_cfg, "temporal_ensemble_coeff"):
            raise ValueError(f"Temporal ensembling is only supported by ACT, not '{policy_cfg.type}'.")
        policy_cfg.temporal_ensemble_coeff = temporal_ensemble_coeff
        n_action_steps = 1  # ACT ensembles a fresh chunk on every control step.
    if n_action_steps is None:
        return
    if not hasattr(policy_cfg, "n_action_steps"):
        raise ValueError(f"Policy type '{policy_cfg.type}' has no n_action_steps to override.")
    if hasattr(policy_cfg, "chunk_size"):
        limit = policy_cfg.chunk_size
    elif hasattr(policy_cfg, "horizon"):
        limit = policy_cfg.horizon - getattr(policy_cfg, "n_obs_steps", 1) + 1
    else:
        limit = None
    if n_action_steps < 1 or (limit is not None and n_action_steps > limit):
        raise ValueError(f"--policy.n_action_steps must be between 1 and {limit} for this checkpoint.")
    policy_cfg.n_action_steps = n_action_steps


def main():
    parser = argparse.ArgumentParser(description="Evaluate AlohaMini robot with a pretrained policy")
    parser.add_argument("--eval.n_episodes", "--num_episodes", dest="num_episodes", type=int, default=2)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--eval.episode_time_s", "--episode_time", dest="episode_time", type=int, default=60)
    parser.add_argument(
        "--eval.reset_time_s",
        "--reset_time",
        dest="reset_time",
        type=float,
        default=10.0,
        help="Reset duration between evaluation episodes in seconds.",
    )
    parser.add_argument(
        "--dataset.single_task",
        "--task_description",
        dest="task_description",
        type=str,
        default="robot task",
    )
    parser.add_argument("--policy.path", "--hf_model_id", dest="policy_path", type=str, required=True)
    parser.add_argument("--dataset.repo_id", "--hf_dataset_id", dest="dataset_repo_id", type=str, default="")
    parser.add_argument(
        "--dataset.record",
        dest="record",
        type=parse_bool,
        nargs="?",
        const=True,
        default=True,
        help="Save evaluation episodes as a dataset. false runs the policy without recording.",
    )
    parser.add_argument(
        "--eval.continuous",
        dest="continuous",
        type=parse_bool,
        nargs="?",
        const=True,
        default=False,
        help="Run until stopped (Ctrl+C) instead of --eval.n_episodes. Without recording the policy runs "
        "as one uninterrupted episode; with recording, episodes of --eval.episode_time_s are saved "
        "back to back with no reset wait.",
    )
    parser.add_argument(
        "--policy.n_action_steps",
        dest="n_action_steps",
        type=int,
        default=None,
        help="Actions executed from each predicted chunk before the policy runs again. Lower = more "
        "frequent predictions (fps / n_action_steps per second). Default: the checkpoint's value.",
    )
    parser.add_argument(
        "--policy.temporal_ensemble_coeff",
        dest="temporal_ensemble_coeff",
        type=float,
        default=None,
        help="ACT only: predict every control step and blend overlapping chunks (implies n_action_steps=1).",
    )
    parser.add_argument(
        "--dataset.push_to_hub",
        dest="push_to_hub",
        type=parse_bool,
        nargs="?",
        const=True,
        default=True,
        help="Whether to upload the evaluation dataset to Hugging Face Hub.",
    )
    parser.add_argument("--robot.remote_ip", "--remote_ip", dest="remote_ip", type=str, default="127.0.0.1")
    parser.add_argument("--robot.id", "--robot_id", dest="robot_id", type=str, default="my_alohamini")
    parser.add_argument(
        "--robot.robot_model",
        "--robot_model",
        dest="robot_model",
        type=str,
        default="alohamini1",
        choices=["alohamini1", "alohamini2", "alohamini2pro"],
        help="Must match the robot_model on the Pi host side",
    )
    parser.add_argument(
        "--inference.type",
        "--inference_type",
        dest="inference_type",
        type=str,
        default="sync",
        choices=["sync", "rtc"],
        help="sync: one policy call per control tick. rtc: async background inference "
        "(only for policies whose predict_action_chunk supports inference_delay/prev_chunk_left_over, "
        "e.g. pi0/pi05/pi0_fast/smolvla/evo1/molmoact2 — not act/diffusion/vqbet).",
    )
    parser.add_argument(
        "--inference.rtc.execution_horizon",
        dest="rtc_execution_horizon",
        type=int,
        default=10,
        help="RTC only: number of steps re-generated per inference call.",
    )
    parser.add_argument(
        "--inference.rtc.max_guidance_weight",
        dest="rtc_max_guidance_weight",
        type=float,
        default=10.0,
        help="RTC only: max guidance weight for prefix inpainting.",
    )
    parser.add_argument(
        "--inference.rtc.queue_threshold",
        dest="rtc_queue_threshold",
        type=int,
        default=30,
        help="RTC only: trigger a new inference call once the action queue drops to this size.",
    )
    parser.add_argument(
        "--engage_ramp_speed",
        dest="engage_ramp_speed",
        type=float,
        default=30.0,
        help="At episode start and after a safety restart, move the arms from their current pose to the "
        "policy's targets at most this fast (joint units per second) so the Host engage gate opens "
        "instead of leaving the arms limp. 0 disables the ramp.",
    )
    parser.add_argument(
        "--interpolation_multiplier",
        dest="interpolation_multiplier",
        type=int,
        default=1,
        help="Send N interpolated actions per policy action for a smoother, higher-rate control loop "
        "(1 = disabled).",
    )
    args = parser.parse_args()
    if args.reset_time < 0:
        parser.error("--eval.reset_time_s must be non-negative")
    if args.record and not args.dataset_repo_id:
        parser.error("--dataset.repo_id is required unless --dataset.record=false")

    device = str(auto_select_torch_device())

    # === Policy ===
    policy_cfg = PreTrainedConfig.from_pretrained(args.policy_path)
    policy_cfg.pretrained_path = args.policy_path
    apply_action_overrides(policy_cfg, args.n_action_steps, args.temporal_ensemble_coeff)
    policy_class = get_policy_class(policy_cfg.type)
    policy = policy_class.from_pretrained(args.policy_path, config=policy_cfg)

    # === Inference engine config (needs to be set up before .to(device)/.eval(), mirroring
    # lerobot.rollout.context.build_rollout_context) ===
    if args.inference_type == "rtc":
        predict_chunk_params = inspect.signature(policy_class.predict_action_chunk).parameters
        accepts_rtc_kwargs = {"inference_delay", "prev_chunk_left_over"}.issubset(
            predict_chunk_params
        ) or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in predict_chunk_params.values())
        if not accepts_rtc_kwargs:
            raise ValueError(
                f"Policy type '{policy_cfg.type}' does not support RTC inference: "
                f"predict_action_chunk() has no inference_delay/prev_chunk_left_over/**kwargs. "
                f"RTC is only supported by VLA-style policies (pi0, pi05, pi0_fast, smolvla, evo1, "
                f"molmoact2). Use --inference.type=sync instead."
            )
        rtc_config = RTCConfig(
            execution_horizon=args.rtc_execution_horizon,
            max_guidance_weight=args.rtc_max_guidance_weight,
        )
        policy.config.rtc_config = rtc_config
        if hasattr(policy, "init_rtc_processor"):
            policy.init_rtc_processor()
        inference_cfg = RTCInferenceConfig(rtc=rtc_config, queue_threshold=args.rtc_queue_threshold)
    else:
        inference_cfg = SyncInferenceConfig()

    policy = policy.to(device)
    policy.eval()

    # === Robot ===
    robot_config = AlohaMiniClientConfig(
        remote_ip=args.remote_ip, id=args.robot_id, robot_model=args.robot_model
    )
    robot = AlohaMiniClient(robot_config)
    robot.connect()
    robot_wrapper = ThreadSafeRobot(robot)

    # === Processors ===
    teleop_action_processor, robot_action_processor, robot_observation_processor = make_default_processors()

    # === Dataset features ===
    # Use all observation features (pos + base vel/height) to match what record_bi.py records.
    observation_features_hw = robot.observation_features
    action_features_hw = robot.action_features

    action_dataset_features = aggregate_pipeline_dataset_features(
        pipeline=teleop_action_processor,
        initial_features=create_initial_features(action=action_features_hw),
        use_videos=True,
    )
    observation_dataset_features = aggregate_pipeline_dataset_features(
        pipeline=robot_observation_processor,
        initial_features=create_initial_features(observation=observation_features_hw),
        use_videos=True,
    )
    dataset_features = combine_feature_dicts(action_dataset_features, observation_dataset_features)
    hw_features = hw_to_dataset_features(observation_features_hw, "observation")
    ordered_action_keys = list(action_features_hw.keys())

    # === Dataset ===
    dataset = None
    if args.record:
        dataset = LeRobotDataset.create(
            repo_id=args.dataset_repo_id,
            fps=args.fps,
            features=dataset_features,
            robot_type=robot.name,
            use_videos=True,
            image_writer_threads=4,
        )

    # === Policy processors (needs dataset stats) ===
    preprocessor, postprocessor = make_pre_post_processors(
        policy_cfg=policy_cfg,
        pretrained_path=args.policy_path,
        dataset_stats=dataset.meta.stats if dataset is not None else None,
        preprocessor_overrides={"device_processor": {"device": device}},
    )

    # === Inference engine ===
    engine = create_inference_engine(
        inference_cfg,
        policy=policy,
        preprocessor=preprocessor,
        postprocessor=postprocessor,
        robot_wrapper=robot_wrapper,
        hw_features=hw_features,
        dataset_features=dataset_features,
        ordered_action_keys=ordered_action_keys,
        task=args.task_description,
        fps=float(args.fps),
        device=device,
    )

    # init_rerun(session_name="alohamini_evaluate")
    steps = getattr(policy_cfg, "n_action_steps", None)
    if args.inference_type == "sync" and isinstance(steps, int) and steps > 0:
        ensemble = getattr(policy_cfg, "temporal_ensemble_coeff", None)
        print(
            f"[EVAL] Policy predicts every {steps} step(s) = {args.fps / steps:.2f} Hz"
            + (f" (temporal ensembling {ensemble})" if ensemble is not None else ""),
            flush=True,
        )
    elif args.inference_type == "rtc":
        print(
            f"[EVAL] RTC: new chunk of {getattr(policy_cfg, 'chunk_size', '?')} requested once "
            f"{args.rtc_queue_threshold} queued actions remain (execution horizon {args.rtc_execution_horizon})",
            flush=True,
        )
    print(
        "[EVAL] Mode: "
        + ("run until stopped" if args.continuous else f"{args.num_episodes} episode(s)")
        + (f", recording to {args.dataset_repo_id}" if dataset is not None else ", not recording"),
        flush=True,
    )
    log_say("Starting evaluation")

    interpolator = ActionInterpolator(multiplier=args.interpolation_multiplier)
    control_interval = interpolator.get_control_interval(args.fps)
    recorded = 0
    # Continuous without recording never needs an episode boundary.
    episode_time = math.inf if args.continuous and dataset is None else args.episode_time
    safety_guard = EvaluationSafetyGuard()
    engage_ramp = EngageRamp(max_step=args.engage_ramp_speed / (args.fps * args.interpolation_multiplier))

    def reset_environment(next_episode: int) -> None:
        """Wait for manual scene reset while draining remote observations."""
        if args.reset_time == 0:
            return

        deadline = time.monotonic() + args.reset_time
        last_remaining = None
        while True:
            remaining = max(0, math.ceil(deadline - time.monotonic()))
            if remaining != last_remaining:
                print(
                    f"\r[RESET] Prepare evaluation episode {next_episode}/{args.num_episodes}: "
                    f"{remaining}s remaining",
                    end="",
                    flush=True,
                )
                last_remaining = remaining
            if remaining == 0:
                break

            # Keep consuming observations during reset so the next episode starts
            # from a fresh remote frame rather than one cached before the reset.
            robot.get_observation()
            sleep_t = min(control_interval, max(0.0, deadline - time.monotonic()))
            if sleep_t > 0:
                precise_sleep(sleep_t)
        print(flush=True)

    try:
        while args.continuous or recorded < args.num_episodes:
            total = "" if args.continuous else f" of {args.num_episodes}"
            log_say(f"Eval episode {recorded + 1}{total}")
            stop_inference(engine)
            engine.reset()
            engine.start()
            interpolator.reset()
            engage_ramp.reset()
            engine.resume()
            start = time.perf_counter()
            restart_pending = False
            episode_frames = 0

            while (time.perf_counter() - start) < episode_time:
                loop_start = time.perf_counter()

                obs_raw = robot.get_observation()
                previous_context = safety_guard.context
                obs_raw, reason = safety_guard.check_observation(robot, obs_raw)
                if reason:
                    if not restart_pending:
                        logging.warning("%s；等待新反馈，暂不发送动作。", reason)
                        engine.pause()
                        interpolator.reset()
                        engage_ramp.reset()
                        restart_pending = True
                    precise_sleep(control_interval)
                    continue
                if restart_pending or (
                    previous_context[0] is not None and safety_guard.context != previous_context
                ):
                    stop_inference(engine)
                    engine.reset()
                    interpolator.reset()
                    engage_ramp.reset()
                    engine.start()
                    engine.resume()
                    restart_pending = False
                inference_context = safety_guard.context
                obs_processed = robot_observation_processor(obs_raw)
                if interpolator.needs_new_action():
                    engine.notify_observation(obs_processed)
                obs_frame = build_dataset_frame(dataset_features, obs_processed, prefix=OBS_STR)

                inference_started = time.perf_counter()
                if interpolator.needs_new_action():
                    action_tensor = engine.get_action(obs_frame)
                    if action_tensor is not None:
                        if action_tensor.ndim != 1 or action_tensor.numel() != len(ordered_action_keys):
                            raise ValueError("Policy action shape does not match the robot actuator schema")
                        interpolator.add(action_tensor.cpu())

                obs_raw, reason = safety_guard.check_observation(
                    robot, obs_raw,
                    refresh=time.perf_counter() - inference_started >= control_interval,
                )
                if reason or safety_guard.context != inference_context:
                    # Discard predictions across joint protection or prolonged feedback loss.
                    engine.pause()
                    interpolator.reset()
                    engage_ramp.reset()
                    restart_pending = True
                    precise_sleep(control_interval)
                    continue
                obs_processed = robot_observation_processor(obs_raw)
                obs_frame = build_dataset_frame(dataset_features, obs_processed, prefix=OBS_STR)
                interp_action = interpolator.get()
                if interp_action is not None:
                    action_dict = {k: interp_action[i].item() for i, k in enumerate(ordered_action_keys)}
                    action_dict = engage_ramp.apply(action_dict, obs_raw)
                    sent = robot.send_action(robot_action_processor((action_dict, obs_raw)))
                    if sent and robot.observation_updated and dataset is not None:
                        action_frame = build_dataset_frame(dataset_features, action_dict, prefix=ACTION)
                        dataset.add_frame({**obs_frame, **action_frame, "task": args.task_description})
                        episode_frames += 1

                dt = time.perf_counter() - loop_start
                if (sleep_t := control_interval - dt) > 0:
                    precise_sleep(sleep_t)

            engine.pause()
            if dataset is None:
                pass
            elif episode_frames:
                with defer_interrupt():
                    dataset.save_episode()
                print(f"[EVAL] Saved episode {recorded + 1} ({episode_frames} frames)", flush=True)
            else:
                logging.warning("No evaluation frames collected; skipping empty episode.")
            recorded += 1
            if not args.continuous and recorded < args.num_episodes:
                reset_environment(recorded + 1)
    finally:
        # A second Ctrl+C from the panel's stop escalation must not cut the hold/save below short.
        previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            engine.pause()
            try:
                robot.send_action(hold_action(robot.last_remote_state))
            finally:
                try:
                    engine.stop()
                finally:
                    try:
                        if dataset is not None and dataset.has_pending_frames():
                            dataset.save_episode()
                    finally:
                        try:
                            if dataset is not None:
                                dataset.finalize()
                        finally:
                            robot.disconnect()
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
    log_say("Evaluation complete")
    if args.push_to_hub and dataset is not None:
        dataset.push_to_hub()


if __name__ == "__main__":
    main()
