"use client";

import type { Health } from "@/lib/api";
import { cameraLabel } from "@/lib/format";

const TONE = { good: "text-good-ink", warn: "text-warn-ink", bad: "text-critical-ink" } as const;
const WORD = { good: "Stream OK", warn: "Slow link", bad: "Stream problem" } as const;

/** Live stream health: capture rate, robot link latency, worst pause between frames, dropped frames, cameras. */
export function HealthStrip({ health }: { health: Health | null }) {
  if (!health || health.ok == null || health.fps == null) {
    return <div className="text-xs text-muted">Stream health: measuring…</div>;
  }
  const lim = health.limits ?? {};
  const fpsBad = health.fps < health.target_fps * (lim.min_fps_ratio ?? 0.9);
  const gapBad = (health.max_gap_ms ?? 0) > (lim.max_gap_s ?? 0.3) * 1000;
  const staleBad = (health.stale_pct ?? 0) > (lim.max_stale_frac ?? 0.1) * 100;
  const latBad = (health.latency_ms ?? 0) > (lim.max_latency_p95_s ?? 0.25) * 1000;
  const blackCams = Object.entries(health.cameras ?? {}).filter(([, v]) => v === "black");
  const tone = TONE[health.ok];
  return (
    <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1 text-sm" role="status" aria-label="Stream health">
      <span className={`flex items-center gap-1.5 font-semibold ${tone}`}>
        <span className="size-2.5 rounded-full" style={{ background: "currentColor" }} aria-hidden />
        {WORD[health.ok]}
      </span>
      <Metric label="FPS" value={`${health.fps.toFixed(1)}/${health.target_fps}`} bad={fpsBad} />
      <Metric label="Link" value={health.latency_ms != null ? `${health.latency_ms.toFixed(0)} ms` : "–"} bad={latBad} />
      <Metric label="Longest gap" value={health.max_gap_ms != null ? `${health.max_gap_ms.toFixed(0)} ms` : "–"} bad={gapBad} />
      <Metric label="Missed" value={`${(health.stale_pct ?? 0).toFixed(0)}%`} bad={staleBad} />
      <Metric
        label="Cameras"
        value={blackCams.length ? `${blackCams.map(([n]) => cameraLabel(n)).join(", ")} black` : "ok"}
        bad={blackCams.length > 0}
      />
    </div>
  );
}

function Metric({ label, value, bad }: { label: string; value: string; bad: boolean }) {
  return (
    <span className="flex items-baseline gap-1.5">
      <span className="eyebrow">{label}</span>
      <span className={`tabular font-mono ${bad ? "font-semibold text-critical-ink" : ""}`}>
        {bad && <span aria-hidden>▲ </span>}
        {value}
      </span>
    </span>
  );
}
