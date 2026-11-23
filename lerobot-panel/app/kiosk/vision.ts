// iPad front camera -> "is someone at the counter, facing the robot, with their lips moving?"
// Runs Google's MediaPipe face landmarker in the browser (~10 fps). Frames never leave the iPad;
// only the summary below goes to the panel with each utterance.
import type { FaceLandmarker } from "@mediapipe/tasks-vision";

const FPS = 10;
const HISTORY = 300; // samples kept (~30 s)
const NOSE = 1;
const EYE_L = 33; // outer eye corners
const EYE_R = 263;
const FACING_YAW = 0.3; // nose offset from the eyes' midpoint, in eye widths; above this the head is turned
export const LIPS_STD = 0.035; // jawOpen wobble (std dev) that counts as talking

export type FaceBox = { x0: number; y0: number; x1: number; y1: number; size: number; facing: boolean; jaw: number | null };
export type Sample = { t: number; near: boolean; facing: boolean; jaw: number | null; faces: number; size: number };
export type Vision = {
  state: "off" | "loading" | "on" | "error";
  error: string;
  samples: Sample[];
  boxes: FaceBox[]; // in whole-picture coordinates
  video: HTMLVideoElement | null;
  crop: Crop; // the part of the picture the face model looks at
};
/** What the camera saw while one utterance was spoken. */
export type VisionSummary = { present: number; facing: number; mouth: number; lips: boolean };

export function newVision(): Vision {
  return { state: "off", error: "", samples: [], boxes: [], video: null, crop: { x: 0, y: 0, w: 1, h: 1 } };
}

export type VisionOptions = {
  minSize: number; // face width (fraction of the whole picture) that counts as "at the counter"
  zoom: number; // look only at 1/zoom of the picture, enlarged (the face model misses small faces)
  cropY: number; // where that part sits: 0 top, 0.5 middle, 1 bottom (horizontally it is always centred)
};

/** Start the camera and the face model; returns a stop function. */
export async function startVision(v: Vision, opts: () => VisionOptions): Promise<() => void> {
  v.state = "loading";
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let stream: MediaStream | null = null;
  let landmarker: FaceLandmarker | null = null;
  const stop = () => {
    stopped = true;
    clearTimeout(timer);
    stream?.getTracks().forEach((t) => t.stop());
    landmarker?.close();
    v.video?.remove();
    v.video = null;
    v.state = "off";
  };
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } }, audio: false });
    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.setAttribute("playsinline", "");
    // iOS stops decoding hidden videos, so keep it "visible" but tiny and transparent.
    Object.assign(video.style, { position: "fixed", left: "0", top: "0", width: "2px", height: "2px", opacity: "0", pointerEvents: "none" });
    document.body.appendChild(video);
    video.srcObject = stream;
    await video.play();
    v.video = video;

    const { FaceLandmarker, FilesetResolver } = await import("@mediapipe/tasks-vision");
    const files = await FilesetResolver.forVisionTasks("/mediapipe/wasm");
    const options = (delegate: "GPU" | "CPU") => ({
      baseOptions: { modelAssetPath: "/mediapipe/face_landmarker.task", delegate },
      runningMode: "VIDEO" as const,
      numFaces: 4,
      outputFaceBlendshapes: true,
    });
    landmarker = await FaceLandmarker.createFromOptions(files, options("GPU")).catch(() => FaceLandmarker.createFromOptions(files, options("CPU")));
    if (stopped) { landmarker.close(); return stop; }
    v.state = "on";

    const crop = document.createElement("canvas");
    const tick = () => {
      if (stopped || !landmarker) return;
      try {
        const { minSize, zoom, cropY } = opts();
        const z = Math.max(1, zoom);
        const vw = video.videoWidth, vh = video.videoHeight;
        if (video.readyState >= 2 && vw && vh) {
          crop.width = Math.round(vw / z);
          crop.height = Math.round(vh / z);
          const y = Math.min(1, Math.max(0, cropY)) * (1 - 1 / z); // top of the crop, fraction of the picture
          crop.getContext("2d")?.drawImage(video, (vw - crop.width) / 2, y * vh, crop.width, crop.height, 0, 0, crop.width, crop.height);
          v.crop = { x: (1 - 1 / z) / 2, y, w: 1 / z, h: 1 / z };
          record(v, landmarker.detectForVideo(crop, performance.now()), minSize, v.crop);
        }
      } catch (e) {
        v.error = String(e);
      }
      timer = setTimeout(tick, 1000 / FPS);
    };
    tick();
  } catch (e) {
    stop();
    v.state = "error";
    v.error = e instanceof Error ? e.message : String(e);
  }
  return stop;
}

type Crop = { x: number; y: number; w: number; h: number };

function record(v: Vision, result: ReturnType<FaceLandmarker["detectForVideo"]>, minSize: number, crop: Crop) {
  const boxes: FaceBox[] = result.faceLandmarks.map((pts, i) => {
    let x0 = 1, y0 = 1, x1 = 0, y1 = 0;
    for (const p of pts) { x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y); x1 = Math.max(x1, p.x); y1 = Math.max(y1, p.y); }
    // crop coordinates -> whole picture
    [x0, y0, x1, y1] = [crop.x + x0 * crop.w, crop.y + y0 * crop.h, crop.x + x1 * crop.w, crop.y + y1 * crop.h];
    const l = pts[EYE_L], r = pts[EYE_R], n = pts[NOSE];
    const eyes = Math.abs(r.x - l.x) || 1e-6;
    const yaw = (n.x - (l.x + r.x) / 2) / eyes;
    const jaw = result.faceBlendshapes[i]?.categories.find((c) => c.categoryName === "jawOpen")?.score ?? null;
    return { x0, y0, x1, y1, size: x1 - x0, facing: Math.abs(yaw) < FACING_YAW, jaw };
  });
  v.boxes = boxes;
  const main = boxes.reduce<FaceBox | null>((a, b) => (!a || b.size > a.size ? b : a), null); // the closest face
  v.samples.push({
    t: Date.now(), faces: boxes.length, size: main?.size ?? 0,
    near: !!main && main.size >= minSize, facing: !!main && main.facing, jaw: main && main.size >= minSize ? main.jaw : null,
  });
  if (v.samples.length > HISTORY) v.samples.splice(0, v.samples.length - HISTORY);
}

/** Summary of [since, until]; null when the camera isn't running (then nothing is gated on it). */
export function summarize(v: Vision, since: number, until = Date.now()): VisionSummary | null {
  if (v.state !== "on") return null;
  const win = v.samples.filter((s) => s.t >= since && s.t <= until);
  if (win.length < 3) return null;
  const near = win.filter((s) => s.near);
  const jaws = near.map((s) => s.jaw).filter((j): j is number => j !== null);
  const mean = jaws.reduce((a, b) => a + b, 0) / (jaws.length || 1);
  const std = Math.sqrt(jaws.reduce((a, b) => a + (b - mean) ** 2, 0) / (jaws.length || 1));
  return {
    present: near.length / win.length,
    facing: near.length ? near.filter((s) => s.facing).length / near.length : 0,
    mouth: Math.round(std * 1000) / 1000,
    lips: jaws.length >= 3 && std >= LIPS_STD,
  };
}

/** Is someone at the counter right now (most of the last `ms`)? */
export function nearNow(v: Vision, ms = 1000): boolean | null {
  const s = summarize(v, Date.now() - ms);
  return s ? s.present >= 0.5 : null;
}
