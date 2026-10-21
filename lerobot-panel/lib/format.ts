export const cameraLabel = (name: string) => name.replace(/^observation\.images\./, "").replaceAll("_", " ");
export const jointLabel = (name: string) => name.replace(/^arm_(left|right)_/, "").replace(/\.pos$/, "").replaceAll("_", " ");
export function fmtDuration(seconds: number | null | undefined) {
  if (seconds == null) return "–";
  const s = Math.max(0, Math.round(seconds));
  return s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m` : s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
}
export function fmtAgo(at: number | null | undefined) {
  if (at == null) return "–";
  const seconds = Math.max(0, Date.now() / 1000 - at);
  return seconds < 60 ? "just now" : seconds >= 86400 ? `${Math.floor(seconds / 86400)}d ago` : `${fmtDuration(seconds)} ago`;
}
export function fmtBytes(bytes: number | null | undefined) {
  if (bytes == null) return "–";
  const unit = Math.min(4, Math.floor(Math.log(Math.max(1, bytes)) / Math.log(1024)));
  return `${(bytes / 1024 ** unit).toFixed(unit ? 1 : 0)} ${["B", "KiB", "MiB", "GiB", "TiB"][unit]}`;
}
