"use client";

import type { Check } from "@/lib/api";
import { Button, Card, StatusIcon } from "./ui";

export function Preflight({ checks, onRefresh, loading }: { checks: Check[] | null; onRefresh: () => void; loading: boolean }) {
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
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
