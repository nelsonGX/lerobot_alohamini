"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { ErrorBox, inputClass } from "@/components/ui";
import { api } from "@/lib/api";
import { cameraLabel, fmtAgo, fmtBytes, fmtDuration } from "@/lib/format";
import { usePoll } from "@/lib/hooks";

export default function DatasetsPage() {
  const { data, error } = usePoll(api.datasets, 10000);
  const [filter, setFilter] = useState("");
  const list = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return (data?.datasets ?? []).filter(
      (d) => !f || d.repo_id.toLowerCase().includes(f) || d.tasks?.some((t) => t.toLowerCase().includes(f)),
    );
  }, [data, filter]);

  const totals = useMemo(() => {
    const ds = data?.datasets ?? [];
    return {
      episodes: ds.reduce((a, d) => a + (d.total_episodes ?? 0), 0),
      duration: ds.reduce((a, d) => a + (d.duration_s ?? 0), 0),
    };
  }, [data]);

  return (
    <div className="grid gap-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Datasets</h1>
          <p className="text-sm text-muted">
            {data ? `${data.datasets.length} datasets · ${totals.episodes} episodes · ${fmtDuration(totals.duration)} of demonstrations` : "Loading…"}
          </p>
        </div>
        <input
          className={`${inputClass} max-w-72`}
          placeholder="Filter by name or task…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>
      {error && <ErrorBox>{error}</ErrorBox>}
      {data && list.length === 0 && (
        <div className="rounded-xl border border-dashed border-line p-10 text-center text-sm text-muted">
          {data.datasets.length === 0 ? (
            <>
              No datasets yet. <Link href="/" className="text-accent underline">Record your first session</Link>.
            </>
          ) : (
            "No datasets match the filter."
          )}
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {list.map((d) => {
          const recording = data?.recording === d.repo_id;
          return (
            <Link
              key={d.repo_id}
              href={`/datasets/view?repo=${encodeURIComponent(d.repo_id)}`}
              className="group rounded-xl border border-line bg-surface p-4 transition hover:border-accent"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-xs text-muted">{d.repo_id.split("/")[0]}/</div>
                  <div className="truncate font-mono font-semibold group-hover:text-accent">{d.repo_id.split("/")[1]}</div>
                </div>
                {recording && (
                  <span className="flex shrink-0 items-center gap-1.5 rounded-full border border-line px-2 py-0.5 text-xs">
                    <span className="pulse-dot size-2 rounded-full bg-critical" /> Recording
                  </span>
                )}
              </div>
              {d.error ? (
                <p className="mt-3 text-xs text-critical-ink">Could not read: {d.error}</p>
              ) : (
                <>
                  <p className="mt-2 line-clamp-2 min-h-10 text-sm text-ink-2">{d.tasks.join(" · ") || "No task"}</p>
                  <dl className="mt-3 grid grid-cols-3 gap-2 text-xs">
                    <div>
                      <dt className="text-muted">Episodes</dt>
                      <dd className="tabular text-base font-semibold">{d.total_episodes}</dd>
                    </div>
                    <div>
                      <dt className="text-muted">Duration</dt>
                      <dd className="tabular text-base font-semibold">{fmtDuration(d.duration_s)}</dd>
                    </div>
                    <div>
                      <dt className="text-muted">Size</dt>
                      <dd className="tabular text-base font-semibold">{fmtBytes(d.size_bytes)}</dd>
                    </div>
                  </dl>
                  <div className="mt-3 flex items-center justify-between gap-2 text-xs text-muted">
                    <span className="truncate">
                      {d.cameras.length ? d.cameras.map(cameraLabel).join(", ") : "no cameras"} · {d.fps} fps
                    </span>
                    <span className="shrink-0">{fmtAgo(d.modified_at)}</span>
                  </div>
                </>
              )}
            </Link>
          );
        })}
      </div>
    </div>
  );
}
