/* The camera tab: preview, guide, auto-capture.
 *
 * getUserMedia needs a secure context. Through the SSH tunnel the app is on
 * localhost, which counts, so the camera works on the laptop running the
 * tunnel. Another device on plain HTTP is not a secure context, which is what
 * domain mode is for. When the context is not secure the tab says so and
 * points at Upload, which works everywhere.
 *
 * Two coordinate systems, and they matter:
 *   raw    the video's own pixels. What the detector sees and what is saved.
 *   view   the same frame mirrored, which is what the person sees.
 * The preview is mirrored because a preview that is not feels broken to move
 * in. The saved file is not mirrored, because a mirrored face is wrong on a
 * card. Everything handed to framing.js is view coordinates, so "move left"
 * means the direction the person sees themselves go.
 *
 * The rules are in framing.js and the detector is in detector.js. This file is
 * the wiring: the stream, the loop, the guide and the countdown.
 */

import * as detector from "./detector.js";
import * as framing from "./framing.js";
import { state, subscribe } from "./state.js";

/** Analysis rate. 15 fps is enough to feel live and leaves the CPU alone. */
const FRAME_MS = 66;

/** The grey crop the light and sharpness numbers are measured on. */
const CROP_PX = 128;

/** How long the framing may break before a running countdown is abandoned. */
const GRACE_MS = 500;

const $ = (id) => document.getElementById(id);

let stream = null;
let track = null;
let loopHandle = null;
let running = false;
let active = false;
let deviceId = "";
/** What the photo panel does with a captured frame: app.js's takePhoto. */
let take = async () => {};

/* Bumped by every start() and stop(). getUserMedia takes long enough that a
 * person can leave the tab while it is still deciding, and a stream handed to
 * nobody keeps the camera lamp on. */
let token = 0;

let lastAnalysis = 0;
let lastTimestamp = 0;
let okSince = null;
let lastOkAt = 0;
let countdownEnd = null;
let capturing = false;

/** The last STILL_FRAMES face boxes, in view coordinates. */
let recent = [];

/** One offscreen canvas for the light and sharpness sample, reused every frame. */
let cropContext = null;

/* ------------------------------------------------------------- the DOM -- */

function show(id, on) {
  $(id).hidden = !on;
}

/* SVG elements are not HTMLElements, so they have no `hidden` property: setting
 * one is silently a no-op and the attribute in the markup stays put. */
function showShape(id, on) {
  if (on) $(id).removeAttribute("hidden");
  else $(id).setAttribute("hidden", "");
}

function setHint(text, level = "") {
  const hint = $("cam-hint");
  hint.textContent = text;
  hint.dataset.level = level;
}

function setNote(text) {
  $("cam-note").textContent = text;
}

function blocked(lead, detail) {
  stop();
  show("cam", false);
  show("cam-blocked", true);
  $("cam-blocked-lead").textContent = lead;
  $("cam-blocked-sub").textContent = detail;
}

/** Lay the guide out for the current video size, in view coordinates. */
function drawGuide(width, height) {
  $("cam-guide").setAttribute("viewBox", `0 0 ${width} ${height}`);

  const [small, large] = framing.SIZE;
  const faceHeight = ((small + large) / 2) * height;
  const top = 0.16 * height;
  const centreX = width / 2;

  /* The ring is a head, not a face box. The detector's box runs brow to chin,
   * and on the fixture portrait a whole head is 1.4 times that tall with its
   * centre 0.18 of a box above the box's. Drawing the ring at box size would
   * have people fit their whole head inside it and be told to move closer
   * forever. */
  const radiusY = (faceHeight * 1.4) / 2;
  const radiusX = radiusY * 0.76;
  const centreY = top + faceHeight / 2 - faceHeight * 0.18;

  const ring = $("cam-ring");
  ring.setAttribute("cx", centreX);
  ring.setAttribute("cy", centreY);
  ring.setAttribute("rx", radiusX);
  ring.setAttribute("ry", radiusY);
  ring.setAttribute("stroke-width", Math.max(2, height * 0.006));

  // The shoulders, drawn from the head, so the outline says where to stand.
  const shoulderY = centreY + radiusY * 1.9;
  const shoulderX = radiusX * 2.9;
  const body = $("cam-body");
  body.setAttribute(
    "d",
    `M ${centreX - shoulderX} ${height}
     C ${centreX - shoulderX} ${shoulderY} ${centreX - radiusX * 1.2} ${centreY + radiusY * 1.2} ${centreX} ${centreY + radiusY * 1.15}
     C ${centreX + radiusX * 1.2} ${centreY + radiusY * 1.2} ${centreX + shoulderX} ${shoulderY} ${centreX + shoulderX} ${height}`,
  );
  body.setAttribute("stroke-width", Math.max(1.5, height * 0.004));
}

/** The ring and the stage wear one colour. From a step back the ring is a
 *  thin line, so the whole preview is framed in it too. */
function setLevel(level) {
  $("cam-ring").dataset.level = level;
  $("cam-stage").dataset.level = level;
}

function paint(result, box) {
  setLevel(result.level);
  const face = $("cam-face");
  showShape("cam-face", Boolean(box));
  if (box) {
    face.setAttribute("x", box.x);
    face.setAttribute("y", box.y);
    face.setAttribute("width", box.width);
    face.setAttribute("height", box.height);
  }
}

/* ------------------------------------------------------- the frame loop -- */

/** Mean luma and Laplacian variance of the face, from a 128 px grey crop.
 *
 * Square, taken from the raw frame, clamped to it. The threshold in framing.js
 * is only meaningful at this size, so the crop is always scaled to CROP_PX.
 */
function measure(video, box) {
  if (!cropContext) {
    const canvas = document.createElement("canvas");
    canvas.width = CROP_PX;
    canvas.height = CROP_PX;
    cropContext = canvas.getContext("2d", { willReadFrequently: true });
  }
  const side = Math.max(8, Math.min(box.width, box.height));
  const x = Math.max(0, Math.min(video.videoWidth - side, box.x + (box.width - side) / 2));
  const y = Math.max(0, Math.min(video.videoHeight - side, box.y + (box.height - side) / 2));
  try {
    cropContext.drawImage(video, x, y, side, side, 0, 0, CROP_PX, CROP_PX);
    return framing.sample(cropContext.getImageData(0, 0, CROP_PX, CROP_PX));
  } catch {
    // A frame that is not decodable yet. Skip the two optional checks.
    return { luma: null, sharpness: null };
  }
}

function analyse(now) {
  const video = $("cam-video");
  const width = video.videoWidth;
  const height = video.videoHeight;
  if (!width || !height) return;

  const timestamp = Math.max(now, lastTimestamp + 1);
  lastTimestamp = timestamp;

  if (!detector.ready()) {
    setHint("The framing guide is off. Use the shutter when you are ready.", "");
    paint({ level: "idle" }, null);
    return;
  }

  const found = detector.detect(video, timestamp);
  let result;
  let viewBox = null;
  if (found) {
    viewBox = framing.mirrorBox(found.box, width);
    recent.push(viewBox);
    if (recent.length > framing.STILL_FRAMES) recent.shift();
    const { luma, sharpness } = measure(video, found.box);
    result = framing.evaluate({
      frame: { width, height },
      face: viewBox,
      eyes: found.eyes ? found.eyes.map((point) => framing.mirrorPoint(point, width)) : null,
      luma,
      sharpness,
      recent,
    });
  } else {
    recent = [];
    result = framing.evaluate({ frame: { width, height }, face: null });
  }

  paint(result, viewBox);
  runCountdown(result, now);
}

/** Hold everything for HOLD_MS, then count 3, 2, 1, then take the photo. */
function runCountdown(result, now) {
  if (capturing) return;

  if (result.ok) {
    lastOkAt = now;
    if (okSince === null) okSince = now;
  } else if (countdownEnd === null) {
    okSince = null;
  }

  if (countdownEnd !== null) {
    if (now - lastOkAt > GRACE_MS) {
      countdownEnd = null;
      okSince = null;
      show("cam-count", false);
      setHint(result.hint || "hold still", result.level);
      return;
    }
    const left = countdownEnd - now;
    if (left <= 0) {
      show("cam-count", false);
      capture();
      return;
    }
    $("cam-count").textContent = String(Math.ceil(left / 1000));
    setHint("hold it", "green");
    return;
  }

  if (result.ok && now - okSince >= framing.HOLD_MS) {
    countdownEnd = now + framing.COUNTDOWN_FROM * 1000;
    $("cam-count").textContent = String(framing.COUNTDOWN_FROM);
    show("cam-count", true);
    setHint("hold it", "green");
    return;
  }

  setHint(result.ok ? "hold it" : result.hint, result.level);
}

function loop() {
  const video = $("cam-video");

  // requestVideoFrameCallback fires once per decoded frame, which is what is
  // wanted. A browser without it gets the paint clock instead.
  const schedule = (callback) => {
    loopHandle = video.requestVideoFrameCallback
      ? video.requestVideoFrameCallback((now) => callback(now))
      : requestAnimationFrame(callback);
  };

  const tick = (now) => {
    if (!running) return;
    const stamp = now ?? performance.now();
    if (stamp - lastAnalysis >= FRAME_MS) {
      lastAnalysis = stamp;
      try {
        analyse(stamp);
      } catch (error) {
        console.warn("framing:", error.message);
      }
    }
    schedule(tick);
  };

  schedule(tick);
}

/* --------------------------------------------------------- the capture -- */

/** The native photo when the browser has ImageCapture, else a canvas frame.
 *  Either way it is the raw frame, not the mirrored preview. */
async function grab(video) {
  if (track && typeof window.ImageCapture === "function") {
    try {
      const shot = await new window.ImageCapture(track).takePhoto();
      if (shot && shot.size > 0) return shot;
    } catch {
      // Plenty of cameras advertise it and then refuse. The canvas always works.
    }
  }
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.95));
}

export async function capture() {
  if (capturing) return;
  capturing = true;
  countdownEnd = null;
  okSince = null;
  show("cam-count", false);
  setHint("saving", "green");
  try {
    const blob = await grab($("cam-video"));
    if (!blob) throw new Error("The browser could not read a frame. Try the shutter again.");
    const name = `camera-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "")}.jpg`;
    await take(new File([blob], name, { type: blob.type }));
  } catch (error) {
    setHint(error.message, "red");
  } finally {
    capturing = false;
  }
}

/* ------------------------------------------------------- start and stop -- */

async function listDevices() {
  const select = $("cam-device");
  try {
    const cameras = (await navigator.mediaDevices.enumerateDevices()).filter((one) => one.kind === "videoinput");
    if (cameras.length < 2) {
      select.hidden = true;
      return;
    }
    select.replaceChildren(
      ...cameras.map((camera, index) => {
        const option = document.createElement("option");
        option.value = camera.deviceId;
        option.textContent = camera.label || `Camera ${index + 1}`;
        return option;
      }),
    );
    if (deviceId) select.value = deviceId;
    select.hidden = false;
  } catch {
    select.hidden = true;
  }
}

async function start() {
  if (running || state.photo) return;
  const mine = ++token;
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    blocked(
      "The camera needs https:// or localhost.",
      "Through make tunnel this page is on localhost, so the camera works there. " +
        "Another device needs the node in domain mode. Upload works everywhere.",
    );
    return;
  }

  show("cam-blocked", false);
  show("cam", true);
  setHint("starting the camera", "");
  setNote("");

  const video = $("cam-video");
  const wanted = {
    video: {
      facingMode: "user",
      width: { ideal: 1920 },
      height: { ideal: 1080 },
      ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
    },
    audio: false,
  };

  let opened;
  try {
    opened = await navigator.mediaDevices.getUserMedia(wanted);
  } catch (error) {
    const name = error?.name || "";
    if (name === "NotAllowedError" || name === "SecurityError") {
      blocked("The browser refused the camera.", "Allow the camera for this page and open the tab again, or use Upload.");
    } else if (name === "NotFoundError" || name === "OverconstrainedError") {
      blocked("No camera on this machine.", "Use Upload, or open the app on a device with a camera.");
    } else {
      blocked("The camera did not start.", error?.message || String(error));
    }
    return;
  }

  if (mine !== token) {
    // The tab changed, or a photo arrived, while the browser was deciding.
    for (const one of opened.getTracks()) one.stop();
    return;
  }

  stream = opened;
  track = stream.getVideoTracks()[0];
  video.srcObject = stream;
  try {
    await video.play();
  } catch {
    // Autoplay on a muted, inline video. Rare, and the loop copes.
  }

  running = true;
  recent = [];
  okSince = null;
  countdownEnd = null;
  lastAnalysis = 0;
  lastTimestamp = 0;

  const size = () => {
    if (!video.videoWidth) return;
    $("cam-stage").style.aspectRatio = `${video.videoWidth} / ${video.videoHeight}`;
    drawGuide(video.videoWidth, video.videoHeight);
  };
  size();
  video.addEventListener("loadedmetadata", size, { once: true });

  listDevices();
  setHint("looking for a face", "");

  await detector.load();
  if (mine !== token) return;
  setNote(
    detector.ready()
      ? "The framing guide runs in this browser on BlazeFace, Apache 2.0, served from this node. No frame leaves the page until the photo is taken."
      : "The framing guide is off: the face detector did not load. The api image fetches it, and make web-vendor fetches it for a checkout. The shutter still works.",
  );
  loop();
}

function stop() {
  running = false;
  token += 1;
  if (loopHandle !== null) {
    const video = $("cam-video");
    if (video.cancelVideoFrameCallback && video.requestVideoFrameCallback) video.cancelVideoFrameCallback(loopHandle);
    else cancelAnimationFrame(loopHandle);
    loopHandle = null;
  }
  if (stream) {
    for (const one of stream.getTracks()) one.stop();
    stream = null;
    track = null;
  }
  $("cam-video").srcObject = null;
  // A retake starts grey, not on the green the last capture ended on.
  setLevel("idle");
  show("cam-count", false);
  okSince = null;
  countdownEnd = null;
  recent = [];
}

/* ------------------------------------------------------------- wiring -- */

/** Called when the photo panel changes tab. The camera only runs on its own tab. */
export function setActive(on) {
  active = on;
  sync();
}

/** The camera runs while its tab is on, there is no photo yet, and the form
 *  is on show. A stream left open behind a hidden panel keeps the lamp on. */
function sync() {
  const wanted = active && !state.photo && !state.result && state.signedIn;
  if (wanted && !running) start();
  else if (!wanted && running) stop();
}

/** `onPhoto(file)` is what the photo panel does with a frame: it shrinks it,
 *  measures it and puts it in the thumb box. */
export function wireCamera(onPhoto) {
  take = onPhoto;
  $("cam-shutter").addEventListener("click", () => capture());
  $("cam-device").addEventListener("change", async (event) => {
    deviceId = event.target.value;
    stop();
    await start();
  });
  // A photo, from either tab, stops the camera. Retake starts it again.
  subscribe((what) => {
    if (what === "photo" || what === "all" || what === "result") sync();
  });
}
