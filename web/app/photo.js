/* A chosen photo, shrunk and measured before it is sent.
 *
 * The api takes 15 MB and scales to 2048 px itself. Doing it here too means a
 * 12 MB phone photo goes over the wire as a few hundred KB. The api still does
 * its own decode, EXIF strip and downscale: this is a convenience, not a trust
 * boundary.
 *
 * `imageOrientation: "from-image"` makes the browser apply the EXIF rotation
 * while it decodes, so the pixels handed over are the right way up and carry
 * no EXIF at all.
 *
 * The same canvas is measured for a face before it is sent, and the box goes
 * to the api with the photo, in the pixels of the JPEG it describes. The card
 * places the person by it.
 */

import * as detector from "./detector.js";

const MAX_PX = 2048;
const MAX_INPUT_BYTES = 40 * 1000 * 1000;
const QUALITY = 0.92;

/** The api refuses a face box with a side under 16 px, so a smaller one is not sent. */
const MIN_FACE_PX = 16;

function fit(width, height) {
  const long = Math.max(width, height);
  if (long <= MAX_PX) return [width, height];
  const scale = MAX_PX / long;
  return [Math.round(width * scale), Math.round(height * scale)];
}

/** A File or Blob in, a { blob, url, name, width, height, bytes, face } out.
 *
 * `face` is { x, y, width, height } in the prepared image's pixels, null when
 * the detector found no face, or undefined when there is no detector to ask.
 */
export async function prepare(file) {
  if (!file.type.startsWith("image/")) {
    throw new Error(`${file.name || "That file"} is not an image. Choose a JPEG, PNG or WebP.`);
  }
  if (file.size > MAX_INPUT_BYTES) {
    throw new Error(`That photo is ${formatBytes(file.size)}. The limit is ${formatBytes(MAX_INPUT_BYTES)}.`);
  }

  let bitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch {
    throw new Error("The browser could not read that image. Try a JPEG or a PNG.");
  }

  const [width, height] = fit(bitmap.width, bitmap.height);
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  context.imageSmoothingQuality = "high";
  context.drawImage(bitmap, 0, 0, width, height);
  bitmap.close();

  // toBlob copies the canvas when it is called and encodes off the main
  // thread, so the detector reads the same pixels while the JPEG is made.
  const encoded = new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", QUALITY));
  const model = await detector.loadStill();
  const found = model ? await detector.detectStill(canvas).catch(() => null) : null;
  const blob = await encoded;
  if (!blob) throw new Error("The browser could not re-encode that image. Try a JPEG or a PNG.");

  return {
    blob,
    url: URL.createObjectURL(blob),
    name: file.name || "photo.jpg",
    width,
    height,
    bytes: blob.size,
    face: model ? clampFace(found, width, height) : undefined,
  };
}

/** The detector's box, clamped to the photo and snapped to whole pixels, or null.
 *
 * The api takes four whole numbers and answers 400 for a box one pixel past the
 * edge, and a 400 means no card. Rounding the four edges, not the origin and
 * the size, keeps x + width and y + height inside the photo.
 */
export function clampFace(found, width, height) {
  const box = found?.box;
  if (!box || ![box.x, box.y, box.width, box.height].every(Number.isFinite)) return null;
  const left = Math.max(0, Math.round(box.x));
  const top = Math.max(0, Math.round(box.y));
  const right = Math.min(width, Math.round(box.x + box.width));
  const bottom = Math.min(height, Math.round(box.y + box.height));
  if (right - left < MIN_FACE_PX || bottom - top < MIN_FACE_PX) return null;
  return { x: left, y: top, width: right - left, height: bottom - top };
}

export function formatBytes(bytes) {
  if (bytes < 1000) return `${bytes} B`;
  if (bytes < 1000 * 1000) return `${Math.round(bytes / 1000)} KB`;
  return `${(bytes / 1000 / 1000).toFixed(1)} MB`;
}
