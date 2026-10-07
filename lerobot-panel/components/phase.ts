import type { Phase } from "@/lib/api";

export const PHASE_META: Record<Phase, { label: string; color: string; description: string }> = {
  starting: { label: "Starting", color: "var(--muted)", description: "Starting the recorder…" },
  connecting: { label: "Connecting", color: "var(--muted)", description: "Connecting to the robot and leader arms…" },
  ready: { label: "Get ready", color: "var(--info)", description: "Not recording. Place the objects as shown, then start." },
  recording: { label: "Recording", color: "var(--critical)", description: "Frames are being recorded. Perform the task now." },
  review: { label: "Review", color: "var(--warn)", description: "Recording stopped. Nothing is saved until you press Save." },
  saving: { label: "Saving", color: "var(--info)", description: "Encoding and saving. Please wait." },
  finalizing: { label: "Closing", color: "var(--info)", description: "Writing dataset metadata. Do not unplug anything." },
  done: { label: "Finished", color: "var(--good)", description: "Session complete and dataset saved." },
  failed: { label: "Failed", color: "var(--critical)", description: "The recorder stopped with an error." },
  aborted: { label: "Stopped", color: "var(--warn)", description: "The session was stopped." },
};
