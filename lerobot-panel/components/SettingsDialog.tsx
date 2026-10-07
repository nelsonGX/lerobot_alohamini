"use client";

import { useEffect, useState } from "react";
import { api, type Settings } from "@/lib/api";
import { Button, ErrorBox, Field, inputClass, Modal } from "./ui";

export function SettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [s, setS] = useState<Settings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open)
      api.settings().then(
        (fresh) => {
          setS(fresh);
          setError(null);
        },
        (e) => setError(e.message),
      );
  }, [open]);

  const set = <K extends keyof Settings>(k: K, v: Settings[K]) => setS((prev) => (prev ? { ...prev, [k]: v } : prev));
  const num = (k: keyof Settings) => ({
    type: "number",
    className: inputClass,
    value: s ? String(s[k]) : "",
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => set(k, Number(e.target.value) as never),
  });

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
      <p className="mb-4 text-sm text-ink-2">Shared by everyone using this panel. Changes apply to the next recording.</p>
      {error && <ErrorBox>{error}</ErrorBox>}
      {s && (
        <div className="grid gap-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Jetson IP" hint="Where ./host runs">
              <input className={inputClass} value={s.jetson_ip} onChange={(e) => set("jetson_ip", e.target.value.trim())} />
            </Field>
            <Field label="Robot model">
              <select className={inputClass} value={s.robot_model} onChange={(e) => set("robot_model", e.target.value)}>
                <option>alohamini1</option>
                <option>alohamini2</option>
                <option>alohamini2pro</option>
              </select>
            </Field>
            <Field label="Leader arm ID" hint="Calibration file name">
              <input className={inputClass} value={s.teleop_id} onChange={(e) => set("teleop_id", e.target.value.trim())} />
            </Field>
            <Field label="Leader arm profile">
              <select className={inputClass} value={s.arm_profile} onChange={(e) => set("arm_profile", e.target.value)}>
                <option>so-arm-5dof</option>
                <option>am-leader-6dof</option>
              </select>
            </Field>
          </div>
          <div className="border-t border-line pt-4">
            <div className="mb-3 text-sm font-semibold">Defaults for new recordings</div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Dataset namespace" hint="Prefix, e.g. alohamini/…">
                <input className={inputClass} value={s.default_namespace} onChange={(e) => set("default_namespace", e.target.value.trim())} />
              </Field>
              <Field label="Episodes per session"><input min={1} {...num("default_num_episodes")} /></Field>
              <Field label="Episode length (s)"><input min={3} {...num("default_episode_time_s")} /></Field>
              <Field label="Reset time (s)"><input min={0} {...num("default_reset_time_s")} /></Field>
              <Field label="FPS"><input min={1} {...num("default_fps")} /></Field>
            </div>
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="primary" onClick={save} disabled={saving}>
              {saving ? "Saving…" : "Save settings"}
            </Button>
          </div>
        </div>
      )}
    </Modal>
  );
}
