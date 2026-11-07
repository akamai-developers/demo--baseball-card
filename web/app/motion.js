/* One motion system.
 *
 * The durations and the two curves are custom properties at the top of
 * app.css, and nothing else in the app writes one down:
 *
 *   --ak-dur-fast     150 ms  hover, focus, colour
 *   --ak-dur-base     200 ms  numbers, bars, a view coming on show
 *   --ak-dur-slow     350 ms  a card dealing in
 *   --ak-dur-flip     500 ms  the card flip
 *   --ak-ease-out             everything that ends on screen
 *   --ak-ease-in-out          the flip
 *
 * Every CSS transition uses them. Every animation started from JavaScript
 * starts here, reads them with getComputedStyle, and asks
 * prefers-reduced-motion first. Under "reduce" animate() starts nothing and
 * returns null, and the element is in its final state.
 *
 * The three write helpers at the end are here for the same reason. A node
 * that is rewritten starts its animations over and loses its image, so a
 * repaint that changes nothing must write nothing.
 */

const query = window.matchMedia("(prefers-reduced-motion: reduce)");

/** True when the reader asked for less motion. Read at the moment of use. */
export function reduced() {
  return query.matches;
}

// The tokens do not change while the page is open, so read each one once.
const tokens = new Map();

function token(name) {
  if (!tokens.has(name)) {
    tokens.set(name, getComputedStyle(document.documentElement).getPropertyValue(name).trim());
  }
  return tokens.get(name);
}

/** `--ak-dur-<name>` in milliseconds. */
export function duration(name) {
  const raw = token(`--ak-dur-${name}`);
  const value = Number.parseFloat(raw);
  if (!Number.isFinite(value)) return 0;
  return raw.endsWith("ms") ? value : value * 1000;
}

/** element.animate() on the tokens, or null under reduced motion. `dur` and
 *  `ease` name tokens. */
export function animate(element, keyframes, { dur = "slow", ease = "out" } = {}) {
  if (reduced() || !element) return null;
  return element.animate(keyframes, { duration: duration(dur), easing: token(`--ak-ease-${ease}`) });
}

/** Set the text only when it differs. */
export function setText(element, value) {
  const text = value === null || value === undefined ? "" : String(value);
  if (element.textContent !== text) element.textContent = text;
}

/** Set an attribute only when it differs. Null removes it. */
export function setAttr(element, name, value) {
  if (value === null || value === undefined) {
    if (element.hasAttribute(name)) element.removeAttribute(name);
  } else if (element.getAttribute(name) !== String(value)) {
    element.setAttribute(name, String(value));
  }
}

/** Show or hide only when that changes. */
export function setHidden(element, hidden) {
  if (element.hidden !== hidden) element.hidden = hidden;
}
