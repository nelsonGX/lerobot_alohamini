"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, videoUrl, type EpisodeData, type EpisodeMeta } from "@/lib/api";
import { cameraLabel } from "@/lib/format";
import { JointCharts } from "./JointCharts";
import { ErrorBox, IconButton, Kbd, Segmented, Toggle } from "./ui";

const SPEEDS = [0.25, 0.5, 1, 2, 4];

/**
 * Plays one episode. In LeRobot v3 several episodes share one mp4 per camera, so each
 * video is clamped to [from_s, to_s] of this episode. The first camera is the clock;
 * the others follow it, and the charts follow the shared playhead.
 */
export function EpisodePlayer({ repo, episode, fps, cameras }: {
  repo: string;
  episode: EpisodeMeta;
  fps: number;
  cameras: string[];
}) {
  const dataKey = `${repo}#${episode.index}`;
  const [loaded, setLoaded] = useState<{ key: string; data?: EpisodeData; error?: string } | null>(null);
  const data = loaded?.key === dataKey ? (loaded.data ?? null) : null;
  const dataError = loaded?.key === dataKey ? (loaded.error ?? null) : null;
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [loop, setLoop] = useState(false);
  const [videoErrors, setVideoErrors] = useState<Record<string, boolean>>({});
  const [focus, setFocus] = useState<string | null>(null);
  const videos = useRef<Record<string, HTMLVideoElement | null>>({});
  const tRef = useRef(0);
  const duration = episode.duration_s;
  const lastFrame = Math.max(0, episode.length - 1);
  const cams = cameras.filter((c) => episode.videos[c]);
  const clockCam = cams.find((c) => !videoErrors[c]) ?? null;

  useEffect(() => {
    let cancelled = false;
    api.episode(repo, episode.index).then(
      (d) => !cancelled && setLoaded({ key: dataKey, data: d }),
      (e) => !cancelled && setLoaded({ key: dataKey, error: e.message }),
    );
    return () => {
      cancelled = true;
    };
  }, [repo, episode.index, dataKey]);

  const seek = useCallback(
    (time: number) => {
      const clamped = Math.max(0, Math.min(duration, time));
      tRef.current = clamped;
      setT(clamped);
      for (const cam of cams) {
        const v = videos.current[cam];
        const seg = episode.videos[cam];
        if (v && seg && v.readyState >= 1) v.currentTime = seg.from_s + clamped;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [duration, episode],
  );

  // New episode: rewind and pause (state during render, videos in an effect).
  const [shownEpisode, setShownEpisode] = useState(dataKey);
  if (shownEpisode !== dataKey) {
    setShownEpisode(dataKey);
    setPlaying(false);
    setT(0);
  }
  useEffect(() => {
    tRef.current = 0;
    for (const cam of cameras) {
      const v = videos.current[cam];
      const seg = episode.videos[cam];
      if (v && seg && v.readyState >= 1) v.currentTime = seg.from_s;
    }
  }, [episode, cameras]);

  // Play/pause & speed for all videos.
  useEffect(() => {
    for (const cam of cams) {
      const v = videos.current[cam];
      if (!v) continue;
      v.playbackRate = speed;
      if (playing) v.play().catch(() => {});
      else v.pause();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, speed, episode.index]);

  // Clock loop.
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    let last = performance.now();
    const step = (now: number) => {
      const dt = (now - last) / 1000;
      last = now;
      let next: number;
      const clock = clockCam ? videos.current[clockCam] : null;
      if (clock && clock.readyState >= 2) {
        next = clock.currentTime - episode.videos[clockCam!].from_s;
        for (const cam of cams) {
          const v = videos.current[cam];
          if (cam === clockCam || !v || v.readyState < 1) continue;
          const want = episode.videos[cam].from_s + next;
          if (Math.abs(v.currentTime - want) > 0.12) v.currentTime = want;
        }
      } else {
        next = tRef.current + dt * speed;
      }
      if (next >= duration - 1 / fps / 2) {
        if (loop) {
          seek(0);
        } else {
          seek(duration);
          setPlaying(false);
          return;
        }
      } else {
        tRef.current = next;
        setT(next);
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, clockCam, episode, loop, speed, duration, fps, seek]);

  const frame = Math.min(lastFrame, Math.round(t * fps));
  const seekFrame = useCallback((f: number) => seek(f / fps), [seek, fps]);

  // Keyboard: space play/pause, ←/→ one frame (shift = 1 s), Home/End.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (e.metaKey || e.ctrlKey || e.altKey || el.closest("input:not([type=range]), textarea, select, [contenteditable]")) return;
      if (e.key === " ") {
        if (tRef.current >= duration - 1 / fps) seek(0);
        setPlaying((p) => !p);
      } else if (e.key === "ArrowRight") {
        setPlaying(false);
        seek(tRef.current + (e.shiftKey ? 1 : 1 / fps));
      } else if (e.key === "ArrowLeft") {
        setPlaying(false);
        seek(tRef.current - (e.shiftKey ? 1 : 1 / fps));
      } else if (e.key === "Home") seek(0);
      else if (e.key === "End") seek(duration);
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [duration, fps, seek]);

  const shownCams = focus ? cams.filter((c) => c === focus) : cams;

  return (
    <div className="grid gap-4">
      {cams.length > 0 ? (
        <div className={`grid gap-3 ${shownCams.length > 1 ? "md:grid-cols-2" : ""}`}>
          {cams.map((cam) => {
            const seg = episode.videos[cam];
            const hidden = focus && focus !== cam;
            return (
              <figure key={cam} className={`overflow-hidden rounded-xl border border-line bg-black ${hidden ? "hidden" : ""}`}>
                {videoErrors[cam] ? (
                  <div className="flex aspect-video items-center justify-center p-6 text-center text-sm text-white/80">
                    This browser cannot play the video (LeRobot encodes AV1). Try Chrome, Edge or Firefox.
                  </div>
                ) : (
                  <video
                    ref={(el) => {
                      videos.current[cam] = el;
                    }}
                    src={videoUrl(repo, cam, seg.chunk, seg.file)}
                    className="aspect-video w-full bg-black object-contain"
                    muted
                    playsInline
                    preload="auto"
                    onLoadedMetadata={(e) => (e.currentTarget.currentTime = seg.from_s + tRef.current)}
                    onError={() => setVideoErrors((v) => ({ ...v, [cam]: true }))}
                    onClick={() => setPlaying((p) => !p)}
                  />
                )}
                <figcaption className="flex items-center justify-between bg-surface px-3 py-1.5 text-xs text-ink-2">
                  <span className="capitalize">{cameraLabel(cam)}</span>
                  {cams.length > 1 && (
                    <button className="rounded px-1.5 py-0.5 text-muted transition hover:bg-surface-2 hover:text-ink" onClick={() => setFocus(focus ? null : cam)}>
                      {focus ? "Show all cameras" : "Enlarge"}
                    </button>
                  )}
                </figcaption>
              </figure>
            );
          })}
        </div>
      ) : (
        <div className="rounded-xl border border-dashed border-line p-4 text-sm text-muted">
          This dataset has no camera streams (the host may have run with <code>--no_cameras</code>). Joint data is shown below.
        </div>
      )}

      {/* Transport */}
      <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-line bg-surface shadow-[var(--shadow)] px-3 py-2">
        <button
          onClick={() => {
            if (!playing && t >= duration - 1 / fps) seek(0);
            setPlaying(!playing);
          }}
          className="flex size-10 shrink-0 items-center justify-center rounded-full bg-accent text-accent-ink shadow-sm transition hover:brightness-110 active:scale-95"
          title={playing ? "Pause (Space)" : "Play (Space)"}
          aria-label={playing ? "Pause" : "Play"}
        >
          {playing ? (
            <svg viewBox="0 0 16 16" className="size-4" fill="currentColor"><rect x="3" y="2" width="3.5" height="12" rx="1" /><rect x="9.5" y="2" width="3.5" height="12" rx="1" /></svg>
          ) : (
            <svg viewBox="0 0 16 16" className="size-4" fill="currentColor"><path d="M4 2.5v11a.5.5 0 0 0 .77.42l8.5-5.5a.5.5 0 0 0 0-.84l-8.5-5.5A.5.5 0 0 0 4 2.5z" /></svg>
          )}
        </button>
        <IconButton label="Previous frame (←)" disabled={frame <= 0} onClick={() => { setPlaying(false); seekFrame(frame - 1); }}>
          <svg viewBox="0 0 16 16" className="size-4" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden><path d="M10 3.5 5.5 8l4.5 4.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
        </IconButton>
        <IconButton label="Next frame (→)" disabled={frame >= lastFrame} onClick={() => { setPlaying(false); seekFrame(frame + 1); }}>
          <svg viewBox="0 0 16 16" className="size-4" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden><path d="m6 3.5 4.5 4.5L6 12.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
        </IconButton>
        <input
          type="range"
          min={0}
          max={lastFrame}
          step={1}
          value={frame}
          onChange={(e) => {
            setPlaying(false);
            seekFrame(Number(e.target.value));
          }}
          className="h-2 min-w-40 flex-1 cursor-pointer accent-[var(--accent)]"
          aria-label="Scrub"
        />
        <span className="tabular w-36 text-right text-xs text-ink-2">
          {t.toFixed(2)}s / {duration.toFixed(1)}s<br />
          <span className="text-muted">frame {frame} / {lastFrame}</span>
        </span>
        <Segmented size="sm" label="Playback speed" value={speed} onChange={setSpeed} options={SPEEDS.map((s) => ({ value: s, label: `${s}×` }))} />
        <Toggle size="sm" checked={loop} onChange={setLoop} label="Loop" />
      </div>
      <p className="-mt-2 text-xs text-muted">
        <Kbd>Space</Kbd> play/pause · <Kbd>←</Kbd> <Kbd>→</Kbd> step a frame (hold Shift for 1 s) · <Kbd>[</Kbd> <Kbd>]</Kbd> previous/next episode · click a chart to seek
      </p>

      {dataError && <ErrorBox>Could not load joint data: {dataError}</ErrorBox>}
      {data ? (
        <JointCharts data={data} frame={Math.min(frame, data.timestamps.length - 1)} onSeekFrame={(f) => { setPlaying(false); seekFrame(f); }} />
      ) : (
        !dataError && <p className="text-sm text-muted">Loading joint data…</p>
      )}
    </div>
  );
}
