import type { Phase } from "@/lib/api";

export const PHASE_META: Record<Phase, { label: string; color: string; description: string }> = {
  starting: { label: "Connecting", color: "var(--muted)", description: "Connecting to the robot and leader arms…" },
  recording: { label: "Recording", color: "var(--critical)", description: "Frames are being saved. Perform the task now." },
  resetting: { label: "Reset", color: "var(--warn)", description: "Not recording. Put the scene back to its start position." },
  saving: { label: "Saving", color: "var(--info)", description: "Encoding and saving the episode. Please wait." },
  waiting: { label: "Get ready", color: "var(--info)", description: "Waiting for a fresh robot observation before the next episode." },
  finalizing: { label: "Finalizing", color: "var(--info)", description: "Writing dataset metadata. Do not unplug anything." },
  done: { label: "Finished", color: "var(--good)", description: "Session complete and dataset saved." },
  failed: { label: "Failed", color: "var(--critical)", description: "The recorder stopped with an error." },
  aborted: { label: "Interrupted", color: "var(--warn)", description: "The recorder was interrupted." },
};
