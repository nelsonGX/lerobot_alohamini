"use client";

import type { ReactNode } from "react";
import { type HostArm, type HostJoint, type HostSnapshot } from "@/lib/api";
import { useTopic } from "@/lib/live";
import { Card } from "./ui";

const CLIENT: Record<HostSnapshot["client"]["state"], { label: string; color: string }> = {
  live: { label: "LIVE", color: "var(--good)" },
  idle: { label: "IDLE", color: "var(--warn)" },
  waiting: { label: "WAITING FOR CLIENT", color: "var(--warn)" },
  watchdog: { label: "WATCHDOG", color: "var(--critical)" },
};

const fmt = (v: number | null, d = 1) => (v == null ? "–" : `${v >= 0 ? "+" : ""}${v.toFixed(d)}`);
const clock = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
const upTime = (s: number) => `${String(Math.floor(s / 3600)).padStart(2, "0")}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;

/** The host's terminal dashboard, mirrored: link state, loop rate, per-joint position/command/current, events. */
export function HostMonitor() {
  const { data } = useTopic("host");
  const s = data?.snapshot ?? null;
  const stale = data?.age_s != null && data.age_s > 2;

  return (
    <Card
      title={
        <span className="flex items-center gap-3">
          Host live status
          {s && <span className="eyebrow">{s.subtitle} · up {upTime(s.uptime_s)}</span>}
        </span>
      }
      actions={stale ? <span className="eyebrow text-warn-ink">no data for {Math.round(data!.age_s!)}s</span> : undefined}
    >
      {!s ? (
        <p className="text-sm text-muted">
          Waiting for the host to publish its status… Hosts started from the panel stream here automatically; one started from a
          terminal on the Jetson shows the same dashboard in that terminal. (The Jetson agent and host need the latest code.)
        </p>
      ) : (
        <div className={`grid gap-6 ${stale ? "opacity-50" : ""}`}>
          <Overview s={s} />
          <div className="grid gap-6 xl:grid-cols-2">
            {s.arms.map((a) => (
              <Arm key={a.side} arm={a} />
            ))}
          </div>
          {s.base ? (
            <div className="flex flex-wrap gap-x-8 gap-y-1 font-mono text-sm">
              <span className="eyebrow">Base &amp; lift</span>
              <span><span className="text-muted">x </span>{fmt(s.base.x, 2)} m/s</span>
              <span><span className="text-muted">y </span>{fmt(s.base.y, 2)} m/s</span>
              <span><span className="text-muted">θ </span>{fmt(s.base.theta, 1)} °/s</span>
              <span><span className="text-muted">lift </span>{s.base.lift_mm.toFixed(1)} mm</span>
            </div>
          ) : (
            <p className="eyebrow">Base &amp; lift not connected (arms-only mode)</p>
          )}
          <Events events={s.events} />
        </div>
      )}
    </Card>
  );
}

function Overview({ s }: { s: HostSnapshot }) {
  const c = CLIENT[s.client.state];
  const hzColor = s.loop_hz >= 45 ? "var(--good)" : s.loop_hz >= 30 ? "var(--warn)" : "var(--critical)";
  return (
    <div className="grid gap-x-8 gap-y-3 md:grid-cols-[auto_1fr]">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 font-mono text-sm">
        <span className="flex items-center gap-2 font-semibold" style={{ color: c.color }}>
          <span className={`size-2.5 rounded-full ${s.client.state === "waiting" ? "pulse-dot" : ""}`} style={{ background: c.color }} />
          {c.label}
          {s.client.state !== "waiting" && s.client.age_s != null && (
            <span className="font-normal text-muted">{s.client.age_s < 1 ? `${Math.round(s.client.age_s * 1000)}ms` : `${s.client.age_s.toFixed(1)}s`}</span>
          )}
        </span>
        <KV k="source" v={s.source} />
        <KV k="owner" v={s.owner ?? "–"} />
        <KV k="watchdog trips" v={String(s.watchdog_events)} bad={s.watchdog_events > 0} />
        <KV
          k="protection"
          v={s.protection.holds ? `${s.protection.holds} holding` : "ok"}
          bad={s.protection.holds > 0}
          extra={s.protection.overcurrent_releases ? `${s.protection.overcurrent_releases} overcurrent releases` : undefined}
        />
      </div>
      <div className="flex items-center gap-3 font-mono text-sm md:justify-end">
        <span className="eyebrow">loop</span>
        <span className="font-semibold tabular-nums" style={{ color: hzColor }}>{s.loop_hz.toFixed(1)} Hz</span>
        <Spark values={s.hz_history} />
        <span className="text-muted tabular-nums">{s.loop_ms.toFixed(1)} ms</span>
      </div>
    </div>
  );
}

function KV({ k, v, bad, extra }: { k: string; v: string; bad?: boolean; extra?: string }) {
  return (
    <span>
      <span className="text-muted">{k} </span>
      <span className={bad ? "text-critical-ink" : ""}>{v}</span>
      {extra && <span className="ml-2 text-critical-ink">{extra}</span>}
    </span>
  );
}

function Spark({ values }: { values: number[] }) {
  const w = 120, h = 22;
  if (values.length < 2) return <span style={{ width: w }} />;
  const top = Math.max(...values, 1);
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - (v / top) * (h - 2) - 1}`).join(" ");
  return (
    <svg width={w} height={h} className="text-ink-2" aria-hidden>
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

function Arm({ arm }: { arm: HostArm }) {
  return (
    <div className="min-w-0">
      <div className="mb-2 flex items-center justify-between gap-3 border-b border-line pb-2">
        <span className="eyebrow !text-ink">{arm.side} arm</span>
        {arm.engaged ? (
          <span className="flex items-center gap-1.5 font-mono text-xs font-semibold text-good-ink">
            <span className="size-2 rounded-full bg-good" /> ENGAGED
          </span>
        ) : (
          <span className="flex items-center gap-1.5 font-mono text-xs font-semibold text-warn-ink">
            <span className="pulse-dot size-2 rounded-full bg-warn" /> LIMP · match leader to follower
            {arm.worst_joint && ` · worst ${arm.worst_joint} ${arm.worst_error_deg?.toFixed(0)}°`}
          </span>
        )}
      </div>
      <div className="grid grid-cols-[10rem_1fr_3.5rem_3.5rem_3rem_6.5rem] items-center gap-x-3 gap-y-1.5 font-mono text-xs">
        <span className="eyebrow">joint</span>
        <span className="eyebrow">● now  ◆ cmd</span>
        <span className="eyebrow text-right">now</span>
        <span className="eyebrow text-right">cmd</span>
        <span className="eyebrow text-right">err</span>
        <span className="eyebrow">current</span>
        {arm.joints.map((j) => (
          <Joint key={j.name} j={j} />
        ))}
      </div>
    </div>
  );
}

function Joint({ j }: { j: HostJoint }): ReactNode {
  const pos = (v: number) => `${Math.min(Math.max((v - j.lo) / (j.hi - j.lo), 0), 1) * 100}%`;
  const err = j.now != null && j.cmd != null ? j.cmd - j.now : null;
  const errColor = err == null ? "" : Math.abs(err) < 3 ? "text-good-ink" : Math.abs(err) < 10 ? "text-warn-ink" : "text-critical-ink";
  const frac = j.cap_ma ? Math.min(j.ma / j.cap_ma, 1) : 0;
  const curColor = frac < 0.5 ? "var(--good)" : frac < 0.85 ? "var(--warn)" : "var(--critical)";
  return (
    <>
      <span className="flex items-baseline gap-1.5"><span className="truncate">{j.name}</span>{j.hold && <span className="font-semibold text-critical-ink">HOLD</span>}</span>
      <div className="relative h-4">
        <div className="absolute inset-x-0 top-1/2 h-px bg-line" />
        {j.lo < 0 && j.hi > 0 && <div className="absolute top-1 h-2 w-px bg-muted" style={{ left: pos(0) }} />}
        {j.cmd != null && (
          <span className="absolute top-1/2 size-2 -translate-x-1/2 -translate-y-1/2 rotate-45 bg-warn" style={{ left: pos(j.cmd) }} />
        )}
        {j.now != null && (
          <span className="absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-ink" style={{ left: pos(j.now) }} />
        )}
      </div>
      <span className="text-right tabular-nums">{fmt(j.now)}</span>
      <span className="text-right tabular-nums text-warn-ink">{fmt(j.cmd)}</span>
      <span className={`text-right tabular-nums ${errColor}`}>{fmt(err)}</span>
      <span className="flex items-center gap-2">
        <span className="h-1.5 w-10 overflow-hidden rounded-[2px] bg-surface-2">
          <span className="block h-full" style={{ width: `${frac * 100}%`, background: curColor }} />
        </span>
        <span className="tabular-nums" style={{ color: curColor }}>{j.ma.toFixed(0)}mA</span>
      </span>
    </>
  );
}

function Events({ events }: { events: HostSnapshot["events"] }) {
  const recent = events.slice(-8);
  return (
    <div>
      <div className="eyebrow mb-2 border-b border-line pb-2">Events</div>
      {recent.length === 0 ? (
        <p className="font-mono text-xs text-muted">(nothing yet)</p>
      ) : (
        <ul className="grid gap-0.5 font-mono text-xs">
          {recent.map((e, i) => (
            <li key={i} className="flex gap-3">
              <span className="shrink-0 text-muted">{clock(e.t)}</span>
              <span className={e.level === "error" ? "text-critical-ink" : e.level === "warn" ? "text-warn-ink" : "text-ink-2"}>{e.msg}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
