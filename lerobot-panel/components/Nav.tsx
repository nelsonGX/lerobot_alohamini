"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { SettingsDialog } from "./SettingsDialog";
import { PHASE_META } from "./phase";

export function Nav() {
  const path = usePathname();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const { data, error } = usePoll(() => api.recorder(1e12), 2000);
  const s = data?.session;
  const active = s?.active;
  const meta = s ? PHASE_META[s.phase] : null;

  const tab = (href: string, label: string) => {
    const on = href === "/" ? path === "/" : path.startsWith(href);
    return (
      <Link
        href={href}
        className={`rounded-md px-3 py-1.5 text-sm font-medium transition ${on ? "bg-surface-2 text-ink" : "text-ink-2 hover:text-ink"}`}
      >
        {label}
      </Link>
    );
  };

  return (
    <header className="sticky top-0 z-20 border-b border-line bg-surface/90 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-7xl items-center gap-2 px-4">
        <Link href="/" className="mr-3 flex items-center gap-2 font-semibold">
          <svg viewBox="0 0 24 24" className="size-6" aria-hidden>
            <rect x="2" y="2" width="20" height="20" rx="6" fill="var(--accent)" />
            <path d="M7 16V9.5a2.5 2.5 0 0 1 5 0V16m0-4h0a2.5 2.5 0 0 1 5 0V16" stroke="white" strokeWidth="1.8" fill="none" strokeLinecap="round" />
          </svg>
          <span className="hidden sm:inline">AlohaMini Panel</span>
        </Link>
        <nav className="flex gap-1">
          {tab("/", "Record")}
          {tab("/datasets", "Datasets")}
        </nav>
        <div className="ml-auto flex items-center gap-2">
          {error ? (
            <span className="flex items-center gap-1.5 rounded-full border border-critical/40 px-3 py-1 text-xs text-critical-ink">
              Backend offline
            </span>
          ) : active && meta ? (
            <Link
              href="/"
              className="flex items-center gap-2 rounded-full border border-line px-3 py-1 text-xs font-medium"
              title={`${s.operator || "Someone"} is recording ${s.dataset}`}
            >
              <span className={`size-2 rounded-full ${s.phase === "recording" ? "pulse-dot" : ""}`} style={{ background: meta.color }} />
              {meta.label}
              {s.episode != null && <span className="tabular text-muted">ep {s.episode}</span>}
              <span className="hidden max-w-40 truncate text-muted md:inline">{s.dataset}</span>
            </Link>
          ) : (
            <span className="hidden items-center gap-1.5 text-xs text-muted sm:flex">
              <span className="size-2 rounded-full border border-muted" /> Idle
            </span>
          )}
          <button
            onClick={() => setSettingsOpen(true)}
            className="rounded-md p-2 text-ink-2 hover:bg-surface-2 hover:text-ink"
            aria-label="Settings"
            title="Settings"
          >
            <svg viewBox="0 0 24 24" className="size-5" fill="none" stroke="currentColor" strokeWidth="1.8">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
            </svg>
          </button>
        </div>
      </div>
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </header>
  );
}
