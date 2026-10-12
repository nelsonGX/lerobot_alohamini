"use client";

import { useEffect, useState } from "react";
import { CameraPreview } from "@/components/CameraPreview";
import { HealthStrip } from "@/components/HealthStrip";
import { LayoutDiagram } from "@/components/LayoutDiagram";
import { PHASE_META } from "@/components/phase";
import { TaskProgress } from "@/components/TaskProgress";
import { HistoryCard } from "@/components/HistoryCard";
import { api, type ProcName, type RobotState } from "@/lib/api";
import { fmtDuration } from "@/lib/format";
import { usePoll } from "@/lib/hooks";
import { useTopic } from "@/lib/live";

const pad = (n: number) => String(n).padStart(2, "0");

/** Read-only view of what the panel is doing, for anyone to watch. No controls; it only subscribes to view topics. */
export default function WatchPage() {
  const recorder = useTopic("recorder");
  const collection = useTopic("collection").data;
  const robot = useTopic("robot").data;
  const host = useTopic("host").data;
  const history = usePoll(api.history, 10000, [recorder.data?.session?.phase]);
  const session = recorder.data?.session ?? null;
  const st = session?.state ?? null;
  const active = !!session?.active;
  const phase = session?.phase;
  const meta = phase ? PHASE_META[phase] : null;
  const config = collection?.config ?? null;
  const slots = config?.slots ?? Object.keys(st?.layout?.names ?? {});
  const remaining = st?.remaining_s ?? st?.episode_time_s ?? 0;

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(280px,34vw)]">
      <div className="grid min-w-0 content-start gap-6">
        <header className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-line pb-3">
          <span className="font-mono text-sm font-semibold tracking-tight">
            alohamini<span className="text-muted">/panel</span>
          </span>
          <span className="rounded-full border border-line px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider text-muted">Live view</span>
          <span className="ml-auto text-xs text-muted">
            {recorder.error ? <span className="text-critical-ink">{recorder.error}</span> : "Updates live"}
          </span>
        </header>

        <Activity robot={robot} recording={active ? PHASE_META[session!.phase].label : null} operator={session?.operator} />

        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <Tile label="Robot host" value={robot ? (robot.jetson.host_listening ? "Running" : "Stopped") : "…"} good={robot?.jetson.host_listening} />
          <Tile label="Jetson" value={robot ? (robot.jetson.reachable ? robot.jetson.ip : "Unreachable") : "…"} good={robot?.jetson.reachable} />
          <Tile
            label="Control link"
            value={host?.snapshot ? host.snapshot.client.state : "–"}
            good={host?.snapshot ? host.snapshot.client.state === "live" : undefined}
          />
          <Tile label="Host loop" value={host?.snapshot ? `${host.snapshot.loop_hz.toFixed(0)} Hz` : "–"} />
        </div>

        {!recorder.data ? (
          <p className="text-sm text-muted">Loading…</p>
        ) : !session || !meta ? (
          <Idle text="No recording session yet." />
        ) : (
          <section className="grid gap-5">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
              <span className="flex items-center gap-2 font-mono text-sm font-semibold uppercase tracking-wider">
                <span className={`size-2.5 rounded-full ${phase === "recording" ? "pulse-dot" : ""}`} style={{ background: meta.color }} aria-hidden />
                {active ? meta.label : phase === "done" ? "Session finished" : meta.label}
              </span>
              {active && st?.episode_number != null && <span className="tabular font-mono text-sm text-muted">EPISODE {st.episode_number}</span>}
              {st?.simulated && <span className="rounded bg-warn/20 px-1.5 py-0.5 text-[11px] font-semibold text-warn-ink">SIMULATION</span>}
              <span className="ml-auto text-sm text-ink-2">{active ? st?.message || meta.description : meta.description}</span>
            </div>

            {active && <HealthStrip health={st?.health ?? null} />}

            {active && st && phase === "recording" && (
              <div>
                <div className="tabular font-mono text-[clamp(4rem,12vw,9rem)] font-medium leading-[0.85] tracking-tighter">
                  {pad(remaining)}
                  <span className="ml-2 text-[0.22em] tracking-normal text-muted">sec</span>
                </div>
                <div className="mt-4 h-2 overflow-hidden rounded-[2px] bg-surface-2">
                  <div
                    className="h-full transition-[width] duration-500 ease-linear"
                    style={{ width: `${(1 - remaining / Math.max(st.episode_time_s, 1)) * 100}%`, background: "var(--critical)" }}
                  />
                </div>
              </div>
            )}

            {active && st && phase === "review" && st.review?.stats && (
              <p className="text-sm text-ink-2">
                Operator is reviewing the last take ({fmtDuration(st.review.stats.duration_s)}, {st.review.stats.frames} frames).
              </p>
            )}

            {active && st?.task_text && (phase === "ready" || phase === "recording" || phase === "review") && (
              <div className="grid gap-4 md:grid-cols-[1fr_minmax(0,18rem)]">
                <div>
                  <div className="eyebrow mb-2">Task</div>
                  <div className="font-mono text-base">{st.task_text}</div>
                </div>
                {st.layout && config && (
                  <div>
                    <div className="eyebrow mb-2">Layout</div>
                    <LayoutDiagram layout={st.layout} objects={config.objects} slots={slots} size="sm" />
                  </div>
                )}
              </div>
            )}

            {!active && session.phase === "done" && (
              <p className="text-sm text-ink-2">
                Saved {session.saved_count} episode{session.saved_count === 1 ? "" : "s"}, {session.discarded_count} discarded.
              </p>
            )}
            {!active && session.phase === "failed" && <p className="text-sm text-critical-ink">The recorder stopped with an error.</p>}

            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line pt-3 font-mono text-xs text-ink-2">
              {session.operator && <span>{session.operator}</span>}
              <span>{session.dataset}</span>
              <span>{st?.saved ?? session.saved_count} saved</span>
              <span>{st?.discarded ?? session.discarded_count} discarded</span>
            </div>
          </section>
        )}

        {collection && !collection.error && collection.config && (
          <section>
            <div className="eyebrow mb-2">
              Progress · {collection.total_episodes} episode{collection.total_episodes === 1 ? "" : "s"} in {collection.config.dataset}
            </div>
            <TaskProgress
              tasks={collection.config.tasks.map((t) => ({ ...t, count: collection.counts[t.id] ?? 0 }))}
              suggestedId={active ? null : collection.suggested_task_id}
              flagged={collection.flagged}
            />
          </section>
        )}

        <HistoryCard history={history.data} />
      </div>

      <div className="min-w-0 xl:sticky xl:top-[4.5rem] xl:self-start">
        <CameraPreview />
      </div>
      <Clock />
    </div>
  );
}

const PROC_ACTIVITY: Record<Exclude<ProcName, "host">, string> = {
  teleop: "Teleoperating",
  restore: "Restoring robot pose",
  calibrate: "Calibrating",
};

/** What the robot is doing right now: recording > teleop/calibration/restore > host idle > off. */
function Activity({ robot, recording, operator }: { robot: RobotState | null; recording: string | null; operator?: string }) {
  let label = "Connecting…";
  let detail = "";
  let color = "var(--muted)";
  let pulse = false;
  if (recording) {
    label = recording === "Recording" ? "Recording" : `Recording session · ${recording}`;
    detail = operator ? `Operator: ${operator}` : "";
    color = "var(--critical)";
    pulse = recording === "Recording";
  } else if (robot) {
    const running = (Object.keys(PROC_ACTIVITY) as (keyof typeof PROC_ACTIVITY)[]).find((n) => robot.procs[n]?.state === "running");
    if (running) {
      label = PROC_ACTIVITY[running];
      if (running === "calibrate" && robot.procs.calibrate_target) label = `Calibrating the ${robot.procs.calibrate_target} arms`;
      color = "var(--info)";
      pulse = true;
    } else if (robot.jetson.host_listening) {
      label = "Idle";
      detail = "Robot host is on, waiting for someone to start";
      color = "var(--good)";
    } else {
      label = "Robot is off";
      detail = "The robot host is not running";
    }
  }
  return (
    <div className="flex items-center gap-4 rounded-lg border border-line bg-surface px-5 py-5">
      <span className={`size-4 shrink-0 rounded-full ${pulse ? "pulse-dot" : ""}`} style={{ background: color }} aria-hidden />
      <div className="min-w-0">
        <div className="eyebrow">Right now</div>
        <div className="text-2xl font-semibold tracking-tight md:text-3xl">{label}</div>
        {detail && <div className="mt-0.5 text-sm text-ink-2">{detail}</div>}
      </div>
    </div>
  );
}

function Tile({ label, value, good }: { label: string; value: string; good?: boolean }) {
  return (
    <div className="rounded-lg border border-line px-3 py-2.5">
      <div className="eyebrow">{label}</div>
      <div className={`mt-1 flex items-center gap-2 font-mono text-sm ${good === false ? "text-warn-ink" : ""}`}>
        {good !== undefined && <span className={`size-2 rounded-full ${good ? "bg-good" : "border border-muted"}`} aria-hidden />}
        {value}
      </div>
    </div>
  );
}

/** Wall clock pinned to the bottom-left of the screen. */
function Clock() {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    const tick = () => setNow(new Date());
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  if (!now) return null;
  return (
    <div className="pointer-events-none fixed bottom-4 left-4 hidden sm:block z-10 rounded-lg border border-line bg-page/90 px-3 py-2 backdrop-blur">
      <div className="tabular font-mono text-xl font-medium leading-none">{now.toLocaleTimeString([], { hour12: false })}</div>
      <div className="eyebrow mt-1">{now.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}</div>
    </div>
  );
}

function Idle({ text }: { text: string }) {
  return (
    <div className="flex items-center gap-2 py-6 font-mono text-sm text-ink-2">
      <span className="size-2.5 rounded-full border border-muted" /> {text}
    </div>
  );
}
