"use client";

import { useEffect, useRef, useState } from "react";
import { api, type ProcName, type ProcSnapshot } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { Button, ErrorBox, inputClass } from "./ui";

/** Live state of a program the panel runs (host, teleop, calibration). */
export function useProc(name: ProcName) {
  const version = useRef(-1);
  const [lines, setLines] = useState<string[]>([]);
  const [fast, setFast] = useState(false);
  const poll = usePoll(
    async () => {
      const { proc } = await api.proc(name, version.current);
      if (proc) {
        version.current = proc.version;
        if (proc.lines) setLines(proc.lines);
      }
      setFast(proc?.state !== undefined && proc.state !== "exited");
      return proc;
    },
    fast ? 500 : 2500,
  );
  return { proc: poll.data, lines, error: poll.error, refresh: poll.refresh };
}

export function ProcConsole({ name, proc, lines, onChange }: {
  name: ProcName;
  proc: ProcSnapshot | null | undefined;
  lines: string[];
  onChange: () => void;
}) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pre = useRef<HTMLPreElement>(null);
  const stick = useRef(true);
  const running = proc?.state === "running";

  useEffect(() => {
    const el = pre.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const send = async (input: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.procInput(name, input);
      onChange();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!proc) return null;
  const prompt = running ? proc.prompt : null;

  return (
    <div className="grid gap-3">
      {prompt && (
        <div className="rounded-lg border-2 border-accent bg-accent/10 p-4">
          <div className="text-xs font-semibold uppercase tracking-wide text-accent">Waiting for you</div>
          {prompt.hint && <p className="mt-1 text-base font-medium">{prompt.hint}</p>}
          <p className="mt-1 font-mono text-xs text-ink-2">{prompt.text}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            {prompt.kind === "choice" ? (
              <>
                <Button variant="primary" disabled={busy} onClick={() => send("c\n")}>Recalibrate</Button>
                <Button disabled={busy} onClick={() => send("\n")}>Keep existing calibration</Button>
              </>
            ) : (
              <Button variant="primary" size="lg" disabled={busy} onClick={() => send("\n")}>
                {/stop/i.test(prompt.text) ? "Done moving joints" : "Continue"}
              </Button>
            )}
          </div>
        </div>
      )}
      {error && <ErrorBox>{error}</ErrorBox>}
      <pre
        ref={pre}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        }}
        className="max-h-80 min-h-24 overflow-auto rounded-lg bg-[#111418] p-3 font-mono text-[12px] leading-[1.45] text-[#d6dde6]"
      >
        {lines.length ? lines.join("\n") : "Starting…"}
      </pre>
      {running && (
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            send(text + "\n");
            setText("");
          }}
        >
          <input
            className={`${inputClass} font-mono`}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Type into the program (rarely needed)…"
            aria-label="Input"
          />
          <Button type="submit" disabled={busy}>{text ? "Send" : "Press Enter"}</Button>
        </form>
      )}
    </div>
  );
}

export function ProcStatus({ proc, runningLabel = "Running" }: { proc: ProcSnapshot | null | undefined; runningLabel?: string }) {
  if (!proc) return <span className="text-xs text-muted">Not started</span>;
  if (proc.state === "running")
    return (
      <span className="flex items-center gap-1.5 text-xs font-medium text-good-ink">
        <span className="pulse-dot size-2 rounded-full bg-good" /> {runningLabel}
      </span>
    );
  if (proc.state === "stopping") return <span className="text-xs font-medium text-warn-ink">Stopping…</span>;
  const ok = proc.exit_code === 0 || proc.stopped;
  return (
    <span className={`text-xs font-medium ${ok ? "text-muted" : "text-critical-ink"}`}>
      {proc.stopped ? "Stopped" : ok ? "Finished" : `Stopped with an error (code ${proc.exit_code})`}
    </span>
  );
}
