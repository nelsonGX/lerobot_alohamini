"use client";

import { useState } from "react";
import { api, type CollectionState, type Settings, type TaskMode } from "@/lib/api";
import { useLocalStorage } from "@/lib/hooks";
import { TaskProgress } from "./TaskProgress";
import { Button, Card, ErrorBox, Field, inputClass, NumberInput } from "./ui";

export function RecordForm({
  settings,
  collection,
  blocked,
  onStarted,
}: {
  settings: Settings | null;
  collection: CollectionState | null;
  blocked: boolean;
  onStarted: () => void;
}) {
  const [operator, setOperator] = useLocalStorage("panel.operator", "");
  const [episodeTime, setEpisodeTime] = useState(60);
  const [taskMode, setTaskMode] = useLocalStorage<TaskMode>("panel.taskMode", "rotate");
  const [auto, setAuto] = useLocalStorage("panel.auto", true);
  const [autoStart, setAutoStart] = useLocalStorage("panel.autoStartS", 8);
  const [autoSave, setAutoSave] = useLocalStorage("panel.autoSaveS", 3);
  const [override, setOverride] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  // Apply the shared default once settings arrive.
  const [defaultsApplied, setDefaultsApplied] = useState(false);
  if (settings && !defaultsApplied) {
    setDefaultsApplied(true);
    setEpisodeTime(settings.default_episode_time_s);
  }

  const cfg = collection?.config;
  const name = operator.trim();
  const damaged = (collection?.problems.length ?? 0) > 0;
  const canStart = !!cfg && !!name && !damaged && (!blocked || override) && !starting;
  const missing = !cfg
    ? "Loading the data plan…"
    : !name
      ? "Enter your name first"
      : damaged
        ? "The shared dataset is damaged"
        : blocked && !override
          ? "Fix the pre-flight checks, or tick “Start anyway”"
          : null;

  const start = async () => {
    if (!canStart) return;
    setError(null);
    setStarting(true);
    try {
      await api.start({ operator: name, episode_time_s: episodeTime, task_mode: taskMode, auto_start_s: auto ? autoStart : 0, auto_save_s: auto ? autoSave : 0 });
      onStarted();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  };

  return (
    <Card title="New recording session">
      <form
        noValidate
        className="grid gap-5"
        onSubmit={(e) => {
          e.preventDefault();
          start();
        }}
        onKeyDown={(e) => {
          // Starting moves the robot: a stray Enter in a text field must not submit.
          if (e.key === "Enter" && (e.target as HTMLElement).tagName === "INPUT") e.preventDefault();
        }}
      >
        {collection?.error && <ErrorBox>{collection.error}</ErrorBox>}
        {damaged && (
          <ErrorBox>
            <div className="font-semibold">The shared dataset has a damaged file, so recording is blocked.</div>
            <div className="mt-1 text-xs">{collection!.problems.join("; ")}</div>
          </ErrorBox>
        )}

        <div className="grid gap-4 sm:grid-cols-[1fr_auto]">
          <Field label="Your name" hint="Saved with every episode you record, so it can be traced back to you">
            <input className={inputClass} value={operator} onChange={(e) => setOperator(e.target.value)} placeholder="e.g. Alex" autoComplete="name" maxLength={60} />
          </Field>
          <Field label="Longest episode" hint="You end it earlier with Done">
            <NumberInput min={3} max={600} step={5} suffix="s" value={episodeTime} onChange={setEpisodeTime} />
          </Field>
        </div>

        {cfg && collection && (
          <div className="grid gap-2">
            <div className="flex items-baseline justify-between gap-3">
              <span className="eyebrow">
                Episodes so far · <span className="font-mono normal-case">{cfg.dataset}</span>
              </span>
              <span className="text-xs text-muted">{collection.exists ? "all sessions are added to this dataset" : "it will be created on first save"}</span>
            </div>
            <TaskProgress tasks={cfg.tasks.map((t) => ({ ...t, count: collection.counts[t.id] ?? 0 }))} suggestedId={null} flagged={collection.flagged} />
          </div>
        )}

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Which object next" hint={taskMode === "rotate" ? "One after another: red → green → blue → red …" : "The same object until its target is reached, then the next one"}>
            <select className={inputClass} value={taskMode} onChange={(e) => setTaskMode(e.target.value as TaskMode)}>
              <option value="rotate">Rotate through the objects</option>
              <option value="single">One object until done</option>
            </select>
          </Field>
          <div className="grid content-start gap-2">
            <label className="flex items-center gap-2 text-sm font-medium">
              <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> Automatic: start and save by themselves
            </label>
            {auto ? (
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-ink-2">
                <span>Start after</span>
                <NumberInput min={1} max={120} step={1} suffix="s" value={autoStart} onChange={setAutoStart} />
                <span>Save after</span>
                <NumberInput min={1} max={60} step={1} suffix="s" value={autoSave} onChange={setAutoSave} />
              </div>
            ) : (
              <span className="text-xs text-muted">You press Start and Save for every episode.</span>
            )}
            <span className="text-xs text-muted">{auto ? "Discard or re-record any time during the countdown. Episodes flagged as bad are never saved automatically." : ""}</span>
          </div>
        </div>

        {error && <ErrorBox>{error}</ErrorBox>}

        <div className="flex flex-wrap items-center gap-x-5 gap-y-3 border-t border-line pt-5">
          <Button type="submit" variant="danger" size="lg" disabled={!canStart} loading={starting} loadingText="Starting…" className="min-w-52" title={missing ?? undefined}>
            <span className="size-3 rounded-full bg-white" />
            Start session
          </Button>
          <div className="grid gap-2">
            {missing ? <span className="text-sm text-ink-2">{missing}</span> : <span className="text-xs text-muted">Connects to the robot. Nothing is recorded until you press Start for an episode.</span>}
            {blocked && (
              <label className="flex items-center gap-2 text-xs">
                <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} /> Start anyway (pre-flight checks are failing)
              </label>
            )}
          </div>
        </div>
      </form>
    </Card>
  );
}
