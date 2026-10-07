"use client";

import { useEffect, useState } from "react";
import { api, type PlanItem, type Settings } from "@/lib/api";
import { useTopic } from "@/lib/live";
import { Button, ErrorBox, Field, inputClass, Modal, NumberInput, Select, Spinner } from "./ui";

export function SettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [s, setS] = useState<Settings | null>(null);
  const [original, setOriginal] = useState<Settings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const collection = useTopic("collection").data;
  const recording = useTopic("recorder").data?.session?.active ?? false;
  const [plan, setPlan] = useState<PlanItem[] | null>(null);
  const [planOriginal, setPlanOriginal] = useState<string>("");
  const [planError, setPlanError] = useState<string | null>(null);
  const [planSaving, setPlanSaving] = useState(false);

  // Load the plan once per opening (not on every live update, which would wipe what is being typed).
  const [planFor, setPlanFor] = useState(false);
  if (!open && planFor) setPlanFor(false);
  if (open && !planFor && collection?.config) {
    setPlanFor(true);
    const items = collection.config.objects.map((o) => {
      const t = collection.config.tasks.find((x) => x.object === o.id)!;
      return { id: o.id, name: o.name, color: o.color, task_text: t.text, target: t.target };
    });
    setPlan(items);
    setPlanOriginal(JSON.stringify(items));
    setPlanError(null);
  }
  const planDirty = !!plan && JSON.stringify(plan) !== planOriginal;
  const setItem = (i: number, patch: Partial<PlanItem>) => setPlan((p) => p && p.map((x, j) => (j === i ? { ...x, ...patch } : x)));
  const savePlan = async () => {
    if (!plan) return;
    setPlanSaving(true);
    setPlanError(null);
    try {
      await api.savePlan(plan);
      setPlanOriginal(JSON.stringify(plan));
    } catch (e) {
      setPlanError(e instanceof Error ? e.message : String(e));
    } finally {
      setPlanSaving(false);
    }
  };
  const hasData = (collection?.total_episodes ?? 0) > 0;

  useEffect(() => {
    if (open)
      api.settings().then(
        (fresh) => {
          setS(fresh);
          setOriginal(fresh);
          setError(null);
        },
        (e) => setError(e.message),
      );
  }, [open]);

  const set = <K extends keyof Settings>(k: K, v: Settings[K]) => setS((prev) => (prev ? { ...prev, [k]: v } : prev));
  const num = (k: keyof Settings) => ({
    value: s ? Number(s[k]) : 0,
    onChange: (v: number) => set(k, v as never),
  });
  const dirty = !!s && !!original && JSON.stringify(s) !== JSON.stringify(original);

  const save = async () => {
    if (!s) return;
    setSaving(true);
    try {
      await api.saveSettings(s);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Panel settings">
      <p className="mb-4 text-sm text-ink-2">Shared by everyone using this panel. Changes apply the next time something is started.</p>
      {error && <ErrorBox>{error}</ErrorBox>}
      {!s && !error && (
        <p className="flex items-center gap-2 text-sm text-muted">
          <Spinner /> Loading settings…
        </p>
      )}
      {s && (
        <form
          noValidate
          className="grid gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            save();
          }}
        >
          <div className="grid grid-cols-2 gap-3">
            <Field label="Jetson IP" hint="Where the robot host runs">
              <input className={`${inputClass} font-mono`} inputMode="decimal" spellCheck={false} value={s.jetson_ip} onChange={(e) => set("jetson_ip", e.target.value.trim())} />
            </Field>
            <Field label="Robot model">
              <Select value={s.robot_model} onChange={(e) => set("robot_model", e.target.value)}>
                <option>alohamini1</option>
                <option>alohamini2</option>
                <option>alohamini2pro</option>
              </Select>
            </Field>
            <Field label="Leader arm ID" hint="Calibration file name">
              <input className={inputClass} value={s.teleop_id} onChange={(e) => set("teleop_id", e.target.value.trim())} />
            </Field>
            <Field label="Leader arm profile">
              <Select value={s.arm_profile} onChange={(e) => set("arm_profile", e.target.value)}>
                <option>so-arm-5dof</option>
                <option>am-leader-6dof</option>
              </Select>
            </Field>
          </div>
          {plan && (
            <div className="border-t border-line pt-4">
              <div className="mb-1 text-sm font-semibold">Objects and tasks</div>
              <p className="mb-3 text-xs text-muted">
                The three objects and the sentence the robot is trained on for each. Saved separately from the settings below.
              </p>
              {hasData && (
                <p className="mb-3 rounded-lg border border-warn/50 bg-warn/10 px-3 py-2 text-xs text-warn-ink">
                  The dataset already has {collection?.total_episodes} episodes. Episodes recorded under a changed sentence stop counting for that task
                  and the policy would see two different sentences. Change a sentence only to fix a typo.
                </p>
              )}
              <div className="grid gap-3">
                {plan.map((it, i) => (
                  <div key={it.id} className="grid grid-cols-[auto_1fr_5rem] items-end gap-2">
                    <input type="color" aria-label={`Colour of ${it.name}`} className="h-10 w-10 cursor-pointer rounded border border-line bg-transparent p-0.5" value={it.color} onChange={(e) => setItem(i, { color: e.target.value })} />
                    <Field label={`Object ${i + 1}`}>
                      <input className={inputClass} value={it.name} maxLength={40} onChange={(e) => setItem(i, { name: e.target.value })} />
                    </Field>
                    <Field label="Target">
                      <input className={inputClass} type="number" min={1} value={it.target} onChange={(e) => setItem(i, { target: Number(e.target.value) })} />
                    </Field>
                    <div className="col-span-3 -mt-1">
                      <input className={inputClass} aria-label={`Task sentence for ${it.name}`} value={it.task_text} maxLength={200} onChange={(e) => setItem(i, { task_text: e.target.value })} />
                    </div>
                  </div>
                ))}
              </div>
              {planError && <div className="mt-3"><ErrorBox>{planError}</ErrorBox></div>}
              <div className="mt-3 flex items-center justify-end gap-2">
                {recording && <span className="mr-auto text-xs text-muted">Finish the running session to edit.</span>}
                <Button disabled={!planDirty || recording} loading={planSaving} loadingText="Saving…" onClick={savePlan}>
                  Save objects and tasks
                </Button>
              </div>
            </div>
          )}
          <div className="border-t border-line pt-4">
            <div className="mb-3 text-sm font-semibold">Jetson</div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Agent port" hint="Port of ./agent on the Jetson">
                <input className={inputClass} type="number" value={s.agent_port} onChange={(e) => set("agent_port", Number(e.target.value))} />
              </Field>
              <div className="col-span-2">
                <Field label="Extra robot host flags" hint="--no_base skips the wheels and lift, like ./host">
                  <input className={`${inputClass} font-mono`} value={s.host_args} onChange={(e) => set("host_args", e.target.value)} />
                </Field>
              </div>
            </div>
          </div>
          <div className="border-t border-line pt-4">
            <div className="mb-3 text-sm font-semibold">Defaults for new recordings</div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Dataset namespace" hint="Prefix, e.g. alohamini/…">
                <input className={inputClass} value={s.default_namespace} onChange={(e) => set("default_namespace", e.target.value.trim())} />
              </Field>
              <Field label="Episodes per session"><NumberInput min={1} max={500} {...num("default_num_episodes")} /></Field>
              <Field label="Episode length"><NumberInput min={3} max={600} step={5} suffix="s" {...num("default_episode_time_s")} /></Field>
              <Field label="Reset time"><NumberInput min={0} max={300} step={5} suffix="s" {...num("default_reset_time_s")} /></Field>
              <Field label="FPS"><NumberInput min={1} max={120} step={5} {...num("default_fps")} /></Field>
            </div>
          </div>
          <div className="flex items-center justify-end gap-2 border-t border-line pt-4">
            {dirty && <span className="mr-auto text-xs text-warn-ink">Unsaved changes</span>}
            {dirty && (
              <Button variant="ghost" onClick={() => setS(original)}>
                Reset
              </Button>
            )}
            <Button onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={!dirty} loading={saving} loadingText="Saving…">
              Save settings
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}
