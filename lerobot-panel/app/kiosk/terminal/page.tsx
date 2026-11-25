"use client";

/* Mock tap-to-pay terminal for the kiosk demo (server: request_payment in server/kiosk.py). Open /kiosk/terminal
 * on a phone or a second screen next to the robot: when Claude asks for payment after a successful pick, the
 * amount shows here. Staff tap the reader (or the green key) to approve, or the red key to decline. No real
 * payment happens. */

import { useEffect, useRef, useState } from "react";
import { Contactless, PAY_KEYFRAMES } from "../contactless";

type Payment = { id: string; item_id: string; item_name: string; amount: string; state: "pending" | "paid" | "declined" | "cancelled"; reason: string; created_at: number; ended_at: number | null };

const DECLINE_REASONS: [string, string][] = [["card declined", "Declined"], ["insufficient funds", "No funds"], ["card read error", "Read error"]];
const RESULT_SHOW_S = 8; // how long approved / declined stays on screen
const PROCESSING_MS = 1400; // "reading card…" before the answer is sent, so it feels like a real terminal

export default function TerminalPage() {
  const [payment, setPayment] = useState<Payment | null>(null);
  const [shop, setShop] = useState("");
  const [wsUp, setWsUp] = useState(false);
  const [reason, setReason] = useState(DECLINE_REASONS[0][0]);
  const [processing, setProcessing] = useState<string | null>(null); // payment id being "read"
  const [now, setNow] = useState(() => Date.now() / 1000);
  const ws = useRef<WebSocket | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    let closed = false;
    let retry: ReturnType<typeof setTimeout>;
    const open = () => {
      const sock = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/kiosk/ws`);
      ws.current = sock;
      sock.onopen = () => { if (ws.current === sock) setWsUp(true); };
      sock.onclose = () => { if (ws.current !== sock) return; setWsUp(false); if (!closed) retry = setTimeout(open, 1500); };
      sock.onerror = () => sock.close();
      sock.onmessage = (e) => {
        const ev = JSON.parse(e.data);
        if (ev.type === "snapshot") { setPayment(ev.payment ?? null); setShop(ev.config?.shop_name ?? ""); }
        else if (ev.type === "payment") { setPayment(ev.payment); setProcessing(null); }
        else if (ev.type === "config") setShop(ev.config?.shop_name ?? "");
      };
    };
    open();
    return () => { closed = true; clearTimeout(retry); clearTimeout(timer.current); ws.current?.close(); };
  }, []);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, []);

  const answer = (ok: boolean) => {
    if (!payment || payment.state !== "pending" || processing) return;
    navigator.vibrate?.(ok ? 30 : [40, 60, 40]);
    setProcessing(payment.id);
    const id = payment.id;
    timer.current = setTimeout(() => {
      if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(JSON.stringify({ type: "payment_result", id, ok, reason: ok ? "" : reason }));
      else setProcessing(null);
    }, PROCESSING_MS);
  };

  const recent = payment && payment.state !== "pending" && payment.ended_at && now - payment.ended_at < RESULT_SHOW_S;
  const shown = payment?.state === "pending" || recent ? payment : null;
  const view = !shown ? "idle" : processing === shown.id ? "processing" : shown.state;
  const pending = view === "pending";
  const clock = new Date(now * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

  return (
    <div className="fixed inset-0 z-50 grid place-items-center overflow-hidden p-4" style={{ background: "radial-gradient(120% 90% at 50% 0%, #2a2a2e 0%, #121214 60%, #0b0b0c 100%)" }}>
      <style>{PAY_KEYFRAMES}</style>

      {/* device body */}
      <div className="flex w-[min(380px,94vw)] flex-col gap-5 rounded-[44px] p-3.5 pb-6" style={{ background: "linear-gradient(160deg, #3a3a3f 0%, #232326 45%, #18181a 100%)", boxShadow: "0 40px 80px -20px rgba(0,0,0,.8), inset 0 1px 0 rgba(255,255,255,.12), inset 0 -2px 0 rgba(0,0,0,.4)" }}>

        {/* screen */}
        <div className="relative flex aspect-[3/4] flex-col overflow-hidden rounded-[32px] bg-[#f6f5f1] text-[#141412]" style={{ boxShadow: "inset 0 0 0 1px rgba(0,0,0,.08)" }}>
          <div className="flex items-center justify-between px-6 pt-4 text-[11px] font-medium tracking-wide text-black/40">
            <span className="tabular-nums">{clock}</span>
            <span className="truncate px-3">{shop}</span>
            <span className="flex items-center gap-1.5">
              <span className={`inline-block size-1.5 rounded-full ${wsUp ? "bg-emerald-500" : "bg-red-500"}`} />{wsUp ? "Online" : "Offline"}
            </span>
          </div>

          <div key={view + (shown?.id ?? "")} className="flex flex-1 flex-col items-center justify-center px-7 text-center" style={{ animation: "pay-in .35s ease-out" }}>
            {view === "idle" && (
              <>
                <div className="text-black/20"><Contactless size={110} /></div>
                <div className="mt-6 text-xl font-semibold text-black/70">Ready</div>
                <div className="mt-1 text-sm text-black/40">等待交易</div>
              </>
            )}

            {pending && shown && (
              <>
                <div className="text-sm font-medium text-black/45">{shown.item_name}</div>
                <div className="mt-1 text-[56px] font-semibold leading-none tracking-tight tabular-nums">{shown.amount}</div>
                <button onClick={() => answer(true)} className="mt-9 rounded-full text-[#0a66ff] transition-transform active:scale-95" aria-label="Tap to pay (approve)">
                  <Contactless size={128} live />
                </button>
                <div className="mt-7 text-lg font-semibold">Tap to pay</div>
                <div className="mt-0.5 text-sm text-black/45">請將卡片或手機靠近感應</div>
              </>
            )}

            {view === "processing" && (
              <>
                <div className="size-20 rounded-full border-[5px] border-black/10 border-t-[#0a66ff]" style={{ animation: "pay-spin .8s linear infinite" }} />
                <div className="mt-7 text-lg font-semibold">Processing…</div>
                <div className="mt-0.5 text-sm text-black/45">處理中，請勿移開卡片</div>
              </>
            )}

            {view === "paid" && shown && (
              <>
                <div className="grid size-28 place-items-center rounded-full bg-emerald-500 text-white" style={{ animation: "pay-pop .5s cubic-bezier(.2,.9,.3,1.3)" }}>
                  <svg viewBox="0 0 52 52" className="size-14" fill="none" stroke="currentColor" strokeWidth={5} strokeLinecap="round" strokeLinejoin="round">
                    <path d="M14 27l8 8 16-17" strokeDasharray="40" strokeDashoffset="40" style={{ animation: "pay-draw .4s .3s ease-out forwards" }} />
                  </svg>
                </div>
                <div className="mt-7 text-2xl font-semibold">Approved</div>
                <div className="mt-1 text-sm text-black/45">付款成功 · {shown.amount}</div>
                <div className="mt-6 text-xs text-black/35">Thank you · 謝謝光臨</div>
              </>
            )}

            {view === "declined" && shown && (
              <>
                <div className="grid size-28 place-items-center rounded-full bg-[#e5484d] text-white" style={{ animation: "pay-pop .45s ease-out, pay-shake .4s .45s ease-in-out" }}>
                  <svg viewBox="0 0 52 52" className="size-12" fill="none" stroke="currentColor" strokeWidth={5} strokeLinecap="round"><path d="M17 17l18 18M35 17L17 35" /></svg>
                </div>
                <div className="mt-7 text-2xl font-semibold">Declined</div>
                <div className="mt-1 text-sm capitalize text-black/45">{shown.reason || "card declined"}</div>
                <div className="mt-6 text-xs text-black/35">付款失敗，請再試一次</div>
              </>
            )}

            {view === "cancelled" && shown && (
              <>
                <div className="text-black/20"><Contactless size={110} /></div>
                <div className="mt-6 text-xl font-semibold text-black/60">Cancelled</div>
                <div className="mt-1 text-sm text-black/40">交易已取消</div>
              </>
            )}
          </div>

          {pending && shown && (
            <div className="pb-4 text-center text-[11px] tabular-nums text-black/30">Waiting {Math.max(0, Math.round(now - shown.created_at))}s</div>
          )}
        </div>

        {/* hardware keys (staff) */}
        <div className="grid grid-cols-2 gap-3 px-2">
          <Key color="#e5484d" disabled={!pending} onClick={() => answer(false)} label="Fail">
            <path d="M8 8l8 8M16 8l-8 8" />
          </Key>
          <Key color="#22a55a" disabled={!pending} onClick={() => answer(true)} label="Success">
            <path d="M6 12.5l4 4 8-9" />
          </Key>
        </div>

        <div className="flex flex-wrap items-center justify-center gap-1.5 px-2 text-[11px]">
          <span className="mr-1 text-white/30">Fail as</span>
          {DECLINE_REASONS.map(([r, short]) => (
            <button key={r} onClick={() => setReason(r)} className={`rounded-full px-2.5 py-1 transition-colors ${reason === r ? "bg-white/15 text-white/85" : "text-white/35 hover:text-white/60"}`}>{short}</button>
          ))}
        </div>
      </div>

      <div className="absolute bottom-3 text-[10px] uppercase tracking-[0.2em] text-white/15">Demo terminal · no real payment</div>
    </div>
  );
}

function Key({ color, disabled, onClick, label, children }: { color: string; disabled: boolean; onClick: () => void; label: string; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="flex items-center justify-center gap-2 rounded-2xl py-4 text-base font-semibold text-white transition-all active:translate-y-0.5 disabled:opacity-30"
      style={{ background: `linear-gradient(180deg, ${color} 0%, color-mix(in srgb, ${color} 78%, black) 100%)`, boxShadow: disabled ? "none" : `0 4px 0 color-mix(in srgb, ${color} 55%, black), 0 8px 18px -6px ${color}` }}
    >
      <svg viewBox="0 0 24 24" className="size-5" fill="none" stroke="currentColor" strokeWidth={2.8} strokeLinecap="round" strokeLinejoin="round">{children}</svg>
      {label}
    </button>
  );
}
