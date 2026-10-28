"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { Button, Card, ErrorBox, Field, inputClass } from "@/components/ui";

const moves = [
  ["rotate_left", "↶ Rotate left"], ["forward", "↑ Forward"], ["rotate_right", "Rotate right ↷"],
  ["left", "← Strafe left"], ["backward", "↓ Backward"], ["right", "Strafe right →"],
] as const;

export function BaseControls({ available }: { available: boolean }) {
  const status = usePoll(api.baseStatus, 1000);
  const [height, setHeight] = useState("");
  const [owned, setOwned] = useState(false);
  const [active, setActive] = useState("stop");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const session = useRef<string | null>(null);
  const sequence = useRef(0);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const moving = useRef(false);
  const inFlight = useRef(false);

  const stopMotion = useCallback(() => {
    if (timer.current) clearInterval(timer.current);
    timer.current = null;
    if (!moving.current) return;
    moving.current = false;
    if (session.current) {
      void api.baseCommand(session.current, ++sequence.current, "stop").catch(e => setError(String(e)));
    }
  }, []);

  const release = useCallback(() => {
    stopMotion();
    setActive("stop");
  }, [stopMotion]);

  useEffect(() => {
    const hide = () => { if (document.hidden) release(); };
    window.addEventListener("blur", release);
    window.addEventListener("pointerup", release);
    window.addEventListener("pointercancel", release);
    document.addEventListener("visibilitychange", hide);
    return () => {
      release();
      window.removeEventListener("blur", release);
      window.removeEventListener("pointerup", release);
      window.removeEventListener("pointercancel", release);
      document.removeEventListener("visibilitychange", hide);
    };
  }, [release]);

  useEffect(() => { if (!available) stopMotion(); }, [available, stopMotion]);

  const connect = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await api.baseConnect(height.trim() ? Number(height) : null);
      session.current = result.session;
      sequence.current = 0;
      setOwned(true);
      await status.refresh();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  const stop = async () => {
    release();
    session.current = null;
    setOwned(false);
    try { await api.baseStop(); await status.refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const enabled = available && owned && !!status.data?.connected && !status.error;
  const hold = (direction: string) => {
    if (!enabled || !session.current || moving.current) return;
    setError(null);
    setActive(direction);
    moving.current = true;
    const send = async () => {
      if (inFlight.current || !moving.current || !session.current) return;
      inFlight.current = true;
      try { await api.baseCommand(session.current, ++sequence.current, direction); }
      catch (e) { release(); setError(e instanceof Error ? e.message : String(e)); }
      finally { inFlight.current = false; }
    };
    void send();
    timer.current = setInterval(send, 100);
  };

  const holdButton = (direction: string, label: string, disabled = false) => (
    <Button key={direction} className="touch-none whitespace-normal px-2 text-xs sm:text-sm" variant={enabled && active === direction ? "primary" : "secondary"}
      disabled={!enabled || disabled} aria-pressed={enabled && active === direction}
      onPointerDown={e => {
        if (e.button !== 0) return;
        e.preventDefault();
        e.currentTarget.setPointerCapture(e.pointerId);
        hold(direction);
      }}
      onPointerUp={release} onPointerCancel={release} onLostPointerCapture={release} onBlur={release}
      onKeyDown={e => {
        if (e.key === " " || e.key === "Enter") { e.preventDefault(); if (!e.repeat) hold(direction); }
      }}
      onKeyUp={e => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); release(); } }}
      onContextMenu={e => e.preventDefault()}
    >{label}</Button>
  );

  return (
    <Card title="Wheels & lift" actions={<span className="text-xs text-muted">{status.data?.connected ? "Connected" : "Disconnected"}</span>}>
      <p className="mb-3 text-sm text-ink-2">
        Hold a button to move; release to stop. Wheels: 3 cm/s, rotation: 10°/s, lift: 1 cm/s.
        Each hold is limited to 3 seconds. Reconnect after a timeout.
      </p>
      {(error || status.error || status.data?.error) && <ErrorBox>{error || status.error || status.data?.error}</ErrorBox>}
      <div className="my-3 flex flex-wrap items-end gap-3">
        <Field label="Current lift height (mm, optional)">
          <input className={inputClass} type="number" min={0} max={600} step="any" value={height}
            disabled={status.data?.connected || busy} placeholder="Leave blank for wheels only"
            onChange={e => setHeight(e.target.value)} />
        </Field>
        <Button variant="primary" loading={busy} disabled={!available || !!status.data?.connected ||
          (!!height.trim() && (!Number.isFinite(Number(height)) || Number(height) < 0 || Number(height) > 600))}
          onClick={connect}>Connect wheels{height.trim() ? " & lift" : ""}</Button>
        <Button variant="danger" onClick={stop}>Stop & disconnect</Button>
      </div>
      <p className="mb-4 text-xs text-muted">
        Enter the measured height above the lift’s lowest position to enable the lift. Connecting does not home or move it.
        Keep the arm host in arms-only mode. Manual base controls are unavailable during recording.
      </p>
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="grid grid-cols-3 gap-2">{moves.map(([direction, label]) => holdButton(direction, label))}</div>
        <div className="grid grid-cols-2 content-start gap-2">
          {holdButton("up", "↑ Lift up", status.data?.lift_height_mm == null)}
          {holdButton("down", "↓ Lift down", status.data?.lift_height_mm == null)}
          <p className="col-span-2 text-sm text-ink-2">Lift height: {status.data?.lift_height_mm == null ? "not referenced" : `${status.data.lift_height_mm.toFixed(1)} mm`}</p>
        </div>
      </div>
      {!!Object.keys(status.data?.motors ?? {}).length && (
        <div className="mt-4 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
          {Object.entries(status.data!.motors).map(([name, motor]) => (
            <span key={name}>{name.replaceAll("_", " ")}: {motor.voltage.toFixed(1)} V · {motor.temperature}°C</span>
          ))}
        </div>
      )}
    </Card>
  );
}
