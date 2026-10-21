"use client";

import { useCallback, useEffect, useState } from "react";
import type { Check, CollectionState, EditJob, HostSnapshot, LogLine, ProcName, ProcSnapshot, RecorderState, RobotState, UploadState } from "./api";

type Topics = {
  recorder: RecorderState; robot: RobotState; host: { snapshot: HostSnapshot | null; age_s: number | null };
  cameras: { cameras: { name: string; live: boolean }[]; error?: string }; preflight: { checks: Check[] };
  collection: CollectionState; upload: UploadState; job: EditJob;
  "recorder.log": { reset: boolean; lines: LogLine[] };
} & Record<`proc:${ProcName}`, { proc: ProcSnapshot | null }>;
type Message = { data?: unknown; error?: string };
const listeners = new Map<string, Set<(message: Message) => void>>();
const cache = new Map<string, Message>();
let socket: WebSocket | null = null;
let retry: ReturnType<typeof setTimeout> | null = null;

function send(message: object) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message)); }
function open() {
  if (socket || !listeners.size) return;
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`);
  socket = ws;
  ws.onopen = () => { for (const sub of listeners.keys()) send({ sub }); };
  ws.onmessage = event => {
    try {
      const message = JSON.parse(event.data) as Message & { topic: string };
      cache.set(message.topic, message);
      listeners.get(message.topic)?.forEach(fn => fn(message));
    } catch { /* Ignore malformed frames; reconnect handles transport failure. */ }
  };
  ws.onclose = () => {
    if (socket !== ws) return;
    socket = null;
    for (const group of listeners.values()) group.forEach(fn => fn({ error: "Backend offline" }));
    if (listeners.size) retry = setTimeout(open, 1500);
  };
  ws.onerror = () => ws.close();
}

export const procTopic = (name: ProcName): `proc:${ProcName}` => `proc:${name}`;
export function useTopic<K extends keyof Topics>(topic: K) {
  const [state, setState] = useState<{ data: Topics[K] | null; error: string | null }>({ data: null, error: null });
  useEffect(() => {
    const receive = (msg: Message) => setState(prev => ({ data: msg.data === undefined ? prev.data : msg.data as Topics[K], error: msg.error ?? null }));
    let group = listeners.get(topic);
    if (!group) { group = new Set(); listeners.set(topic, group); }
    group.add(receive);
    if (cache.has(topic)) receive(cache.get(topic)!);
    open(); send({ sub: topic });
    return () => {
      group.delete(receive);
      if (!group.size) { listeners.delete(topic); cache.delete(topic); send({ unsub: topic }); }
      if (!listeners.size) { if (retry) clearTimeout(retry); const ws = socket; socket = null; ws?.close(); }
    };
  }, [topic]);
  const refresh = useCallback(() => { send({ refresh: topic }); }, [topic]);
  return { ...state, refresh };
}

export function useRecorderLog() {
  const { data } = useTopic("recorder.log");
  const [lines, setLines] = useState<LogLine[]>([]);
  const [previous, setPrevious] = useState(data);
  if (data !== previous) {
    setPrevious(data);
    if (data) setLines(old => (data.reset ? data.lines : [...old, ...data.lines]).slice(-2000));
  }
  return lines;
}
