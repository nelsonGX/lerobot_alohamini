"use client";

import { useEffect, useRef, useState } from "react";
import { fmtAgo, fmtDuration } from "@/lib/format";
import { Button, Card, ErrorBox, StatusIcon, type Status } from "./ui";

export type Kind = "train" | "sim_eval" | "robot_eval";
export type Job = {
  id: string; name: string; kind: Kind; state: string; started_at: number; ended_at?: number | null;
  exit_code: number | null; output_dir: string; lines?: string[]; checkpoints?: string[];
  dataset_path?: string; result?: unknown; request?: Record<string, unknown>;
};
export const kindLabels: Record<Kind, string> = { train: "Training", sim_eval: "Simulation", robot_eval: "Live robot" };
export const isActive = (j: Job | null | undefined) => !!j && ["running", "stopping"].includes(j.state);

export type EpisodeCommand = "success" | "fail" | "end" | "discard" | "start";
/** Progress the live evaluation prints as `[EVAL:event] {json}` (examples/alohamini/eval_control.py). */
type EvalEvent = {
  event: "episode_start" | "episode_end" | "discarded" | "waiting" | "outcome";
  at: number; episode: number; total: number | null; done: number; success: number; fail: number;
  cap?: number | null; until?: number | null; outcome?: "success" | "fail" | null; frames?: number; duration_s?: number; saved?: boolean;
};
const EVENT_PREFIX = "[EVAL:event] ";
const isEvent = (line: string) => line.startsWith(EVENT_PREFIX);

function evalEvents(lines: string[] = []) {
  const events: EvalEvent[] = [];
  for (const line of lines) {
    if (!isEvent(line)) continue;
    try { events.push(JSON.parse(line.slice(EVENT_PREFIX.length))); } catch { /* a line cut off by the log tail */ }
  }
  return events;
}

/** Current phase of a live evaluation and the last finished episode (with any later relabel). */
function evalState(job: Job) {
  const events = evalEvents(job.lines);
  const current = events.at(-1) ?? null;
  const last = events.reduce<EvalEvent | null>((prev, e) =>
    e.event === "episode_end" ? e : e.event === "outcome" && prev ? { ...prev, outcome: e.outcome } : prev, null);
  return { current, last };
}

const stateStatus = (state: string): Status =>
  state === "succeeded" ? "ok" : state === "failed" ? "fail" : state === "running" || state === "stopping" ? "info" : state === "interrupted" ? "warn" : "idle";

function bigNumber(text: string) {
  const m = /^([\d.]+)([KMBT]?)$/.exec(text);
  return m ? Number(m[1]) * 1000 ** " KMBT".indexOf(m[2] || " ") : null;
}

/** What the job's console says about how far along it is. */
export function jobProgress(job: Job) {
  const lines = job.lines ?? [];
  const req = job.request ?? {};
  if (job.kind === "train") {
    for (let i = lines.length - 1; i >= 0; i--) {
      const step = /\bstep:(\S+)/.exec(lines[i]);
      if (!step) continue;
      const done = bigNumber(step[1]);
      const loss = /\bloss:([\d.]+)/.exec(lines[i])?.[1];
      const total = Number(req.steps) || null;
      return { fraction: done != null && total ? Math.min(1, done / total) : null, text: `Step ${step[1]}${total ? ` / ${total.toLocaleString()}` : ""}${loss ? ` · loss ${loss}` : ""}` };
    }
    return { fraction: null, text: "Loading dataset and model…" };
  }
  if (job.kind === "sim_eval") {
    for (let i = lines.length - 1; i >= 0; i--) {
      const pct = [...lines[i].matchAll(/(\d+)%\|/g)].pop();
      if (pct) return { fraction: Number(pct[1]) / 100, text: `${pct[1]}% of ${req.episodes ?? "?"} episodes` };
    }
    return { fraction: null, text: "Starting simulation…" };
  }
  const saved = lines.filter(l => l.includes("[EVAL] Saved episode")).length;
  const rate = lines.find(l => l.startsWith("[EVAL] Policy predicts") || l.startsWith("[EVAL] RTC"))?.replace("[EVAL] ", "");
  const total = req.continuous ? null : Number(req.episodes) || null;
  const current = evalState(job).current;
  const done = current?.done ?? saved;
  const parts = [req.continuous ? `Running until stopped · ${done} done` : `${done} / ${total ?? "?"} episodes`,
    req.record === false ? "not recording" : `${saved} saved`, current && verdictText(current), rate];
  return { fraction: total ? done / total : null, text: parts.filter(Boolean).join(" · ") };
}

function Elapsed({ job }: { job: Job }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!isActive(job)) return;
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [job]);
  return <>{fmtDuration((isActive(job) ? now : job.ended_at ?? now) - job.started_at)}</>;
}

function ProgressBar({ fraction, active }: { fraction: number | null; active: boolean }) {
  return <div className="h-1.5 overflow-hidden rounded-full bg-surface-2" role="progressbar" aria-valuenow={fraction != null ? Math.round(fraction * 100) : undefined} aria-valuemin={0} aria-valuemax={100}>
    <div className={`h-full rounded-full bg-accent transition-[width] duration-500 ${fraction == null && active ? "pulse-dot w-full opacity-40" : ""}`} style={fraction != null ? { width: `${fraction * 100}%` } : undefined} />
  </div>;
}

export function LogView({ lines, className = "max-h-96 min-h-40" }: { lines?: string[]; className?: string }) {
  const ref = useRef<HTMLPreElement>(null);
  const stick = useRef(true);
  useEffect(() => {
    if (stick.current && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [lines]);
  const shown = lines?.filter(l => !isEvent(l));
  return <pre ref={ref} onScroll={e => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 30; }}
    className={`overflow-auto rounded-lg bg-[#111418] p-3 font-mono text-xs leading-relaxed text-[#d6dde6] ${className}`}>{shown?.length ? shown.join("\n") : "Waiting for output…"}</pre>;
}

function verdictText({ success, fail }: { success: number; fail: number }) {
  return success + fail ? `${success} success · ${fail} fail · ${Math.round(100 * success / (success + fail))}%` : null;
}

function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now() / 1000), 250);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

/** Live evaluation: what the robot is doing now, and buttons to end, judge, redo or start episodes. */
function EpisodeControls({ job, onCommand }: { job: Job; onCommand: (command: EpisodeCommand) => Promise<void> }) {
  const { current, last } = evalState(job);
  const running = job.state === "running";
  const now = useNow(running && !!current);
  const [sent, setSent] = useState<{ command: EpisodeCommand; after: EvalEvent | null } | null>(null);
  // A sent command is pending until the program reports a new event (or a few seconds pass).
  const pending = sent && sent.after?.at === current?.at && sent.after?.event === current?.event ? sent.command : null;
  useEffect(() => {
    if (!sent) return;
    const t = setTimeout(() => setSent(null), 5000);
    return () => clearTimeout(t);
  }, [sent]);
  const send = async (command: EpisodeCommand) => {
    setSent({ command, after: current });
    try { await onCommand(command); } catch { setSent(null); }
  };
  if (!current) return <p className="rounded-lg bg-surface-2 px-3 py-2 text-sm text-ink-2">Loading the policy and connecting to the robot…</p>;

  const label = `Episode ${current.episode}${current.total ? ` of ${current.total}` : ""}`;
  const inEpisode = current.event === "episode_start";
  const elapsed = Math.max(0, now - current.at);
  const off = !running || !!pending;
  const btn = (command: EpisodeCommand, text: string, variant: "good" | "danger" | "secondary" | "ghost" | "primary", hint: string) =>
    <Button key={command} variant={variant} title={hint} disabled={off} loading={pending === command} onClick={() => send(command)}>{text}</Button>;

  return <div className="grid gap-3 rounded-lg border border-line bg-surface-2/60 p-3">
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
      <div>
        <div className="eyebrow">{inEpisode ? "Robot running" : "Between episodes"}</div>
        <div className="text-base font-semibold">
          {inEpisode ? label : `Reset the scene for episode ${current.episode}${current.total ? ` of ${current.total}` : ""}`}
        </div>
      </div>
      <div className="tabular text-sm text-ink-2">
        {inEpisode
          ? <>{fmtDuration(elapsed)}{current.cap ? ` / ${fmtDuration(current.cap)}` : ""}</>
          : current.until ? `Starts in ${fmtDuration(current.until - now)}` : "Waiting for you"}
      </div>
    </div>
    {inEpisode && current.cap ? <ProgressBar fraction={Math.min(1, elapsed / current.cap)} active /> : null}

    {inEpisode ? <div className="flex flex-wrap gap-2">
      {btn("success", "Success", "good", "End this episode and mark it a success")}
      {btn("fail", "Fail", "danger", "End this episode and mark it a failure")}
      {btn("end", "End episode", "secondary", "End this episode without a verdict")}
      {btn("discard", "Discard & redo", "ghost", "Throw this episode away and run it again")}
    </div> : <div className="flex flex-wrap items-center gap-2">
      {btn("start", current.until ? "Start now" : "Start episode", "primary", "Start the next episode")}
      {last && <span className="ml-auto flex flex-wrap items-center gap-2 text-sm">
        <span className="text-ink-2">Episode {last.episode}: <b>{last.outcome ?? "no verdict"}</b>{last.frames ? ` · ${last.frames} frames` : ""}</span>
        {last.outcome !== "success" && btn("success", "Mark success", "ghost", "Change the last episode's verdict to success")}
        {last.outcome !== "fail" && btn("fail", "Mark fail", "ghost", "Change the last episode's verdict to failure")}
      </span>}
    </div>}

    <div className="tabular flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
      <span>{current.done} done</span>
      <span className="text-good-ink">{current.success} success</span>
      <span className="text-critical-ink">{current.fail} fail</span>
      {current.done - current.success - current.fail > 0 && <span>{current.done - current.success - current.fail} no verdict</span>}
      {current.success + current.fail > 0 && <b className="text-ink-2">{Math.round(100 * current.success / (current.success + current.fail))}% success</b>}
    </div>
  </div>;
}

/** The job currently using the GPU: progress, live log tail and a stop button. */
export function ActiveJob({ job, busy, onStop, onCommand }: { job: Job; busy: boolean; onStop: () => void; onCommand: (command: EpisodeCommand) => Promise<void> }) {
  const progress = jobProgress(job);
  const live = job.kind === "robot_eval";
  return <section className={`rounded-lg border-2 bg-surface ${live ? "border-warn" : "border-accent"}`}>
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 p-4">
      <span className={`size-2.5 shrink-0 rounded-full ${live ? "bg-warn" : "bg-accent"} pulse-dot`} aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="eyebrow">{kindLabels[job.kind]} · {job.state === "stopping" ? "stopping" : "running"} · <Elapsed job={job} /></div>
        <div className="mt-0.5 truncate text-lg font-semibold">{job.name}</div>
      </div>
      <Button variant="danger" size="lg" loading={job.state === "stopping"} loadingText="Stopping…" disabled={busy} onClick={onStop}>
        <svg viewBox="0 0 16 16" className="size-4" aria-hidden><rect x="3.5" y="3.5" width="9" height="9" rx="1.5" fill="currentColor" /></svg>
        {live ? "Stop robot" : "Stop job"}
      </Button>
    </div>
    <div className="grid gap-2 px-4 pb-4">
      {live && <EpisodeControls job={job} onCommand={onCommand} />}
      <ProgressBar fraction={progress.fraction} active />
      <p className="tabular text-sm text-ink-2">{progress.text}</p>
      <LogView lines={job.lines?.slice(-200)} className="max-h-40 min-h-24" />
    </div>
  </section>;
}

export function JobList({ jobs, selected, onSelect }: { jobs: Job[]; selected: string | null; onSelect: (id: string) => void }) {
  if (!jobs.length) return <p className="text-sm text-muted">Runs you start appear here, including their logs and checkpoints.</p>;
  return <ul className="-mx-4 -my-4 max-h-[28rem] divide-y divide-line overflow-auto">
    {jobs.map(j => <li key={j.id}>
      <button type="button" onClick={() => onSelect(j.id)} aria-current={selected === j.id || undefined}
        className={`flex w-full items-center gap-3 px-4 py-2.5 text-left ${selected === j.id ? "bg-surface-2" : "hover:bg-surface-2/60"}`}>
        <StatusIcon status={stateStatus(j.state)} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium">{j.name}</span>
          <span className="block text-xs text-muted">{kindLabels[j.kind]} · {fmtAgo(j.started_at)}</span>
        </span>
        <span className={`text-xs ${j.state === "failed" ? "text-critical-ink" : "text-muted"}`}>{j.state}</span>
      </button>
    </li>)}
  </ul>;
}

export function JobDetail({ job, error, onClose, onUseCheckpoint }: {
  job: Job | null; error: string | null; onClose: () => void; onUseCheckpoint: (path: string) => void;
}) {
  return <Card title={job ? job.name : "Loading…"} actions={<Button size="sm" variant="ghost" onClick={onClose}>Close</Button>}>
    {error && <ErrorBox>{error}</ErrorBox>}
    {job && <div className="grid gap-3">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted">Status</dt><dd className="flex items-center gap-1.5"><StatusIcon status={stateStatus(job.state)} />{job.state}{job.exit_code != null && ` (exit ${job.exit_code})`}</dd>
        <dt className="text-muted">Type</dt><dd>{kindLabels[job.kind]}</dd>
        <dt className="text-muted">Duration</dt><dd><Elapsed job={job} /></dd>
        {!isActive(job) && <><dt className="text-muted">Progress</dt><dd>{jobProgress(job).text}</dd></>}
        <dt className="text-muted">Saved at</dt><dd className="break-all font-mono text-xs leading-5">{job.dataset_path || job.output_dir}</dd>
      </dl>
      <LogView lines={job.lines} />
      {job.result != null && <details open className="rounded-lg border border-line p-3"><summary className="text-sm font-medium">Evaluation results</summary><pre className="mt-2 max-h-64 overflow-auto text-xs">{JSON.stringify(job.result, null, 2)}</pre></details>}
      {!!job.checkpoints?.length && <div className="grid gap-1.5">
        <div className="eyebrow">Checkpoints</div>
        {job.checkpoints.map(p => <div key={p} className="flex items-center justify-between gap-2 rounded-lg border border-line px-3 py-1.5 text-sm">
          <span className="font-mono">{p.split("/checkpoints/")[1]?.replace("/pretrained_model", "") ?? p}</span>
          <Button size="sm" onClick={() => onUseCheckpoint(p)}>Evaluate</Button>
        </div>)}
      </div>}
    </div>}
  </Card>;
}
