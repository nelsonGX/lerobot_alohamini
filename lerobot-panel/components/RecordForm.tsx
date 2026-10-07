"use client";

import { useMemo, useState } from "react";
import { api, type DatasetSummary, type Settings } from "@/lib/api";
import { fmtDuration } from "@/lib/format";
import { useLocalStorage } from "@/lib/hooks";
import { Button, Card, ErrorBox, Field, inputClass, StatusIcon } from "./ui";

const NAME_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

export function RecordForm({
  settings,
  datasets,
  blocked,
  initialDataset,
  initialTask,
  onStarted,
}: {
  settings: Settings | null;
  datasets: DatasetSummary[];
  blocked: boolean;
  initialDataset?: string;
  initialTask?: string;
  onStarted: () => void;
}) {
  const [operator, setOperator] = useLocalStorage("panel.operator", "");
  const [dataset, setDataset] = useState(initialDataset ?? "");
  const [task, setTask] = useState(initialTask ?? "");
  const [numEpisodes, setNumEpisodes] = useState(10);
  const [episodeTime, setEpisodeTime] = useState(60);
  const [resetTime, setResetTime] = useState(10);
  const [fps, setFps] = useState(30);
  const [override, setOverride] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  // Apply shared defaults once settings arrive.
  const [defaultsApplied, setDefaultsApplied] = useState(false);
  if (settings && !defaultsApplied) {
    setDefaultsApplied(true);
    setNumEpisodes(settings.default_num_episodes);
    setEpisodeTime(settings.default_episode_time_s);
    setResetTime(settings.default_reset_time_s);
    setFps(settings.default_fps);
    if (!dataset) setDataset(`${settings.default_namespace}/`);
  }

  const existing = useMemo(() => datasets.find((d) => d.repo_id === dataset), [datasets, dataset]);

  // Appending to a dataset: reuse its task text when the field is empty.
  const [seenExisting, setSeenExisting] = useState<string | null>(null);
  if (existing && existing.repo_id !== seenExisting) {
    setSeenExisting(existing.repo_id);
    if (existing.tasks.length && !task) setTask(existing.tasks[existing.tasks.length - 1]);
  }

  const nameValid = NAME_RE.test(dataset);
  const effectiveFps = existing ? existing.fps : fps;
  const estimate = numEpisodes * (episodeTime + resetTime + 5);
  const canStart = nameValid && task.trim().length >= 3 && (!blocked || override) && !starting;

  const start = async () => {
    setError(null);
    setStarting(true);
    try {
      await api.start({
        operator,
        dataset,
        task: task.trim(),
        num_episodes: numEpisodes,
        episode_time_s: episodeTime,
        reset_time_s: resetTime,
        fps: effectiveFps,
      });
      onStarted();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  };

  return (
    <Card title="New recording session">
      <div className="grid gap-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Your name" hint="Shown to teammates and in the session history">
            <input className={inputClass} value={operator} onChange={(e) => setOperator(e.target.value)} placeholder="e.g. Alex" />
          </Field>
          <Field
            label="Dataset"
            hint={
              dataset && !nameValid ? (
                <span className="text-critical-ink">Use namespace/name — letters, digits, _ . - only</span>
              ) : (
                "Pick an existing one to add episodes, or type a new name"
              )
            }
          >
            <input
              className={`${inputClass} font-mono`}
              value={dataset}
              list="dataset-options"
              onChange={(e) => setDataset(e.target.value.trim())}
              placeholder="alohamini/pick_cube"
              spellCheck={false}
            />
            <datalist id="dataset-options">
              {datasets.map((d) => (
                <option key={d.repo_id} value={d.repo_id}>
                  {d.total_episodes} episodes
                </option>
              ))}
            </datalist>
          </Field>
        </div>

        {nameValid && (
          <div className="flex items-start gap-2 rounded-lg bg-surface-2 px-3 py-2 text-sm">
            <StatusIcon status="info" className="mt-0.5" />
            {existing ? (
              <span>
                <b>Adding to an existing dataset</b> — it has {existing.total_episodes} episodes (
                {fmtDuration(existing.duration_s)}) at {existing.fps} FPS. New episodes are appended.
              </span>
            ) : (
              <span>
                <b>A new dataset</b> will be created.
              </span>
            )}
          </div>
        )}

        <Field label="Task description" hint="One plain sentence describing what the robot should do. The policy is trained on this text, so keep it identical across sessions of the same task.">
          <textarea
            className={`${inputClass} min-h-20`}
            value={task}
            onChange={(e) => setTask(e.target.value)}
            placeholder="Pick up the red cube and place it in the bowl"
          />
        </Field>
        {existing && existing.tasks.length > 0 && !existing.tasks.includes(task.trim()) && task.trim() && (
          <div className="-mt-3 text-xs text-warn-ink">
            ⚠ Different from this dataset&apos;s existing task{existing.tasks.length > 1 ? "s" : ""}:{" "}
            {existing.tasks.map((t) => (
              <button key={t} className="mr-2 underline" onClick={() => setTask(t)}>
                “{t}”
              </button>
            ))}
          </div>
        )}

        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Field label="Episodes">
            <input type="number" min={1} className={`${inputClass} tabular`} value={numEpisodes} onChange={(e) => setNumEpisodes(Number(e.target.value))} />
          </Field>
          <Field label="Episode length" hint="seconds (max)">
            <input type="number" min={3} className={`${inputClass} tabular`} value={episodeTime} onChange={(e) => setEpisodeTime(Number(e.target.value))} />
          </Field>
          <Field label="Reset time" hint="seconds between episodes">
            <input type="number" min={0} className={`${inputClass} tabular`} value={resetTime} onChange={(e) => setResetTime(Number(e.target.value))} />
          </Field>
          <Field label="FPS" hint={existing ? "fixed by dataset" : undefined}>
            <input type="number" min={1} className={`${inputClass} tabular`} value={effectiveFps} disabled={!!existing} onChange={(e) => setFps(Number(e.target.value))} />
          </Field>
        </div>

        <p className="text-xs text-muted">
          Up to ~{fmtDuration(estimate)} for the whole session. You can end any episode early, so the episode length is just a
          maximum.
        </p>

        {error && <ErrorBox>{error}</ErrorBox>}

        <div className="flex flex-wrap items-center gap-4">
          <Button variant="danger" size="lg" onClick={start} disabled={!canStart} className="min-w-48">
            <span className="size-3 rounded-full bg-white" />
            {starting ? "Starting…" : "Start recording"}
          </Button>
          {blocked && (
            <label className="flex items-center gap-2 text-sm text-ink-2">
              <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} />
              Start anyway (pre-flight checks failing)
            </label>
          )}
        </div>
      </div>
    </Card>
  );
}
