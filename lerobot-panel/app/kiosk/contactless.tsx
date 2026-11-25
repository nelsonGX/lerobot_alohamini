/* Contactless-payment symbol (four arcs) with optional expanding rings, shared by the kiosk face and the mock
 * terminal. Colours follow `currentColor`. */

export const PAY_KEYFRAMES = `
@keyframes pay-ring { 0% { transform: scale(.6); opacity: .55 } 100% { transform: scale(1.9); opacity: 0 } }
@keyframes pay-wave { 0%, 100% { opacity: .25 } 40% { opacity: 1 } }
@keyframes pay-pop { 0% { transform: scale(.4); opacity: 0 } 60% { transform: scale(1.08); opacity: 1 } 100% { transform: scale(1) } }
@keyframes pay-draw { to { stroke-dashoffset: 0 } }
@keyframes pay-spin { to { transform: rotate(360deg) } }
@keyframes pay-shake { 0%, 100% { transform: translateX(0) } 20%, 60% { transform: translateX(-6px) } 40%, 80% { transform: translateX(6px) } }
@keyframes pay-in { from { opacity: 0; transform: translateY(6px) } to { opacity: 1; transform: none } }
`;

export function Contactless({ size = 96, live = false }: { size?: number; live?: boolean }) {
  const arcs = [
    "M30 34a20 20 0 0 1 0 28",
    "M42 26a32 32 0 0 1 0 44",
    "M54 18a44 44 0 0 1 0 60",
    "M66 10a56 56 0 0 1 0 76",
  ];
  return (
    <div className="relative grid place-items-center" style={{ width: size, height: size }}>
      {live && [0, 1].map((i) => (
        <span key={i} className="absolute inset-0 rounded-full border-2 border-current" style={{ animation: `pay-ring 2.2s ease-out ${i * 1.1}s infinite` }} />
      ))}
      <svg viewBox="0 0 96 96" width={size * 0.62} height={size * 0.62} fill="none" stroke="currentColor" strokeWidth={7} strokeLinecap="round" aria-hidden>
        {arcs.map((d, i) => (
          <path key={d} d={d} style={live ? { animation: `pay-wave 1.6s ease-in-out ${i * 0.15}s infinite` } : undefined} />
        ))}
      </svg>
    </div>
  );
}
