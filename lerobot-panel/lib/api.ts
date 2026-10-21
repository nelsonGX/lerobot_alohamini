/** Browser API contracts for server/main.py. Agent credentials never appear in these responses. */
export async function request<T = unknown>(path: string, method = "GET", body?: unknown): Promise<T> {
  const res = await fetch(path, { method, headers: body === undefined ? undefined : { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await res.json();
  if (!res.ok) {
    const detail = data.detail ?? data.error ?? res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data as T;
}
export type Settings = { jetson_ip: string; robot_model: string; teleop_id: string; arm_profile: string; default_namespace: string; default_fps: number; default_episode_time_s: number; default_reset_time_s: number; default_num_episodes: number; obs_port: number; agent_port: number; host_args: string; host_cameras: boolean };
export type Phase = "starting" | "connecting" | "ready" | "recording" | "review" | "saving" | "finalizing" | "done" | "failed" | "aborted";
export type TaskMode = "rotate" | "single";
export type ControlAction = "start" | "done" | "save" | "discard" | "rerecord" | "finish" | "set_task" | "set_auto" | "abort";
export type PlanItem = { id: string; name: string; color: string; task_text: string; target: number };
export type NewDatasetItem = Omit<PlanItem, "id">;
export type TaskProgress = { id: string; text: string; object: string; target: number; count: number };
export type CollectionConfig = { dataset: string; objects: { id: string; name: string; color: string }[]; slots: string[]; tasks: TaskProgress[]; source: string; upload: { hub: boolean; rsync: boolean; hub_repo_id: string; rsync_dest: string } };
export type CollectionState = { config: CollectionConfig; counts: Record<string, number>; flagged: Record<string, number>; total_episodes: number; suggested_task_id: string | null; exists: boolean; problems: string[]; error?: string };
export type Layout = { names: Record<string, string>; target_slot: string };
export type Health = { ok: "good" | "warn" | "bad" | null; fps: number | null; target_fps: number; max_gap_ms?: number; stale_pct?: number; latency_ms?: number; cameras?: Record<string, string>; limits?: { min_fps_ratio?: number; max_gap_s?: number; max_stale_frac?: number; max_latency_p95_s?: number } };
export type Flag = { code: string; message: string; severity: string };
export type EngineState = { session_id: string; operator: string; dataset: string; fps: number; episode_time_s: number; phase: Phase; message: string; tasks: TaskProgress[]; suggested_task_id: string | null; task_mode: TaskMode; auto: boolean; auto_in_s: number | null; task_id: string | null; task_text: string | null; layout: Layout | null; attempt: number; episode_number: number | null; remaining_s: number | null; health: Health | null; review: { stats: { duration_s: number; frames: number; fps: number | null }; flags: Flag[]; recommend: string } | null; episodes: { status: string; flags: string[] }[]; saved: number; discarded: number; other_count: number; simulated: boolean };
export type Session = { id: string; operator: string; dataset: string; episode_time_s: number; fps: number; started_at: number; ended_at: number | null; phase: Phase; active: boolean; error: string | null; error_hint: string | null; stop_requested: boolean; saved_count: number; discarded_count: number; state: EngineState | null; now: number };
export type LogLine = { id: number; text: string };
export type RecorderState = { session: Session | null; log: LogLine[]; log_seq: number };
export type HistoryEntry = { id: string; operator: string; dataset: string; started_at: number; ended_at: number | null; outcome: Phase; episodes_saved: number; episodes_discarded: number; episodes_flagged: number; error: string | null };
export type Check = { id: string; label: string; status: "ok" | "warn" | "fail"; detail: string; hint: string; action: string };
export type ProcName = "host" | "teleop" | "restore" | "calibrate";
export type ProcSnapshot = { name: string; title: string; state: "running" | "stopping" | "exited"; exit_code: number | null; stopped: boolean; started_at: number; ended_at: number | null; version: number; prompt: { kind: string; text: string; hint: string } | null; first_line?: number; lines?: string[]; waiting_for_input?: boolean };
export type JetsonStatus = { ip: string; reachable: boolean; agent_running: boolean; agent_ok: boolean; token_set: boolean; token_rejected: boolean; host_listening: boolean; host_managed: boolean; system: { hostname: string; repo: string; uptime_s: number; cpu_percent: number | null; cpus: number; load: number[]; mem: { used: number; total: number }; disk: { used: number; total: number }; temps: { name: string; c: number }[]; devices: { name: string; ok: boolean }[] } | null };
export type RobotState = { jetson: JetsonStatus; procs: Record<ProcName, ProcSnapshot | null> & { calibrate_target: string | null }; recording: string | null };
export type HostJoint = { name: string; now: number | null; cmd: number | null; lo: number; hi: number; ma: number; cap_ma: number; hold: boolean };
export type HostArm = { side: string; engaged: boolean; worst_joint: string | null; worst_error_deg: number | null; joints: HostJoint[] };
export type HostSnapshot = { subtitle: string; uptime_s: number; arms: HostArm[]; base: { x: number; y: number; theta: number; lift_mm: number } | null; events: { t: number; level: string; msg: string }[]; client: { state: "live" | "idle" | "waiting" | "watchdog"; age_s: number | null }; source: string; owner: string | null; watchdog_events: number; protection: { holds: number; overcurrent_releases: number }; loop_hz: number; hz_history: number[]; loop_ms: number };
export type UploadState = { running: boolean; status: string; method: string; dry_run: boolean; output: string[]; last_success: { at: number; by: string; episodes: number } | null };
export type DatasetSummary = { repo_id: string; path: string; codebase_version: string; robot_type: string; fps: number; total_episodes: number; total_frames: number; duration_s: number; cameras: string[]; tasks: string[]; size_bytes: number | null; modified_at: number; error?: string };
export type EpisodeMeta = { index: number; length: number; duration_s: number; tasks: string[]; videos: Record<string, { chunk: number; file: number; from_s: number; to_s: number }>; collection: { operator: string | null; recorded_at: number | null; task_id: string | null; layout: Record<string, string> | null; target_slot: string | null; flagged: boolean; flags: Flag[]; fps: number | null } | null };
export type DatasetDetail = DatasetSummary & { recording: boolean; episodes: EpisodeMeta[]; features: Record<string, { dtype: string; shape: number[]; names: string[] | null }> };
export type EpisodeData = { timestamps: number[]; series: Record<string, Record<string, (number | null)[]>> };
export type EditJob = { running: boolean; repo_id: string | null; description: string; status: string; output: string[]; finished_at: number | null };
const query = (p: Record<string, string | number>) => new URLSearchParams(Object.entries(p).map(([k, v]) => [k, String(v)])).toString();
export const videoUrl = (repo: string, camera: string, chunk: number, file: number) => `/api/video?${query({ repo, camera, chunk, file })}`;
export const cameraStreamUrl = (name: string) => `/api/camera/${encodeURIComponent(name)}`;
export type BaseStatus = { connected: boolean; direction: string; lift_height_mm: number | null; error: string | null; port: string; motors: Record<string, { voltage: number; temperature: number; current_ma: number }> };
export const api = {
  baseStatus: () => request<BaseStatus>("/api/base/status"),
  baseConnect: (lift_height_mm: number | null) => request<BaseStatus & { session: string }>("/api/base/connect", "POST", { lift_height_mm }),
  baseCommand: (session: string, seq: number, direction: string) => request<BaseStatus>("/api/base/command", "POST", { session, seq, direction }),
  baseStop: () => request<BaseStatus>("/api/base/stop", "POST"),
  settings: () => request<Settings>("/api/settings"),
  saveSettings: (s: Settings) => request<Settings>("/api/settings", "PUT", s),
  savePlan: (items: PlanItem[]) => request("/api/collection/plan", "PUT", items),
  newDataset: (dataset: string, items: NewDatasetItem[]) => request<CollectionState>("/api/collection/new", "POST", { dataset, items }),
  saveUploadTargets: (hub_repo_id: string, rsync_dest: string) => request<CollectionState>("/api/collection/upload", "PUT", { hub_repo_id, rsync_dest }),
  collection: () => request<CollectionState>("/api/collection"),
  history: () => request<HistoryEntry[]>("/api/history"),
  start: (body: { operator: string; episode_time_s: number; task_mode: TaskMode; auto_start_s: number; auto_save_s: number }) => request("/api/recorder/start", "POST", body),
  control: (action: ControlAction, task_id?: string, on?: boolean) => request("/api/recorder/control", "POST", { action, task_id, on }),
  hostStart: (cameras: boolean) => request("/api/host/start", "POST", { cameras }),
  hostStop: () => request("/api/host/stop", "POST"),
  teleopStart: () => request("/api/teleop/start", "POST"),
  restoreStart: () => request("/api/restore/start", "POST"),
  calibrateStart: (target: "leader" | "follower") => request("/api/calibrate/start", "POST", { target }),
  procStop: (name: ProcName) => request(`/api/proc/${name}/stop`, "POST"),
  procInput: (name: ProcName, text: string) => request(`/api/proc/${name}/input`, "POST", { text }),
  jetsonSetup: (token: string) => request("/api/jetson/setup", "POST", { token }),
  uploadStart: (method: string, dry_run: boolean, operator: string) => request("/api/upload/start", "POST", { method, dry_run, operator }),
  datasets: () => request<{ datasets: DatasetSummary[]; recording: string | null }>("/api/datasets"),
  dataset: (repo: string) => request<DatasetDetail>(`/api/dataset?${query({ repo })}`),
  episode: (repo: string, ep: number) => request<EpisodeData>(`/api/episode?${query({ repo, ep })}`),
  deleteEpisodes: (repo: string, episodes: number[]) => request("/api/dataset/delete-episodes", "POST", { repo, episodes }),
  deleteDataset: (repo: string, confirm: string) => request("/api/dataset/delete", "POST", { repo, confirm }),
};
