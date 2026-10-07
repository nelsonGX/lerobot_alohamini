"use client";

import { useMemo, useState } from "react";
import { api, type DatasetSummary, type Settings } from "@/lib/api";
import { fmtDuration } from "@/lib/format";
import { useLocalStorage } from "@/lib/hooks";
import { Button, Card, Combobox, ErrorBox, Field, inputClass, NumberInput, StatusIcon, Toggle } from "./ui";

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
  const nameTouched = dataset.includes("/") && !dataset.endsWith("/");
  const taskShort = task.trim().length > 0 && task.trim().length < 3;
  const effectiveFps = existing ? existing.fps : fps;
  const estimate = numEpisodes * (episodeTime + resetTime + 5);
  const canStart = nameValid && task.trim().length >= 3 && (!blocked || override) && !starting;
  // Tell the user what is still missing instead of just greying the button out.
  const missing = !nameValid
    ? "Enter a dataset name (namespace/name)"
    : task.trim().length < 3
      ? "Describe the task"
      : blocked && !override
        ? "Fix the pre-flight checks, or tick “Start anyway”"
        : null;
  const datasetOptions = useMemo(
    () =>
      datasets.map((d) => ({
        value: d.repo_id,
        meta: `${d.total_episodes} ep · ${fmtDuration(d.duration_s)}`,
        keywords: d.tasks.join(" "),
      })),
    [datasets],
  );
  const recentTasks = useMemo(() => {
    const seen = new Set<string>();
    for (const t of existing?.tasks ?? []) seen.add(t);
    for (const d of datasets) for (const t of d.tasks) if (seen.size < 6) seen.add(t);
    return [...seen].filter((t) => t !== task.trim()).slice(0, 5);
  }, [datasets, existing, task]);

  const start = async () => {
    if (!canStart) return;
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
      <form
        noValidate
        className="grid gap-5"
        onSubmit={(e) => {
          e.preventDefault();
          start();
        }}
        onKeyDown={(e) => {
          if (e.key !== "Enter") return;
          if (e.metaKey || e.ctrlKey) {
            e.preventDefault();
            start();
          } else if ((e.target as HTMLElement).tagName === "INPUT") {
            // Starting moves the robot: a stray Enter in a text field must not submit.
            e.preventDefault();
          }
        }}
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Your name" hint="Shown to teammates and in the session history">
            <input className={inputClass} value={operator} onChange={(e) => setOperator(e.target.value)} placeholder="e.g. Alex" autoComplete="name" />
          </Field>
          <Field
            label="Dataset"
            error={nameTouched && !nameValid ? "Use namespace/name — letters, digits, _ . - only" : undefined}
            hint="Pick an existing one to add episodes, or type a new name"
          >
            <Combobox
              className="font-mono"
              value={dataset}
              onChange={(v) => setDataset(v.trim())}
              options={datasetOptions}
              invalid={nameTouched && !nameValid}
              placeholder="alohamini/pick_cube"
              spellCheck={false}
              emptyText="No existing dataset matches — a new one will be created"
            />
          </Field>
        </div>

        {nameValid && (
          <div className="pop-in flex items-start gap-2 rounded-lg bg-surface-2 px-3 py-2 text-sm">
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

        <Field
          label="Task description"
          error={taskShort ? "A bit more detail, please" : undefined}
          hint="One plain sentence describing what the robot should do. The policy is trained on this text, so keep it identical across sessions of the same task."
        >
          <textarea
            className={`${inputClass} min-h-20 resize-y`}
            value={task}
            aria-invalid={taskShort || undefined}
            onChange={(e) => setTask(e.target.value)}
            placeholder="Pick up the red cube and place it in the bowl"
          />
        </Field>
        {existing && existing.tasks.length > 0 && !existing.tasks.includes(task.trim()) && task.trim() ? (
          <div className="-mt-3 flex flex-wrap items-center gap-1.5 rounded-lg border border-warn/50 bg-warn/10 px-3 py-2 text-xs text-warn-ink">
            <StatusIcon status="warn" className="size-3.5" />
            Different from this dataset&apos;s task{existing.tasks.length > 1 ? "s" : ""} — use:
            {existing.tasks.map((t) => (
              <TaskChip key={t} text={t} onClick={() => setTask(t)} />
            ))}
          </div>
        ) : (
          !task.trim() &&
          recentTasks.length > 0 && (
            <div className="-mt-3 flex flex-wrap items-center gap-1.5 text-xs text-muted">
              Recent:
              {recentTasks.map((t) => (
                <TaskChip key={t} text={t} onClick={() => setTask(t)} />
              ))}
            </div>
          )
        )}

        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Field label="Episodes">
            <NumberInput min={1} max={500} value={numEpisodes} onChange={setNumEpisodes} presets={[5, 10, 20, 50]} />
          </Field>
          <Field label="Episode length" hint="maximum">
            <NumberInput min={3} max={600} step={5} suffix="s" value={episodeTime} onChange={setEpisodeTime} />
          </Field>
          <Field label="Reset time" hint="between episodes">
            <NumberInput min={0} max={300} step={5} suffix="s" value={resetTime} onChange={setResetTime} />
          </Field>
          <Field label="FPS" hint={existing ? "fixed by dataset" : undefined}>
            <NumberInput min={1} max={120} step={5} value={effectiveFps} disabled={!!existing} onChange={setFps} />
          </Field>
        </div>

        <p className="text-xs text-muted">
          Up to ~{fmtDuration(estimate)} for the whole session. You can end any episode early, so the episode length is just a
          maximum.
        </p>

        {error && <ErrorBox>{error}</ErrorBox>}

        <div className="flex flex-wrap items-center gap-x-5 gap-y-3 border-t border-line pt-5">
          <Button
            type="submit"
            variant="danger"
            size="lg"
            disabled={!canStart}
            loading={starting}
            loadingText="Starting…"
            className="min-w-52"
            title={missing ?? "Ctrl+Enter"}
          >
            <span className="size-3 rounded-full bg-white" />
            Start recording
          </Button>
          <div className="grid gap-2">
            {missing ? (
              <span className="text-sm text-ink-2">{missing}</span>
            ) : (
              <span className="text-xs text-muted">
                Ready — or press <kbd className="font-mono">Ctrl+Enter</kbd>
              </span>
            )}
            {blocked && (
              <Toggle
                size="sm"
                checked={override}
                onChange={setOverride}
                label="Start anyway"
                description="Pre-flight checks are failing"
              />
            )}
          </div>
        </div>
      </form>
    </Card>
  );
}

function TaskChip({ text, onClick }: { text: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title="Use this task"
      className="max-w-full truncate rounded-full border border-line bg-surface px-2.5 py-0.5 text-xs text-ink-2 transition hover:border-accent hover:text-accent active:scale-95"
    >
      {text}
    </button>
  );
}
