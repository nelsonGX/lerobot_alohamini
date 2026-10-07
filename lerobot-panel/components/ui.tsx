"use client";

import Link from "next/link";
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type ComponentProps,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
} from "react";

type Variant = "primary" | "secondary" | "danger" | "ghost" | "good" | "warn";
type Size = "sm" | "md" | "lg";

const variants: Record<Variant, string> = {
  primary: "bg-ink text-page border-transparent hover:opacity-85 active:opacity-75",
  secondary: "bg-transparent text-ink border-line hover:border-ink-2 hover:bg-surface-2 active:bg-surface-2",
  danger: "bg-critical text-white border-transparent hover:brightness-110 active:brightness-95",
  good: "bg-good text-white border-transparent hover:brightness-110 active:brightness-95",
  warn: "bg-warn text-black border-transparent hover:brightness-105 active:brightness-95",
  ghost: "bg-transparent text-ink-2 border-transparent hover:bg-surface-2 hover:text-ink active:bg-surface-2",
};
const sizes: Record<Size, string> = { sm: "h-8 px-3 text-sm", md: "h-10 px-4 text-sm", lg: "h-14 px-5 text-base" };

const buttonBase =
  "relative inline-flex select-none items-center justify-center gap-2 whitespace-nowrap rounded-md border font-medium " +
  "transition-[background-color,border-color,color,filter,transform,opacity] duration-150 " +
  "enabled:active:translate-y-px focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
  "disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none";

export function buttonClass(variant: Variant = "secondary", size: Size = "md", className = "") {
  return `${buttonBase} ${sizes[size]} ${variants[variant]} ${className}`;
}

export function Spinner({ className = "size-4" }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" className={`spin shrink-0 ${className}`} fill="none" aria-hidden>
      <circle cx="8" cy="8" r="6" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" />
      <path d="M14 8a6 6 0 0 0-6-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

/**
 * `loading` shows a spinner and blocks clicks; `loadingText` replaces the label while it runs.
 * Disabled buttons keep their `title`, so pass one to explain *why* something can't be pressed.
 */
export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  loadingText,
  className = "",
  children,
  disabled,
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: Size; loading?: boolean; loadingText?: ReactNode }) {
  return (
    <button
      {...props}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={buttonClass(variant, size, className)}
    >
      {loading && <Spinner />}
      {loading && loadingText ? loadingText : children}
    </button>
  );
}

/** A link styled as a button (avoids nesting a <button> inside an <a>). */
export function LinkButton({
  variant = "secondary",
  size = "md",
  disabled,
  className = "",
  ...props
}: ComponentProps<typeof Link> & { variant?: Variant; size?: Size; disabled?: boolean }) {
  if (disabled)
    return (
      <span aria-disabled="true" className={`${buttonClass(variant, size, className)} cursor-not-allowed opacity-40 shadow-none`}>
        {props.children}
      </span>
    );
  return <Link {...props} className={buttonClass(variant, size, className)} />;
}

export function IconButton({ label, className = "", children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      {...props}
      className={`inline-flex size-8 shrink-0 items-center justify-center rounded-md text-ink-2 transition hover:bg-surface-2 hover:text-ink active:scale-95 disabled:cursor-not-allowed disabled:opacity-40 ${className}`}
    >
      {children}
    </button>
  );
}

export function Card({ title, actions, children, className = "" }: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-lg border border-line bg-surface ${className}`}>
      {(title || actions) && (
        <header className="flex min-h-12 items-center justify-between gap-3 border-b border-line px-4 py-2.5">
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

/** Label + control + hint. `error` replaces the hint and turns it red. */
export function Field({ label, hint, error, optional, children }: {
  label: string;
  hint?: ReactNode;
  error?: ReactNode;
  optional?: boolean;
  children: ReactNode;
}) {
  return (
    <label className="block min-w-0">
      <span className="mb-1.5 flex items-baseline gap-1.5 text-sm font-medium">
        {label}
        {optional && <span className="text-xs font-normal text-muted">optional</span>}
      </span>
      {children}
      {error ? (
        <span className="mt-1.5 flex items-start gap-1 text-xs text-critical-ink" role="alert">
          <StatusIcon status="fail" className="mt-px size-3.5" />
          {error}
        </span>
      ) : (
        hint && <span className="mt-1.5 block text-xs text-muted">{hint}</span>
      )}
    </label>
  );
}

export const inputClass =
  "w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink shadow-xs outline-none transition " +
  "placeholder:text-muted hover:border-ink-2/40 focus:border-accent focus:ring-3 focus:ring-accent/20 " +
  "aria-[invalid=true]:border-critical aria-[invalid=true]:focus:ring-critical/20 " +
  "disabled:cursor-not-allowed disabled:bg-surface-2 disabled:text-ink-2 disabled:hover:border-line";

const Chevron = ({ className = "" }: { className?: string }) => (
  <svg viewBox="0 0 16 16" className={`size-4 shrink-0 ${className}`} fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
    <path d="m4 6 4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

/** Native <select> (keyboard + touch friendly) with consistent styling and a chevron. */
export function Select({ className = "", children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <div className="relative">
      <select {...props} className={`${inputClass} appearance-none pr-9 ${className}`}>
        {children}
      </select>
      <Chevron className="pointer-events-none absolute top-1/2 right-3 -translate-y-1/2 text-muted" />
    </div>
  );
}

/**
 * Number field with big −/+ steppers and a unit suffix. The text can be empty or out of
 * range while typing; it is clamped when the field loses focus.
 */
export function NumberInput({
  value,
  onChange,
  min = -Infinity,
  max = Infinity,
  step = 1,
  suffix,
  presets,
  disabled,
  className = "",
  ...props
}: Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "min" | "max" | "step"> & {
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  suffix?: string;
  presets?: number[];
}) {
  const [text, setText] = useState(String(value));
  const [focused, setFocused] = useState(false);
  const shown = focused ? text : String(value);
  const clamp = (v: number) => Math.min(max, Math.max(min, v));
  const bump = (dir: 1 | -1) => {
    const next = clamp((Number.isFinite(value) ? value : 0) + dir * step);
    onChange(next);
    setText(String(next));
  };
  // `step` only drives the −/+ buttons: on the <input> the browser would treat it as a rule
  // (min 1, step 5 → only 1, 6, 11… valid) and block submit with a native popup.
  // The input comes first in the DOM so a wrapping <label> focuses it, not the "−" button.
  const stepBtn =
    "flex w-9 shrink-0 items-center justify-center text-ink-2 transition hover:bg-surface-2 hover:text-ink active:bg-line disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent";

  return (
    <div className="grid gap-1.5">
      <div
        className={`flex h-10 overflow-hidden rounded-lg border border-line bg-surface shadow-xs transition focus-within:border-accent focus-within:ring-3 focus-within:ring-accent/20 ${disabled ? "bg-surface-2" : "hover:border-ink-2/40"} ${className}`}
      >
        <div className="relative order-2 flex min-w-0 flex-1 items-center">
          <input
            {...props}
            type="number"
            inputMode="numeric"
            min={Number.isFinite(min) ? min : undefined}
            max={Number.isFinite(max) ? max : undefined}
            step="any"
            disabled={disabled}
            value={shown}
            onFocus={(e) => {
              setText(String(value));
              setFocused(true);
              e.currentTarget.select();
            }}
            onBlur={() => {
              setFocused(false);
              const n = Number(text);
              const next = text.trim() === "" || !Number.isFinite(n) ? value : clamp(n);
              if (next !== value) onChange(next);
            }}
            onChange={(e) => {
              setText(e.target.value);
              const n = Number(e.target.value);
              if (e.target.value.trim() !== "" && Number.isFinite(n) && n >= min && n <= max) onChange(n);
            }}
            className={`tabular h-full w-full min-w-0 bg-transparent text-center text-sm text-ink outline-none disabled:cursor-not-allowed disabled:text-ink-2 ${suffix ? "pr-7" : ""}`}
          />
          {suffix && <span className="pointer-events-none absolute right-2 text-xs text-muted">{suffix}</span>}
        </div>
        <button type="button" tabIndex={-1} className={`${stepBtn} order-1 border-r border-line`} disabled={disabled || value <= min} onClick={() => bump(-1)} aria-label="Decrease">
          <svg viewBox="0 0 16 16" className="size-3.5" stroke="currentColor" strokeWidth="2" aria-hidden><path d="M3.5 8h9" strokeLinecap="round" /></svg>
        </button>
        <button type="button" tabIndex={-1} className={`${stepBtn} order-3 border-l border-line`} disabled={disabled || value >= max} onClick={() => bump(1)} aria-label="Increase">
          <svg viewBox="0 0 16 16" className="size-3.5" stroke="currentColor" strokeWidth="2" aria-hidden><path d="M3.5 8h9M8 3.5v9" strokeLinecap="round" /></svg>
        </button>
      </div>
      {presets && !disabled && (
        <div className="flex flex-wrap gap-1">
          {presets.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => onChange(p)}
              className={`tabular rounded-md border px-2 py-0.5 text-xs transition ${p === value ? "border-accent bg-accent/10 font-medium text-accent" : "border-line text-ink-2 hover:border-ink-2/40 hover:text-ink"}`}
            >
              {p}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** On/off switch. Clicking the label text toggles it too. */
export function Toggle({ checked, onChange, label, description, disabled, size = "md", title }: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: ReactNode;
  description?: ReactNode;
  disabled?: boolean;
  size?: "sm" | "md";
  title?: string;
}) {
  // Not a <label>: a label around a <button> re-dispatches the click and toggles it twice.
  const id = useId();
  const track = size === "sm" ? "h-4 w-7" : "h-5 w-9";
  const knob = size === "sm" ? "size-3 data-[on=true]:translate-x-3" : "size-4 data-[on=true]:translate-x-4";
  const toggle = () => !disabled && onChange(!checked);
  return (
    <div
      title={title}
      className={`inline-flex items-start gap-2.5 ${disabled ? "cursor-not-allowed opacity-50" : ""} ${size === "sm" ? "text-xs" : "text-sm"}`}
    >
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-labelledby={label ? `${id}-label` : undefined}
        disabled={disabled}
        onClick={toggle}
        className={`relative mt-px inline-flex shrink-0 items-center rounded-full p-0.5 transition-colors disabled:cursor-not-allowed ${track} ${checked ? "bg-accent" : "bg-line hover:bg-ink-2/30"}`}
      >
        <span data-on={checked} className={`rounded-full bg-white shadow-sm transition-transform duration-150 ${knob}`} />
      </button>
      {(label || description) && (
        <span className={`min-w-0 leading-tight ${disabled ? "" : "cursor-pointer"}`} onClick={toggle}>
          {label && <span id={`${id}-label`} className="text-ink">{label}</span>}
          {description && <span className="mt-0.5 block text-xs text-muted">{description}</span>}
        </span>
      )}
    </div>
  );
}

/** Pill-style single choice (radiogroup with arrow-key navigation). */
export function Segmented<T extends string | number>({ value, onChange, options, size = "md", label }: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: ReactNode }[];
  size?: "sm" | "md";
  label: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const idx = options.findIndex((o) => o.value === value);
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-lg border border-line bg-surface-2 p-0.5">
      {options.map((o, i) => {
        const on = o.value === value;
        return (
          <button
            key={String(o.value)}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={on}
            tabIndex={on || (idx < 0 && i === 0) ? 0 : -1}
            onClick={() => onChange(o.value)}
            onKeyDown={(e) => {
              const d = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
              if (!d) return;
              e.preventDefault();
              e.stopPropagation();
              const n = (i + d + options.length) % options.length;
              onChange(options[n].value);
              refs.current[n]?.focus();
            }}
            className={`rounded-md font-medium transition ${size === "sm" ? "px-2 py-0.5 text-xs" : "px-3 py-1 text-sm"} ${on ? "bg-surface text-ink shadow-sm" : "text-muted hover:text-ink"}`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export type ComboOption = { value: string; label?: ReactNode; meta?: ReactNode; keywords?: string };

/**
 * Free-text input with a filterable suggestion list (replaces <datalist>, which is
 * unstyled and behaves differently in every browser). ↑/↓ to move, Enter to pick, Esc to close.
 */
export function Combobox({ value, onChange, options, placeholder, className = "", invalid, emptyText = "No matches", ...props }: {
  value: string;
  onChange: (v: string) => void;
  options: ComboOption[];
  placeholder?: string;
  className?: string;
  invalid?: boolean;
  emptyText?: ReactNode;
} & Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange">) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [typed, setTyped] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const id = useId();

  // Show everything until the user types; then filter by what they typed.
  const filtered = useMemo(() => {
    const q = value.trim().toLowerCase();
    if (!typed || !q) return options;
    return options.filter((o) => `${o.value} ${o.keywords ?? ""}`.toLowerCase().includes(q));
  }, [options, value, typed]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => !wrap.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [open]);

  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  const pick = (v: string) => {
    onChange(v);
    setOpen(false);
    setTyped(false);
  };

  return (
    <div ref={wrap} className="relative">
      <input
        {...props}
        role="combobox"
        aria-expanded={open}
        aria-controls={`${id}-list`}
        aria-autocomplete="list"
        aria-activedescendant={open && filtered[active] ? `${id}-${active}` : undefined}
        aria-invalid={invalid || undefined}
        autoComplete="off"
        value={value}
        placeholder={placeholder}
        className={`${inputClass} pr-9 ${className}`}
        onFocus={() => {
          setOpen(true);
          setTyped(false);
        }}
        onClick={() => setOpen(true)}
        onChange={(e) => {
          onChange(e.target.value);
          setTyped(true);
          setOpen(true);
          setActive(0);
        }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            if (!open) return setOpen(true);
            const d = e.key === "ArrowDown" ? 1 : -1;
            setActive((a) => (filtered.length ? (a + d + filtered.length) % filtered.length : 0));
          } else if (e.key === "Enter" && open && filtered[active]) {
            e.preventDefault();
            pick(filtered[active].value);
          } else if (e.key === "Escape" && open) {
            e.preventDefault();
            e.stopPropagation();
            setOpen(false);
          } else if (e.key === "Tab") setOpen(false);
        }}
      />
      <button
        type="button"
        tabIndex={-1}
        aria-label={open ? "Hide suggestions" : "Show suggestions"}
        onClick={() => {
          setOpen((o) => !o);
          setTyped(false);
          wrap.current?.querySelector("input")?.focus();
        }}
        className="absolute inset-y-0 right-0 flex w-9 items-center justify-center text-muted hover:text-ink"
      >
        <Chevron className={`transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && options.length > 0 && (
        <ul
          ref={list}
          id={`${id}-list`}
          role="listbox"
          className="pop-in absolute z-30 mt-1 max-h-64 w-full overflow-auto rounded-lg border border-line bg-surface p-1 shadow-lg"
        >
          {filtered.length === 0 ? (
            <li className="px-3 py-2 text-sm text-muted">{emptyText}</li>
          ) : (
            filtered.map((o, i) => {
              const selected = o.value === value;
              return (
                <li
                  key={o.value}
                  id={`${id}-${i}`}
                  data-index={i}
                  role="option"
                  aria-selected={selected}
                  onPointerDown={(e) => e.preventDefault()}
                  onClick={() => pick(o.value)}
                  onPointerMove={() => setActive(i)}
                  className={`flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm ${i === active ? "bg-surface-2" : ""}`}
                >
                  <span className="min-w-0 flex-1 truncate font-mono">{o.label ?? o.value}</span>
                  {o.meta && <span className="shrink-0 text-xs text-muted">{o.meta}</span>}
                  <svg viewBox="0 0 16 16" className={`size-4 shrink-0 text-accent ${selected ? "" : "invisible"}`} fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                    <path d="m3.5 8.5 3 3 6-7" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </li>
              );
            })
          )}
        </ul>
      )}
    </div>
  );
}

/** Search box with an icon, a clear button and an optional "/" shortcut to focus it. */
export function SearchInput({ value, onChange, placeholder, shortcut = true, className = "" }: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  shortcut?: boolean;
  className?: string;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!shortcut) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || (e.target as HTMLElement).closest("input, textarea, select, [contenteditable]")) return;
      e.preventDefault();
      ref.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [shortcut]);
  return (
    <div className={`relative ${className}`}>
      <svg viewBox="0 0 16 16" className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden>
        <circle cx="7" cy="7" r="4.5" />
        <path d="m10.5 10.5 3 3" strokeLinecap="round" />
      </svg>
      <input
        ref={ref}
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            onChange("");
            e.currentTarget.blur();
          }
        }}
        placeholder={placeholder}
        aria-label={placeholder}
        className={`${inputClass} px-9 [&::-webkit-search-cancel-button]:hidden`}
      />
      {value ? (
        <button
          type="button"
          onClick={() => {
            onChange("");
            ref.current?.focus();
          }}
          aria-label="Clear search"
          className="absolute top-1/2 right-2 flex size-6 -translate-y-1/2 items-center justify-center rounded text-muted hover:bg-surface-2 hover:text-ink"
        >
          <svg viewBox="0 0 16 16" className="size-3.5" stroke="currentColor" strokeWidth="2" aria-hidden><path d="m4 4 8 8m0-8-8 8" strokeLinecap="round" /></svg>
        </button>
      ) : (
        shortcut && <span className="pointer-events-none absolute top-1/2 right-2.5 -translate-y-1/2 text-muted"><Kbd>/</Kbd></span>
      )}
    </div>
  );
}

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
      className="m-auto w-[min(520px,calc(100vw-32px))] rounded-lg border border-line bg-surface p-0 text-ink shadow-2xl backdrop:bg-black/50"
    >
      <div className="flex items-center justify-between gap-3 border-b border-line py-2 pr-2 pl-5">
        <span className="font-semibold">{title}</span>
        <IconButton label="Close" onClick={onClose}>
          <svg viewBox="0 0 16 16" className="size-4" stroke="currentColor" strokeWidth="1.8" aria-hidden><path d="m4 4 8 8m0-8-8 8" strokeLinecap="round" /></svg>
        </IconButton>
      </div>
      <div className="p-4">{children}</div>
    </dialog>
  );
}

export function ErrorBox({ children }: { children: ReactNode }) {
  return (
    <div role="alert" className="flex gap-2 rounded-lg border border-critical/40 bg-critical/10 px-3 py-2 text-sm text-critical-ink">
      <StatusIcon status="fail" className="mt-0.5" />
      <div className="min-w-0">{children}</div>
    </div>
  );
}
