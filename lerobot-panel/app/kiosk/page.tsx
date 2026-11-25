"use client";

/* Shop-assistant face for the iPad on the robot (server: server/kiosk.py).
 * Mic -> AssemblyAI streaming (direct, temporary token) -> final transcript -> /api/kiosk/ws -> Claude + tools
 * -> sentences with ElevenLabs mp3 -> played here in order. ?debug=1 opens the drawer, ?monitor=1 watches
 * from another screen without mic or sound. */

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Contactless, PAY_KEYFRAMES } from "./contactless";
import { LIPS_STD, nearNow, newVision, startVision, summarize, type Vision, type VisionSummary } from "./vision";

type Item = { id: string; name: string; description: string; price: string; position: string; on_table: boolean; back_stock: number; sold: number; checkpoint: string; task: string; episode_time_s: number };
type Config = { shop_name: string; language: string; model: string; effort: "low" | "medium" | "high"; tts_model: string; voice_id: string; extra_instructions: string; armed: boolean; simulate_picks: boolean; idle_reset_s: number; mic_gate_db: number; barge_in_chars: number; duck_gain: number; addressee_model: string; camera_listen: boolean; face_min_size: number; camera_zoom: number; camera_crop_y: number; greet_on_arrival: boolean; leave_reset_s: number; customer_photos: boolean; pick_watch_model: string; pick_watch_after_s: number; pick_watch_every_s: number; pick_watch_misses: number; items: Item[] };
type Pick = { job_id: string; item_id: string; item_name: string; state: string; detail: string; active: boolean; elapsed_s: number; time_limit_s: number; simulated: boolean; episode_started_at: number | null; verdict: "in_box" | "missed" | null; last_check: string };
type Payment = { id: string; item_id: string; item_name: string; amount: string; state: "pending" | "paid" | "declined" | "cancelled"; reason: string; created_at: number; ended_at: number | null };
type Note = { at: number; message: string; urgent: boolean; source: string };
type LogEvent = { type: string; at: number; [k: string]: unknown };
type Keys = { anthropic: boolean; assemblyai: boolean; elevenlabs: boolean };
type Standby = { job_id: string; state: "off" | "loading" | "ready" | "busy" | "stopping" | "error"; detail: string; policies: string[]; started_at: number | null; ready_at: number | null };

const FACE: Record<string, [string, string]> = {
  // [resting, mouth open] — the second one alternates while talking.
  happy: [":)", ":D"], excited: [":D", ":O"], thinking: [":/", ":o"], sad: [":(", ":C"],
  surprised: [":O", ":o"], love: [":3", ":D"], neutral: [":|", ":o"],
};
// Said while the robot talks, these interrupt it at once (otherwise it takes config.barge_in_chars).
const STOP_WORDS = /停|等一下|等等|暫停|不要了|stop|wait|hold on/i;
const GATE_OFF_DB = -80;
const FRAME_MS = 50; // one mic level reading per worklet frame
const LOUD_MS = 250; // a turn needs this much speech above the gate to count
const TURN_LEAD_MS = 2000; // speech starts this long before its first transcript arrives
const DUCK_MAX_MS = 6000; // give up waiting and come back to full volume

type Levels = { t: number[]; db: number[] };

/** Milliseconds of mic input louder than `gate` since `since` (Date.now() time). */
function loudMs(levels: Levels, since: number, gate: number) {
  let n = 0;
  for (let i = levels.t.length - 1; i >= 0 && levels.t[i] >= since; i--) if (levels.db[i] >= gate) n++;
  return n * FRAME_MS;
}

function speechLength(text: string) {
  // Chinese has no spaces: count CJK characters, plus Latin words.
  const cjk = (text.match(/[㐀-鿿]/g) ?? []).length;
  const words = text.replace(/[㐀-鿿]/g, " ").trim().split(/\s+/).filter(Boolean).length;
  return cjk + words * 2;
}

/** `part` (a partial transcript) looks like the customer saying `said` again. */
function repeats(part: string, said: string) {
  const norm = (t: string) => t.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
  const a = norm(part), b = norm(said);
  return a.length > 0 && b.includes(a);
}

export default function KioskPage() {
  const search = useSyncExternalStore(() => () => {}, () => location.search, () => "");
  const params = new URLSearchParams(search);
  const monitor = params.get("monitor") === "1";
  const [startedTap, setStarted] = useState(false);
  const started = startedTap || monitor;
  const [debugToggle, setDebug] = useState<boolean | null>(null);
  const debug = debugToggle ?? params.get("debug") === "1";
  const [wsUp, setWsUp] = useState(false);
  const [stt, setStt] = useState("off");
  const [partial, setPartial] = useState("");
  const [serverState, setServerState] = useState("idle");
  const [emotion, setEmotion] = useState("happy");
  const [speaking, setSpeaking] = useState(false);
  const [caption, setCaption] = useState("");
  const [heard, setHeard] = useState("");
  const [log, setLog] = useState<LogEvent[]>([]);
  const [pick, setPick] = useState<Pick | null>(null);
  const [payment, setPayment] = useState<Payment | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [notes, setNotes] = useState<Note[]>([]);
  const [keys, setKeys] = useState<Keys | null>(null);
  const [standby, setStandby] = useState<Standby | null>(null);
  const [halfDuplex, setHalfDuplex] = useState(false);
  const [blink, setBlink] = useState(false);
  const [mouth, setMouth] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ws = useRef<WebSocket | null>(null);
  const ctx = useRef<AudioContext | null>(null);
  const sources = useRef<Set<AudioBufferSourceNode>>(new Set());
  const outGain = useRef<GainNode | null>(null);
  const ducked = useRef(false);
  const lastSaid = useRef(""); // the customer's last accepted utterance: saying it again doesn't duck the robot
  const duckTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const playAt = useRef(0);
  const chain = useRef<Promise<void>>(Promise.resolve());
  const ignored = useRef<Set<string>>(new Set());
  const seenTurns = useRef<Set<string>>(new Set());
  const speakingRef = useRef(false);
  const worklet = useRef<AudioWorkletNode | null>(null);
  const sttEndAt = useRef<number | null>(null);
  const halfDuplexRef = useRef(false);
  const monitorRef = useRef(false);
  const cfgLang = useRef("zh-TW");
  const gateRef = useRef({ db: -45, chars: 6, duck: 0.2 });
  const levels = useRef<Levels>({ t: [], db: [] });
  const vision = useRef<Vision>(newVision());
  const camRef = useRef({ on: true, minSize: 0.06, zoom: 1.5, cropY: 0.3 });
  useEffect(() => {
    camRef.current = { on: config?.camera_listen ?? true, minSize: config?.face_min_size ?? 0.06, zoom: config?.camera_zoom ?? 1.5, cropY: config?.camera_crop_y ?? 0.3 };
  }, [config?.camera_listen, config?.face_min_size, config?.camera_zoom, config?.camera_crop_y]);
  useEffect(() => { gateRef.current = { db: config?.mic_gate_db ?? -45, chars: config?.barge_in_chars ?? 6, duck: config?.duck_gain ?? 0.2 }; }, [config?.mic_gate_db, config?.barge_in_chars, config?.duck_gain]);
  useEffect(() => { halfDuplexRef.current = halfDuplex; }, [halfDuplex]);
  useEffect(() => { cfgLang.current = config?.language ?? "zh-TW"; }, [config?.language]);
  useEffect(() => { monitorRef.current = monitor; }, [monitor]);

  const send = useCallback((msg: object) => {
    if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(JSON.stringify(msg));
  }, []);
  const addLog = useCallback((ev: LogEvent) => setLog((l) => [...l.slice(-300), ev]), []);

  // ---- playback

  // Someone is talking over the robot: turn it down (not off) until we know whether it was meant for us.
  const duck = useCallback((on: boolean) => {
    const set = (down: boolean) => {
      ducked.current = down;
      const g = outGain.current;
      if (g) g.gain.setTargetAtTime(down ? gateRef.current.duck : 1, g.context.currentTime, down ? 0.05 : 0.15);
    };
    clearTimeout(duckTimer.current);
    set(on);
    if (on) duckTimer.current = setTimeout(() => set(false), DUCK_MAX_MS);
  }, []);

  const stopAudio = useCallback(() => {
    for (const s of sources.current) { try { s.stop(); } catch { /* already stopped */ } }
    sources.current.clear();
    if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();
    for (const t of seenTurns.current) ignored.current.add(t);
    chain.current = Promise.resolve();
    playAt.current = 0;
    ducked.current = false;
    clearTimeout(duckTimer.current);
    if (outGain.current) outGain.current.gain.value = 1;
  }, []);

  const enqueueSpeech = useCallback((turn: string, text: string, audio: string | null, lang: string) => {
    if (monitorRef.current || ignored.current.has(turn)) return;
    seenTurns.current.add(turn);
    chain.current = chain.current.then(async () => {
      if (ignored.current.has(turn)) return;
      const ac = ctx.current;
      if (audio && ac) {
        const bytes = Uint8Array.from(atob(audio), (c) => c.charCodeAt(0));
        const buffer = await ac.decodeAudioData(bytes.buffer);
        if (ignored.current.has(turn)) return;
        const src = ac.createBufferSource();
        src.buffer = buffer;
        src.connect(outGain.current ?? ac.destination);
        const at = Math.max(ac.currentTime + 0.02, playAt.current);
        src.start(at);
        playAt.current = at + buffer.duration;
        sources.current.add(src);
        src.onended = () => sources.current.delete(src);
        setTimeout(() => { if (!ignored.current.has(turn)) setCaption(text); }, Math.max(0, (at - ac.currentTime) * 1000));
      } else if (typeof speechSynthesis !== "undefined") {
        // No ElevenLabs key (or it failed): the browser's own voice.
        await new Promise<void>((resolve) => {
          const u = new SpeechSynthesisUtterance(text);
          u.lang = lang;
          u.onend = u.onerror = () => resolve();
          setCaption(text);
          speechSynthesis.speak(u);
        });
      }
    }).catch((e) => send({ type: "client_log", message: `playback: ${e}` }));
  }, [send]);

  // Is sound coming out right now?
  useEffect(() => {
    const id = setInterval(() => {
      const ac = ctx.current;
      const now = !!ac && playAt.current > ac.currentTime + 0.05;
      const tts = typeof speechSynthesis !== "undefined" && speechSynthesis.speaking;
      const on = now || tts;
      if (on !== speakingRef.current) {
        speakingRef.current = on;
        setSpeaking(on);
        worklet.current?.port.postMessage({ muted: on && halfDuplexRef.current });
      }
    }, 80);
    return () => clearInterval(id);
  }, []);

  // Someone walked up / left (from the camera): the server greets them and starts fresh after they leave.
  useEffect(() => {
    if (monitor) return;
    let present = false;
    const id = setInterval(() => {
      const v = vision.current;
      if (v.state !== "on") return;
      const recent = summarize(v, Date.now() - 1500);
      const longer = summarize(v, Date.now() - 4000);
      if (!present && recent && recent.present >= 0.8) present = true;
      else if (present && longer && longer.present === 0) present = false;
      else return;
      send({ type: "presence", present, facing: recent?.facing ?? 0 });
    }, 500);
    return () => clearInterval(id);
  }, [monitor, send]);

  // Blinking and talking animation.
  useEffect(() => {
    let t: ReturnType<typeof setTimeout>;
    const loop = () => { setBlink(true); setTimeout(() => setBlink(false), 140); t = setTimeout(loop, 2500 + Math.random() * 3000); };
    t = setTimeout(loop, 2000);
    return () => clearTimeout(t);
  }, []);
  useEffect(() => {
    if (!speaking) return;
    const id = setInterval(() => setMouth((m) => !m), 150);
    return () => clearInterval(id);
  }, [speaking]);

  // ---- server connection

  useEffect(() => {
    let closed = false;
    let retry: ReturnType<typeof setTimeout>;
    const open = () => {
      const sock = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/kiosk/ws`);
      ws.current = sock;
      sock.onopen = () => setWsUp(true);
      sock.onclose = () => { setWsUp(false); if (!closed) retry = setTimeout(open, 1500); };
      sock.onerror = () => sock.close();
      sock.onmessage = (e) => {
        const ev = JSON.parse(e.data) as LogEvent;
        switch (ev.type) {
          case "snapshot":
            setLog((ev.log as LogEvent[]) ?? []);
            setServerState(ev.state as string);
            setPick(ev.pick as Pick | null);
            setPayment((ev.payment as Payment | null) ?? null);
            setNotes((ev.notifications as Note[]) ?? []);
            setConfig(ev.config as Config);
            setKeys(ev.keys as Keys);
            setStandby(ev.standby as Standby);
            return;
          case "state": setServerState(ev.state as string); return;
          case "face": setEmotion(ev.face as string); return;
          case "speech": enqueueSpeech(ev.turn as string, ev.text as string, ev.audio as string | null, (cfgLang.current || "zh-TW")); if (ev.error) addLog({ type: "error", at: ev.at, where: "tts", message: ev.error }); return;
          case "pick": setPick(ev.pick as Pick); break;
          case "payment": setPayment(ev.payment as Payment); break;
          case "config": setConfig(ev.config as Config); return;
          case "standby": setStandby(ev.standby as Standby); return;
          case "notification": setNotes((n) => [ev as unknown as Note, ...n].slice(0, 50)); break;
          case "notifications_cleared": setNotes([]); return;
          case "photo_request": { // Claude's look_at_customer: one front-camera frame, as the camera sees it
            if (monitorRef.current) return;
            const video = vision.current.video;
            if (vision.current.state !== "on" || !video) { send({ type: "photo", id: ev.id, error: `camera ${vision.current.state}` }); return; }
            const c = document.createElement("canvas");
            c.width = Math.min(800, video.videoWidth);
            c.height = Math.round((c.width * video.videoHeight) / video.videoWidth);
            c.getContext("2d")?.drawImage(video, 0, 0, c.width, c.height);
            send({ type: "photo", id: ev.id, image: c.toDataURL("image/jpeg", 0.75).split(",")[1] });
            return;
          }
          case "interrupted": stopAudio(); break;
          case "reset": if (!ev.keep_audio) stopAudio(); lastSaid.current = ""; setCaption(""); setHeard(""); setEmotion("happy"); break;
          case "user": stopAudio(); setHeard(ev.text as string); lastSaid.current = ev.text as string; break; // accepted: the robot stops and listens
          case "noted": duck(false); setHeard(ev.text as string); break; // a repeat or "嗯": remembered, the robot keeps talking
          case "ignored": duck(false); break; // not for us: back to full volume
        }
        addLog(ev);
      };
    };
    open();
    return () => { closed = true; clearTimeout(retry); ws.current?.close(); };
  }, [addLog, enqueueSpeech, stopAudio, duck, send]);

  // ---- microphone -> AssemblyAI

  const startStt = useCallback(async (ac: AudioContext) => {
    // Automatic gain would boost far-away voices to the customer's level, which defeats the loudness gate.
    const gated = gateRef.current.db > GATE_OFF_DB;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: !gated, channelCount: 1 } });
    await ac.audioWorklet.addModule("/pcm-worklet.js");
    const node = new AudioWorkletNode(ac, "pcm16-downsampler", { processorOptions: { targetRate: 16000 } });
    const silent = ac.createGain();
    silent.gain.value = 0;
    ac.createMediaStreamSource(stream).connect(node).connect(silent).connect(ac.destination);
    worklet.current = node;

    let sock: WebSocket | null = null;
    const connect = async () => {
      setStt("connecting");
      try {
        const res = await fetch("/api/kiosk/stt-token");
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail ?? res.statusText);
        sock = new WebSocket(data.url);
        sock.binaryType = "arraybuffer";
        sock.onopen = () => setStt("listening");
        sock.onclose = () => { setStt("reconnecting"); sock = null; setTimeout(connect, 1000); };
        sock.onerror = () => sock?.close();
        let turnOrder = -1;
        let turnSince = 0;
        sock.onmessage = (e) => {
          const msg = JSON.parse(e.data);
          if (msg.type !== "Turn") return;
          const text: string = msg.transcript ?? "";
          if (msg.turn_order !== turnOrder) { turnOrder = msg.turn_order; turnSince = Date.now() - TURN_LEAD_MS; }
          // Near (the customer at the iPad) or far (people chatting nearby)?
          const gate = gateRef.current;
          const loud = gate.db <= GATE_OFF_DB || loudMs(levels.current, turnSince, gate.db) >= LOUD_MS;
          // Camera (null when it isn't running, then only the mic decides): was someone at the counter?
          const seen: VisionSummary | null = summarize(vision.current, turnSince);
          const stop = STOP_WORDS.test(text);
          if (msg.end_of_turn) {
            setPartial("");
            if (!text.trim()) return;
            if (!loud) {
              send({ type: "client_log", message: `ignored (quieter than ${gate.db} dB): ${text}` });
              return;
            }
            if (seen && seen.present < 0.3 && !stop) {
              send({ type: "client_log", message: `ignored (no one at the counter): ${text}` });
              if (ducked.current) duck(false);
              return;
            }
            sttEndAt.current = Date.now();
            // The server checks it was said to the robot before stopping the robot's speech.
            send({ type: "user_text", text, mic: true, robot_talking: speakingRef.current || ducked.current, vision: seen, metrics: { stt_confidence: msg.end_of_turn_confidence } });
            return;
          }
          if (!loud) return;
          if (!stop && nearNow(vision.current) === false) return; // nobody in front: don't even react
          setPartial(text);
          if (!speakingRef.current) return;
          if (stop) {
            stopAudio();
            send({ type: "interrupt" });
          } else if (!ducked.current && speechLength(text) >= gate.chars && !repeats(text, lastSaid.current)) {
            duck(true);
          }
        };
      } catch (err) {
        setStt(`error: ${err instanceof Error ? err.message : err}`);
        setTimeout(connect, 5000);
      }
    };
    node.port.onmessage = (e) => {
      const pcm: ArrayBuffer = e.data instanceof ArrayBuffer ? e.data : e.data.pcm; // older cached worklet: bare PCM
      if (typeof e.data.level === "number") {
        const l = levels.current;
        l.t.push(Date.now());
        l.db.push(e.data.level);
        if (l.t.length > 400) { l.t.splice(0, 100); l.db.splice(0, 100); } // ~15 s of history
      }
      if (sock?.readyState === WebSocket.OPEN) sock.send(pcm);
    };
    await connect();
  }, [send, stopAudio, duck]);

  const start = async () => {
    setError(null);
    try {
      const ac = new AudioContext();
      await ac.resume(); // iOS: audio must be unlocked inside the tap
      ctx.current = ac;
      outGain.current = ac.createGain();
      outGain.current.connect(ac.destination);
      // Keep the iPad screen on.
      try { await (navigator as Navigator & { wakeLock?: { request: (t: string) => Promise<unknown> } }).wakeLock?.request("screen"); } catch { /* optional */ }
      setStarted(true);
      if (camRef.current.on) void startVision(vision.current, () => camRef.current);
      await startStt(ac);
    } catch (e) {
      setError(`${e instanceof Error ? e.message : e}. The microphone needs HTTPS: open https://<panel-ip>/kiosk (nginx).`);
    }
  };

  // ---- face

  const tool = serverState.startsWith("tool:") ? serverState.slice(5) : null;
  const moving = !!pick?.active;
  let face = (FACE[emotion] ?? FACE.happy)[speaking && mouth ? 1 : 0];
  let label = "";
  if (!started) { face = "-_-"; label = "點一下開始 · tap to start"; }
  else if (speaking) { /* emotion face with moving mouth */ }
  else if (partial) { face = ":o"; }
  else if (tool === "look_at_table") { face = "o_o"; label = "看一下桌上…"; }
  else if (tool) { face = "•_•"; label = "查詢中…"; }
  else if (serverState === "thinking") { face = ":/"; label = "…"; }
  else if (moving) { face = "^_^"; label = `正在拿${pick?.item_name ?? ""}…`; }
  else if (payment?.state === "pending") { label = "請感應付款 · tap to pay"; }
  if (blink && !speaking) face = face.replace(/^[:;]/, "·");

  const progress = moving && pick ? Math.min(1, pick.elapsed_s / Math.max(1, pick.time_limit_s)) : 0;

  return (
    <div className="fixed inset-0 z-50 flex select-none bg-black text-white" style={{ WebkitUserSelect: "none" }}>
      <div className="relative flex flex-1 flex-col items-center justify-center" style={{ containerType: "inline-size" }} onClick={!started ? start : undefined}>
        <div className="font-mono font-bold leading-none tracking-tight transition-transform" style={{ fontSize: "min(30cqw, 48vh)", transform: partial ? "scale(1.04)" : "none" }}>
          {face}
        </div>
        <div className="mt-6 h-10 text-2xl text-white/60">{label}</div>
        {payment?.state === "pending" && !moving && (
          <div className="mt-4 flex items-center gap-6 rounded-3xl bg-white/[0.06] py-4 pl-5 pr-9 ring-1 ring-white/10" style={{ animation: "pay-in .4s ease-out" }}>
            <style>{PAY_KEYFRAMES}</style>
            <div className="text-[#4d94ff]"><Contactless size={88} live /></div>
            <div className="text-left">
              <div className="text-base text-white/50">{payment.item_name}</div>
              <div className="text-5xl font-semibold tracking-tight tabular-nums">{payment.amount}</div>
            </div>
          </div>
        )}
        {moving && <div className="mt-2 h-2 w-1/3 overflow-hidden rounded-full bg-white/15"><div className="h-full bg-white/70 transition-all" style={{ width: `${progress * 100}%` }} /></div>}
        <div className="absolute inset-x-0 bottom-6 px-10 text-center">
          {partial ? <p className="text-2xl text-white/50">{partial}</p> : heard && !speaking ? <p className="text-xl text-white/30">「{heard}」</p> : null}
          {caption && (speaking || serverState !== "idle") && <p className="mt-2 text-3xl text-white/85">{caption}</p>}
        </div>
        {error && <p className="absolute top-6 mx-8 rounded-lg bg-red-900/80 px-4 py-2 text-lg">{error}</p>}
        {config && started && (!config.armed || standby?.state === "loading" || standby?.state === "error") && (
          <span className="absolute left-4 top-4 rounded-full bg-white/10 px-3 py-1 text-xs text-white/50">
            {!config.armed ? "arm not armed" : standby?.state === "loading" ? "warming up…" : "robot error (see debug)"}
          </span>
        )}
        {!wsUp && <span className="absolute left-4 top-10 rounded-full bg-red-900/70 px-3 py-1 text-xs">backend offline</span>}
        <button onClick={(e) => { e.stopPropagation(); setDebug(!debug); }} className="absolute bottom-3 right-3 rounded-md px-3 py-2 font-mono text-xs text-white/25 hover:text-white/70">
          debug
        </button>
      </div>
      {debug && (
        <Debug
          log={log} pick={pick} payment={payment} config={config} standby={standby} notes={notes} keys={keys} wsUp={wsUp} stt={stt} serverState={serverState}
          monitor={monitor} levels={levels} vision={vision} halfDuplex={halfDuplex} setHalfDuplex={setHalfDuplex} send={send} setConfig={setConfig} sttEndAt={sttEndAt}
        />
      )}
    </div>
  );
}

// ---------- debug drawer ----------

function Debug({ log, pick, payment, config, standby, notes, keys, wsUp, stt, serverState, monitor, levels, vision, halfDuplex, setHalfDuplex, send, setConfig }: {
  log: LogEvent[]; pick: Pick | null; payment: Payment | null; config: Config | null; standby: Standby | null; notes: Note[]; keys: Keys | null; wsUp: boolean; stt: string; serverState: string;
  monitor: boolean; levels: React.RefObject<Levels>; vision: React.RefObject<Vision>; halfDuplex: boolean; setHalfDuplex: (v: boolean) => void; send: (m: object) => void; setConfig: (c: Config) => void; sttEndAt: React.RefObject<number | null>;
}) {
  const [text, setText] = useState("");
  const [tab, setTab] = useState<"log" | "shop" | "setup">("log");
  const [saveError, setSaveError] = useState<string | null>(null);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [log.length, tab]);

  const save = async (next: Config) => {
    setConfig(next);
    setSaveError(null);
    const res = await fetch("/api/kiosk/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(next) });
    if (!res.ok) setSaveError(JSON.stringify((await res.json()).detail ?? res.statusText));
  };
  const setItem = (i: number, patch: Partial<Item>) => config && save({ ...config, items: config.items.map((it, j) => (j === i ? { ...it, ...patch } : it)) });
  const dot = (ok: boolean | undefined) => <span className={`inline-block size-2 rounded-full ${ok ? "bg-green-500" : "bg-red-500"}`} />;
  const box = "rounded border border-white/15 bg-white/5 px-2 py-1 text-sm text-white";

  return (
    <aside className="flex w-[min(460px,48vw)] flex-col border-l border-white/10 bg-neutral-950 text-sm text-white/90">
      <div className="grid gap-1 border-b border-white/10 p-3 font-mono text-xs">
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <span>{dot(wsUp)} server</span><span>{dot(stt === "listening")} stt: {monitor ? "monitor" : stt}</span><span>state: {serverState}</span>
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-white/60">
          <span>{dot(keys?.anthropic)} anthropic</span><span>{dot(keys?.assemblyai)} assemblyai</span><span>{dot(keys?.elevenlabs)} elevenlabs</span>
        </div>
      </div>

      <div className="grid gap-2 border-b border-white/10 p-3">
        <div className="flex items-center gap-2">
          <button onClick={() => config && save({ ...config, armed: !config.armed })} className={`flex-1 rounded-md px-3 py-2 font-semibold ${config?.armed ? "bg-green-700" : "bg-neutral-700"}`}>
            {config?.armed ? "Arm ARMED (tap to disarm)" : "Arm disarmed (tap to arm)"}
          </button>
          <button onClick={() => send({ type: "stop_robot" })} className="rounded-md bg-red-700 px-4 py-2 font-bold" title="Stop the arm, unload the models and disarm">STOP</button>
        </div>
        <StandbyLine standby={standby} config={config} />
        {pick && (
          <div className="flex items-center gap-2 rounded-md bg-white/5 px-2 py-1.5 font-mono text-xs">
            <span className="flex-1">{pick.simulated ? "[sim] " : ""}{pick.item_name}: {pick.state}{pick.verdict ? ` (${pick.verdict === "in_box" ? "in box" : "missed"})` : ""} · {pick.elapsed_s}s/{pick.time_limit_s}s {pick.detail}{pick.active && pick.last_check && !pick.verdict ? ` · cam: ${pick.last_check}` : ""}</span>
            {pick.active && <button onClick={() => send({ type: "end_episode" })} className="rounded bg-white/15 px-2 py-1">done → end episode</button>}
          </div>
        )}
        {payment?.state === "pending" ? (
          <div className="flex items-center gap-2 rounded-md bg-sky-950 px-2 py-1.5 font-mono text-xs">
            <span className="flex-1">payment {payment.amount} · {payment.item_name}: waiting</span>
            <button onClick={() => send({ type: "payment_result", id: payment.id, ok: false, reason: "card declined" })} className="rounded bg-red-700 px-2 py-1">fail</button>
            <button onClick={() => send({ type: "payment_result", id: payment.id, ok: true })} className="rounded bg-green-700 px-2 py-1">success</button>
          </div>
        ) : (
          <p className="text-xs text-white/40">
            Payments: <a href="/kiosk/terminal" target="_blank" className="underline">mock card terminal</a>
            {payment ? ` · last: ${payment.amount} ${payment.state}` : ""}
          </p>
        )}
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (text.trim()) { send({ type: "user_text", text }); setText(""); } }}>
          <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Type as the customer…" className={`${box} flex-1`} />
          <button className="rounded-md bg-white/15 px-3">Send</button>
          <button type="button" onClick={() => send({ type: "reset" })} className="rounded-md bg-white/10 px-3" title="Forget the conversation (new customer)">Reset</button>
        </form>
      </div>

      <div className="flex border-b border-white/10 text-xs">
        {(["log", "shop", "setup"] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)} className={`flex-1 py-2 ${tab === t ? "bg-white/10" : "text-white/50"}`}>
            {t === "shop" ? `shop / owner${notes.length ? ` (${notes.length})` : ""}` : t}
          </button>
        ))}
      </div>

      <div className="min-w-0 flex-1 overflow-y-auto overflow-x-hidden p-3">
        {tab === "log" && (
          <div className="grid gap-1.5 font-mono text-xs">
            {log.map((ev, i) => <LogRow key={i} ev={ev} />)}
            <div ref={end} />
          </div>
        )}

        {tab === "shop" && config && (
          <div className="grid gap-4">
            <div>
              <div className="mb-1 flex items-center justify-between text-xs text-white/50">
                <span>Notes for the owner</span>
                {notes.length > 0 && <button onClick={() => send({ type: "clear_notifications" })}>clear</button>}
              </div>
              {notes.length === 0 && <p className="text-white/40">None.</p>}
              {notes.map((n, i) => (
                <p key={i} className={`mb-1 rounded px-2 py-1 ${n.urgent ? "bg-red-900/50" : "bg-white/5"}`}>
                  <span className="mr-2 font-mono text-xs text-white/40">{new Date(n.at * 1000).toLocaleTimeString()} {n.source}</span>{n.message}
                </p>
              ))}
            </div>
            <div className="grid gap-2">
              <span className="text-xs text-white/50">Inventory (simulated)</span>
              {config.items.map((it, i) => (
                <div key={it.id} className="grid grid-cols-[1fr_auto_auto_auto] items-center gap-2 rounded bg-white/5 px-2 py-1.5">
                  <span>{it.name} <span className="text-white/40">{it.price}</span></span>
                  <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={it.on_table} onChange={(e) => setItem(i, { on_table: e.target.checked })} /> on table</label>
                  <label className="flex items-center gap-1 text-xs">back <input type="number" min={0} value={it.back_stock} onChange={(e) => setItem(i, { back_stock: Math.max(0, Number(e.target.value)) })} className={`${box} w-14`} /></label>
                  <span className="text-xs text-white/50">sold {it.sold}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {tab === "setup" && config && <Setup key={JSON.stringify(config)} config={config} save={save} box={box} levels={levels} vision={vision} halfDuplex={halfDuplex} setHalfDuplex={setHalfDuplex} />}
        {saveError && <p className="mt-2 rounded bg-red-900/60 px-2 py-1 text-xs">{saveError}</p>}
      </div>
    </aside>
  );
}

function StandbyLine({ standby, config }: { standby: Standby | null; config: Config | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id); }, []);
  if (!config) return null;
  if (config.simulate_picks) return <p className="text-xs text-white/50">Simulated picks: nothing loads on the GPU.</p>;
  const s = standby?.state ?? "off";
  const since = standby?.started_at ? Math.max(0, Math.round(now / 1000 - standby.started_at)) : 0;
  const text = {
    off: config.armed ? "Models not loaded (no checkpoints set, or waiting to retry)." : "Models unloaded. Arm to warm up (~7 s).",
    loading: `Warming up: loading ${standby?.policies.join(", ")} and connecting… ${since}s`,
    ready: `Warm: ${standby?.policies.join(", ")} loaded; picks start immediately.`,
    busy: "Warm: running a pick.",
    stopping: "Unloading…",
    error: `Not warm: ${standby?.detail} (picks fall back to a cold start)`,
  }[s];
  const color = s === "ready" || s === "busy" ? "text-green-400" : s === "error" ? "text-red-400" : "text-white/60";
  return <p className={`text-xs ${color}`}>{text}</p>;
}

function Setup({ config, save, box, levels, vision, halfDuplex, setHalfDuplex }: { config: Config; save: (c: Config) => void; box: string; levels: React.RefObject<Levels>; vision: React.RefObject<Vision>; halfDuplex: boolean; setHalfDuplex: (v: boolean) => void }) {
  const [draft, setDraft] = useState(config); // remounted (key) when the saved config changes
  const catalog = useCatalog();
  const field = (label: string, el: React.ReactNode) => <label className="grid min-w-0 gap-1 text-xs text-white/60">{label}{el}</label>;
  const text = (k: keyof Config) => <input value={String(draft[k])} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} className={box} />;
  const item = (i: number, patch: Partial<Item>) => setDraft({ ...draft, items: draft.items.map((it, j) => (j === i ? { ...it, ...patch } : it)) });
  return (
    <div className="grid gap-3">
      <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={draft.simulate_picks} onChange={(e) => setDraft({ ...draft, simulate_picks: e.target.checked })} /> Simulate picks (rehearse without the robot)</label>
      <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={halfDuplex} onChange={(e) => setHalfDuplex(e.target.checked)} /> Mute mic while talking (use if the robot hears itself; this iPad only)</label>
      <div className="grid gap-2 rounded border border-white/10 p-2">
        <MicMeter levels={levels} gate={draft.mic_gate_db} />
        {field(`Loudness gate: ${draft.mic_gate_db <= GATE_OFF_DB ? "off" : `${draft.mic_gate_db} dB`} (quieter speech is ignored; set it between the customer's level and the room's)`,
          <input type="range" min={GATE_OFF_DB} max={-10} value={draft.mic_gate_db} onChange={(e) => setDraft({ ...draft, mic_gate_db: Number(e.target.value) })} />)}
        {field("Characters needed to interrupt the robot (停 / 等一下 / stop always work)",
          <input type="number" min={1} max={30} value={draft.barge_in_chars} onChange={(e) => setDraft({ ...draft, barge_in_chars: Number(e.target.value) })} className={box} />)}
        {field(`Robot volume while someone talks over it: ${draft.duck_gain >= 1 ? "not lowered" : draft.duck_gain <= 0 ? "muted" : `${Math.round(draft.duck_gain * 100)}%`} (until the addressee check decides whether it was an interruption)`,
          <input type="range" min={0} max={1} step={0.05} value={draft.duck_gain} onChange={(e) => setDraft({ ...draft, duck_gain: Number(e.target.value) })} />)}
        {field("Addressee check model (\"\" = off): ignores speech not said to the robot", text("addressee_model"))}
        <p className="text-xs text-white/40">Turning the gate on or off takes effect after reloading the page (it switches the mic&apos;s automatic gain).</p>
      </div>
      <div className="grid gap-2 rounded border border-white/10 p-2">
        <CameraView vision={vision} minSize={draft.face_min_size} />
        <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={draft.camera_listen} onChange={(e) => setDraft({ ...draft, camera_listen: e.target.checked })} /> Use the front camera: only listen when someone is at the counter (takes effect after reloading)</label>
        {field(`"At the counter" face size: ${Math.round(draft.face_min_size * 100)}% of the picture width (stand where customers stand and set it just below their size)`,
          <input type="range" min={0.02} max={0.4} step={0.01} value={draft.face_min_size} onChange={(e) => setDraft({ ...draft, face_min_size: Number(e.target.value) })} />)}
        {field(`Camera zoom: ${draft.camera_zoom.toFixed(1)}× (the face model misses small faces; zoom in until customers at the counter are boxed, the dashed frame is what it looks at)`,
          <input type="range" min={1} max={3} step={0.1} value={draft.camera_zoom} onChange={(e) => setDraft({ ...draft, camera_zoom: Number(e.target.value) })} />)}
        {field(`Zoomed part: ${draft.camera_crop_y <= 0.15 ? "top" : draft.camera_crop_y >= 0.85 ? "bottom" : draft.camera_crop_y < 0.45 ? "upper" : draft.camera_crop_y > 0.55 ? "lower" : "middle"} (move it to where customers' faces are)`,
          <input type="range" min={0} max={1} step={0.05} value={draft.camera_crop_y} onChange={(e) => setDraft({ ...draft, camera_crop_y: Number(e.target.value) })} />)}
        <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={draft.customer_photos} onChange={(e) => setDraft({ ...draft, customer_photos: e.target.checked })} /> Claude may look at the customer (sends a front-camera photo to Claude when it calls look_at_customer)</label>
        <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={draft.greet_on_arrival} onChange={(e) => setDraft({ ...draft, greet_on_arrival: e.target.checked })} /> Greet people who walk up</label>
        {field("New customer when nobody is at the counter for (s, 0 = off)", <input type="number" min={0} max={600} value={draft.leave_reset_s} onChange={(e) => setDraft({ ...draft, leave_reset_s: Number(e.target.value) })} className={box} />)}
      </div>
      <div className="grid grid-cols-2 gap-2">
        {field("Shop name", text("shop_name"))}
        {field("Language", text("language"))}
        {field("Claude model", text("model"))}
        {field("Effort", <select value={draft.effort} onChange={(e) => setDraft({ ...draft, effort: e.target.value as Config["effort"] })} className={box}><option>low</option><option>medium</option><option>high</option></select>)}
        {field("ElevenLabs model", text("tts_model"))}
        {field("ElevenLabs voice id", text("voice_id"))}
        {field("Pick camera watch model (\"\" = off): ends a pick early when it's in the box or failing", text("pick_watch_model"))}
        {field("Watch: first look after (s) · then every (s) · fails in a row to give up", <div className="flex gap-1">
          <input type="number" min={1} max={120} value={draft.pick_watch_after_s} onChange={(e) => setDraft({ ...draft, pick_watch_after_s: Number(e.target.value) })} className={`${box} w-16`} />
          <input type="number" min={1} max={60} value={draft.pick_watch_every_s} onChange={(e) => setDraft({ ...draft, pick_watch_every_s: Number(e.target.value) })} className={`${box} w-16`} />
          <input type="number" min={1} max={10} value={draft.pick_watch_misses} onChange={(e) => setDraft({ ...draft, pick_watch_misses: Number(e.target.value) })} className={`${box} w-16`} />
        </div>)}
        {field("New customer after idle (s)", <input type="number" value={draft.idle_reset_s} onChange={(e) => setDraft({ ...draft, idle_reset_s: Number(e.target.value) })} className={box} />)}
      </div>
      {field("Extra instructions for Claude", <textarea value={draft.extra_instructions} onChange={(e) => setDraft({ ...draft, extra_instructions: e.target.value })} rows={3} className={box} />)}
      <div className="flex items-center justify-between text-xs text-white/50">
        <span>{catalog.error ? `Checkpoints: ${catalog.error}` : catalog.data ? `${catalog.data.runs.length} runs on the GPU server` : "Loading checkpoints…"}</span>
        <button onClick={catalog.reload} className="rounded bg-white/10 px-2 py-1">refresh</button>
      </div>
      {draft.items.map((it, i) => (
        <div key={i} className="grid gap-2 rounded border border-white/10 p-2">
          <div className="grid grid-cols-3 gap-2">
            {field("id", <input value={it.id} onChange={(e) => item(i, { id: e.target.value })} className={`${box} w-full`} />)}
            {field("Name", <input value={it.name} onChange={(e) => item(i, { name: e.target.value })} className={`${box} w-full`} />)}
            {field("Price", <input value={it.price} onChange={(e) => item(i, { price: e.target.value })} className={`${box} w-full`} />)}
          </div>
          {field("Where on the table", <input value={it.position} onChange={(e) => item(i, { position: e.target.value })} className={box} />)}
          {field("Description", <input value={it.description} onChange={(e) => item(i, { description: e.target.value })} className={box} />)}
          {field("ACT checkpoint on the GPU server", <CheckpointSelect value={it.checkpoint} onChange={(checkpoint) => item(i, { checkpoint })} catalog={catalog} box={box} />)}
          <div className="grid grid-cols-[1fr_auto] gap-2">
            {field("Task text", <input value={it.task} onChange={(e) => item(i, { task: e.target.value })} className={box} />)}
            {field("Time limit (s)", <input type="number" value={it.episode_time_s} onChange={(e) => item(i, { episode_time_s: Number(e.target.value) })} className={`${box} w-20`} />)}
          </div>
        </div>
      ))}
      <div className="flex gap-2">
        <button onClick={() => save(draft)} className="flex-1 rounded-md bg-white/20 py-2 font-semibold">Save</button>
        <button onClick={() => setDraft({ ...draft, items: [...draft.items, { id: `item_${draft.items.length + 1}`, name: "New item", description: "", price: "", position: "", on_table: true, back_stock: 0, sold: 0, checkpoint: "", task: "", episode_time_s: 40 }] })} className="rounded-md bg-white/10 px-3">+ item</button>
      </div>
    </div>
  );
}

// ---------- checkpoint dropdown (same scan as Train & Eval: the GPU server's outputs/) ----------

type Checkpoint = { path: string; label: string; step: number | null; policy: string; latest: boolean; modified_at: number };
type Catalog = { root: string; runs: { name: string; checkpoints: Checkpoint[] }[] };

function useCatalog() {
  const [data, setData] = useState<Catalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let live = true;
    fetch("/api/gpu/checkpoints")
      .then(async (res) => {
        const body = await res.json();
        if (!res.ok) throw new Error(body.detail ?? res.statusText);
        if (live) { setData(body as Catalog); setError(null); }
      })
      .catch((e) => { if (live) setError(e instanceof Error ? e.message : String(e)); });
    return () => { live = false; };
  }, [version]);
  return { data, error, reload: () => setVersion((v) => v + 1) };
}

const CUSTOM = "__custom__";

function CheckpointSelect({ value, onChange, catalog, box }: { value: string; onChange: (v: string) => void; catalog: ReturnType<typeof useCatalog>; box: string }) {
  const [showAll, setShowAll] = useState(false);
  const all = catalog.data?.runs.flatMap((r) => r.checkpoints) ?? [];
  const known = all.some((c) => c.path === value);
  const [custom, setCustom] = useState(!!value && !known && !!catalog.data);
  const runs = (catalog.data?.runs ?? []).map((r) => ({ ...r, checkpoints: r.checkpoints.filter((c) => showAll || c.latest || c.path === value) }));
  const selectValue = custom || (value && !known) ? CUSTOM : value;
  return (
    <div className="grid min-w-0 gap-1">
      <div className="flex min-w-0 gap-2">
        <select value={selectValue} onChange={(e) => { const v = e.target.value; setCustom(v === CUSTOM); if (v !== CUSTOM) onChange(v); }} className={`${box} w-0 min-w-0 flex-1`}>
          <option value="">— none (robot can&apos;t pick this) —</option>
          {runs.map((r) => (
            <optgroup key={r.name} label={r.name}>
              {r.checkpoints.map((c) => (
                <option key={c.path} value={c.path}>
                  {r.name} · {c.step != null ? `step ${c.step}` : c.label}{c.latest ? " (latest)" : ""} · {c.policy}
                </option>
              ))}
            </optgroup>
          ))}
          <option value={CUSTOM}>Other path or Hub model id…</option>
        </select>
        <label className="flex items-center gap-1 whitespace-nowrap text-white/50"><input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} /> all steps</label>
      </div>
      {selectValue === CUSTOM && <input value={value} onChange={(e) => onChange(e.target.value)} placeholder="/path/to/checkpoints/last/pretrained_model or user/model" className={box} />}
    </div>
  );
}

/** What the front camera sees, with each face boxed (green: at the counter) and the numbers the gate uses. */
function CameraView({ vision, minSize }: { vision: React.RefObject<Vision>; minSize: number }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [info, setInfo] = useState("");
  useEffect(() => {
    const id = setInterval(() => {
      const v = vision.current;
      const c = canvas.current;
      if (v.state !== "on" || !v.video || !c) {
        setInfo(v.state === "error" ? `Camera error: ${v.error}` : v.state === "loading" ? "Camera starting…" : "Camera off (tap to start the kiosk; needs HTTPS)");
        return;
      }
      const g = c.getContext("2d");
      if (!g) return;
      c.width = 240; c.height = Math.round((240 * v.video.videoHeight) / (v.video.videoWidth || 1)) || 180;
      g.save(); g.scale(-1, 1); g.drawImage(v.video, -c.width, 0, c.width, c.height); g.restore(); // mirrored, like a selfie
      const k = v.crop; // the part the face model looks at (x is centred, so mirroring doesn't move it)
      g.setLineDash([4, 4]); g.strokeStyle = "#fbbf24"; g.lineWidth = 1;
      g.strokeRect(k.x * c.width, k.y * c.height, k.w * c.width, k.h * c.height);
      g.setLineDash([]);
      for (const b of v.boxes) {
        g.strokeStyle = b.size >= minSize ? "#22c55e" : "#a3a3a3";
        g.lineWidth = 2;
        g.strokeRect((1 - b.x1) * c.width, b.y0 * c.height, b.size * c.width, (b.y1 - b.y0) * c.height);
      }
      const main = v.boxes.reduce<(typeof v.boxes)[number] | null>((a, b) => (!a || b.size > a.size ? b : a), null);
      const s = summarize(v, Date.now() - 1500);
      setInfo(main
        ? `${v.boxes.length} face(s) · closest ${Math.round(main.size * 100)}% · ${main.facing ? "facing" : "turned away"} · mouth ${s?.mouth ?? "–"} (${s?.lips ? "talking" : "still"}, talking ≥ ${LIPS_STD})`
        : "No face");
    }, 200);
    return () => clearInterval(id);
  }, [vision, minSize]);
  return (
    <div className="grid gap-1 text-xs text-white/60">
      <canvas ref={canvas} className="w-60 rounded bg-black" />
      <div>{info}</div>
    </div>
  );
}

/** Live mic level with the gate marked, for tuning on site: speak at the iPad, then watch the room. */
function MicMeter({ levels, gate }: { levels: React.RefObject<Levels>; gate: number }) {
  const [now, setNow] = useState({ db: -100, peak: -100 });
  useEffect(() => {
    const id = setInterval(() => {
      const l = levels.current;
      const n = l.db.length;
      if (!n) return;
      const recent = l.db.slice(Math.max(0, n - 20)); // last second
      setNow({ db: l.db[n - 1], peak: Math.max(...recent) });
    }, 150);
    return () => clearInterval(id);
  }, [levels]);
  const pct = (db: number) => `${Math.min(100, Math.max(0, ((db + 80) / 70) * 100))}%`;
  return (
    <div className="grid gap-1 text-xs text-white/60">
      <div>Mic now {now.db > -100 ? `${now.db.toFixed(0)} dB` : "–"} · peak {now.peak > -100 ? `${now.peak.toFixed(0)} dB` : "–"}</div>
      <div className="relative h-3 overflow-hidden rounded bg-white/10">
        <div className={`absolute inset-y-0 left-0 ${now.peak >= gate ? "bg-green-500" : "bg-white/30"}`} style={{ width: pct(now.peak) }} />
        {gate > GATE_OFF_DB && <div className="absolute inset-y-0 w-0.5 bg-amber-400" style={{ left: pct(gate) }} />}
      </div>
    </div>
  );
}

function LogRow({ ev }: { ev: LogEvent }) {
  const [open, setOpen] = useState(false);
  const t = new Date(ev.at * 1000).toLocaleTimeString([], { hour12: false });
  const head = <span className="text-white/30">{t}</span>;
  switch (ev.type) {
    case "user": {
      const v = ev.vision as VisionSummary | null | undefined;
      return <div>{head} <span className="text-sky-300">customer:</span> {String(ev.text)}{v && <span className="text-white/30"> [cam: {v.lips ? "lips moving" : "lips still"}, facing {Math.round(v.facing * 100)}%]</span>}</div>;
    }
    case "noted":
      return <div>{head} <span className="text-sky-300/60">customer (robot kept talking):</span> {String(ev.text)}</div>;
    case "presence":
      return <div className="text-white/40">{head} {ev.present ? "someone walked up" : "nobody at the counter"}</div>;
    case "robot_event":
      return <div>{head} <span className="text-amber-300">robot event:</span> {String(ev.text)}</div>;
    case "assistant":
      return <div>{head} <span className="text-green-300">robot:</span> {String(ev.text)}</div>;
    case "tool": {
      const images = (ev.images as string[]) ?? [];
      return (
        <div className={`rounded px-1.5 py-1 ${ev.is_error ? "bg-red-900/40" : "bg-white/5"}`}>
          <button onClick={() => setOpen(!open)} className="text-left">
            {head} <span className="text-violet-300">tool {String(ev.name)}</span>({JSON.stringify(ev.input)}) <span className="text-white/40">{String(ev.duration_s)}s</span>
          </button>
          {open && <pre className="mt-1 whitespace-pre-wrap break-all text-white/60">{String(ev.result)}</pre>}
          {images.length > 0 && <div className="mt-1 flex gap-1">{images.map((b, i) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img key={i} src={`data:image/jpeg;base64,${b}`} alt="what Claude saw" className="h-24 rounded" />
          ))}</div>}
        </div>
      );
    }
    case "turn_done": {
      const m = ev.metrics as Record<string, number | null>;
      return <div className="text-white/40">{head} turn: first text {m.first_text_s ?? "–"}s · first audio {m.first_audio_s ?? "–"}s · total {m.total_s}s</div>;
    }
    case "llm_step": {
      const u = ev.usage as Record<string, number | null>;
      return <div className="text-white/30">{head} {String(ev.model)} {String(ev.stop_reason)} · in {u.input_tokens} (cached {u.cache_read_input_tokens ?? 0}) out {u.output_tokens}</div>;
    }
    case "addressee":
      return <div className={ev.to_robot ? "text-white/30" : "text-orange-300/80"}>{head} {ev.verdict === "same" ? "for me, nothing new" : ev.to_robot ? "for me" : "not for me"} ({String(ev.duration_s)}s): {String(ev.text)}</div>;
    case "error":
      return <div className="text-red-400">{head} error [{String(ev.where)}]: {String(ev.message)}</div>;
    case "notification":
      return <div className="text-amber-200">{head} owner note: {String(ev.message)}</div>;
    case "payment": {
      const p = ev.payment as Payment;
      const color = p.state === "paid" ? "text-green-300" : p.state === "declined" ? "text-red-300" : "text-sky-300";
      return <div className={color}>{head} payment {p.amount} ({p.item_name}): {p.state}{p.reason ? ` · ${p.reason}` : ""}</div>;
    }
    case "pick_check": {
      const color = ev.verdict === "in_box" ? "text-green-300/80" : ev.verdict === "failing" ? "text-orange-300/80" : "text-white/40";
      return <div className={color}>{head} pick cam {String(ev.elapsed_s)}s: {String(ev.verdict)}{ev.verdict === "failing" ? ` (${String(ev.misses)} in a row)` : ""} {ev.reason ? `· ${String(ev.reason)}` : ""} <span className="text-white/30">{String(ev.duration_s)}s</span></div>;
    }
    case "pick": {
      const p = ev.pick as Pick;
      return <div className="text-white/50">{head} pick {p.item_name}: {p.state}</div>;
    }
    default:
      return <div className="text-white/40">{head} {ev.type} {ev.reason ? String(ev.reason) : ev.message ? String(ev.message) : ""}</div>;
  }
}
