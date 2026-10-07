"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { EpisodePlayer } from "@/components/EpisodePlayer";
import { Button, Card, ErrorBox, IconButton, inputClass, Kbd, LinkButton, Modal, StatusIcon } from "@/components/ui";
import { api, type DatasetDetail, type JobState } from "@/lib/api";
import { cameraLabel, fmtAgo, fmtBytes, fmtDuration } from "@/lib/format";

export default function DatasetViewPage() {
  return (
    <Suspense fallback={<p className="text-sm text-muted">Loading…</p>}>
      <DatasetView />
    </Suspense>
  );
}

function DatasetView() {
  const params = useSearchParams();
  const router = useRouter();
  const repo = params.get("repo") ?? "";
  const epParam = params.get("ep");
  const [ds, setDs] = useState<DatasetDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmDrop, setConfirmDrop] = useState(false);
  const [job, setJob] = useState<JobState | null>(null);
  const [showJob, setShowJob] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const load = useCallback(() => setReloadKey((k) => k + 1), []);

  useEffect(() => {
    let cancelled = false;
    api.dataset(repo).then(
      (d) => {
        if (cancelled) return;
        setDs(d);
        setError(null);
      },
      (e) => !cancelled && setError(e instanceof Error ? e.message : String(e)),
    );
    return () => {
      cancelled = true;
    };
  }, [repo, reloadKey]);

  // Track a running edit job for this dataset; reload when it finishes.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    let cancelled = false;
    let wasRunning = false;
    const tick = async () => {
      try {
        const j = await api.job();
        if (cancelled) return;
        setJob(j);
        if (j.running && j.repo_id === repo) setShowJob(true);
        if (wasRunning && !j.running && j.repo_id === repo) {
          setSelected(new Set());
          load();
        }
        wasRunning = j.running && j.repo_id === repo;
      } catch {
        /* ignore */
      }
      if (!cancelled) timer = setTimeout(tick, wasRunning ? 1000 : 5000);
    };
    tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [repo, load]);

  const episodes = useMemo(() => ds?.episodes ?? [], [ds]);
  const current = episodes.find((e) => String(e.index) === epParam) ?? episodes[0];
  const busy = !!job?.running && job.repo_id === repo;

  // Keep the current episode visible in the sidebar when moving with [ ].
  const listRef = useRef<HTMLUListElement>(null);
  const currentIndex = current?.index;
  useEffect(() => {
    listRef.current?.querySelector(`[data-ep="${currentIndex}"]`)?.scrollIntoView({ block: "nearest" });
  }, [currentIndex]);

  const goTo = useCallback(
    (index: number) => router.replace(`/datasets/view?repo=${encodeURIComponent(repo)}&ep=${index}`, { scroll: false }),
    [router, repo],
  );

  // [ and ] move between episodes.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest("input:not([type=range]), textarea, select")) return;
      if (!current) return;
      if (e.key.toLowerCase() === "x" && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const idx = current.index;
        setSelected((s) => {
          const n = new Set(s);
          if (n.has(idx)) n.delete(idx);
          else n.add(idx);
          return n;
        });
        return;
      }
      if (e.key !== "[" && e.key !== "]") return;
      const i = episodes.indexOf(current) + (e.key === "]" ? 1 : -1);
      if (episodes[i]) goTo(episodes[i].index);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current, episodes, goTo]);

  if (!repo) return <ErrorBox>No dataset selected.</ErrorBox>;
  if (error && !ds)
    return (
      <div className="grid gap-4">
        <Link href="/datasets" className="text-sm text-muted hover:text-ink">← Datasets</Link>
        <ErrorBox>{error}</ErrorBox>
      </div>
    );
  if (!ds) return <p className="text-sm text-muted">Loading {repo}…</p>;

  const toggle = (i: number) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(i)) n.delete(i);
      else n.add(i);
      return n;
    });

  const avgLen = episodes.length ? ds.duration_s / episodes.length : 0;
  const trainCmd = `./train ${ds.repo_id} act`;

  return (
    <div className="grid gap-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <Link href="/datasets" className="text-sm text-muted hover:text-ink">← Datasets</Link>
          <h1 className="mt-1 truncate font-mono text-xl font-semibold">{ds.repo_id}</h1>
          <p className="text-sm text-ink-2">{ds.tasks.join(" · ") || "No task"}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <LinkButton
            variant="danger"
            disabled={ds.recording}
            href={`/?dataset=${encodeURIComponent(ds.repo_id)}&task=${encodeURIComponent(ds.tasks.at(-1) ?? "")}`}
          >
            <span className="size-2.5 rounded-full bg-white" /> Record more
          </LinkButton>
          <Button variant="ghost" className="text-critical-ink" onClick={() => setConfirmDrop(true)} disabled={ds.recording || busy}>
            Delete dataset…
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="Episodes" value={String(ds.total_episodes)} />
        <Stat label="Total time" value={fmtDuration(ds.duration_s)} />
        <Stat label="Avg episode" value={fmtDuration(avgLen)} />
        <Stat label="Frames" value={ds.total_frames.toLocaleString()} sub={`${ds.fps} fps`} />
        <Stat label="Cameras" value={String(ds.cameras.length)} sub={ds.cameras.map(cameraLabel).join(", ")} />
        <Stat label="On disk" value={fmtBytes(ds.size_bytes)} sub={`updated ${fmtAgo(ds.modified_at)}`} />
      </div>

      {ds.recording && (
        <div className="flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-sm">
          <span className="pulse-dot size-2.5 rounded-full bg-critical" />
          This dataset is being recorded right now. New episodes appear here once the session finishes.
        </div>
      )}
      {showJob && job && job.repo_id === repo && (
        <div className="rounded-lg border border-line bg-surface px-3 py-2 text-sm">
          <div className="flex items-center gap-2">
            <StatusIcon status={job.running ? "info" : job.status === "done" ? "ok" : "fail"} />
            <b>{job.description}</b>
            <span className="text-ink-2">{job.running ? "— working, this can take a minute…" : job.status === "done" ? "— done" : "— failed"}</span>
            {!job.running && (
              <Button size="sm" variant="ghost" className="ml-auto" onClick={() => setShowJob(false)}>Dismiss</Button>
            )}
          </div>
          {job.status === "failed" && <pre className="mt-2 max-h-40 overflow-auto text-xs">{job.output.join("\n")}</pre>}
        </div>
      )}

      {episodes.length === 0 ? (
        <Card>
          <p className="text-sm text-muted">No episodes in this dataset yet.</p>
        </Card>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[260px_minmax(0,1fr)]">
          <aside className="flex max-h-[calc(100vh-120px)] flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-[var(--shadow)] lg:sticky lg:top-20">
            <div className="flex items-center justify-between border-b border-line px-3 py-2 text-xs text-muted">
              <span>{episodes.length} episodes</span>
              {selected.size > 0 ? (
                <span className="pop-in flex items-center gap-1">
                  <button className="rounded px-1.5 py-0.5 hover:bg-surface-2 hover:text-ink" onClick={() => setSelected(new Set())}>Clear</button>
                  <button
                    className="rounded bg-critical px-2 py-0.5 font-semibold text-white transition hover:brightness-110 disabled:opacity-40"
                    onClick={() => setConfirmDelete(true)}
                    disabled={busy || ds.recording}
                  >
                    Delete {selected.size}
                  </button>
                </span>
              ) : (
                <span>tick to select</span>
              )}
            </div>
            <ul ref={listRef} className="flex-1 overflow-y-auto py-1">
              {episodes.map((e) => {
                const on = current?.index === e.index;
                const short = e.duration_s < avgLen * 0.4;
                return (
                  <li
                    key={e.index}
                    data-ep={e.index}
                    className={`flex items-center gap-2 border-l-2 px-3 py-1.5 transition-colors ${on ? "border-accent bg-surface-2" : "border-transparent hover:bg-surface-2/60"} ${selected.has(e.index) ? "text-critical-ink" : ""}`}
                  >
                    <input type="checkbox" className="size-4" checked={selected.has(e.index)} onChange={() => toggle(e.index)} aria-label={`Select episode ${e.index}`} />
                    <button className="flex min-w-0 flex-1 items-baseline gap-2 py-0.5 text-left" onClick={() => goTo(e.index)} aria-current={on || undefined}>
                      <span className={`tabular w-10 text-sm ${on ? "font-semibold" : ""}`}>#{e.index}</span>
                      <span className="tabular text-xs text-ink-2">{e.duration_s.toFixed(1)}s</span>
                      {short && <span className="text-[10px] text-warn-ink" title="Much shorter than average — worth checking">short</span>}
                      {ds.tasks.length > 1 && <span className="truncate text-xs text-muted">{e.tasks[0]}</span>}
                    </button>
                  </li>
                );
              })}
            </ul>
          </aside>

          <div className="min-w-0">
            {current && (
              <>
                <div className="mb-3 flex flex-wrap items-baseline gap-x-3">
                  <h2 className="text-lg font-semibold">Episode {current.index}</h2>
                  <span className="text-sm text-ink-2">
                    {current.duration_s.toFixed(1)}s · {current.length} frames · {current.tasks.join(", ")}
                  </span>
                  <span className="ml-auto flex items-center gap-2">
                    <span className="flex items-center rounded-lg border border-line">
                      <Button size="sm" variant="ghost" className="rounded-r-none" title="Previous episode ( [ )" onClick={() => goTo(episodes[episodes.indexOf(current) - 1]?.index)} disabled={episodes.indexOf(current) === 0}>‹ Prev</Button>
                      <span className="tabular border-x border-line px-2 text-xs text-muted">{episodes.indexOf(current) + 1} / {episodes.length}</span>
                      <Button size="sm" variant="ghost" className="rounded-l-none" title="Next episode ( ] )" onClick={() => goTo(episodes[episodes.indexOf(current) + 1]?.index)} disabled={episodes.indexOf(current) === episodes.length - 1}>Next ›</Button>
                    </span>
                    <Button
                      size="sm"
                      variant={selected.has(current.index) ? "warn" : "secondary"}
                      onClick={() => toggle(current.index)}
                      aria-pressed={selected.has(current.index)}
                      title="Toggle with X"
                    >
                      {selected.has(current.index) ? "✓ Marked for deletion" : "Mark as bad"} <Kbd>X</Kbd>
                    </Button>
                  </span>
                </div>
                <EpisodePlayer repo={ds.repo_id} episode={current} fps={ds.fps} cameras={ds.cameras} />
              </>
            )}
          </div>
        </div>
      )}

      <Card title="Use this dataset">
        <div className="grid gap-2 text-sm">
          <div className="text-ink-2">Train a policy on the GPU server from the repo root:</div>
          <CopyLine text={trainCmd} />
          <div className="text-ink-2">Stored at:</div>
          <CopyLine text={ds.path} />
        </div>
      </Card>

      <DeleteEpisodesModal
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        repo={repo}
        episodes={[...selected].sort((a, b) => a - b)}
        onStarted={() => {
          setConfirmDelete(false);
          setShowJob(true);
          api.job().then(setJob);
        }}
      />
      <DeleteDatasetModal open={confirmDrop} onClose={() => setConfirmDrop(false)} repo={repo} onDeleted={() => router.push("/datasets")} />
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="min-w-0 rounded-2xl border border-line bg-surface shadow-[var(--shadow)] px-4 py-3">
      <div className="text-xs text-muted">{label}</div>
      <div className="tabular mt-0.5 text-lg font-semibold">{value}</div>
      {sub && <div className="truncate text-xs text-muted capitalize">{sub}</div>}
    </div>
  );
}

function CopyLine({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async (e: React.MouseEvent<HTMLButtonElement>) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API needs HTTPS/localhost; fall back to selecting the text.
      const code = e.currentTarget.parentElement?.querySelector("code");
      if (code) window.getSelection()?.selectAllChildren(code);
    }
  };
  return (
    <div className="flex items-center gap-2 rounded-lg bg-surface-2 py-1 pr-1 pl-3">
      <code className="min-w-0 flex-1 truncate font-mono text-xs" title={text}>{text}</code>
      <IconButton label={copied ? "Copied" : "Copy"} onClick={copy} className={copied ? "text-good-ink" : ""}>
        {copied ? (
          <svg viewBox="0 0 16 16" className="size-4" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden><path d="m3.5 8.5 3 3 6-7" strokeLinecap="round" strokeLinejoin="round" /></svg>
        ) : (
          <svg viewBox="0 0 16 16" className="size-4" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden><rect x="5.5" y="5.5" width="8" height="8" rx="1.5" /><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2" /></svg>
        )}
      </IconButton>
    </div>
  );
}

function DeleteEpisodesModal({ open, onClose, repo, episodes, onStarted }: {
  open: boolean;
  onClose: () => void;
  repo: string;
  episodes: number[];
  onStarted: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.deleteEpisodes(repo, episodes);
      onStarted();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open={open} onClose={onClose} title={`Delete ${episodes.length} episode${episodes.length === 1 ? "" : "s"}?`}>
      <div className="grid gap-3 text-sm text-ink-2">
        <p>
          Episodes <b className="text-ink">{episodes.map((e) => `#${e}`).join(", ")}</b> will be removed from{" "}
          <span className="font-mono">{repo}</span>. The remaining episodes are renumbered from 0.
        </p>
        <p>
          A backup of the current dataset is kept at <span className="font-mono">{repo}_old</span> (replacing any older
          backup).
        </p>
        {error && <ErrorBox>{error}</ErrorBox>}
        <div className="mt-2 flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="danger" onClick={submit} loading={busy} loadingText="Starting…">Delete episodes</Button>
        </div>
      </div>
    </Modal>
  );
}

function DeleteDatasetModal({ open, onClose, repo, onDeleted }: { open: boolean; onClose: () => void; repo: string; onDeleted: () => void }) {
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (typed !== repo) return;
    setBusy(true);
    try {
      await api.deleteDataset(repo, typed);
      onDeleted();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open={open} onClose={onClose} title="Delete the whole dataset?">
      <div className="grid gap-3 text-sm text-ink-2">
        <p>This permanently deletes every episode and video in <span className="font-mono text-ink">{repo}</span> from this machine. It cannot be undone.</p>
        <label className="grid gap-1">
          <span>Type the dataset name to confirm:</span>
          <input
            className={`${inputClass} font-mono ${typed === repo ? "border-critical" : ""}`}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            placeholder={repo}
            spellCheck={false}
            autoComplete="off"
          />
        </label>
        {error && <ErrorBox>{error}</ErrorBox>}
        <div className="mt-2 flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="danger" disabled={typed !== repo} loading={busy} loadingText="Deleting…" onClick={submit}>Delete forever</Button>
        </div>
      </div>
    </Modal>
  );
}
