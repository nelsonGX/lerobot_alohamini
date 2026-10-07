"use client";

import { useEffect, useRef, type ButtonHTMLAttributes, type ReactNode } from "react";

type Variant = "primary" | "secondary" | "danger" | "ghost" | "good" | "warn";

const variants: Record<Variant, string> = {
  primary: "bg-accent text-accent-ink hover:brightness-110 border-transparent",
  secondary: "bg-surface text-ink border-line hover:bg-surface-2",
  danger: "bg-critical text-white border-transparent hover:brightness-110",
  good: "bg-good text-white border-transparent hover:brightness-110",
  warn: "bg-warn text-black border-transparent hover:brightness-105",
  ghost: "bg-transparent text-ink-2 border-transparent hover:bg-surface-2 hover:text-ink",
};

export function Button({
  variant = "secondary",
  size = "md",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md" | "lg" }) {
  const sizes = { sm: "h-8 px-3 text-sm", md: "h-10 px-4 text-sm", lg: "h-14 px-5 text-base" };
  return (
    <button
      {...props}
      className={`inline-flex items-center justify-center gap-2 rounded-lg border font-medium transition disabled:cursor-not-allowed disabled:opacity-40 ${sizes[size]} ${variants[variant]} ${className}`}
    />
  );
}

export function Card({ title, actions, children, className = "" }: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-xl border border-line bg-surface ${className}`}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold">{title}</h2>
          {actions}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="rounded border border-current/30 px-1.5 py-0.5 font-mono text-[11px] leading-none opacity-80">
      {children}
    </kbd>
  );
}

export type Status = "ok" | "warn" | "fail" | "info" | "idle";

/** Status is never conveyed by color alone: each state has its own glyph. */
export function StatusIcon({ status, className = "" }: { status: Status; className?: string }) {
  const common = `inline-block size-4 shrink-0 ${className}`;
  if (status === "ok")
    return (
      <svg viewBox="0 0 16 16" className={common} aria-label="OK">
        <circle cx="8" cy="8" r="7" fill="var(--good)" />
        <path d="M4.5 8.2 7 10.5l4.5-5" stroke="white" strokeWidth="1.8" fill="none" strokeLinecap="round" />
      </svg>
    );
  if (status === "warn")
    return (
      <svg viewBox="0 0 16 16" className={common} aria-label="Warning">
        <path d="M8 1.5 15 14H1z" fill="var(--warn)" />
        <path d="M8 6v3.5M8 11.6v.1" stroke="black" strokeWidth="1.7" strokeLinecap="round" />
      </svg>
    );
  if (status === "fail")
    return (
      <svg viewBox="0 0 16 16" className={common} aria-label="Problem">
        <circle cx="8" cy="8" r="7" fill="var(--critical)" />
        <path d="m5.3 5.3 5.4 5.4m0-5.4-5.4 5.4" stroke="white" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    );
  if (status === "info")
    return (
      <svg viewBox="0 0 16 16" className={common} aria-label="Info">
        <circle cx="8" cy="8" r="7" fill="var(--info)" />
        <path d="M8 7.2v4M8 4.6v.1" stroke="white" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    );
  return (
    <svg viewBox="0 0 16 16" className={common} aria-label="Idle">
      <circle cx="8" cy="8" r="6.2" fill="none" stroke="var(--muted)" strokeWidth="1.6" />
    </svg>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-muted">{hint}</span>}
    </label>
  );
}

export const inputClass =
  "w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink outline-none placeholder:text-muted focus:border-accent focus:ring-2 focus:ring-accent/25 disabled:opacity-60";

export function Modal({ open, onClose, title, children }: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onClick={(e) => e.target === ref.current && onClose()}
      className="m-auto w-[min(520px,calc(100vw-32px))] rounded-xl border border-line bg-surface p-0 text-ink backdrop:bg-black/50"
    >
      <div className="border-b border-line px-5 py-3 font-semibold">{title}</div>
      <div className="p-5">{children}</div>
    </dialog>
  );
}

export function ErrorBox({ children }: { children: ReactNode }) {
  return (
    <div className="flex gap-2 rounded-lg border border-critical/40 bg-critical/10 px-3 py-2 text-sm text-critical-ink">
      <StatusIcon status="fail" className="mt-0.5" />
      <div className="min-w-0">{children}</div>
    </div>
  );
}
