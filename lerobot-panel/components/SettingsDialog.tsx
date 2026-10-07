"use client";

import { useEffect, useState } from "react";
import { api, type Settings } from "@/lib/api";
import { Button, ErrorBox, Field, inputClass, Modal, NumberInput, Select, Spinner } from "./ui";

export function SettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [s, setS] = useState<Settings | null>(null);
  const [original, setOriginal] = useState<Settings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

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
          <div className="border-t border-line pt-4">
            <div className="mb-3 text-sm font-semibold">Jetson</div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Username" hint="Set by Robot → Jetson → Connect">
                <input className={inputClass} value={s.jetson_user} onChange={(e) => set("jetson_user", e.target.value.trim())} />
              </Field>
              <Field label="Repo folder" hint="Where ./host lives on the Jetson">
                <input className={inputClass} value={s.jetson_repo} onChange={(e) => set("jetson_repo", e.target.value.trim())} />
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
