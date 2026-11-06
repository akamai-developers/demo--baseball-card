/* The app's state, and the shapes it is built from.
 *
 * One object, one subscribe(). Anything that changes what is on screen goes
 * through update(), which tells the listeners. There is no framework here on
 * purpose: the whole app is a few views of plain DOM.
 *
 * tests/unit/test_app_data.py holds every list below to the file it copies:
 * styles.json, common/schemas.py, api/pipeline.py and examples/stats.json.
 */

/** The two styles in packages/card/src/card/styles.json. A style is a
 *  template and a prompt, so these words are all the page knows of one. */
export const STYLES = [
  { name: "akamai", label: "Akamai", look: "A navy frame with the Akamai logo, over a stadium at night." },
  { name: "classic", label: "Classic", look: "A plain cream border with no logo, over a ballpark by day." },
];

/** The six swing numbers the api takes, in the order of METRICS in
 *  common/schemas.py, with its ranges. `step` is the slider's, `value` where
 *  it starts. Each is off until it is ticked, so nothing goes on a card that
 *  was not measured. */
export const METRICS = [
  { key: "bat_speed_mph", label: "Bat speed", unit: "mph", min: 50, max: 85, step: 0.1, value: 68.2 },
  { key: "exit_velo_mph", label: "Exit velocity", unit: "mph", min: 60, max: 115, step: 0.1, value: 88.5 },
  { key: "launch_angle_deg", label: "Launch angle", unit: "deg", min: -10, max: 35, step: 0.5, value: 14 },
  { key: "swing_length_ft", label: "Swing length", unit: "ft", min: 4.5, max: 10, step: 0.1, value: 7.1 },
  { key: "attack_angle_deg", label: "Attack angle", unit: "deg", min: -5, max: 25, step: 0.5, value: 9 },
  { key: "time_to_contact_ms", label: "Time to contact", unit: "ms", min: 100, max: 220, step: 1, value: 155 },
];

/** Positions that fit the api's 4-character limit. */
export const POSITIONS = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH", "SP", "RP", "UTIL"];

/** The six steps, in the order api/pipeline.py lists them on a job. */
export const STEPS = ["cutout", "art", "comps", "text", "compose", "render"];

/** examples/stats.json, for the Sample player button. */
export const SAMPLE = {
  name: "Sam Rivera",
  number: 23,
  position: "SS",
  bats: "R",
  throws: "R",
  height_in: 72,
  weight_lb: 190,
  hometown: "Chicago, IL",
  metrics: {
    bat_speed_mph: 68.2,
    exit_velo_mph: 88.5,
    launch_angle_deg: 14.0,
    swing_length_ft: 7.1,
    attack_angle_deg: 9.0,
    time_to_contact_ms: 155,
  },
};

/** The player fields, as stats.json names them. */
export const FIELDS = ["name", "number", "position", "bats", "throws", "height_in", "weight_lb", "hometown"];

export const state = {
  signedIn: false,
  // GET /healthz, /gpu and /models, as last read. Null until then.
  health: null,
  gpus: null,
  models: null,
  // { blob, url, name, width, height, bytes, face } once a photo is chosen.
  photo: null,
  // Each field as typed, "" when empty.
  player: Object.fromEntries(FIELDS.map((key) => [key, ""])),
  // key -> { value, on }. Off until ticked.
  metrics: Object.fromEntries(METRICS.map((m) => [m.key, { value: m.value, on: false }])),
  style: STYLES[0].name,
  // The card this tab made last, as results.js tracks it, and whether Make a
  // card shows it in place of the form.
  card: null,
  result: false,
};

const listeners = new Set();

export function subscribe(listener) {
  listeners.add(listener);
}

/** Tell everyone something changed. `what` lets a listener skip work. */
export function update(what = "all") {
  for (const listener of listeners) listener(what);
}

/** "akamai" is "Akamai". A style the page does not know keeps its name. */
export function styleLabel(name) {
  return STYLES.find((style) => style.name === name)?.label || name;
}

/** The stats.json body for the card: the fields that were filled in, and the
 *  numbers that were ticked. */
export function statsFor() {
  const body = {};
  for (const key of FIELDS) {
    const value = String(state.player[key]).trim();
    if (value === "") continue;
    body[key] = ["number", "height_in", "weight_lb"].includes(key) ? Number(value) : value;
  }
  const metrics = {};
  for (const { key } of METRICS) {
    if (state.metrics[key].on) metrics[key] = state.metrics[key].value;
  }
  if (Object.keys(metrics).length > 0) body.metrics = metrics;
  return body;
}

/** Why Generate is off, or "" when it is not. */
export function whyNotReady() {
  if (!state.photo) return "Add a photo and a name to start.";
  if (!state.player.name.trim()) return "Type a name. It goes on the front.";
  return "";
}
