"use client";

import { useEffect, useRef, useState } from "react";
import { api, type Phase, type Session } from "@/lib/api";
import { fmtDuration, fmtTime } from "@/lib/format";
import { useLocalStorage } from "@/lib/hooks";
import { PHASE_META } from "./phase";
import { Button, Card, ErrorBox, Kbd, LinkButton, Modal, Spinner, StatusIcon, Toggle } from "./ui";

type Action = "next" | "rerecord" | "stop" | "discard_stop" | "abort";

function speak(text: string) {
  try {
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
  } catch {
    /* speech unavailable */
  }
}

export function LiveSession({
  session: s,
  log,
  onNewSession,
  onRecordMore,
}: {
  session: Session;
  log: { id: number; text: string }[];
  onNewSession: () => void;
  onRecordMore: () => void;
}) {
  const meta = PHASE_META[s.phase];
  const [voice, setVoice] = useLocalStorage("panel.voice", true);
  const [pending, setPending] = useState<Action | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmAbort, setConfirmAbort] = useState(false);
  const [showLog, setShowLog] = useState(false);
  const controllable = s.phase === "recording" || s.phase === "resetting";

  // Voice cues: the operator is looking at the robot, not the screen.
  const prev = useRef<{ phase: Phase; episode: number | null } | null>(null);
  useEffect(() => {
    const p = prev.current;
    prev.current = { phase: s.phase, episode: s.episode };
    if (!voice || !p || (p.phase === s.phase && p.episode === s.episode)) return;
    if (s.phase === "recording") speak(`Recording episode ${s.episode ?? ""}`);
    else if (s.phase === "resetting" && p.phase === "recording") speak("Reset the scene");
    else if (s.phase === "done") speak("Session complete");
    else if (s.phase === "failed") speak("Recording failed");
  }, [s.phase, s.episode, voice]);

  // Clear the "sent" state once the recorder reacts (or after a timeout).
  const stepKey = `${s.phase}:${s.episode}`;
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

  const send = async (action: Action) => {
    setError(null);
    setPending(action);
    try {
      await api.control(action);
    } catch (e) {
      setPending(null);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  // Keyboard shortcuts mirror the terminal ones: N next, R re-record, Q stop.
  const sendRef = useRef(send);
  useEffect(() => {
    sendRef.current = send;
  });
  useEffect(() => {
    if (!controllable) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (e.metaKey || e.ctrlKey || e.altKey || t.closest("input, textarea, select, [contenteditable]")) return;
      const k = e.key.toLowerCase();
      if (k === "n" || e.key === "ArrowRight") sendRef.current("next");
      else if (k === "r" || e.key === "ArrowLeft") sendRef.current("rerecord");
      else if (k === "q") sendRef.current("stop");
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [controllable]);

  const total = s.phase === "recording" ? s.episode_time_s : s.phase === "resetting" ? s.reset_time_s : null;
  const progress = total && s.remaining_s != null ? 1 - s.remaining_s / Math.max(total, 1) : null;
  const fpsLow = s.live_fps != null && s.live_fps < s.fps * 0.9;
  const discarded = s.episodes.filter((e) => e.status === "discarded").length;

  return (
    <div className="grid gap-4">
      {/* Phase banner */}
      <section className="overflow-hidden rounded-xl border border-line bg-surface">
        <div className="h-1.5" style={{ background: meta.color }} />
        <div className="grid gap-6 p-5 md:grid-cols-[1fr_auto]">
          <div className="min-w-0">
            <div className="flex items-center gap-3">
              <span
                className={`size-3.5 rounded-full ${s.phase === "recording" ? "pulse-dot" : ""}`}
                style={{ background: meta.color }}
                aria-hidden
              />
              <span className="text-2xl font-semibold tracking-tight">{meta.label}</span>
              {s.episode != null && s.active && <span className="tabular text-lg text-ink-2">Episode {s.episode}</span>}
            </div>
            <p className="mt-1 text-ink-2">{meta.description}</p>
            <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
              <dt className="text-muted">Dataset</dt>
              <dd className="truncate font-mono">{s.dataset}{s.resume && <span className="ml-2 font-sans text-xs text-muted">(appending)</span>}</dd>
              <dt className="text-muted">Task</dt>
              <dd className="truncate">{s.task}</dd>
              <dt className="text-muted">Operator</dt>
              <dd>{s.operator || "—"} · started {fmtTime(s.started_at)}</dd>
            </dl>
          </div>
          <div className="flex items-center gap-6 md:justify-end">
            {s.active && s.remaining_s != null && (
              <div className="text-right">
                <div className="tabular text-6xl font-semibold leading-none">{s.remaining_s}<span className="text-2xl text-muted">s</span></div>
                <div className="mt-1 text-xs text-muted">{s.phase === "recording" ? "until episode auto-ends" : "until next episode"}</div>
              </div>
            )}
          </div>
        </div>
        {progress != null && (
          <div className="mx-5 mb-5 h-2 overflow-hidden rounded-full bg-surface-2">
            <div className="h-full rounded-full transition-[width] duration-500 ease-linear" style={{ width: `${progress * 100}%`, background: meta.color }} />
          </div>
        )}
      </section>

      {/* Stats */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Saved this session" value={`${s.saved_count} / ${s.num_episodes}`} />
        <Stat label="Discarded" value={String(discarded)} />
        <Stat
          label="Capture rate"
          value={s.live_fps != null ? `${s.live_fps.toFixed(1)} fps` : "–"}
          sub={`target ${s.fps}`}
          status={s.live_fps == null ? undefined : fpsLow ? "warn" : "ok"}
        />
        <Stat label="Elapsed" value={fmtDuration((s.ended_at ?? s.now) - s.started_at)} />
      </div>

      {/* Controls */}
      {s.active && (
        <Card
          title="Controls"
          actions={
            <Toggle size="sm" checked={voice} onChange={setVoice} label="Voice cues" />
          }
        >
          {s.phase === "recording" ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <Button variant="good" size="lg" disabled={!!pending} loading={pending === "next"} onClick={() => send("next")}>
                Episode done — save it <Kbd>N</Kbd>
              </Button>
              <Button variant="warn" size="lg" disabled={!!pending} loading={pending === "rerecord"} onClick={() => send("rerecord")}>
                Mistake — discard &amp; redo <Kbd>R</Kbd>
              </Button>
            </div>
          ) : s.phase === "resetting" ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <Button variant="primary" size="lg" disabled={!!pending} loading={pending === "next"} onClick={() => send("next")}>
                Scene is reset — start next <Kbd>N</Kbd>
              </Button>
              <Button variant="warn" size="lg" disabled={!!pending} loading={pending === "rerecord"} onClick={() => send("rerecord")}>
                Discard episode {s.episode} &amp; redo <Kbd>R</Kbd>
              </Button>
            </div>
          ) : (
            <p className="text-sm text-muted">{meta.description} Controls unlock when recording or resetting.</p>
          )}
          <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-4">
            <Button size="sm" disabled={!controllable || !!pending} loading={pending === "stop"} onClick={() => send("stop")}>
              Save &amp; finish session <Kbd>Q</Kbd>
            </Button>
            <Button size="sm" disabled={!controllable || !!pending} loading={pending === "discard_stop"} onClick={() => send("discard_stop")}>
              Discard current &amp; finish
            </Button>
            <Button size="sm" variant="ghost" className="ml-auto text-critical-ink" onClick={() => setConfirmAbort(true)}>
              Force stop…
            </Button>
          </div>
          {pending && (
            <p className="pop-in mt-3 flex items-center gap-1.5 text-xs text-ink-2" role="status">
              <Spinner className="size-3.5" /> Sent — waiting for the recorder…
            </p>
          )}
          {s.stop_requested && <p className="mt-3 text-xs text-ink-2">Finishing the session after this step…</p>}
          {error && <div className="mt-3"><ErrorBox>{error}</ErrorBox></div>}
          <p className="mt-3 text-xs text-muted">
            “Save” keeps what was recorded so far. The current episode is only written after the reset period, so “discard” works until then.
          </p>
        </Card>
      )}

      {/* Result */}
      {!s.active && (
        <Card>
          {s.phase === "done" ? (
            <div className="flex flex-wrap items-center gap-4">
              <StatusIcon status="ok" className="size-6" />
              <div className="min-w-0 flex-1">
                <div className="font-semibold">Saved {s.saved_count} episode{s.saved_count === 1 ? "" : "s"} to {s.dataset}</div>
                <div className="truncate text-xs text-muted">{s.dataset_path}</div>
              </div>
              <LinkButton variant="primary" href={`/datasets/view?repo=${encodeURIComponent(s.dataset)}`}>
                Review episodes
              </LinkButton>
              <Button onClick={onRecordMore}>Record more</Button>
              <Button variant="ghost" onClick={onNewSession}>New session</Button>
            </div>
          ) : (
            <div className="grid gap-3">
              <ErrorBox>
                <div className="font-semibold">{s.error_hint ?? (s.phase === "aborted" ? "Recording was interrupted." : "The recorder stopped with an error.")}</div>
                {s.saved_count > 0 && <div className="mt-1">{s.saved_count} episode(s) were saved before it stopped.</div>}
              </ErrorBox>
              {s.error && <pre className="max-h-56 overflow-auto rounded-lg bg-surface-2 p-3 text-xs">{s.error}</pre>}
              <div className="flex gap-2">
                <Button variant="primary" onClick={onRecordMore}>Try again</Button>
                <Button variant="ghost" onClick={onNewSession}>Back to setup</Button>
              </div>
            </div>
          )}
        </Card>
      )}

      {/* Episode timeline */}
      {s.episodes.length > 0 && (
        <Card title="Episodes this session">
          <ul className="flex flex-wrap gap-2">
            {s.episodes.map((e, i) => (
              <li
                key={i}
                className="flex items-center gap-2 rounded-lg border border-line px-2.5 py-1.5 text-sm"
                title={e.frames != null ? `${e.frames} frames, ${e.duration_s?.toFixed(1)}s, ${e.fps?.toFixed(1)} fps` : undefined}
              >
                <StatusIcon
                  status={e.status === "saved" ? "ok" : e.status === "discarded" ? "fail" : e.status === "saving" ? "info" : "warn"}
                />
                <span className="tabular font-medium">#{e.number}</span>
                <span className="text-xs text-muted">
                  {e.status === "saved" ? "saved" : e.status === "discarded" ? "discarded" : e.status}
                  {e.duration_s != null && ` · ${e.duration_s.toFixed(0)}s`}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {/* Raw log */}
      <Card
        title="Recorder output"
        actions={
          <Button size="sm" variant="ghost" onClick={() => setShowLog((v) => !v)} aria-expanded={showLog}>
            {showLog ? "Hide" : "Show full log"}
          </Button>
        }
      >
        {showLog ? <LogView log={log} /> : <p className="text-xs text-muted">{log.at(-1)?.text ?? "No output yet"}</p>}
      </Card>

      <Modal open={confirmAbort} onClose={() => setConfirmAbort(false)} title="Force stop the recorder?">
        <p className="text-sm text-ink-2">
          This interrupts the recorder like Ctrl+C. The current episode is lost and the dataset may not be finalized
          properly. Prefer <b>Save &amp; finish session</b> unless the recorder is stuck.
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

function Stat({ label, value, sub, status }: { label: string; value: string; sub?: string; status?: "ok" | "warn" }) {
  return (
    <div className="rounded-xl border border-line bg-surface px-4 py-3">
      <div className="text-xs text-muted">{label}</div>
      <div className="mt-1 flex items-center gap-2">
        {status && <StatusIcon status={status} />}
        <span className="tabular text-xl font-semibold">{value}</span>
        {sub && <span className="text-xs text-muted">{sub}</span>}
      </div>
    </div>
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
