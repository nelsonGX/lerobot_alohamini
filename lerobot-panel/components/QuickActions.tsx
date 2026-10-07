"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { useTopic } from "@/lib/live";
import { Spinner } from "./ui";

type Item = { key: string; label: string; hint: string; blocked: string | null; danger?: boolean; run: () => Promise<unknown> };

export function QuickActions() {
  const robot = useTopic("robot");
  const settings = usePoll(api.settings, 10000);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const j = robot.data?.jetson;
  const procs = robot.data?.procs;
  const recording = robot.data?.recording;
  const isRunning = (n: "teleop" | "restore") => procs?.[n]?.state === "running";
  const hostUp = !!j?.host_listening;
  const teleopOn = isRunning("teleop");
  const restoreOn = isRunning("restore");

  const items: Item[] = [
    {
      key: "restore",
      label: "Match follower to leader",
      hint: "Slowly move the follower arms to the leader pose",
      blocked: restoreOn ? "Already running" : recording ? "A recording is running" : teleopOn ? "Stop teleoperation first" : !hostUp ? "Start the robot host first" : null,
      run: api.restoreStart,
    },
    teleopOn
      ? { key: "teleop", label: "Stop teleoperation", hint: "", blocked: null, danger: true, run: () => api.procStop("teleop") }
      : { key: "teleop", label: "Start teleoperation", hint: "Follow the leader arms, nothing is saved", blocked: recording ? "A recording is running" : !hostUp ? "Start the robot host first" : null, run: api.teleopStart },
    hostUp
      ? { key: "host", label: "Stop robot host", hint: "", blocked: recording ? "A recording is running" : null, danger: true, run: api.hostStop }
      : { key: "host", label: "Start robot host", hint: "On the Jetson", blocked: !j?.agent_ok ? "Connect to the Jetson first" : null, run: () => api.hostStart(settings.data?.host_cameras ?? false) },
  ];

  const go = async (item: Item) => {
    setBusy(item.key);
    setError(null);
    try {
      await item.run();
      setOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
      robot.refresh();
    }
  };

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="flex items-center gap-1.5 rounded-md border border-line px-2.5 py-1 text-xs font-medium text-ink-2 hover:border-ink-2 hover:text-ink"
      >
        Quick actions
        <svg viewBox="0 0 12 12" className={`size-3 transition ${open ? "rotate-180" : ""}`} fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M2 4.5 6 8.5l4-4" /></svg>
      </button>
      {open && (
        <div role="menu" className="absolute right-0 top-full z-30 mt-2 w-72 rounded-lg border border-line bg-surface p-1 shadow-lg">
          {items.map((item) => (
            <button
              key={item.key}
              role="menuitem"
              disabled={!!item.blocked || busy !== null}
              onClick={() => go(item)}
              className="flex w-full items-start justify-between gap-2 rounded-md px-3 py-2 text-left text-sm enabled:hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <span>
                <span className={`block font-medium ${item.danger ? "text-critical-ink" : ""}`}>{item.label}</span>
                <span className="block text-xs text-muted">{item.blocked ?? item.hint}</span>
              </span>
              {busy === item.key && <Spinner />}
            </button>
          ))}
          {error && <p className="px-3 py-2 text-xs text-critical-ink">{error}</p>}
        </div>
      )}
    </div>
  );
}
