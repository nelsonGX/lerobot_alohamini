# Recording the "pick 1 of 3 objects" dataset: teammate guide

You do not need to know LeRobot. The panel tells you what to do for every episode.

## Each session

1. **Check the robot**: the leader arms are plugged into this computer, and the robot host is running (**Robot** page →
   *Start robot host*). The **Pre-flight checks** on the Record page should be green.
2. Open the panel (`http://<this computer>:8000`), type **your name**, press **Start session**.
   Your name is saved with every episode you record.

## Each episode

| Step | What you see | What you do |
|---|---|---|
| **Get ready** | A picture of 3 objects (left / centre / right, as the front camera sees them) and the task, e.g. *pick up the red cube* | Put the objects on the table **exactly like the picture**. Keep the pre-selected task (the one with the fewest episodes) unless you have a reason; you can only pick from the list. Press **Start recording** (`Space`). |
| **Recording** | Countdown | Do the task with the leader arms. Press **Done** (`Space`) when the object is picked, or wait for the timer. |
| **Review** | Automatic checks (✔ or what looks wrong) | **Save** (`S`) keeps it. **Re-record** (`R`) throws it away and repeats the *same* layout. **Discard** (`D`) throws it away and moves on to a *new* layout. |

Nothing is written to the dataset until you press **Save**, so a discarded or interrupted episode never leaves anything
behind. The arms keep following the leader arms in *Get ready* and *Review*, so you can reset the scene while you decide.

When you are finished press **Finish session** (only possible between episodes).

## Stream health (top of the screen)

Green = fine. If something turns red (▲) during recording, the episode will probably be flagged:

- **FPS low / Longest gap / Missed**: the robot link is struggling. Move closer to the router, stop other heavy network use, retry.
- **Cameras: … black**: a camera is unplugged or covered. Fix it before recording more.
- **Link**: how long the robot takes to answer (milliseconds).

## Flagged episodes

After each recording the panel flags likely-bad episodes: too short, low frame rate, pauses in the stream, missing/black
camera, arms never moved. Warnings (slow link, ran into the time limit) are shown too. Flagged episodes are
recommended for **Discard** or **Re-record**; you *can* still save them. Later, on the **Datasets** page, *Select N flagged*
lets a maintainer review and delete them.

## Upload

After a session (no recording running) press **Upload dataset** on the Record page and choose where it goes.
Use **Test run** first if you are unsure. Nothing is uploaded unless the dataset is intact.

## If something goes wrong

- **Force stop** is safe: the episode in progress is discarded, the dataset is closed properly, saved episodes stay.
- *"The shared dataset is damaged"*: do not record. A file was cut off by a crash or power loss. Ask the maintainer.
- Want to rehearse without the robot? Ask the maintainer to start the panel with `PANEL_SIMULATE=1 ./panel`.

---

## For the maintainer

**Plan and tasks**: `examples/alohamini/collection.yaml` (restart `./panel` after editing): the one shared dataset name,
the three objects, the task strings (the only strings that can ever be recorded; the policy is trained on them verbatim), targets
(default 50 per task), automatic-flag thresholds, upload destinations. The suggested task is the one with the lowest
share of its target done; counts are read from the dataset itself.

**Where the data is**: `~/.cache/huggingface/lerobot/<dataset>` (`HF_LEROBOT_HOME`). Every session resumes this one dataset;
only one session can write at a time.

**Per-episode metadata** (operator, task, object layout and target slot, stream-health numbers, flags, timestamps) is in
`<dataset>/meta/collection.jsonl`, one JSON line per episode, indexed by `episode_index`. LeRobot has no field for this, so it
lives beside the dataset and is uploaded with it. Deleting episodes on the Datasets page keeps it aligned; deleting
episodes by other means (e.g. running `lerobot-edit-dataset` by hand) does not.

**Crash safety**: LeRobot only finishes a parquet file when it is closed, so the engine closes and reopens the dataset after
every saved episode (`checkpoint_every: 1`). A kill or power cut can then only affect the episode being saved at that moment.
The panel refuses to record into or upload a dataset it finds damaged.

**Upload setup**: Hub: set `upload.hub_repo_id`, run `uv run hf auth login` once on this machine. The repo is created private
and an existing public one is refused. GPU server: set `upload.rsync_dest` (`user@host:/path`) and enable SSH key login
(`ssh-copy-id`); the upload never waits for a password.

**Not enforced**: `./record_server` and `record_bi.py` still accept any task text and do not write this metadata; use the panel.
