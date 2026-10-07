"use client";

import { cameraStreamUrl } from "@/lib/api";
import { cameraLabel } from "@/lib/format";
import { useTopic } from "@/lib/live";

/** Live view of the robot's cameras, relayed from the host's camera stream (needs "Record cameras" on). */
export function CameraPreview() {
  const { data } = useTopic("cameras");
  const cams = data?.cameras ?? [];

  // On wide screens the column is as tall as the viewport and the tiles share it, so every camera is visible at once.
  return (
    <section className="flex flex-col rounded-lg border border-line bg-surface xl:h-[calc(100vh-5rem)]">
      <header className="flex min-h-10 items-center justify-between gap-3 border-b border-line px-3 py-1.5">
        <h2 className="text-sm font-semibold">Cameras</h2>
        {cams.length > 0 && <span className="eyebrow">{cams.filter((c) => c.live).length} live</span>}
      </header>
      {data?.error ? (
        <p className="p-3 text-sm text-critical-ink">{data.error}</p>
      ) : cams.length === 0 ? (
        <p className="p-3 text-sm text-muted">
          No camera frames yet. Start the robot host with <b>Record cameras</b> on (Robot page); previews appear here automatically.
        </p>
      ) : (
        <div className="grid min-h-0 flex-1 gap-2 p-2 xl:[grid-template-rows:repeat(var(--n),minmax(0,1fr))]" style={{ "--n": cams.length } as React.CSSProperties}>
          {cams.map((c) => (
            <figure key={c.name} className="relative m-0 min-h-0 overflow-hidden rounded-md bg-surface-2">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={cameraStreamUrl(c.name)}
                alt={cameraLabel(c.name)}
                className={`aspect-[4/3] size-full object-contain xl:aspect-auto ${c.live ? "" : "opacity-40"}`}
              />
              {!c.live && <span className="eyebrow absolute inset-0 grid place-items-center">no signal</span>}
              <figcaption className="absolute left-1.5 top-1.5 rounded bg-black/55 px-1.5 py-0.5 text-xs capitalize text-white">
                {cameraLabel(c.name)}
              </figcaption>
            </figure>
          ))}
        </div>
      )}
    </section>
  );
}
