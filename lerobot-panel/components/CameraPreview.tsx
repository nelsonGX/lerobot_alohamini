"use client";

import { api, cameraStreamUrl } from "@/lib/api";
import { cameraLabel } from "@/lib/format";
import { usePoll } from "@/lib/hooks";
import { Card } from "./ui";

/** Live view of the robot's cameras, relayed from the host's camera stream (needs "Record cameras" on). */
export function CameraPreview() {
  const { data } = usePoll(api.cameras, 2000);
  const cams = data?.cameras ?? [];

  return (
    <Card title="Cameras" actions={cams.length ? <span className="eyebrow">{cams.filter((c) => c.live).length} live</span> : undefined}>
      {data?.error ? (
        <p className="text-sm text-critical-ink">{data.error}</p>
      ) : cams.length === 0 ? (
        <p className="text-sm text-muted">
          No camera frames yet. Start the robot host with <b>Record cameras</b> on (Robot page); previews appear here automatically.
        </p>
      ) : (
        <div className="grid gap-3">
          {cams.map((c) => (
            <figure key={c.name} className="m-0">
              <div className="relative aspect-[4/3] overflow-hidden rounded-md bg-surface-2">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={cameraStreamUrl(c.name)} alt={cameraLabel(c.name)} className={`size-full object-contain ${c.live ? "" : "opacity-40"}`} />
                {!c.live && <span className="eyebrow absolute inset-0 grid place-items-center">no signal</span>}
              </div>
              <figcaption className="mt-1 text-xs capitalize text-muted">{cameraLabel(c.name)}</figcaption>
            </figure>
          ))}
        </div>
      )}
    </Card>
  );
}
