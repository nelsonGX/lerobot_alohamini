"use client";

import { memo, useMemo, useRef, useState } from "react";
import type { EpisodeData } from "@/lib/api";
import { jointLabel } from "@/lib/format";

const W = 300;
const H = 72;

type Joint = {
  name: string;
  state: (number | null)[] | null;
  action: (number | null)[] | null;
  min: number;
  max: number;
};

function range(values: (number | null)[][]) {
  let min = Infinity;
  let max = -Infinity;
  for (const arr of values)
    for (const v of arr)
      if (v != null) {
        if (v < min) min = v;
        if (v > max) max = v;
      }
  return { min, max };
}

function groupOf(name: string) {
  if (name.startsWith("arm_left_")) return "Left arm";
  if (name.startsWith("arm_right_")) return "Right arm";
  return "Base & lift";
}

export function JointCharts({
  data,
  frame,
  onSeekFrame,
}: {
  data: EpisodeData;
  frame: number;
  onSeekFrame: (f: number) => void;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const [showFlat, setShowFlat] = useState(false);
  const [view, setView] = useState<"charts" | "table">("charts");
  const n = data.timestamps.length;

  const { groups, flat } = useMemo(() => {
    const state = data.series["observation.state"] ?? {};
    const action = data.series["action"] ?? {};
    const names = Array.from(new Set([...Object.keys(state), ...Object.keys(action)]));
    const joints: Joint[] = names.map((name) => {
      const s = state[name] ?? null;
      const a = action[name] ?? null;
      const { min, max } = range([s ?? [], a ?? []]);
      return { name, state: s, action: a, min, max };
    });
    const isFlat = (j: Joint) => !(j.max - j.min > 1e-6);
    const groups = new Map<string, Joint[]>();
    for (const j of joints.filter((j) => !isFlat(j))) {
      const g = groupOf(j.name);
      groups.set(g, [...(groups.get(g) ?? []), j]);
    }
    return { groups: [...groups.entries()], flat: joints.filter(isFlat) };
  }, [data]);

  const shown = hover ?? frame;

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-ink-2">
        <span className="flex items-center gap-1.5">
          <svg width="22" height="8" aria-hidden>
            <line x1="1" y1="4" x2="21" y2="4" stroke="var(--series-state)" strokeWidth="2" strokeLinecap="round" />
          </svg>
          State (follower arm)
        </span>
        <span className="flex items-center gap-1.5">
          <svg width="22" height="8" aria-hidden>
            <line x1="1" y1="4" x2="21" y2="4" stroke="var(--series-action)" strokeWidth="2" strokeDasharray="4 3" strokeLinecap="round" />
          </svg>
          Action (leader command)
        </span>
        <span className="tabular text-muted">
          {hover != null ? "hover" : "playhead"} · frame {shown} · {data.timestamps[shown]?.toFixed(2) ?? "–"}s
        </span>
        <div className="ml-auto flex rounded-lg border border-line p-0.5">
          {(["charts", "table"] as const).map((v) => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={`rounded-md px-2.5 py-1 capitalize ${view === v ? "bg-surface-2 text-ink" : "text-muted"}`}
            >
              {v}
            </button>
          ))}
        </div>
      </div>

      {view === "table" ? (
        <ValuesTable groups={groups} flat={flat} frame={shown} />
      ) : (
        groups.map(([group, joints]) => (
          <div key={group}>
            <h4 className="mb-2 text-xs font-semibold tracking-wide text-muted uppercase">{group}</h4>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
              {joints.map((j) => (
                <JointChart
                  key={j.name}
                  joint={j}
                  n={n}
                  frame={frame}
                  hover={hover}
                  onHover={setHover}
                  onSeek={onSeekFrame}
                />
              ))}
            </div>
          </div>
        ))
      )}

      {flat.length > 0 && view === "charts" && (
        <div className="text-xs text-muted">
          <button className="underline" onClick={() => setShowFlat((v) => !v)}>
            {flat.length} constant signal{flat.length > 1 ? "s" : ""} hidden
          </button>
          {showFlat && (
            <span>
              : {flat.map((j) => `${j.name} = ${j.state?.[0] ?? j.action?.[0]}`).join(", ")}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function fmtVal(v: number | null | undefined) {
  return v == null ? "–" : Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1);
}

const JointChart = memo(function JointChart({
  joint,
  n,
  frame,
  hover,
  onHover,
  onSeek,
}: {
  joint: Joint;
  n: number;
  frame: number;
  hover: number | null;
  onHover: (f: number | null) => void;
  onSeek: (f: number) => void;
}) {
  const ref = useRef<SVGSVGElement>(null);
  const dragging = useRef(false);
  const pad = (joint.max - joint.min) * 0.08 || 1;
  const lo = joint.min - pad;
  const hi = joint.max + pad;
  const x = (i: number) => (n <= 1 ? 0 : (i / (n - 1)) * W);
  const y = (v: number) => H - ((v - lo) / (hi - lo)) * H;

  const paths = useMemo(() => {
    const toPath = (arr: (number | null)[] | null) => {
      if (!arr) return "";
      let d = "";
      let pen = false;
      arr.forEach((v, i) => {
        if (v == null) {
          pen = false;
          return;
        }
        d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
        pen = true;
      });
      return d;
    };
    return { state: toPath(joint.state), action: toPath(joint.action) };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [joint, n]);

  const frameAt = (clientX: number) => {
    const r = ref.current!.getBoundingClientRect();
    return Math.max(0, Math.min(n - 1, Math.round(((clientX - r.left) / r.width) * (n - 1))));
  };

  const shown = hover ?? frame;
  const zeroInRange = lo < 0 && hi > 0;

  return (
    <div className="rounded-lg border border-line bg-surface px-3 pt-2 pb-1.5">
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="truncate font-medium" title={joint.name}>{jointLabel(joint.name)}</span>
        <span className="tabular shrink-0 text-ink-2">
          <span className="font-semibold text-ink">{fmtVal(joint.state?.[shown])}</span>
          <span className="text-muted"> state · </span>
          <span className="font-semibold text-ink">{fmtVal(joint.action?.[shown])}</span>
          <span className="text-muted"> action</span>
        </span>
      </div>
      <div className="relative mt-1">
        <svg
          ref={ref}
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          className="block h-[72px] w-full cursor-crosshair touch-none overflow-visible"
          onPointerMove={(e) => {
            const f = frameAt(e.clientX);
            onHover(f);
            if (dragging.current) onSeek(f);
          }}
          onPointerLeave={() => onHover(null)}
          onPointerDown={(e) => {
            dragging.current = true;
            e.currentTarget.setPointerCapture(e.pointerId);
            onSeek(frameAt(e.clientX));
          }}
          onPointerUp={() => (dragging.current = false)}
          role="img"
          aria-label={`${joint.name} state and action over the episode`}
        >
          {zeroInRange && (
            <line x1="0" x2={W} y1={y(0)} y2={y(0)} stroke="var(--grid)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
          )}
          <path d={paths.action} fill="none" stroke="var(--series-action)" strokeWidth="1.5" strokeDasharray="4 3" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
          <path d={paths.state} fill="none" stroke="var(--series-state)" strokeWidth="2" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
          {hover != null && (
            <line x1={x(hover)} x2={x(hover)} y1="0" y2={H} stroke="var(--muted)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
          )}
          <line x1={x(frame)} x2={x(frame)} y1="0" y2={H} stroke="var(--ink)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
        </svg>
        <span className="tabular pointer-events-none absolute top-0 left-0.5 text-[10px] text-muted">{fmtVal(joint.max)}</span>
        <span className="tabular pointer-events-none absolute bottom-0 left-0.5 text-[10px] text-muted">{fmtVal(joint.min)}</span>
      </div>
    </div>
  );
});

function ValuesTable({ groups, flat, frame }: { groups: [string, Joint[]][]; flat: Joint[]; frame: number }) {
  const rows = [...groups.flatMap(([, js]) => js), ...flat];
  return (
    <div className="overflow-x-auto rounded-lg border border-line">
      <table className="w-full text-sm">
        <thead className="bg-surface-2 text-left text-xs text-muted">
          <tr>
            <th className="px-3 py-2 font-medium">Signal</th>
            <th className="px-3 py-2 text-right font-medium">State</th>
            <th className="px-3 py-2 text-right font-medium">Action</th>
            <th className="px-3 py-2 text-right font-medium">Δ (action − state)</th>
            <th className="px-3 py-2 text-right font-medium">Min</th>
            <th className="px-3 py-2 text-right font-medium">Max</th>
          </tr>
        </thead>
        <tbody className="tabular divide-y divide-line">
          {rows.map((j) => {
            const s = j.state?.[frame];
            const a = j.action?.[frame];
            return (
              <tr key={j.name}>
                <td className="px-3 py-1.5 font-mono text-xs">{j.name}</td>
                <td className="px-3 py-1.5 text-right">{fmtVal(s)}</td>
                <td className="px-3 py-1.5 text-right">{fmtVal(a)}</td>
                <td className="px-3 py-1.5 text-right text-ink-2">{s != null && a != null ? fmtVal(a - s) : "–"}</td>
                <td className="px-3 py-1.5 text-right text-muted">{fmtVal(j.min)}</td>
                <td className="px-3 py-1.5 text-right text-muted">{fmtVal(j.max)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
