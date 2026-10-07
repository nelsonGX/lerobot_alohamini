"use client";

import type { TaskProgress as Task } from "@/lib/api";

/**
 * Episodes recorded per task against its target. With `onSelect` it is the task picker (the only way to
 * choose a task: there is no free text), and the task furthest behind is marked "Next up".
 */
export function TaskProgress({
  tasks,
  suggestedId,
  selectedId,
  onSelect,
  disabled,
  flagged,
}: {
  tasks: Task[];
  suggestedId: string | null;
  selectedId?: string | null;
  onSelect?: (id: string) => void;
  disabled?: boolean;
  flagged?: Record<string, number>;
}) {
  return (
    <ul className="grid gap-2" role={onSelect ? "radiogroup" : undefined} aria-label="Tasks">
      {tasks.map((t) => {
        const pct = Math.min(100, (t.count / t.target) * 100);
        const done = t.count >= t.target;
        const selected = selectedId === t.id;
        const body = (
          <>
            <div className="flex items-baseline gap-2">
              <span className="min-w-0 flex-1 truncate text-sm font-medium">{t.text}</span>
              {t.id === suggestedId && (
                <span className="rounded-full bg-accent/15 px-2 py-0.5 text-[11px] font-semibold text-accent">Next up</span>
              )}
              {done && <span className="text-[11px] font-semibold text-good-ink">target reached</span>}
              <span className="tabular font-mono text-xs text-ink-2">
                {t.count} / {t.target}
              </span>
            </div>
            <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-surface-2" aria-hidden>
              <div className="h-full rounded-full transition-[width] duration-500" style={{ width: `${pct}%`, background: done ? "var(--good)" : "var(--ink)" }} />
            </div>
            {!!flagged?.[t.id] && (
              <div className="mt-1 text-[11px] text-warn-ink">{flagged[t.id]} flagged as possibly bad</div>
            )}
          </>
        );
        return (
          <li key={t.id}>
            {onSelect ? (
              <button
                type="button"
                role="radio"
                aria-checked={selected}
                disabled={disabled}
                onClick={() => onSelect(t.id)}
                className={`block w-full rounded-lg border px-3 py-2.5 text-left transition disabled:cursor-not-allowed disabled:opacity-50 ${selected ? "border-ink bg-surface-2" : "border-line hover:border-ink-2"}`}
              >
                {body}
              </button>
            ) : (
              <div className="rounded-lg border border-line px-3 py-2.5">{body}</div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
