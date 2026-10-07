"use client";

import type { CollectionConfig, Layout } from "@/lib/api";

/** The random object layout for this episode, as seen from the robot's front camera. The target is outlined. */
export function LayoutDiagram({
  layout,
  objects,
  slots,
  size = "lg",
}: {
  layout: Layout;
  objects: CollectionConfig["objects"];
  slots: string[];
  size?: "sm" | "lg";
}) {
  const color = (name: string) => objects.find((o) => o.name === name)?.color ?? "var(--muted)";
  const big = size === "lg";
  return (
    <div>
      <ol className="grid gap-2" style={{ gridTemplateColumns: `repeat(${slots.length}, minmax(0, 1fr))` }} aria-label="Object layout">
        {slots.map((slot) => {
          const name = layout.names[slot];
          const target = slot === layout.target_slot;
          return (
            <li
              key={slot}
              className={`flex flex-col items-center rounded-lg border-2 text-center ${big ? "gap-2 px-2 py-5" : "gap-1 px-1.5 py-2"} ${target ? "border-ink bg-surface-2" : "border-line"}`}
            >
              <span className={`rounded-full border border-black/20 ${big ? "size-14" : "size-7"}`} style={{ background: color(name) }} aria-hidden />
              <span className={`font-medium ${big ? "text-base" : "text-xs"}`}>{name}</span>
              {target && <span className={`font-semibold uppercase tracking-wider ${big ? "text-xs" : "text-[10px]"}`}>pick this</span>}
            </li>
          );
        })}
      </ol>
      <div className="mt-1.5 flex justify-between px-1 text-[11px] uppercase tracking-wider text-muted" aria-hidden>
        {slots.map((s) => (
          <span key={s} className="flex-1 text-center">
            {s}
          </span>
        ))}
      </div>
    </div>
  );
}
