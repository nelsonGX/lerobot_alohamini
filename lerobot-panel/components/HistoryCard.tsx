"use client";

import Link from "next/link";
import type { HistoryEntry } from "@/lib/api";
import { fmtAgo, fmtDuration } from "@/lib/format";
import { Card, StatusIcon } from "./ui";

export function HistoryCard({ history }: { history: HistoryEntry[] | null }) {
  return (
    <Card title="Recent sessions">
      {!history ? (
        <p className="text-sm text-muted">Loading…</p>
      ) : history.length === 0 ? (
        <p className="text-sm text-muted">No sessions recorded from the panel yet.</p>
      ) : (
        <ul className="-my-2 divide-y divide-line">
          {history.slice(0, 8).map((h) => (
            <li key={h.id} className="flex gap-2.5 py-2.5">
              <StatusIcon status={h.outcome === "done" ? "ok" : h.outcome === "aborted" ? "warn" : "fail"} className="mt-0.5" />
              <div className="min-w-0 flex-1 text-sm">
                <Link href={`/datasets/view?repo=${encodeURIComponent(h.dataset)}`} className="block truncate font-mono text-xs hover:underline">
                  {h.dataset}
                </Link>
                <div className="text-xs text-ink-2">
                  {h.episodes_saved} saved{h.episodes_discarded > 0 && `, ${h.episodes_discarded} discarded`}
                  {h.ended_at && ` · ${fmtDuration(h.ended_at - h.started_at)}`}
                </div>
                <div className="text-xs text-muted">
                  {h.operator || "Someone"} · {fmtAgo(h.started_at)}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
