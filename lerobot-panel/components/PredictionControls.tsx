"use client";

import type { ReactNode } from "react";
import { Button, Field, NumberInput, Segmented, StatusIcon, Toggle } from "./ui";

/**
 * How often the policy predicts. A chunking policy outputs `chunk` actions per model call and executes
 * `steps` of them before calling the model again; fewer steps = more frequent, more reactive predictions.
 * The chunk length is baked into the weights, so it can only be chosen for a new training run.
 */
export type Prediction = {
  chunkSize: number | null; steps: number | null; ensemble: boolean;
  inference: "sync" | "rtc"; threshold: number; horizon: number;
};
export const initialPrediction: Prediction = { chunkSize: null, steps: null, ensemble: false, inference: "sync", threshold: 30, horizon: 10 };

// Library defaults (diffusion: horizon 16 − 2 observation steps + 1).
const defaults: Record<string, { chunk: number; steps: number }> = {
  act: { chunk: 100, steps: 100 }, diffusion: { chunk: 15, steps: 8 },
  smolvla: { chunk: 50, steps: 50 }, pi0: { chunk: 50, steps: 50 }, pi05: { chunk: 50, steps: 50 },
};
const VLA = new Set(["smolvla", "pi0", "pi05", "pi0_fast"]);
const ENSEMBLE_COEFF = 0.01;  // ACT paper value.
export const LIVE_FPS = 30;

export const isVla = (policy: string) => VLA.has(policy);

export function modelChunk(policy: string, chunk: number | null | undefined, steps: number | null | undefined) {
  const d = defaults[policy] ?? { chunk: 50, steps: 50 };
  return { chunk: chunk ?? d.chunk, steps: steps ?? chunk ?? d.steps, known: chunk != null || policy in defaults };
}

/** Fields for the GPU job request. */
export function predictionPayload(p: Prediction, kind: "train" | "sim_eval" | "robot_eval", policy: string, finetune: boolean) {
  if (kind === "train") return { chunk_size: finetune || policy === "diffusion" ? null : p.chunkSize, n_action_steps: p.steps };
  if (kind === "robot_eval" && p.inference === "rtc") return { inference: "rtc", rtc_queue_threshold: p.threshold, rtc_execution_horizon: p.horizon };
  if (p.ensemble && policy === "act") return { inference: "sync", temporal_ensemble_coeff: ENSEMBLE_COEFF };
  return { inference: "sync", n_action_steps: p.steps };
}

const hz = (steps: number) => LIVE_FPS / Math.max(1, steps);
const fmtHz = (v: number) => (v >= 10 ? v.toFixed(0) : v.toFixed(1));

export function PredictionControls({ kind, policy, chunk, defaultSteps, finetune, value, onChange }: {
  kind: "train" | "sim_eval" | "robot_eval";
  policy: string;
  chunk: number | null;
  defaultSteps: number | null;
  finetune?: boolean;
  value: Prediction;
  onChange: (p: Prediction) => void;
}) {
  const set = (patch: Partial<Prediction>) => onChange({ ...value, ...patch });
  const model = modelChunk(policy, kind === "train" ? value.chunkSize : chunk, defaultSteps);
  const live = kind === "robot_eval";
  const rtc = live && value.inference === "rtc";
  const ensemble = value.ensemble && policy === "act" && !rtc;
  const steps = ensemble ? 1 : Math.min(value.steps ?? model.steps, model.chunk);
  const rtcEvery = Math.max(1, model.chunk - value.threshold);
  const presets = [1, 2, 5, 10, 20, 25, 50, 100].filter(n => n < model.chunk).concat(model.chunk);

  if (kind === "train") {
    const fixed = finetune || policy === "diffusion";
    return <div className="grid gap-3 sm:grid-cols-2">
      <Field label="Chunk size" hint={finetune ? "Fixed by the pretrained model." : policy === "diffusion" ? "Diffusion uses a horizon of 16." : `Actions predicted per model call. Default ${defaults[policy]?.chunk ?? "?"}.`}>
        <NumberInput value={fixed ? model.chunk : value.chunkSize ?? model.chunk} min={1} max={1000} disabled={fixed}
          onChange={v => set({ chunkSize: v, steps: value.steps != null ? Math.min(value.steps, v) : null })} />
      </Field>
      <Field label="Default steps per prediction" hint="Saved in the model; you can still lower it when evaluating.">
        <NumberInput value={steps} min={1} max={model.chunk} onChange={v => set({ steps: v })} />
      </Field>
    </div>;
  }

  return <div className="grid gap-4">
    <div className="flex flex-wrap items-end justify-between gap-3 rounded-lg bg-surface-2 px-4 py-3">
      <div>
        <div className="eyebrow">Prediction rate</div>
        <div className="tabular mt-1 text-2xl font-semibold">
          {live ? <>{rtc ? "up to " : ""}{fmtHz(hz(rtc ? rtcEvery : steps))} <span className="text-base font-normal text-muted">Hz</span></>
            : <>every {steps} <span className="text-base font-normal text-muted">step{steps === 1 ? "" : "s"}</span></>}
        </div>
      </div>
      <p className="max-w-xs text-right text-xs text-muted">
        {rtc ? `New ${model.chunk}-action chunk once ${value.threshold} actions remain; limited by how fast the model runs.`
          : ensemble ? "Predicts a full chunk on every step and blends the overlapping chunks."
          : `Executes ${steps} of ${model.chunk} predicted actions, then predicts again${live ? ` (every ${(steps / LIVE_FPS).toFixed(2)} s at ${LIVE_FPS} fps)` : ""}.`}
        {!model.known && " Chunk size unknown for this model; the server checks it."}
      </p>
    </div>

    {live && <Field label="Inference engine">
      <Segmented label="Inference engine" value={value.inference} onChange={v => set({ inference: v })}
        options={[{ value: "sync", label: "In control loop" }, { value: "rtc", label: "Background (RTC)" }]} />
    </Field>}

    {rtc ? <>
      {!isVla(policy) && policy !== "unknown" && <Note status="warn">RTC needs a VLA policy (SmolVLA, π0, π0.5). {policy} only works with the in-loop engine.</Note>}
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Predict again when this many actions remain" hint={`Higher = more frequent. ${model.chunk - 1} keeps the model running back to back.`}>
          <NumberInput value={value.threshold} min={1} max={Math.max(1, model.chunk - 1)} presets={[10, 20, 30, 40, 45].filter(n => n < model.chunk)} onChange={v => set({ threshold: v })} />
        </Field>
        <Field label="Blend horizon" hint="Steps of the old chunk the new one is guided to match. Default 10.">
          <NumberInput value={value.horizon} min={1} max={model.chunk} onChange={v => set({ horizon: v })} />
        </Field>
      </div>
    </> : <>
      <Field label="Steps per prediction" hint={value.steps == null && !ensemble ? `Using the model's setting (${model.steps}).` : undefined}>
        <NumberInput value={steps} min={1} max={model.chunk} presets={presets} disabled={ensemble} onChange={v => set({ steps: v })} />
      </Field>
      {value.steps != null && !ensemble && <Button size="sm" variant="ghost" className="justify-self-start" onClick={() => set({ steps: null })}>Use the model&apos;s setting ({model.steps})</Button>}
      {policy === "act" && <Toggle checked={ensemble} onChange={v => set({ ensemble: v })} label="Temporal ensembling"
        description="Predict every step and average overlapping chunks. Smoothest ACT option; one model call per control step." />}
      {live && isVla(policy) && steps < model.chunk / 2 && <Note status="warn">
        Each {policy} prediction takes roughly 0.1–0.3 s, and in-loop prediction pauses the robot while it runs. To predict often without pauses, use the background engine.
        <Button size="sm" className="mt-2" onClick={() => set({ inference: "rtc", threshold: Math.max(1, model.chunk - steps) })}>Switch to Background (RTC)</Button>
      </Note>}
    </>}
  </div>;
}

function Note({ status, children }: { status: "warn" | "info"; children: ReactNode }) {
  return <div className={`flex gap-2 rounded-lg border px-3 py-2 text-sm ${status === "warn" ? "border-warn/50 bg-warn/10" : "border-line"}`}>
    <StatusIcon status={status} className="mt-0.5" /><div className="grid min-w-0 justify-items-start">{children}</div>
  </div>;
}
