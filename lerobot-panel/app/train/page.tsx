"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Card, ErrorBox, Field, inputClass, Select } from "@/components/ui";
import { CameraPreview } from "@/components/CameraPreview";
import { HostMonitor } from "@/components/HostMonitor";
import { api, request } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

type Kind = "train" | "sim_eval" | "robot_eval";
type Job = {
  id: string; name: string; kind: Kind; state: string; started_at: number;
  exit_code: number | null; output_dir: string; lines?: string[]; checkpoints?: string[];
  dataset_path?: string; result?: unknown;
};
type Status = {
  hostname: string; repo: string; disk_free: number; gpu_error: string | null; jobs: Job[];
  gpus: { index: string; name: string; used_mb: number | null; total_mb: number | null; utilization: number | null; temperature: number | null }[];
};
const labels: Record<Kind, string> = { train: "Train", sim_eval: "Simulation", robot_eval: "Live robot" };
const defaults = { aloha: "AlohaInsertion-v0", pusht: "PushT-v0", libero: "libero_10", metaworld: "metaworld-push-v2" };
const fetchConnection = () => request<{ configured: boolean; host: string; port: number }>("/api/gpu/connection");

export default function TrainPage() {
  const connection = usePoll(fetchConnection, 30000);
  const [host, setHost] = useState("");
  const [port, setPort] = useState(8766);
  const [token, setToken] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [kind, setKind] = useState<Kind>("train");
  const [name, setName] = useState("aloha-run");
  const [dataset, setDataset] = useState("");
  const [root, setRoot] = useState("");
  const [policy, setPolicy] = useState("act");
  const [checkpoint, setCheckpoint] = useState("");
  const [steps, setSteps] = useState(20000);
  const [batch, setBatch] = useState(8);
  const [saveFreq, setSaveFreq] = useState(5000);
  const [workers, setWorkers] = useState(4);
  const [env, setEnv] = useState<keyof typeof defaults>("aloha");
  const [simTask, setSimTask] = useState(defaults.aloha);
  const [robotTask, setRobotTask] = useState("");
  const [episodes, setEpisodes] = useState(10);
  const [duration, setDuration] = useState(60);
  const [reset, setReset] = useState(10);
  const [ready, setReady] = useState(false);
  const [evalDataset, setEvalDataset] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const status = usePoll(useCallback(async () => connection.data?.configured ? request<Status>("/api/gpu/status") : null, [connection.data?.configured]), 3000);
  const detail = usePoll(useCallback(async () => selected ? request<Job>(`/api/gpu/jobs/${selected}`) : null, [selected]), 1500);
  const collection = usePoll(api.collection, 30000);
  const running = status.data?.jobs.find(j => ["running", "stopping"].includes(j.state));
  const selectedJob = detail.data?.id === selected ? detail.data : null;
  const consoleRef = useRef<HTMLPreElement>(null);
  const stick = useRef(true);
  useEffect(() => {
    if (stick.current && consoleRef.current) consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
  }, [selectedJob?.lines]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setError(null);
    try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const connect = () => run(async () => {
    await request("/api/gpu/connect", "POST", { host: host || connection.data?.host, port, token });
    setToken(""); setEditing(false); setSelected(null);
    await connection.refresh(); await status.refresh();
  });
  const start = () => run(async () => {
    const job = await request<Job>("/api/gpu/jobs", "POST", {
      kind, name, dataset: kind === "robot_eval" ? evalDataset : dataset,
      dataset_root: root, policy, checkpoint, steps, batch_size: batch, save_freq: saveFreq,
      num_workers: workers, env, task: kind === "robot_eval" ? robotTask : simTask,
      episodes, episode_time_s: duration, reset_time_s: reset, robot_ready: ready,
    });
    setSelected(job.id); setReady(false); await status.refresh();
  });
  const stop = (id: string) => run(async () => {
    await request(`/api/gpu/jobs/${id}/stop`, "POST"); await status.refresh(); await detail.refresh();
  });
  const numeric = (label: string, value: number, set: (v: number) => void, min = 1, max = 10000000) => (
    <Field label={label}><input className={inputClass} type="number" required min={min} max={max} value={value} onChange={e => set(Number(e.target.value))} /></Field>
  );

  return <div className="mx-auto grid max-w-7xl gap-5">
    <div><h1 className="text-2xl font-semibold">Train &amp; Eval</h1><p className="mt-1 text-sm text-muted">Run policies on your GPU server. Jobs continue when you close this page.</p></div>
    {(error || connection.error) && <ErrorBox>{error || connection.error}</ErrorBox>}
    <Card title="GPU server" actions={<Button size="sm" onClick={() => { setHost(connection.data?.host ?? ""); setPort(connection.data?.port ?? 8766); setEditing(!editing); }}>Connection settings</Button>}>
      {!connection.data?.configured || editing ? <form className="grid gap-3 sm:grid-cols-[1fr_7rem_1fr_auto]" onSubmit={e => { e.preventDefault(); connect(); }}>
        <p className="text-sm text-muted sm:col-span-4">Run <code>./gpu_agent</code> on the GPU server, then enter its address and printed token here.</p>
        <Field label="Server address"><input required className={inputClass} placeholder="192.168.1.20" value={host} onChange={e => setHost(e.target.value)} /></Field>
        {numeric("Port", port, setPort, 1, 65535)}
        <Field label="Agent token"><input required type="password" autoComplete="off" className={inputClass} value={token} onChange={e => setToken(e.target.value)} /></Field>
        <Button className="self-end" type="submit" variant="primary" loading={busy}>Connect</Button>
      </form> : <div className="grid gap-3">
        {status.error && <ErrorBox>{status.error} Jobs may still be running on the server.</ErrorBox>}
        {status.data && <><p className="text-sm"><b>{status.data.hostname}</b> · {connection.data.host}:{connection.data.port} · {(status.data.disk_free / 1e9).toFixed(0)} GB disk free</p>
          <div className="grid gap-3 sm:grid-cols-2">{status.data.gpus.map(g => <div key={g.index} className="rounded border border-line p-3 text-sm"><b>{g.name}</b><p className="mt-1 font-mono text-xs text-muted">{g.utilization ?? "?"}% busy · {g.used_mb ?? "?"} / {g.total_mb ?? "?"} MB · {g.temperature ?? "?"}°C</p></div>)}</div>
          {status.data.gpu_error && <p className="text-sm text-warn-ink">{status.data.gpu_error}</p>}</>}
      </div>}
    </Card>
    {running && <div className="flex flex-wrap items-center gap-3 rounded-lg border border-accent p-3"><span className="flex-1 text-sm"><b>{running.name}</b> · {labels[running.kind]} · {running.state}</span><Button onClick={() => setSelected(running.id)}>View logs</Button><Button variant="danger" disabled={busy || running.state === "stopping"} onClick={() => stop(running.id)}>Stop job</Button></div>}
    <div className="grid items-start gap-5 lg:grid-cols-2">
      <Card title="New job">
        <form className="grid gap-4" onSubmit={e => { e.preventDefault(); start(); }}>
          <Field label="Operation"><Select value={kind} onChange={e => { setKind(e.target.value as Kind); setReady(false); }}>{Object.entries(labels).map(([v, label]) => <option key={v} value={v}>{label}</option>)}</Select></Field>
          <Field label="Run name"><input required pattern="[A-Za-z0-9][A-Za-z0-9_.-]{0,59}" className={inputClass} value={name} onChange={e => setName(e.target.value)} /></Field>
          {kind === "train" && <>
            <Field label="Training dataset"><input required className={inputClass} placeholder="namespace/dataset" value={dataset} onChange={e => setDataset(e.target.value)} /></Field>
            {collection.data?.config && <Button size="sm" type="button" onClick={() => setDataset(collection.data!.config.dataset)}>Use collection dataset</Button>}
            <p className="text-xs text-muted">Upload the dataset first using Record → Upload, or copy it to the GPU server. The panel’s local files are not automatically transferred.</p>
            <Field label="Policy"><Select value={policy} onChange={e => setPolicy(e.target.value)}>{["act", "diffusion", "smolvla", "pi0", "pi05"].map(p => <option key={p}>{p}</option>)}</Select></Field>
            <div className="grid grid-cols-2 gap-3">{numeric("Training steps", steps, setSteps)}{numeric("Batch size", batch, setBatch, 1, 4096)}</div>
          </>}
          <Field label={kind === "train" ? "Pretrained model (optional)" : "Checkpoint or model ID"}><input required={kind !== "train"} className={inputClass} value={checkpoint} onChange={e => setCheckpoint(e.target.value)} placeholder="GPU checkpoint path or namespace/model" /></Field>
          {kind === "train" && <p className="text-xs text-muted">A pretrained model supplies its own policy type and starts a new fine-tuning run. VLA policies need their pretrained weights.</p>}
          {kind === "sim_eval" && <>
            <Field label="Simulation environment"><Select value={env} onChange={e => { const v = e.target.value as keyof typeof defaults; setEnv(v); setSimTask(defaults[v]); }}>{Object.keys(defaults).map(v => <option key={v}>{v}</option>)}</Select></Field>
            <Field label="Simulation task"><input required className={inputClass} value={simTask} onChange={e => setSimTask(e.target.value)} /></Field>
            <p className="text-xs text-muted">Use an environment matching the policy’s training data and observation/action shapes. Install that environment’s dependencies on the GPU server.</p>
            <div className="grid grid-cols-2 gap-3">{numeric("Episodes", episodes, setEpisodes, 1, 10000)}{numeric("Parallel environments", batch, setBatch, 1, 4096)}</div>
          </>}
          {kind === "robot_eval" && <>
            <p className="text-sm text-ink-2">Inference runs on the GPU server and controls the Jetson configured on the <Link className="underline" href="/robot">Robot page</Link>. Start its host with cameras, then clear the workspace.</p>
            <Field label="Task instruction"><input required className={inputClass} value={robotTask} onChange={e => setRobotTask(e.target.value)} placeholder="Pick up the red block" /></Field>
            <Field label="New evaluation dataset"><input required className={inputClass} value={evalDataset} onChange={e => setEvalDataset(e.target.value)} placeholder="namespace/eval_run_001" /></Field>
            <p className="text-xs text-muted">Episodes are saved on the GPU server. Use a new dataset name for each run.</p>
            <div className="grid grid-cols-3 gap-3">{numeric("Episodes", episodes, setEpisodes, 1, 10000)}{numeric("Seconds / episode", duration, setDuration, 1, 600)}{numeric("Reset seconds", reset, setReset, 0, 600)}</div>
            <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" required checked={ready} onChange={e => setReady(e.target.checked)} />I am at the robot, the workspace is clear, and I am ready to stop it. The robot will move when evaluation starts.</label>
          </>}
          {kind === "train" && <details><summary className="cursor-pointer text-sm text-muted">Training options</summary><div className="mt-3 grid gap-3">
            <Field label="Dataset directory on GPU server (optional)"><input className={inputClass} value={root} onChange={e => setRoot(e.target.value)} placeholder="/data/namespace/dataset" /></Field>
            <div className="grid grid-cols-2 gap-3">{numeric("Save every N steps", saveFreq, setSaveFreq)}{numeric("Data workers", workers, setWorkers, 0, 64)}</div>
          </div></details>}
          <Button type="submit" variant={kind === "robot_eval" ? "warn" : "primary"} loading={busy} disabled={!status.data || !!status.error || !!running}>{kind === "train" ? "Start training" : kind === "sim_eval" ? "Start simulation evaluation" : "Start live evaluation"}</Button>
        </form>
      </Card>
      <div className="grid gap-5">
        <Card title="Jobs"><div className="grid gap-2">{!status.data?.jobs.length && <p className="text-sm text-muted">Your training and evaluation runs will appear here.</p>}{status.data?.jobs.map(j => <button key={j.id} className={`flex items-center justify-between gap-3 rounded border p-3 text-left text-sm ${selected === j.id ? "border-accent bg-surface-2" : "border-line"}`} onClick={() => setSelected(j.id)}><span><b>{j.name}</b><span className="mt-1 block text-xs text-muted">{labels[j.kind]} · {new Date(j.started_at * 1000).toLocaleString()}</span></span><span className={j.state === "failed" ? "text-critical-ink" : "text-muted"}>{j.state}</span></button>)}</div></Card>
        {selected && <Card title={selectedJob?.name ?? "Job details"}>
          {detail.error && <ErrorBox>{detail.error}</ErrorBox>}
          {selectedJob && <div className="grid gap-3">
            <p className="text-sm">{selectedJob.state}{selectedJob.exit_code != null && ` · exit ${selectedJob.exit_code}`}</p>
            {["running", "stopping"].includes(selectedJob.state) && <Button variant="danger" disabled={busy || selectedJob.state === "stopping"} onClick={() => stop(selectedJob.id)}>Stop job</Button>}
            <pre ref={consoleRef} onScroll={e => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 30; }} className="max-h-96 min-h-32 overflow-auto rounded bg-[#111418] p-3 font-mono text-xs text-[#d6dde6]">{selectedJob.lines?.join("\n") || "Waiting for output…"}</pre>
            <p className="break-all text-xs text-muted">Saved on GPU server: {selectedJob.dataset_path || selectedJob.output_dir}</p>
            {!!selectedJob.checkpoints?.length && <Field label="Use a saved checkpoint"><Select value="" onChange={e => { setCheckpoint(e.target.value); setKind("sim_eval"); }}><option value="" disabled>Choose checkpoint for evaluation…</option>{selectedJob.checkpoints.map(p => <option key={p} value={p}>{p.split("/checkpoints/")[1]}</option>)}</Select></Field>}
            {selectedJob.result != null && <details open><summary className="text-sm font-medium">Evaluation results</summary><pre className="mt-2 max-h-64 overflow-auto text-xs">{JSON.stringify(selectedJob.result, null, 2)}</pre></details>}
          </div>}
        </Card>}
      </div>
    </div>
    {(kind === "robot_eval" || running?.kind === "robot_eval") && <><HostMonitor /><CameraPreview /></>}
  </div>;
}
