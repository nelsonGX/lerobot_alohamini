"use client";

import { useCallback, useEffect, useRef, useState, type DependencyList, type SetStateAction } from "react";

/** Poll without overlapping requests. Ignore responses from a previous target or unmounted view. */
export function usePoll<T>(fetcher: () => Promise<T>, interval: number, deps: DependencyList = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fn = useRef(fetcher);
  const generation = useRef(0);
  const pending = useRef<number | null>(null);
  useEffect(() => { fn.current = fetcher; }, [fetcher]);
  const refresh = useCallback(async () => {
    const gen = generation.current;
    if (pending.current === gen) return;
    pending.current = gen;
    try { const value = await fn.current(); if (gen === generation.current) { setData(value); setError(null); } }
    catch (e) { if (gen === generation.current) setError(e instanceof Error ? e.message : String(e)); }
    finally { if (pending.current === gen) pending.current = null; }
  }, []);
  useEffect(() => {
    const gen = ++generation.current;
    void refresh();
    const timer = setInterval(refresh, interval);
    return () => { generation.current = gen + 1; clearInterval(timer); };
    // Callers use deps for additional refresh triggers, such as a recording phase change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetcher, interval, refresh, ...deps]);
  return { data, error, refresh };
}

export function useLocalStorage<T>(key: string, initial: T): [T, (value: SetStateAction<T>) => void] {
  const [value, setValue] = useState(initial);
  useEffect(() => {
    // Read browser-only preferences after hydration so the server and first render agree.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    try { const stored = localStorage.getItem(key); if (stored !== null) setValue(JSON.parse(stored)); } catch { /* Storage may be disabled. */ }
  }, [key]);
  const update = useCallback((next: SetStateAction<T>) => {
    setValue(prev => {
      const result = typeof next === "function" ? (next as (v: T) => T)(prev) : next;
      try { localStorage.setItem(key, JSON.stringify(result)); } catch { /* Keep the in-memory value. */ }
      return result;
    });
  }, [key]);
  return [value, update];
}
