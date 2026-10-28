"use client";

import { useCallback, useState } from "react";
import { request } from "@/lib/api";
import { fmtAgo } from "@/lib/format";
import { usePoll } from "@/lib/hooks";
import { ErrorBox, IconButton, inputClass, Segmented, Spinner } from "./ui";

export type Checkpoint = {
  path: string; label: string; step: number | null; policy: string; latest: boolean; modified_at: number;
  chunk_size: number | null; n_action_steps: number | null;
};
type Catalog = { root: string; runs: { name: string; checkpoints: Checkpoint[] }[] };

/** Saved models in the GPU server's outputs/, refreshed every 10 s. */
export function useCheckpoints(connected: boolean) {
  const catalog = usePoll(useCallback(async () => connected ? request<Catalog>("/api/gpu/checkpoints") : null, [connected]), 10000);
  const find = (path: string) => catalog.data?.runs.flatMap(r => r.checkpoints).find(c => c.path === path) ?? null;
  return { ...catalog, find };
}

export function CheckpointPicker({ catalog, value, onChange, connected, optional }: {
  catalog: ReturnType<typeof useCheckpoints>;
  value: string;
  onChange: (value: string) => void;
  connected: boolean;
  optional?: boolean;
}) {
  const [view, setView] = useState<"last" | "all">("last");
  const [refreshing, setRefreshing] = useState(false);
  const runs = (catalog.data?.runs ?? []).map(run => ({ ...run, checkpoints: run.checkpoints.filter(c => view === "all" || c.latest) }));
  const selected = catalog.find(value);
  const refresh = async () => {
    setRefreshing(true);
    try { await catalog.refresh(); } finally { setRefreshing(false); }
  };

  return <div className="grid gap-2">
    <div className="flex items-center justify-between gap-2">
      <Segmented size="sm" label="Checkpoints to show" value={view} onChange={setView} options={[{ value: "last", label: "Latest per run" }, { value: "all", label: "All checkpoints" }]} />
      <IconButton label="Rescan GPU outputs" onClick={refresh} disabled={!connected || refreshing}>
        {refreshing ? <Spinner /> : <svg viewBox="0 0 16 16" className="size-4" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden><path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 2.5v3h-3" strokeLinecap="round" strokeLinejoin="round" /></svg>}
      </IconButton>
    </div>
    <div className="max-h-64 overflow-auto rounded-lg border border-line" role="listbox" aria-label="Saved models">
      {!connected ? <p className="p-3 text-sm text-muted">Connect the GPU server to list its models.</p>
        : !catalog.data ? <p className="flex items-center gap-2 p-3 text-sm text-muted"><Spinner /> Scanning GPU outputs…</p>
        : !runs.length ? <p className="p-3 text-sm text-muted">No saved models in <code>{catalog.data.root}</code> yet.</p>
        : runs.map(run => <div key={run.name}>
          <div className="eyebrow sticky top-0 border-b border-line bg-surface-2 px-3 py-1 normal-case tracking-normal">{run.name}</div>
          {run.checkpoints.map(c => {
            const on = c.path === value;
            return <button key={c.path} type="button" role="option" aria-selected={on} onClick={() => onChange(on && optional ? "" : c.path)}
              className={`flex w-full items-center gap-3 border-b border-line px-3 py-2 text-left text-sm last:border-b-0 ${on ? "bg-accent/10" : "hover:bg-surface-2"}`}>
              <span className={`size-3.5 shrink-0 rounded-full border-2 ${on ? "border-accent bg-accent" : "border-line"}`} aria-hidden />
              <span className="min-w-0 flex-1"><b className="font-medium">{c.step != null ? `Step ${c.step.toLocaleString()}` : c.label}</b>{c.latest && <span className="ml-2 rounded bg-surface-2 px-1.5 py-0.5 text-xs text-ink-2">latest</span>}</span>
              <span className="shrink-0 font-mono text-xs text-muted">{c.policy}{c.chunk_size ? ` · chunk ${c.chunk_size}` : ""}</span>
              <span className="hidden w-16 shrink-0 text-right text-xs text-muted sm:block">{fmtAgo(c.modified_at)}</span>
            </button>;
          })}
        </div>)}
    </div>
    {catalog.error && <ErrorBox>{catalog.error}</ErrorBox>}
    <input className={`${inputClass} font-mono`} value={value} onChange={e => onChange(e.target.value)} aria-label="Checkpoint path or model ID" required={!optional}
      placeholder={optional ? "Optional: GPU path or namespace/model to fine-tune" : "Or type a GPU path / namespace/model"} />
    {value && !selected && <p className="text-xs text-muted">Not in the list above: the GPU server will load it as given.</p>}
  </div>;
}
