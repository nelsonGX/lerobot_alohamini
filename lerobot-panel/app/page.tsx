"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { HistoryCard } from "@/components/HistoryCard";
import { LiveSession } from "@/components/LiveSession";
import { Preflight } from "@/components/Preflight";
import { RecordForm } from "@/components/RecordForm";
import { ErrorBox } from "@/components/ui";
import { api, type RecorderState, type Session } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

export default function RecordPage() {
  return (
    <Suspense>
      <Record />
    </Suspense>
  );
}

function Record() {
  const params = useSearchParams();
  const router = useRouter();
  const [state, setState] = useState<RecorderState | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [log, setLog] = useState<{ id: number; text: string }[]>([]);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [prefill, setPrefill] = useState<{ dataset?: string; task?: string; key: number }>(() => ({
    dataset: params.get("dataset") ?? undefined,
    task: params.get("task") ?? undefined,
    key: 0,
  }));
  const lastLogId = useRef(0);
  const sessionId = useRef<string | null>(null);

  // Recorder state: fast while a session runs, slower when idle.
  const active = !!state?.session?.active;
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    let cancelled = false;
    const tick = async () => {
      try {
        const st = await api.recorder(lastLogId.current);
        if (st.session?.id !== sessionId.current) {
          sessionId.current = st.session?.id ?? null;
          lastLogId.current = 0;
          const full = await api.recorder(0);
          setLog(full.log);
          lastLogId.current = full.log.at(-1)?.id ?? 0;
          setState(full);
        } else {
          if (st.log.length) {
            setLog((prev) => [...prev, ...st.log].slice(-2000));
            lastLogId.current = st.log.at(-1)!.id;
          }
          setState(st);
        }
        setBackendError(null);
      } catch (e) {
        setBackendError(e instanceof Error ? e.message : String(e));
      }
      if (!cancelled) timer = setTimeout(tick, active ? 400 : 1500);
    };
    tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [active]);

  const preflight = usePoll(api.preflight, active ? 15000 : 5000);
  const settings = usePoll(api.settings, 30000);
  const datasets = usePoll(api.datasets, 10000);
  const history = usePoll(api.history, 10000, [state?.session?.phase]);
  const [checking, setChecking] = useState(false);
  const recheck = useCallback(async () => {
    setChecking(true);
    await preflight.refresh();
    setChecking(false);
  }, [preflight]);

  const session: Session | null = state?.session ?? null;
  // A finished session's result stays up until dismissed (or for 30 min, for whoever opens the panel next).
  const showSession =
    session && (session.active || (dismissed !== session.id && session.now - (session.ended_at ?? 0) < 1800));
  const blocked = !!preflight.data?.checks.some((c) => c.status === "fail");

  const newSession = (withPrefill: boolean) => {
    if (session) setDismissed(session.id);
    setPrefill({
      dataset: withPrefill ? session?.dataset : undefined,
      task: withPrefill ? session?.task : undefined,
      key: prefill.key + 1,
    });
    router.replace("/");
    datasets.refresh();
  };

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
      <div className="min-w-0">
        {backendError && (
          <div className="mb-4">
            <ErrorBox>Cannot reach the panel backend: {backendError}</ErrorBox>
          </div>
        )}
        {!state ? (
          <p className="text-sm text-muted">Loading…</p>
        ) : showSession ? (
          <LiveSession
            session={session}
            log={log}
            onNewSession={() => newSession(false)}
            onRecordMore={() => newSession(true)}
          />
        ) : (
          <RecordForm
            key={prefill.key}
            settings={settings.data}
            datasets={datasets.data?.datasets ?? []}
            blocked={blocked}
            initialDataset={prefill.dataset}
            initialTask={prefill.task}
            onStarted={() => {
              lastLogId.current = 0;
              sessionId.current = null;
            }}
          />
        )}
      </div>
      <aside className="grid content-start gap-4">
        <Preflight checks={preflight.data?.checks ?? null} onRefresh={recheck} loading={checking} />
        <HistoryCard history={history.data} />
        <HowTo />
      </aside>
    </div>
  );
}

function HowTo() {
  return (
    <details className="rounded-2xl border border-line bg-surface px-5 py-3.5 text-sm shadow-[var(--shadow)]">
      <summary className="cursor-pointer font-semibold">How recording works</summary>
      <ol className="mt-3 list-decimal space-y-1.5 pl-4 text-ink-2">
        <li>
          Make sure the robot host is running (<Link href="/robot" className="text-accent hover:underline">Robot</Link> page) and both
          leader arms are plugged in here.
        </li>
        <li>Fill in the dataset and task, then press <b>Start recording</b>.</li>
        <li>
          When it says <b>Recording</b>, do the task with the leader arms. Press <b>N</b> when done (or wait for the timer).
        </li>
        <li>During <b>Reset</b>, put objects back. Press <b>N</b> to start the next episode right away.</li>
        <li>Made a mistake? Press <b>R</b> any time before the next episode starts to throw that episode away.</li>
        <li>Afterwards, open the dataset to review each episode and delete bad ones.</li>
      </ol>
    </details>
  );
}
