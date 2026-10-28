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
  const parts = [req.continuous ? "Running until stopped" : `${saved} / ${total ?? "?"} episodes`, req.record === false ? "not recording" : req.continuous ? `${saved} saved` : null, rate];
  return { fraction: total ? saved / total : null, text: parts.filter(Boolean).join(" · ") };
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
  return <pre ref={ref} onScroll={e => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 30; }}
    className={`overflow-auto rounded-lg bg-[#111418] p-3 font-mono text-xs leading-relaxed text-[#d6dde6] ${className}`}>{lines?.length ? lines.join("\n") : "Waiting for output…"}</pre>;
}

/** The job currently using the GPU: progress, live log tail and a stop button. */
export function ActiveJob({ job, busy, onStop }: { job: Job; busy: boolean; onStop: () => void }) {
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
