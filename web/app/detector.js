/* The face detector, and what the app does when it is not there.
 *
 * MediaPipe Tasks Vision 1.0.1 with the BlazeFace short-range model, both
 * Apache 2.0, both served from this node out of /vendor. make web-vendor puts
 * them there and checks them against web/vendor.lock, and the api image runs
 * the same script, so the page never asks a CDN for anything.
 *
 * web/vendor is gitignored, so an api run from a fresh clone has no files and
 * the import 404s. The pinned wasm build is also the SIMD one, so a browser
 * without WebAssembly SIMD fails the same way. Neither is fatal: the camera
 * keeps its preview and its shutter button and drops the guide, and a photo
 * goes without a face box.
 *
 * Boxes come back in the video's own pixels, unmirrored. framing.mirrorBox()
 * turns them into what the person sees.
 *
 * Two detectors use one copy of the bundle and the wasm. The camera's runs in
 * VIDEO mode, once a frame, on timestamps that only go up. detectStill() runs
 * on a second one in IMAGE mode, made the first time it is asked for, and
 * measures the photo that is about to be sent, from either tab.
 */

const BUNDLE = "/vendor/tasks-vision/vision_bundle.mjs";
const WASM_DIR = "/vendor/tasks-vision/wasm";
const MODEL = "/vendor/models/blaze_face_short_range.tflite";

const MIN_CONFIDENCE = 0.5;

/** BlazeFace returns six keypoints in this order. Only the eyes are used. */
const RIGHT_EYE = 0;
const LEFT_EYE = 1;

/* Where detectStill() looks, as x, y, width and height fractions of the photo.
 * BlazeFace short range sees a 128 px square, so on the whole photo a face
 * under about a tenth of its height shrinks to a few pixels and is missed. The
 * two crops zoom in where a standing person's face is: the top 60 percent by
 * the middle 60, then the top 40 by the middle 40. */
export const PASSES = [
  [0, 0, 1, 1],
  [0.2, 0, 0.6, 0.6],
  [0.3, 0, 0.4, 0.4],
];

let detector = null;
let status = "idle"; // idle | loading | ready | unavailable

/** The one load in flight, so two quick tab switches do not build two. */
let loading = null;

/** The bundle and the wasm, loaded once for both detectors. */
let runtime = null;

/** The IMAGE-mode detector, as a promise of it or of null. */
let still = null;

export function ready() {
  return status === "ready";
}

/** Load the camera's detector once. Returns it, or null when there will not be one. */
export function load() {
  if (status === "ready" || status === "unavailable") return Promise.resolve(detector);
  if (!loading) loading = open();
  return loading;
}

function loadRuntime() {
  if (!runtime) {
    runtime = import(BUNDLE).then(async (vision) => ({
      vision,
      fileset: await vision.FilesetResolver.forVisionTasks(WASM_DIR),
    }));
  }
  return runtime;
}

async function create(runningMode) {
  const { vision, fileset } = await loadRuntime();
  return vision.FaceDetector.createFromOptions(fileset, {
    baseOptions: { modelAssetPath: MODEL, delegate: "CPU" },
    runningMode,
    minDetectionConfidence: MIN_CONFIDENCE,
  });
}

async function open() {
  status = "loading";
  try {
    detector = await create("VIDEO");
    status = "ready";
  } catch (error) {
    detector = null;
    status = "unavailable";
    console.warn("no framing guide:", error?.message || String(error));
  }
  return detector;
}

/** The biggest face in one video frame, in the video's own pixels.
 *
 * Returns { box: {x, y, width, height}, eyes: [{x, y}, {x, y}] | null, score },
 * or null when there is no face, no detector, or the frame is not ready.
 */
export function detect(video, timestampMs) {
  if (status !== "ready" || !detector) return null;
  const width = video.videoWidth;
  const height = video.videoHeight;
  if (!width || !height) return null;

  let result;
  try {
    result = detector.detectForVideo(video, timestampMs);
  } catch {
    // A frame the wasm build refused. The next one usually works.
    return null;
  }

  const detections = result?.detections || [];
  if (detections.length === 0) return null;

  let best = detections[0];
  for (const detection of detections) {
    const area = detection.boundingBox.width * detection.boundingBox.height;
    if (area > best.boundingBox.width * best.boundingBox.height) best = detection;
  }

  const box = best.boundingBox;
  const points = best.keypoints || [];
  // Keypoints are normalised, the box is in pixels. Not a typo, that is the API.
  const eye = (index) => (points[index] ? { x: points[index].x * width, y: points[index].y * height } : null);
  const right = eye(RIGHT_EYE);
  const left = eye(LEFT_EYE);

  return {
    box: { x: box.originX, y: box.originY, width: box.width, height: box.height },
    eyes: right && left ? [right, left] : null,
    score: best.categories?.[0]?.score ?? 0,
  };
}

/** Make the IMAGE-mode detector, once. Resolves to it, or to null. */
export function loadStill() {
  if (!still) {
    still = create("IMAGE").catch((error) => {
      console.warn("no face measurement:", error?.message || String(error));
      return null;
    });
  }
  return still;
}

/** The best detection in one pass of PASSES, in the canvas's own pixels, or null. */
export async function scan(canvas, [fx, fy, fw, fh]) {
  const model = await loadStill();
  if (!model || !canvas.width || !canvas.height) return null;

  const left = Math.round(fx * canvas.width);
  const top = Math.round(fy * canvas.height);
  const width = Math.max(1, Math.round(fw * canvas.width));
  const height = Math.max(1, Math.round(fh * canvas.height));

  // A crop is copied pixel for pixel. The detector does its own scaling.
  let source = canvas;
  if (width !== canvas.width || height !== canvas.height) {
    source = document.createElement("canvas");
    source.width = width;
    source.height = height;
    source.getContext("2d").drawImage(canvas, left, top, width, height, 0, 0, width, height);
  }

  let result;
  try {
    result = model.detect(source);
  } catch {
    return null;
  }
  let best = null;
  for (const detection of result?.detections || []) {
    const score = detection.categories?.[0]?.score ?? 0;
    if (best && score <= best.score) continue;
    const box = detection.boundingBox;
    best = { box: { x: box.originX + left, y: box.originY + top, width: box.width, height: box.height }, score };
  }
  return best;
}

/** The face in a still photo, in the canvas's own pixels.
 *
 * Every pass in PASSES, and the detection with the highest score wins. Returns
 * { box: {x, y, width, height}, score }, or null when no pass finds a face or
 * the detector files are missing. The box can run past the edge of the canvas.
 * photo.js clamps it.
 */
export async function detectStill(canvas) {
  let best = null;
  for (const pass of PASSES) {
    const found = await scan(canvas, pass);
    if (found && (!best || found.score > best.score)) best = found;
  }
  return best;
}
