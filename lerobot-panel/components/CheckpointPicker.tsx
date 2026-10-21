"use client";

import { useCallback, useState } from "react";
import { request } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { Button, ErrorBox, Field, inputClass, Select } from "./ui";

type Checkpoint = { path: string; label: string; step: number | null; policy: string; latest: boolean };
type Catalog = { root: string; runs: { name: string; checkpoints: Checkpoint[] }[] };

export function CheckpointPicker({ value, onChange, connected, required }: {
  value: string;
  onChange: (value: string) => void;
  connected: boolean;
  required: boolean;
}) {
  const [view, setView] = useState("last");
  const [refreshing, setRefreshing] = useState(false);
  const catalog = usePoll(useCallback(async () => connected ? request<Catalog>("/api/gpu/checkpoints") : null, [connected]), 10000);
  const runs = catalog.data?.runs ?? [];
  const visible = runs.map(run => ({ ...run, checkpoints: run.checkpoints.filter(c => view === "all" || c.latest) }));
  const listed = visible.some(run => run.checkpoints.some(c => c.path === value));
  const changeView = (next: string) => {
    setView(next);
    if (next === "last") {
      const run = runs.find(run => run.checkpoints.some(c => c.path === value));
      const last = run?.checkpoints.find(c => c.latest);
      if (last) onChange(last.path);
    }
  };
  const refresh = async () => {
    setRefreshing(true);
    try { await catalog.refresh(); } finally { setRefreshing(false); }
  };

  return <div className="grid gap-3 rounded-lg border border-line p-3">
    <div className="flex items-center justify-between gap-2">
      <span className="text-sm font-medium">Models in GPU outputs</span>
      <Button size="sm" type="button" onClick={refresh} loading={refreshing} disabled={!connected}>Refresh</Button>
    </div>
    <Field label="Show checkpoints">
      <Select value={view} onChange={e => changeView(e.target.value)}>
        <option value="last">Last checkpoint per run</option>
        <option value="all">All checkpoints</option>
      </Select>
    </Field>
    <Field label="Saved model">
      <Select value={listed ? value : ""} disabled={!connected || !!catalog.error || !runs.length} onChange={e => onChange(e.target.value)}>
        <option value="" disabled>Choose a model and checkpoint…</option>
        {visible.map(run => <optgroup key={run.name} label={run.name}>
          {run.checkpoints.map(c => <option key={c.path} value={c.path}>
            {c.latest ? "Last · " : ""}{c.step != null ? `Step ${c.step.toLocaleString()}` : c.label} · {c.policy}
          </option>)}
        </optgroup>)}
      </Select>
    </Field>
    {catalog.error && <ErrorBox>{catalog.error}</ErrorBox>}
    <p className="text-xs text-muted">
      {!connected ? "Connect the GPU server to discover models." : !catalog.data ? "Scanning GPU outputs…" : !runs.length ? "No saved models found in GPU outputs yet." : "Includes runs started outside the panel. Updates every 10 seconds. Last uses the saved last checkpoint, or the highest step if no last link exists."}
    </p>
    <Field label={required ? "Selected checkpoint or model ID" : "Pretrained model (optional)"}>
      <input required={required} className={inputClass} value={value} onChange={e => onChange(e.target.value)} placeholder="Choose above, or enter a GPU path / namespace/model" />
    </Field>
    {value && <Button type="button" size="sm" variant="ghost" onClick={() => onChange("")}>Clear selection</Button>}
  </div>;
}
