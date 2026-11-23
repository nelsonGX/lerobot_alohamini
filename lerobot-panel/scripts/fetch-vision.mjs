// Puts the face model files under public/mediapipe/ so the iPad loads them from the panel (runs before `next build`).
// The WASM runtime comes from node_modules; the model (~4 MB) is downloaded once from Google.
import { copyFileSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const out = join(root, "public", "mediapipe");
const MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task";

mkdirSync(join(out, "wasm"), { recursive: true });
for (const f of ["vision_wasm_internal.js", "vision_wasm_internal.wasm"]) {
  copyFileSync(join(root, "node_modules", "@mediapipe", "tasks-vision", "wasm", f), join(out, "wasm", f));
}
const model = join(out, "face_landmarker.task");
if (!existsSync(model)) {
  const res = await fetch(MODEL_URL);
  if (!res.ok) {
    console.warn(`fetch-vision: could not download the face model (${res.status}); the kiosk camera will be off.`);
  } else {
    writeFileSync(model, Buffer.from(await res.arrayBuffer()));
    console.log("fetch-vision: downloaded face_landmarker.task");
  }
}
