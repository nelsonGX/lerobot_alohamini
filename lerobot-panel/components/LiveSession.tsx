"use client";

import { useEffect, useRef, useState } from "react";
import { api, type Phase, type Session } from "@/lib/api";
import { fmtDuration } from "@/lib/format";
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

  const kept = s.episodes.filter((e) => e.status !== "discarded");
  const slots = Math.max(s.num_episodes, kept.length);
  const lastLive = kept.at(-1) && ["recording", "resetting", "saving"].includes(kept.at(-1)!.status);
  const curIdx = lastLive ? kept.length - 1 : kept.length;
  const pad = (n: number) => String(n).padStart(2, "0");

  return (
    <div className="grid gap-6">
      {/* The stage: one screen with everything the operator needs, no boxes */}
      <section className="border-y border-line">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-line py-3">
          <span className="flex items-center gap-2 font-mono text-sm font-semibold uppercase tracking-wider">
            <span className={`size-2.5 rounded-full ${s.phase === "recording" ? "pulse-dot" : ""}`} style={{ background: meta.color }} aria-hidden />
            {meta.label}
          </span>
          {s.episode != null && s.active && (
            <span className="tabular font-mono text-sm text-muted">
              EP {pad(s.episode)}/{pad(s.num_episodes)}
            </span>
          )}
          <span className="ml-auto text-sm text-ink-2">{meta.description}</span>
        </div>

        <div className="grid gap-8 py-8 md:grid-cols-[1fr_20rem] md:gap-12 md:py-12">
          <div className="min-w-0">
            {s.active && s.remaining_s != null ? (
              <>
                <div className="tabular font-mono text-[clamp(5rem,16vw,11rem)] font-medium leading-[0.85] tracking-tighter">
                  {pad(s.remaining_s)}
                  <span className="ml-2 text-[0.22em] tracking-normal text-muted">sec</span>
                </div>
                <div className="eyebrow mt-4">{s.phase === "recording" ? "until episode auto-ends" : "until next episode"}</div>
              </>
            ) : (
              <div className="font-mono text-6xl font-medium tracking-tighter text-ink-2">{meta.label}</div>
            )}
          </div>

          <dl className="self-end font-mono text-sm">
            <Row k="Dataset" v={`${s.dataset}${s.resume ? " +" : ""}`} />
            <Row k="Task" v={s.task} />
            <Row k="Operator" v={s.operator || "—"} />
            <Row k="Capture" v={s.live_fps != null ? `${s.live_fps.toFixed(1)} / ${s.fps} fps` : "–"} bad={fpsLow} />
            <Row k="Elapsed" v={fmtDuration((s.ended_at ?? s.now) - s.started_at)} />
            <Row k="Discarded" v={String(discarded)} />
          </dl>
        </div>

        {/* Timeline: one segment per episode; the live one fills with the countdown */}
        <div className="pb-6">
          <ul className="flex gap-1.5" aria-label="Episode progress">
            {Array.from({ length: slots }, (_, i) => {
              const st = kept[i]?.status;
              const cur = s.active && i === curIdx;
              return (
                <li key={i} className="h-2 flex-1 overflow-hidden rounded-[2px] bg-surface-2" title={`Episode ${i + 1}`}>
                  <div
                    className="h-full transition-[width] duration-500 ease-linear"
                    style={{
                      width: st === "saved" ? "100%" : cur ? `${(progress ?? 0) * 100}%` : "0%",
                      background: cur ? meta.color : "var(--ink)",
                    }}
                  />
                </li>
              );
            })}
          </ul>
          <div className="eyebrow mt-2 flex justify-between">
            <span>{s.saved_count} of {s.num_episodes} saved</span>
            {s.stop_requested && <span>finishing after this step…</span>}
          </div>
        </div>

        {s.active && (
          <div className="flex flex-wrap items-stretch gap-3 border-t border-line py-5">
            {s.phase === "recording" || s.phase === "resetting" ? (
              <>
                <Button variant="primary" size="lg" className="h-16 min-w-72 flex-1 text-lg md:flex-none" disabled={!!pending} loading={pending === "next"} onClick={() => send("next")}>
                  {s.phase === "recording" ? "Done — save episode" : "Scene reset — start next"} <Kbd>N</Kbd>
                </Button>
                <Button size="lg" className="h-16" disabled={!!pending} loading={pending === "rerecord"} onClick={() => send("rerecord")}>
                  {s.phase === "recording" ? "Redo" : `Discard #${s.episode} & redo`} <Kbd>R</Kbd>
                </Button>
              </>
            ) : (
              <p className="self-center text-sm text-muted">Controls unlock when recording or resetting.</p>
            )}
            <div className="ml-auto flex flex-wrap items-center gap-2 self-center">
              <Toggle size="sm" checked={voice} onChange={setVoice} label="Voice" />
              <Button size="sm" variant="ghost" disabled={!controllable || !!pending} loading={pending === "stop"} onClick={() => send("stop")}>
                Save &amp; finish <Kbd>Q</Kbd>
              </Button>
              <Button size="sm" variant="ghost" disabled={!controllable || !!pending} loading={pending === "discard_stop"} onClick={() => send("discard_stop")}>
                Discard &amp; finish
              </Button>
              <Button size="sm" variant="ghost" className="text-critical-ink" onClick={() => setConfirmAbort(true)}>
                Force stop
              </Button>
            </div>
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
      </section>

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

      <details className="border-b border-line text-sm" open={showLog} onToggle={(e) => setShowLog(e.currentTarget.open)}>
        <summary className="flex items-center gap-3 pb-3">
          <span className="eyebrow">Recorder output</span>
          {!showLog && <span className="min-w-0 flex-1 truncate text-xs text-muted">{log.at(-1)?.text ?? "No output yet"}</span>}
        </summary>
        <div className="pb-3">
          <LogView log={log} />
        </div>
      </details>

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

function Row({ k, v, bad }: { k: string; v: string; bad?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-t border-line py-2 first:border-t-0">
      <dt className="eyebrow shrink-0">{k}</dt>
      <dd className={`tabular min-w-0 truncate ${bad ? "text-critical-ink" : ""}`} title={v}>{v}</dd>
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
