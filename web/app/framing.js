/* The framing rules, as arithmetic over a face box.
 *
 * Nothing here touches the DOM, the camera or the detector. It takes numbers
 * and returns a verdict, so tests/integration/test_app_camera.py can feed it
 * made-up boxes and check every row of the table without a camera.
 *
 * Coordinates are VIEW coordinates: the frame as the person sees it on screen,
 * which is mirrored. mirrorBox() converts a detector box into them. Getting
 * that wrong swaps "move left" and "move right", which in a mirror is the one
 * mistake a person cannot work around.
 *
 * The table, with the thresholds below it:
 *
 *   centered   face centre within 8% of the frame width of the middle
 *   size       face height 18 to 30% of the frame height
 *   headroom   face top at least 8% of the frame height below the top edge
 *   level      the line between the eyes under 8 degrees off horizontal
 *   light      mean luma of the face crop between 70 and 200
 *   sharp      Laplacian variance of a 128 px grey face crop above SHARP_MIN
 *   still      the face box moved under 2% of the frame width over 10 frames
 */

/** Face centre may sit this far from the middle, as a fraction of frame width. */
const CENTER_TOL = 0.08;

/** Face height as a fraction of frame height. */
export const SIZE = [0.18, 0.3];

/** The top of the face must be at least this far below the top edge. */
const HEADROOM_MIN = 0.08;

/** Degrees off horizontal the eye line may be. */
const LEVEL_MAX_DEG = 8;

/** Mean luma of the face crop, 0 to 255. */
export const LUMA = [70, 200];

/* Laplacian variance of the 128 px grey face crop, below which the frame is
 * called soft.
 *
 * Calibrated through this same code on the 640x480 frame the tests cut out of
 * tests/fixtures/portrait.jpg. That frame's face crop measures 2883. The same
 * crop after a Gaussian blur measures 115 at 1 px, 13 at 2 px and 5 at 3 px. A
 * head moving at 1/30 s smears about 2 to 4 px at this size, so anything the
 * shutter should refuse is under 20 and anything it should take is over 2000.
 * 120 sits in that gap, just above the 1 px case, so a frame that is only
 * slightly soft is refused too. test_a_blurred_frame_is_called_soft asserts
 * both ends. */
export const SHARP_MIN = 120;

/** How far the face box may drift, as a fraction of frame width. */
const STILL_MAX = 0.02;

/** Frames the stillness window holds. About 0.7 s at the loop's 15 fps. */
export const STILL_FRAMES = 10;

/** Everything has to pass for this long before the countdown starts. */
export const HOLD_MS = 1500;

/** The countdown, in seconds. */
export const COUNTDOWN_FROM = 3;

/** A detector box into view coordinates. The preview is mirrored, the file is not. */
export function mirrorBox(box, frameWidth) {
  return { ...box, x: frameWidth - box.x - box.width };
}

/** A detector point into view coordinates. */
export function mirrorPoint(point, frameWidth) {
  return { ...point, x: frameWidth - point.x };
}

/** Mean luma and Laplacian variance of an ImageData-shaped { data, width, height }.
 *
 * Pure over the pixel array, so a test can build one by hand. The caller draws
 * the face crop into a 128 px canvas first: the threshold is only meaningful at
 * one size, and a variance measured on a 1080 px crop is a different number.
 */
export function sample(image) {
  const { data, width, height } = image;
  const grey = new Float32Array(width * height);
  let total = 0;
  for (let i = 0, p = 0; p < grey.length; i += 4, p += 1) {
    const value = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
    grey[p] = value;
    total += value;
  }
  const luma = grey.length > 0 ? total / grey.length : 0;

  // The 4-neighbour Laplacian, over the interior only.
  let sum = 0;
  let sumSquares = 0;
  let count = 0;
  for (let y = 1; y < height - 1; y += 1) {
    for (let x = 1; x < width - 1; x += 1) {
      const p = y * width + x;
      const edge = grey[p - width] + grey[p + width] + grey[p - 1] + grey[p + 1] - 4 * grey[p];
      sum += edge;
      sumSquares += edge * edge;
      count += 1;
    }
  }
  const mean = count > 0 ? sum / count : 0;
  const sharpness = count > 0 ? sumSquares / count - mean * mean : 0;
  return { luma, sharpness };
}

/** How far the face drifted across a window of boxes, as a fraction of frame width.
 *
 * The spread of the centres around their own mean, not the distance between the
 * first and the last, so a face that wanders out and back still counts as moving.
 * Null when there is not enough history to say.
 */
function stillness(boxes, frameWidth) {
  if (!Array.isArray(boxes) || boxes.length < 2 || !frameWidth) return null;
  const window = boxes.slice(-STILL_FRAMES);
  const centres = window.map((box) => ({ x: box.x + box.width / 2, y: box.y + box.height / 2 }));
  const mean = centres.reduce(
    (acc, point) => ({ x: acc.x + point.x / centres.length, y: acc.y + point.y / centres.length }),
    { x: 0, y: 0 },
  );
  let worst = 0;
  for (const point of centres) {
    worst = Math.max(worst, Math.hypot(point.x - mean.x, point.y - mean.y));
  }
  return worst / frameWidth;
}

/** Degrees the eye line is off horizontal, or null when there are no eyes. */
function eyeAngle(eyes) {
  if (!Array.isArray(eyes) || eyes.length < 2) return null;
  const [left, right] = eyes;
  const degrees = Math.abs((Math.atan2(right.y - left.y, right.x - left.x) * 180) / Math.PI);
  // Eye order is the detector's, so a 180 degree line is still level.
  return degrees > 90 ? 180 - degrees : degrees;
}

function check(name, ok, hint, value) {
  return { name, ok, hint: ok ? "" : hint, value };
}

/** Judge one frame.
 *
 * sample: {
 *   frame:  { width, height },        the video's own pixels
 *   face:   { x, y, width, height },  view coordinates, or null when none was found
 *   eyes:   [{ x, y }, { x, y }],     view coordinates, optional
 *   luma:   0 to 255,                 optional
 *   sharpness: Laplacian variance,    optional
 *   recent: [box, ...],               view coordinates, newest last, optional
 * }
 *
 * Returns { ok, hint, level, checks }. `hint` is the first failing check's, so
 * the person is told one thing at a time. `level` colours the ring: green when
 * everything passes, amber when one thing is off, red when more than one is,
 * or when there is no face at all.
 *
 * A missing measurement passes. The detector can lose the eye keypoints for a
 * frame or two, and refusing to capture over that would feel broken.
 */
export function evaluate(frameSample) {
  const frame = frameSample.frame || { width: 0, height: 0 };
  const face = frameSample.face;

  if (!face || !frame.width || !frame.height) {
    return { ok: false, hint: "step into the frame", level: "red", checks: [] };
  }

  const centre = (face.x + face.width / 2 - frame.width / 2) / frame.width;
  const size = face.height / frame.height;
  const [small, large] = SIZE;
  const headroom = face.y / frame.height;
  const angle = eyeAngle(frameSample.eyes);
  const { luma, sharpness } = frameSample;
  const drift = stillness(frameSample.recent, frame.width);

  const checks = [
    check("centered", Math.abs(centre) <= CENTER_TOL, centre > 0 ? "move left" : "move right", centre),
    check("size", size >= small && size <= large, size < small ? "move closer" : "move back", size),
    check("headroom", headroom >= HEADROOM_MIN, "lower the camera", headroom),
    check("level", angle === null || angle <= LEVEL_MAX_DEG, "level your head", angle),
    check(
      "light",
      luma === undefined || luma === null || (luma >= LUMA[0] && luma <= LUMA[1]),
      luma < LUMA[0] ? "more light" : "too bright",
      luma ?? null,
    ),
    check("sharp", sharpness === undefined || sharpness === null || sharpness >= SHARP_MIN, "hold still", sharpness ?? null),
    check("still", drift === null || drift <= STILL_MAX, "hold still", drift),
  ];

  const failed = checks.filter((one) => !one.ok);
  return {
    ok: failed.length === 0,
    hint: failed.length > 0 ? failed[0].hint : "",
    level: failed.length === 0 ? "green" : failed.length === 1 ? "amber" : "red",
    checks,
  };
}
