"use client";

import Link from "next/link";
import { useState } from "react";
import { api, type Check } from "@/lib/api";
import { Button, Card, StatusIcon } from "./ui";

export function Preflight({ checks, onRefresh, loading }: { checks: Check[] | null; onRefresh: () => void; loading: boolean }) {
  const [fixing, setFixing] = useState<string | null>(null);
  const [fixError, setFixError] = useState<string | null>(null);
  const fix = async (c: Check) => {
    setFixing(c.id);
    setFixError(null);
    try {
      if (c.action === "start_host") await api.hostStart((await api.settings()).host_cameras);
      else if (c.action === "stop_teleop") await api.procStop("teleop");
    } catch (e) {
      setFixError(e instanceof Error ? e.message : String(e));
    } finally {
      setFixing(null);
      onRefresh();
    }
  };
  const failing = checks?.filter((c) => c.status === "fail").length ?? 0;
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          Pre-flight checks
          {checks && (
            <span className={`text-xs font-normal ${failing ? "text-critical-ink" : "text-good-ink"}`}>
              {failing ? `${failing} problem${failing > 1 ? "s" : ""}` : "all good"}
            </span>
          )}
        </span>
      }
      actions={
        <Button size="sm" variant="ghost" onClick={onRefresh} disabled={loading}>
          {loading ? "Checking…" : "Re-check"}
        </Button>
      }
    >
      {!checks ? (
        <p className="text-sm text-muted">Checking hardware…</p>
      ) : (
        <ul className="grid gap-3">
          {checks.map((c) => (
            <li key={c.id} className="flex gap-2.5">
              <StatusIcon status={c.status} className="mt-0.5" />
              <div className="min-w-0 text-sm">
                <div className="font-medium">{c.label}</div>
                <div className="break-words text-xs text-ink-2">{c.detail}</div>
                {c.hint && <div className="mt-0.5 break-words text-xs text-muted">→ {c.hint}</div>}
                {(c.action === "start_host" || c.action === "stop_teleop") && (
                  <Button size="sm" variant="primary" className="mt-1.5" disabled={fixing === c.id} onClick={() => fix(c)}>
                    {fixing === c.id ? "Working…" : c.action === "start_host" ? "Start robot host" : "Stop teleoperation"}
                  </Button>
                )}
                {(c.action === "setup_jetson" || c.action === "calibrate_leader") && (
                  <Link href={c.action === "setup_jetson" ? "/robot" : "/robot#calibration"} className="mt-1 inline-block text-xs font-medium text-accent hover:underline">
                    {c.action === "setup_jetson" ? "Open Robot page →" : "Calibrate now →"}
                  </Link>
                )}
                {fixError && fixing === null && c.action === "start_host" && <div className="mt-1 text-xs text-critical-ink">{fixError}</div>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
