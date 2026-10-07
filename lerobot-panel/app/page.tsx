"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Suspense, useCallback, useState } from "react";
import { CameraPreview } from "@/components/CameraPreview";
import { HostMonitor } from "@/components/HostMonitor";
import { HistoryCard } from "@/components/HistoryCard";
import { LiveSession } from "@/components/LiveSession";
import { Preflight } from "@/components/Preflight";
import { RecordForm } from "@/components/RecordForm";
import { UploadCard } from "@/components/UploadCard";
import { ErrorBox } from "@/components/ui";
import { api, type Session } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { useRecorderLog, useTopic } from "@/lib/live";

export default function RecordPage() {
  return (
    <Suspense>
      <Record />
    </Suspense>
  );
}

function Record() {
  const router = useRouter();
  const recorder = useTopic("recorder");
  const { data: state, error: backendError } = recorder;
  const log = useRecorderLog();
  const [dismissed, setDismissed] = useState<string | null>(null);

  const preflight = useTopic("preflight");
  const collection = useTopic("collection").data;
  const settings = usePoll(api.settings, 30000);
  const history = usePoll(api.history, 10000, [state?.session?.phase]);
  const [checking, setChecking] = useState(false);
  const recheck = useCallback(async () => {
    setChecking(true);
    await preflight.refresh();
    setTimeout(() => setChecking(false), 600);
  }, [preflight]);

  const session: Session | null = state?.session ?? null;
  // A finished session's result stays up until dismissed (or for 30 min, for whoever opens the panel next).
  const showSession =
    session && (session.active || (dismissed !== session.id && session.now - (session.ended_at ?? 0) < 1800));
  const blocked = !!preflight.data?.checks.some((c) => c.status === "fail");

  const newSession = () => {
    if (session) setDismissed(session.id);
    router.replace("/");
  };

  const live = !!showSession;

  const checks = (
    <div className="grid gap-x-8 md:grid-cols-2">
      <details className="border-b border-line text-sm">
        <summary className="py-2.5 font-medium">
          Pre-flight checks{" "}
          <span className={blocked ? "text-critical-ink" : "text-good-ink"}>{blocked ? "— problems" : "— all good"}</span>
        </summary>
        <div className="pb-3">
          <Preflight checks={preflight.data?.checks ?? null} onRefresh={recheck} loading={checking} />
        </div>
      </details>
      <details className="border-b border-line text-sm">
        <summary className="py-2.5 font-medium">Recent sessions</summary>
        <div className="pb-3">
          <HistoryCard history={history.data} />
        </div>
      </details>
    </div>
  );

  // Cameras are always the rightmost column, beside everything else.
  return (
    <div
      className={`grid gap-6 ${live ? "xl:grid-cols-[minmax(0,1fr)_minmax(260px,22vw)]" : "lg:grid-cols-[minmax(0,1fr)_320px] xl:grid-cols-[minmax(0,1fr)_320px_minmax(260px,22vw)]"}`}
    >
      <div className="grid min-w-0 content-start gap-6">
        <div>
          {backendError && (
            <div className="mb-4">
              <ErrorBox>Cannot reach the panel backend: {backendError}</ErrorBox>
            </div>
          )}
          {!state ? (
            <p className="text-sm text-muted">Loading…</p>
          ) : showSession ? (
            <LiveSession session={session} log={log} config={collection?.config ?? null} onNewSession={newSession} />
          ) : (
            <RecordForm settings={settings.data} collection={collection} blocked={blocked} onStarted={() => void recorder.refresh()} />
          )}
        </div>
        {live && session?.active && <HostMonitor />}
        {!session?.active && <UploadCard collection={collection} recording={false} />}
        {/* Mid-session the operator needs the controls, not setup: checks and history fold away. */}
        {live && checks}
      </div>
      {!live && (
        <aside className="grid content-start gap-4">
          <Preflight checks={preflight.data?.checks ?? null} onRefresh={recheck} loading={checking} />
          <HistoryCard history={history.data} />
          <HowTo />
        </aside>
      )}
      <div className="min-w-0 xl:sticky xl:top-14 xl:self-start">
        <CameraPreview />
      </div>
    </div>
  );
}

function HowTo() {
  return (
    <details className="rounded-lg border border-line px-4 py-3 text-sm">
      <summary className="cursor-pointer font-semibold">How recording works</summary>
      <ol className="mt-3 list-decimal space-y-1.5 pl-4 text-ink-2">
        <li>
          Make sure the robot host is running (<Link href="/robot" className="text-accent hover:underline">Robot</Link> page) and both
          leader arms are plugged in here.
        </li>
        <li>Enter your name and press <b>Start session</b>.</li>
        <li>
          For each episode the screen shows a <b>layout</b> and the <b>task</b> that is furthest behind. Place the objects like the picture,
          then press <b>Start recording</b>.
        </li>
        <li>Do the task with the leader arms. Press <b>Done</b> when finished (or wait for the timer).</li>
        <li>
          <b>Review</b>: nothing is saved yet. <b>Save</b> keeps it, <b>Re-record</b> throws it away and repeats the same layout,
          <b> Discard</b> throws it away and moves on. Episodes flagged as bad say why.
        </li>
        <li>When you are done, press <b>Finish session</b>, then <b>Upload dataset</b>.</li>
      </ol>
    </details>
  );
}
