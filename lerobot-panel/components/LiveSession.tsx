"use client";

import { useEffect, useRef, useState } from "react";
import { api, type CollectionConfig, type ControlAction, type EngineState, type Phase, type Session } from "@/lib/api";
import { fmtDuration } from "@/lib/format";
import { useLocalStorage } from "@/lib/hooks";
import { HealthStrip } from "./HealthStrip";
import { LayoutDiagram } from "./LayoutDiagram";
import { PHASE_META } from "./phase";
import { TaskProgress } from "./TaskProgress";
import { Button, Card, ErrorBox, Kbd, LinkButton, Modal, Spinner, StatusIcon, Toggle } from "./ui";

function speak(text: string) {
  try {
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
  } catch {
    /* speech unavailable */
  }
}

const pad = (n: number) => String(n).padStart(2, "0");

/** What to say when a new episode is set up: the operator is looking at the table, not the screen. */
function readyCue(st: EngineState): string {
  const slots = st.layout ? Object.entries(st.layout.names).map(([slot, name]) => `${slot}: ${name}`).join(", ") : "";
  return `Episode ${st.episode_number ?? ""}. ${st.task_text ?? ""}. Place the objects. ${slots}`;
}

export function LiveSession({
  session: s,
  log,
  config,
  onNewSession,
}: {
  session: Session;
  log: { id: number; text: string }[];
  config: CollectionConfig | null;
  onNewSession: () => void;
}) {
  const st = s.state;
  const phase: Phase = s.phase;
  const meta = PHASE_META[phase];
  const [voice, setVoice] = useLocalStorage("panel.voice", true);
  const [pending, setPending] = useState<ControlAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmAbort, setConfirmAbort] = useState(false);
  const [showLog, setShowLog] = useState(false);

  // Voice cues on phase changes and when a new episode is set up.
  const prev = useRef<{ phase: Phase; episode: number | null } | null>(null);
  useEffect(() => {
    const p = prev.current;
    prev.current = { phase, episode: st?.episode_number ?? null };
    if (!voice || !st || !p || (p.phase === phase && p.episode === st.episode_number)) return;
    if (phase === "ready") speak(readyCue(st));
    else if (phase === "recording") speak("Recording");
    else if (phase === "review") speak(st.review?.recommend === "discard" ? "Check this episode. It may be bad." : "Review. Save or discard.");
    else if (phase === "done") speak("Session complete");
    else if (phase === "failed") speak("Recording failed");
  }, [phase, st, voice]);

  // Clear the "sent" state once the recorder reacts (or after a timeout).
  const stepKey = `${phase}:${st?.episode_number}:${st?.attempt}`;
  const [lastStep, setLastStep] = useState(stepKey);
  if (lastStep !== stepKey) {
    setLastStep(stepKey);
    setPending(null);
  }
  useEffect(() => {
    if (!pending) return;
    const t = setTimeout(() => setPending(null), 4000);
    return () => clearTimeout(t);
  }, [pending]);

  const send = async (action: ControlAction, taskId?: string) => {
    setError(null);
    if (action !== "set_task") setPending(action);
    try {
      await api.control(action, taskId);
    } catch (e) {
      setPending(null);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  // Keyboard: Space starts/ends an episode; once it is in review, S saves, R re-records, D discards.
  // Throwing data away is never one key during recording.
  const sendRef = useRef(send);
  useEffect(() => {
    sendRef.current = send;
  });
  useEffect(() => {
    if (!s.active) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (e.metaKey || e.ctrlKey || e.altKey || e.repeat || t.closest("input, textarea, select, button, [contenteditable]")) return;
      const k = e.key.toLowerCase();
      if (k === " " && phase === "ready") sendRef.current("start");
      else if (k === " " && phase === "recording") sendRef.current("done");
      else if (phase === "review" && k === "s") sendRef.current("save");
      else if (phase === "review" && k === "r") sendRef.current("rerecord");
      else if (phase === "review" && k === "d") sendRef.current("discard");
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [s.active, phase]);

  const objects = config?.objects ?? [];
  const slots = config?.slots ?? Object.keys(st?.layout?.names ?? {});
  const busy = !!pending;

  return (
    <div className="grid gap-6">
      <section className="border-y border-line">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-line py-3">
          <span className="flex items-center gap-2 font-mono text-sm font-semibold uppercase tracking-wider">
            <span className={`size-2.5 rounded-full ${phase === "recording" ? "pulse-dot" : ""}`} style={{ background: meta.color }} aria-hidden />
            {meta.label}
          </span>
          {st?.episode_number != null && s.active && <span className="tabular font-mono text-sm text-muted">EPISODE {st.episode_number}</span>}
          {st?.simulated && <span className="rounded bg-warn/20 px-1.5 py-0.5 text-[11px] font-semibold text-warn-ink">SIMULATION</span>}
          <span className="ml-auto text-sm text-ink-2">{st?.message || meta.description}</span>
        </div>

        {s.active && (
          <div className="border-b border-line py-3">
            <HealthStrip health={st?.health ?? null} />
          </div>
        )}

        {s.active && st && (phase === "ready" || phase === "recording" || phase === "review") && (
          <div className="py-6 md:py-8">
            {phase === "ready" && <Ready st={st} objects={objects} slots={slots} onTask={(id) => send("set_task", id)} onStart={() => send("start")} busy={busy} />}
            {phase === "recording" && <Recording st={st} objects={objects} slots={slots} busy={busy} pending={pending} onSend={send} />}
            {phase === "review" && <Review st={st} busy={busy} pending={pending} onSend={send} />}
          </div>
        )}

        {s.active && (!st || !["ready", "recording", "review"].includes(phase)) && (
          <div className="flex items-center gap-3 py-12 text-ink-2">
            <Spinner className="size-5" />
            <span className="font-mono text-lg">{meta.description}</span>
          </div>
        )}

        {(pending || error) && (
          <div className="border-t border-line py-3">
            {pending && (
              <p className="pop-in flex items-center gap-1.5 text-xs text-ink-2" role="status">
                <Spinner className="size-3.5" /> Sent — waiting for the recorder…
              </p>
            )}
            {error && <ErrorBox>{error}</ErrorBox>}
          </div>
        )}

        {s.active && st && (
          <div className="flex flex-wrap items-center gap-3 border-t border-line py-3">
            <SessionChips st={st} />
            <div className="ml-auto flex flex-wrap items-center gap-2">
              <Toggle size="sm" checked={voice} onChange={setVoice} label="Voice" />
              <Button size="sm" variant="ghost" disabled={phase !== "ready" || busy} loading={pending === "finish"} onClick={() => send("finish")} title={phase === "ready" ? undefined : "Save or discard the current episode first"}>
                Finish session
              </Button>
              <Button size="sm" variant="ghost" className="text-critical-ink" onClick={() => setConfirmAbort(true)}>
                Force stop
              </Button>
            </div>
          </div>
        )}
      </section>

      {!s.active && <Result s={s} onNewSession={onNewSession} />}

      <details className="border-b border-line text-sm" open={showLog} onToggle={(e) => setShowLog(e.currentTarget.open)}>
        <summary className="flex items-center gap-3 pb-3">
          <span className="eyebrow">Recorder output</span>
          {!showLog && <span className="min-w-0 flex-1 truncate text-xs text-muted">{log.at(-1)?.text ?? "No output yet"}</span>}
        </summary>
        <div className="pb-3">
          <LogView log={log} />
        </div>
      </details>

      <Modal open={confirmAbort} onClose={() => setConfirmAbort(false)} title="Force stop the session?">
        <p className="text-sm text-ink-2">
          The episode in progress is <b>discarded</b> (it is never saved half-way), the dataset is closed properly and the session ends. Episodes you
          already saved are kept. Use <b>Finish session</b> instead unless the recorder is stuck.
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <Button onClick={() => setConfirmAbort(false)}>Cancel</Button>
          <Button
            variant="danger"
            onClick={() => {
              setConfirmAbort(false);
              send("abort");
            }}
          >
            Force stop
          </Button>
        </div>
      </Modal>
    </div>
  );
}

type Common = { st: EngineState; busy: boolean };
type Send = (a: ControlAction, taskId?: string) => void;

function Ready({ st, objects, slots, onTask, onStart, busy }: Common & { objects: CollectionConfig["objects"]; slots: string[]; onTask: (id: string) => void; onStart: () => void }) {
  return (
    <div className="grid gap-8 md:grid-cols-[1fr_minmax(0,22rem)]">
      <div>
        <div className="eyebrow mb-3">1 · Place the objects like this</div>
        {st.layout && <LayoutDiagram layout={st.layout} objects={objects} slots={slots} />}
        {st.attempt > 1 && <p className="mt-3 text-xs text-muted">Same layout as the episode you just redid (attempt {st.attempt}).</p>}
      </div>
      <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] content-start gap-4">
        <div>
          <div className="eyebrow mb-3">2 · Task for this episode</div>
          <TaskProgress tasks={st.tasks} suggestedId={st.suggested_task_id} selectedId={st.task_id} onSelect={onTask} />
        </div>
        <Button variant="danger" size="lg" className="h-16 text-lg" disabled={busy || !st.task_id} onClick={onStart}>
          <span className="size-3 rounded-full bg-white" /> Start recording <Kbd>Space</Kbd>
        </Button>
        <p className="text-xs text-muted">Press it once the objects are placed like the picture. The arms follow the leader arms now, but nothing is recorded until you start.</p>
      </div>
    </div>
  );
}

function Recording({ st, objects, slots, busy, pending, onSend }: Common & { objects: CollectionConfig["objects"]; slots: string[]; pending: ControlAction | null; onSend: Send }) {
  const remaining = st.remaining_s ?? st.episode_time_s;
  const progress = 1 - remaining / Math.max(st.episode_time_s, 1);
  return (
    <div className="grid gap-8 md:grid-cols-[1fr_20rem] md:gap-12">
      <div className="min-w-0">
        <div className="tabular font-mono text-[clamp(5rem,16vw,11rem)] font-medium leading-[0.85] tracking-tighter">
          {pad(remaining)}
          <span className="ml-2 text-[0.22em] tracking-normal text-muted">sec</span>
        </div>
        <div className="eyebrow mt-4">until the episode ends by itself</div>
        <div className="mt-4 h-2 overflow-hidden rounded-[2px] bg-surface-2">
          <div className="h-full transition-[width] duration-500 ease-linear" style={{ width: `${progress * 100}%`, background: "var(--critical)" }} />
        </div>
        <div className="mt-6 flex flex-wrap items-stretch gap-3">
          <Button variant="primary" size="lg" className="h-16 min-w-72 flex-1 text-lg md:flex-none" disabled={busy} loading={pending === "done"} onClick={() => onSend("done")}>
            Done — task finished <Kbd>Space</Kbd>
          </Button>
          <Button size="lg" className="h-16" disabled={busy} loading={pending === "rerecord"} onClick={() => onSend("rerecord")} title="Throw this recording away and redo the same task and layout">
            Re-record
          </Button>
          <Button size="lg" className="h-16" disabled={busy} loading={pending === "discard"} onClick={() => onSend("discard")} title="Throw this recording away and move on to a new layout">
            Discard
          </Button>
        </div>
      </div>
      <div className="grid content-start gap-4 self-end">
        <div className="font-mono text-sm">{st.task_text}</div>
        {st.layout && <LayoutDiagram layout={st.layout} objects={objects} slots={slots} size="sm" />}
      </div>
    </div>
  );
}

function Review({ st, busy, pending, onSend }: Common & { pending: ControlAction | null; onSend: Send }) {
  const r = st.review;
  const recommend = r?.recommend ?? "save";
  const stats = r?.stats;
  return (
    <div className="grid gap-5 md:max-w-2xl">
      <div>
        <div className="text-lg font-semibold">Keep this episode?</div>
        <div className="mt-1 text-sm text-ink-2">
          {st.task_text}
          {stats && (
            <span className="tabular font-mono text-xs text-muted">
              {" "}
              · {fmtDuration(stats.duration_s)} · {stats.frames} frames{stats.fps != null && ` · ${stats.fps.toFixed(1)} fps`}
            </span>
          )}
        </div>
      </div>
      <ul className="grid gap-2" aria-label="Automatic checks">
        {r && r.flags.length === 0 && (
          <li className="flex items-center gap-2 text-sm">
            <StatusIcon status="ok" /> No problems detected.
          </li>
        )}
        {r?.flags.map((f) => (
          <li key={f.code} className="flex items-start gap-2 text-sm">
            <StatusIcon status={f.severity === "bad" ? "fail" : "warn"} className="mt-0.5" />
            <span>{f.message}</span>
          </li>
        ))}
      </ul>
      {recommend === "discard" && <p className="text-sm font-medium text-critical-ink">Flagged as probably bad: discarding or re-recording is recommended.</p>}
      <div className="flex flex-wrap items-stretch gap-3">
        <Button variant={recommend === "save" ? "primary" : "secondary"} size="lg" className="h-16 min-w-52" disabled={busy} loading={pending === "save"} onClick={() => onSend("save")}>
          Save episode <Kbd>S</Kbd>
        </Button>
        <Button variant={recommend === "discard" ? "primary" : "secondary"} size="lg" className="h-16" disabled={busy} loading={pending === "rerecord"} onClick={() => onSend("rerecord")} title="Throw it away and redo the same task and layout">
          Re-record <Kbd>R</Kbd>
        </Button>
        <Button size="lg" className="h-16" disabled={busy} loading={pending === "discard"} onClick={() => onSend("discard")} title="Throw it away and move on to a new layout">
          Discard <Kbd>D</Kbd>
        </Button>
      </div>
      <p className="text-xs text-muted">The arms still follow the leader arms, so you can reset the scene while you decide. Nothing is written to the dataset until you press Save.</p>
    </div>
  );
}

function SessionChips({ st }: { st: EngineState }) {
  const flagged = st.episodes.filter((e) => e.status === "saved" && e.flags.length > 0).length;
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-xs text-ink-2">
      <span>{st.operator}</span>
      <span>{st.saved} saved</span>
      <span>{st.discarded} discarded</span>
      {flagged > 0 && <span className="text-warn-ink">{flagged} flagged</span>}
    </div>
  );
}

function Result({ s, onNewSession }: { s: Session; onNewSession: () => void }) {
  const flagged = s.state?.episodes.filter((e) => e.status === "saved" && e.flags.length > 0).length ?? 0;
  return (
    <Card>
      {s.phase === "done" ? (
        <div className="flex flex-wrap items-center gap-4">
          <StatusIcon status="ok" className="size-6" />
          <div className="min-w-0 flex-1">
            <div className="font-semibold">
              Saved {s.saved_count} episode{s.saved_count === 1 ? "" : "s"} to {s.dataset}
            </div>
            <div className="text-xs text-muted">
              {s.discarded_count} discarded{flagged > 0 && ` · ${flagged} saved with flags (review them in the dataset)`}
            </div>
          </div>
          <LinkButton variant="primary" href={`/datasets/view?repo=${encodeURIComponent(s.dataset)}`}>
            Review episodes
          </LinkButton>
          <Button onClick={onNewSession}>New session</Button>
        </div>
      ) : (
        <div className="grid gap-3">
          <ErrorBox>
            <div className="font-semibold">{s.error_hint ?? (s.phase === "aborted" ? "The session was stopped." : "The recorder stopped with an error.")}</div>
            {s.saved_count > 0 && <div className="mt-1">{s.saved_count} episode(s) were saved before it stopped.</div>}
          </ErrorBox>
          {s.error && <pre className="max-h-56 overflow-auto rounded-lg bg-surface-2 p-3 text-xs whitespace-pre-wrap">{s.error}</pre>}
          <div className="flex gap-2">
            <Button variant="primary" onClick={onNewSession}>
              Back to setup
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}

function LogView({ log }: { log: { id: number; text: string }[] }) {
  const ref = useRef<HTMLPreElement>(null);
  const stick = useRef(true);
  useEffect(() => {
    const el = ref.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [log]);
  return (
    <pre
      ref={ref}
      onScroll={(e) => {
        const el = e.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}
      className="max-h-80 overflow-auto rounded-lg bg-surface-2 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap"
    >
      {log.map((l) => l.text).join("\n")}
    </pre>
  );
}
