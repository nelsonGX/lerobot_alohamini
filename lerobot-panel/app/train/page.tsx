"use client";

import Link from "next/link";
import { useCallback, useState, type ReactNode } from "react";
import { Button, Card, ErrorBox, Field, inputClass, Modal, NumberInput, Segmented, Select, StatusIcon, Toggle } from "@/components/ui";
import { CameraPreview } from "@/components/CameraPreview";
import { CheckpointPicker, useCheckpoints } from "@/components/CheckpointPicker";
import { ActiveJob, isActive, JobDetail, JobList, type Job, type Kind } from "@/components/GpuJobs";
import { HostMonitor } from "@/components/HostMonitor";
import { initialPrediction, isVla, PredictionControls, predictionPayload } from "@/components/PredictionControls";
import { api, request } from "@/lib/api";
import { useLocalStorage, usePoll } from "@/lib/hooks";

type Status = {
  hostname: string; repo: string; disk_free: number; gpu_error: string | null; jobs: Job[];
  gpus: { index: string; name: string; used_mb: number | null; total_mb: number | null; utilization: number | null; temperature: number | null }[];
};
const envTasks = { aloha: "AlohaInsertion-v0", pusht: "PushT-v0", libero: "libero_10", metaworld: "metaworld-push-v2" };
const policies = ["act", "diffusion", "smolvla", "pi0", "pi05"];
const baseModels: Record<string, string> = { smolvla: "lerobot/smolvla_base", pi0: "lerobot/pi0_base", pi05: "lerobot/pi05_base" };
const fetchConnection = () => request<{ configured: boolean; host: string; port: number }>("/api/gpu/connection");

const initialForm = {
  kind: "robot_eval" as Kind, name: "aloha-run",
  // Training
  dataset: "", root: "", policy: "smolvla", finetune: true, trainCheckpoint: "lerobot/smolvla_base",
  steps: 20000, batch: 8, saveFreq: 5000, workers: 4, trainPrediction: initialPrediction,
  // Evaluation
  checkpoint: "", env: "aloha" as keyof typeof envTasks, simTask: envTasks.aloha, episodes: 10, parallel: 8,
  robotTask: "", continuous: true, record: false, evalDataset: "", duration: 60, reset: 10, evalPrediction: initialPrediction,
};
type Form = typeof initialForm;

const operations: { kind: Kind; title: string; text: string }[] = [
  { kind: "train", title: "Train", text: "Learn a policy from a dataset" },
  { kind: "sim_eval", title: "Simulate", text: "Score a checkpoint in a simulator" },
  { kind: "robot_eval", title: "Live robot", text: "Run a checkpoint on AlohaMini" },
];

function guessPolicy(path: string) {
  const name = path.toLowerCase();
  return policies.slice().sort((a, b) => b.length - a.length).find(p => name.includes(p)) ?? "unknown";
}

export default function TrainPage() {
  const connection = usePoll(fetchConnection, 30000);
  const configured = !!connection.data?.configured;
  const status = usePoll(useCallback(async () => configured ? request<Status>("/api/gpu/status") : null, [configured]), 3000);
  const catalog = useCheckpoints(configured);
  const collection = usePoll(api.collection, 30000);
  const [stored, setStored] = useLocalStorage<Form>("panel.train.form", initialForm);
  const form = { ...initialForm, ...stored };
  const set = (patch: Partial<Form>) => setStored(prev => ({ ...initialForm, ...prev, ...patch }));
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const running = status.data?.jobs.find(isActive) ?? null;
  const runningId = running?.id ?? null;
  const active = usePoll(useCallback(async () => runningId ? request<Job>(`/api/gpu/jobs/${runningId}`) : null, [runningId]), 1500);
  const detail = usePoll(useCallback(async () => selected && selected !== runningId ? request<Job>(`/api/gpu/jobs/${selected}`) : null, [selected, runningId]), 3000);
  const activeJob = running && active.data?.id === running.id ? active.data : running;
  const selectedJob = selected === running?.id ? activeJob : detail.data?.id === selected ? detail.data : null;

  const { kind } = form;
  const live = kind === "robot_eval";
  const evalMeta = catalog.find(form.checkpoint);
  const trainMeta = catalog.find(form.trainCheckpoint);
  const evalPolicy = evalMeta?.policy ?? (form.checkpoint ? guessPolicy(form.checkpoint) : "unknown");
  const trainPolicy = form.finetune && form.trainCheckpoint ? trainMeta?.policy ?? guessPolicy(form.trainCheckpoint) : form.policy;

  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setError(null);
    try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const start = () => run(async () => {
    const prediction = kind === "train"
      ? predictionPayload(form.trainPrediction, kind, trainPolicy, form.finetune && !!form.trainCheckpoint)
      : predictionPayload(form.evalPrediction, kind, evalPolicy, false);
    await request<Job>("/api/gpu/jobs", "POST", {
      kind, name: form.name,
      dataset: kind === "train" ? form.dataset : live && form.record ? form.evalDataset : "",
      dataset_root: kind === "train" ? form.root : "",
      policy: form.policy,
      checkpoint: kind === "train" ? (form.finetune ? form.trainCheckpoint : "") : form.checkpoint,
      steps: form.steps, batch_size: kind === "sim_eval" ? form.parallel : form.batch, save_freq: form.saveFreq, num_workers: form.workers,
      env: form.env, task: live ? form.robotTask : form.simTask,
      episodes: form.episodes, episode_time_s: form.duration, reset_time_s: form.reset, robot_ready: ready,
      continuous: live && form.continuous, record: !live || form.record,
      ...prediction,
    });
    setReady(false); setSelected(null); await status.refresh();
  });
  const stop = (id: string) => run(async () => {
    await request(`/api/gpu/jobs/${id}/stop`, "POST"); await status.refresh(); await active.refresh();
  });

  const blocked = !configured ? "Connect a GPU server first."
    : status.error ? "The GPU server is not responding."
    : !status.data ? "Checking the GPU server…"
    : running ? "Another job is running. Stop it or wait for it to finish."
    : live && !ready ? "Confirm you are at the robot." : null;
  const collectionDataset = collection.data?.config?.dataset;
  const suggestEvalName = () => {
    const ns = (collectionDataset ?? "local/x").split("/")[0];
    const stamp = new Date().toISOString().slice(0, 16).replace(/[-:]/g, "").replace("T", "_");
    set({ evalDataset: `${ns}/eval_${form.name}_${stamp}` });
  };

  return <div className="mx-auto grid max-w-7xl gap-5">
    <header className="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold">Train &amp; Eval</h1>
        <p className="mt-1 text-sm text-muted">Runs on your GPU server and keeps going if you close this page.</p>
      </div>
      {configured && <ServerBadge status={status.data} error={status.error} address={`${connection.data!.host}:${connection.data!.port}`} onEdit={() => setConnecting(true)} />}
    </header>

    {(error || connection.error) && <ErrorBox>{error || connection.error}</ErrorBox>}
    {connection.data && !configured && <Card title="Connect your GPU server"><ConnectForm initialHost="" initialPort={8766} onDone={async () => { await connection.refresh(); }} /></Card>}
    <Modal open={connecting} onClose={() => setConnecting(false)} title="GPU server connection">
      <ConnectForm initialHost={connection.data?.host ?? ""} initialPort={connection.data?.port ?? 8766}
        onDone={async () => { setConnecting(false); setSelected(null); await connection.refresh(); await status.refresh(); }} />
    </Modal>

    {activeJob && <ActiveJob job={activeJob} busy={busy} onStop={() => stop(activeJob.id)} />}

    <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(320px,400px)]">
      <form className="rounded-lg border border-line bg-surface" onSubmit={e => { e.preventDefault(); start(); }}>
        <div role="radiogroup" aria-label="What to run" className="grid grid-cols-3 gap-2 border-b border-line p-3">
          {operations.map(op => {
            const on = op.kind === kind;
            return <button key={op.kind} type="button" role="radio" aria-checked={on} onClick={() => { set({ kind: op.kind }); setReady(false); }}
              className={`rounded-lg border p-3 text-left transition ${on ? "border-accent bg-accent/10" : "border-line hover:border-ink-2/40 hover:bg-surface-2"}`}>
              <span className={`block text-sm font-semibold ${on ? "text-accent" : ""}`}>{op.title}</span>
              <span className="mt-0.5 hidden text-xs text-muted sm:block">{op.text}</span>
            </button>;
          })}
        </div>

        <div className="divide-y divide-line">
          {kind === "train" && <>
            <Section title="Dataset">
              <Field label="Dataset (namespace/name)" hint="Upload it with Record → Upload, or copy it to the GPU server. Local panel files are not transferred.">
                <div className="flex gap-2">
                  <input required className={`${inputClass} font-mono`} placeholder="namespace/dataset" value={form.dataset} onChange={e => set({ dataset: e.target.value })} />
                  {collectionDataset && form.dataset !== collectionDataset && <Button type="button" onClick={() => set({ dataset: collectionDataset })}>Use collection</Button>}
                </div>
              </Field>
            </Section>
            <Section title="Policy">
              <Segmented label="Starting point" value={form.finetune ? "finetune" : "scratch"} onChange={v => set({ finetune: v === "finetune" })}
                options={[{ value: "finetune", label: "Fine-tune a model" }, { value: "scratch", label: "From scratch" }]} />
              {form.finetune ? <>
                <div className="flex flex-wrap gap-1.5">{Object.values(baseModels).map(id => <button key={id} type="button" onClick={() => set({ trainCheckpoint: id })}
                  className={`rounded-md border px-2 py-0.5 font-mono text-xs ${form.trainCheckpoint === id ? "border-accent bg-accent/10 text-accent" : "border-line text-ink-2 hover:text-ink"}`}>{id}</button>)}</div>
                <CheckpointPicker catalog={catalog} value={form.trainCheckpoint} onChange={v => set({ trainCheckpoint: v })} connected={configured} />
                <p className="text-xs text-muted">The model supplies its own policy type ({trainPolicy}) and starts a new run.</p>
              </> : <Field label="Policy type" hint={isVla(form.policy) ? "VLA policies train best from pretrained weights; consider fine-tuning instead." : undefined}>
                <Select value={form.policy} onChange={e => set({ policy: e.target.value })}>{policies.map(p => <option key={p}>{p}</option>)}</Select>
              </Field>}
            </Section>
            <Section title="Training">
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Training steps"><NumberInput value={form.steps} min={1} max={10000000} step={1000} presets={[5000, 20000, 50000, 100000]} onChange={v => set({ steps: v })} /></Field>
                <Field label="Batch size"><NumberInput value={form.batch} min={1} max={4096} presets={[4, 8, 16, 32, 64]} onChange={v => set({ batch: v })} /></Field>
              </div>
            </Section>
            <Section title="Action chunking">
              <PredictionControls kind="train" policy={trainPolicy} chunk={trainMeta?.chunk_size ?? null} defaultSteps={trainMeta?.n_action_steps ?? null}
                finetune={form.finetune && !!form.trainCheckpoint} value={form.trainPrediction} onChange={p => set({ trainPrediction: p })} />
            </Section>
            <Section title="Advanced" collapsible>
              <Field label="Dataset directory on GPU server" optional><input className={`${inputClass} font-mono`} value={form.root} onChange={e => set({ root: e.target.value })} placeholder="/data/namespace/dataset" /></Field>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Save a checkpoint every"><NumberInput value={form.saveFreq} min={1} max={10000000} step={1000} suffix="steps" onChange={v => set({ saveFreq: v })} /></Field>
                <Field label="Data loader workers"><NumberInput value={form.workers} min={0} max={64} onChange={v => set({ workers: v })} /></Field>
              </div>
            </Section>
          </>}

          {kind !== "train" && <Section title="Model">
            <CheckpointPicker catalog={catalog} value={form.checkpoint} onChange={v => { set({ checkpoint: v, evalPrediction: { ...form.evalPrediction, steps: null } }); setReady(false); }} connected={configured} />
          </Section>}

          {kind === "sim_eval" && <Section title="Simulation">
            <p className="text-xs text-muted">The environment must match the policy&apos;s observation/action shapes. A policy trained on the real robot won&apos;t work in the Aloha benchmark.</p>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Environment"><Select value={form.env} onChange={e => { const env = e.target.value as Form["env"]; set({ env, simTask: envTasks[env] }); }}>{Object.keys(envTasks).map(v => <option key={v}>{v}</option>)}</Select></Field>
              <Field label="Task"><input required className={`${inputClass} font-mono`} value={form.simTask} onChange={e => set({ simTask: e.target.value })} /></Field>
              <Field label="Episodes"><NumberInput value={form.episodes} min={1} max={10000} presets={[10, 50, 100]} onChange={v => set({ episodes: v })} /></Field>
              <Field label="Parallel environments"><NumberInput value={form.parallel} min={1} max={4096} onChange={v => set({ parallel: v })} /></Field>
            </div>
          </Section>}

          {live && <>
            <Section title="Task">
              <Field label="Instruction" hint={isVla(evalPolicy) ? "Use the same wording as in the training data." : "Stored with the episodes; ACT/diffusion ignore it."}>
                <input required className={inputClass} value={form.robotTask} onChange={e => set({ robotTask: e.target.value })} placeholder="Pick up the red block and put it in the bowl" />
              </Field>
            </Section>
            <Section title="Run">
              <Segmented label="Run length" value={form.continuous ? "continuous" : "episodes"} onChange={v => set({ continuous: v === "continuous" })}
                options={[{ value: "continuous", label: "Until I stop it" }, { value: "episodes", label: "Fixed episodes" }]} />
              <Toggle checked={form.record} onChange={v => set({ record: v })} label="Record evaluation dataset"
                description={form.continuous ? "Saves an episode every N seconds; the robot holds still briefly while each one is written." : "Saves each episode on the GPU server."} />
              {form.record && <Field label="New dataset" hint="Must not exist yet on the GPU server.">
                <div className="flex gap-2">
                  <input required className={`${inputClass} font-mono`} value={form.evalDataset} onChange={e => set({ evalDataset: e.target.value })} placeholder="namespace/eval_run_001" />
                  <Button type="button" onClick={suggestEvalName}>New name</Button>
                </div>
              </Field>}
              {form.continuous
                ? form.record && <Field label="Episode length"><NumberInput value={form.duration} min={1} max={600} step={10} suffix="s" presets={[30, 60, 120, 300]} onChange={v => set({ duration: v })} /></Field>
                : <div className="grid gap-3 sm:grid-cols-3">
                  <Field label="Episodes"><NumberInput value={form.episodes} min={1} max={10000} onChange={v => set({ episodes: v })} /></Field>
                  <Field label="Episode length"><NumberInput value={form.duration} min={1} max={600} step={10} suffix="s" onChange={v => set({ duration: v })} /></Field>
                  <Field label="Reset time"><NumberInput value={form.reset} min={0} max={600} step={5} suffix="s" onChange={v => set({ reset: v })} /></Field>
                </div>}
            </Section>
          </>}

          {kind !== "train" && <Section title="Prediction frequency">
            <PredictionControls kind={kind} policy={evalPolicy} chunk={evalMeta?.chunk_size ?? null} defaultSteps={evalMeta?.n_action_steps ?? null}
              value={form.evalPrediction} onChange={p => set({ evalPrediction: p })} />
          </Section>}

          <Section title="Start" hideTitle>
            <Field label="Run name" hint="Letters, numbers, dots, dashes, underscores.">
              <input required pattern="[A-Za-z0-9][A-Za-z0-9_.\-]{0,59}" className={`${inputClass} font-mono`} value={form.name} onChange={e => set({ name: e.target.value })} />
            </Field>
            {live && <>
              <p className="text-sm text-ink-2">Inference runs on the GPU server and drives the Jetson set on the <Link className="underline" href="/robot">Robot page</Link>. Start its host with cameras first.</p>
              <label className="flex items-start gap-3 rounded-lg border-2 border-warn/60 bg-warn/10 p-3 text-sm">
                <input type="checkbox" className="mt-0.5 size-4" checked={ready} onChange={e => setReady(e.target.checked)} />
                <span>I am at the robot, the workspace is clear, and I can reach the physical stop. <b>The robot moves as soon as this starts{form.continuous ? " and keeps going until stopped" : ""}.</b></span>
              </label>
            </>}
            <div className="flex flex-wrap items-center justify-end gap-3">
              {blocked && <span className="flex items-center gap-1.5 text-sm text-muted"><StatusIcon status="idle" />{blocked}</span>}
              <Button type="submit" size="lg" variant={live ? "warn" : "primary"} loading={busy} disabled={!!blocked}>
                {kind === "train" ? "Start training" : kind === "sim_eval" ? "Start simulation" : form.continuous ? "Start robot" : "Start evaluation"}
              </Button>
            </div>
          </Section>
        </div>
      </form>

      <div className="grid gap-5 lg:sticky lg:top-4">
        {selected && <JobDetail job={selectedJob} error={selected === running?.id ? active.error : detail.error} onClose={() => setSelected(null)}
          onUseCheckpoint={path => { set({ checkpoint: path, kind: kind === "train" ? "robot_eval" : kind }); setReady(false); setSelected(null); }} />}
        <Card title="Runs"><JobList jobs={status.data?.jobs ?? []} selected={selected} onSelect={id => setSelected(id === selected ? null : id)} /></Card>
      </div>
    </div>

    {(live || running?.kind === "robot_eval") && <div className="grid gap-5 lg:grid-cols-2"><CameraPreview /><HostMonitor /></div>}
  </div>;
}

function Section({ title, children, collapsible, hideTitle }: { title: string; children: ReactNode; collapsible?: boolean; hideTitle?: boolean }) {
  if (collapsible) return <details className="group px-4 py-3">
    <summary className="eyebrow flex list-none items-center gap-1.5 py-1">
      <svg viewBox="0 0 16 16" className="size-3 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden><path d="m6 4 4 4-4 4" strokeLinecap="round" strokeLinejoin="round" /></svg>{title}
    </summary>
    <div className="mt-3 grid gap-4">{children}</div>
  </details>;
  return <section className="grid gap-4 px-4 py-4" aria-label={title}>
    {!hideTitle && <h2 className="eyebrow">{title}</h2>}
    {children}
  </section>;
}

function ServerBadge({ status, error, address, onEdit }: { status: Status | null; error: string | null; address: string; onEdit: () => void }) {
  return <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-line bg-surface py-1.5 pr-1.5 pl-3 text-sm">
    <span className="flex items-center gap-1.5"><StatusIcon status={error ? "fail" : status ? "ok" : "idle"} /><b>{status?.hostname ?? address}</b></span>
    {error ? <span className="text-critical-ink">Not responding</span> : status && <>
      {status.gpus.map(g => <span key={g.index} className="tabular font-mono text-xs text-ink-2" title={g.name}>
        GPU{status.gpus.length > 1 ? g.index : ""} {g.utilization ?? "?"}% · {g.used_mb != null && g.total_mb ? `${(g.used_mb / 1024).toFixed(1)}/${(g.total_mb / 1024).toFixed(0)} GB` : "? GB"} · {g.temperature ?? "?"}°C
      </span>)}
      {status.gpu_error && <span className="text-xs text-warn-ink">{status.gpu_error}</span>}
      <span className="tabular font-mono text-xs text-muted">{(status.disk_free / 1e9).toFixed(0)} GB free</span>
    </>}
    <Button size="sm" variant="ghost" onClick={onEdit}>Connection</Button>
  </div>;
}

function ConnectForm({ initialHost, initialPort, onDone }: { initialHost: string; initialPort: number; onDone: () => Promise<void> }) {
  const [host, setHost] = useState(initialHost);
  const [port, setPort] = useState(initialPort);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true); setError(null);
    try { await request("/api/gpu/connect", "POST", { host, port, token }); setToken(""); await onDone(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  return <form className="grid gap-3" onSubmit={e => { e.preventDefault(); submit(); }}>
    <p className="text-sm text-muted">On the GPU server run <code className="rounded bg-surface-2 px-1">./gpu_agent</code>, then enter its address and the token it prints.</p>
    <div className="grid gap-3 sm:grid-cols-[1fr_8rem]">
      <Field label="Server address"><input required className={`${inputClass} font-mono`} placeholder="192.168.1.20" value={host} onChange={e => setHost(e.target.value)} /></Field>
      <Field label="Port"><NumberInput value={port} min={1} max={65535} onChange={setPort} /></Field>
    </div>
    <Field label="Agent token"><input required type="password" autoComplete="off" className={`${inputClass} font-mono`} value={token} onChange={e => setToken(e.target.value)} /></Field>
    {error && <ErrorBox>{error}</ErrorBox>}
    <Button className="justify-self-end" type="submit" variant="primary" loading={busy}>Connect</Button>
  </form>;
}
