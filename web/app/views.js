/* Three views, one page.
 *
 *   #make   the photo, the player, the style and Generate, then the card
 *   #node   the models, the GPUs and the last card's ledger
 *   #how    the pipeline in five lines, and the model table
 *
 * The address is the hash, so a view survives a reload and the back button
 * walks back through them. Nothing is fetched to change view: every view is in
 * the page already, and only the one on show is not hidden. All of them are
 * inside #app, which stays hidden until a key is accepted. An address that
 * names no view shows #make.
 *
 * A view opened by a click or a key gets the focus on its heading, so the next
 * Tab goes into it and a screen reader says where it landed.
 */

import * as motion from "./motion.js";

export const VIEWS = ["make", "node", "how"];

export function parse(hash) {
  const name = String(hash || "").replace(/^#\/?/, "");
  return VIEWS.includes(name) ? name : "make";
}

let current = null;
let onShow = () => {};

export function route() {
  return current;
}

const viewOf = (name) => document.querySelector(`main[data-view="${name}"]`);

/** Put `next` on show. `moved` is true when a person asked for it, and false
 *  for the view the page opens on, which takes no focus. */
function show(next, moved) {
  const previous = current;
  current = next;
  for (const name of VIEWS) motion.setHidden(viewOf(name), name !== next);
  for (const link of document.querySelectorAll("#nav .nav-link")) {
    motion.setAttr(link, "aria-current", link.dataset.view === next ? "page" : null);
  }
  onShow(next, previous);
  if (next === previous) return;
  window.scrollTo(0, 0);
  motion.animate(viewOf(next), [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "none" }], {
    dur: "base",
  });
  if (moved) viewOf(next).querySelector("h1")?.focus({ preventScroll: true });
}

/** Wire the hash to the views. `handler(view, previous)` draws a view as it
 *  comes on show. */
export function wireViews(handler) {
  onShow = handler;
  window.addEventListener("hashchange", () => {
    if (!document.getElementById("app").hidden) show(parse(location.hash), true);
  });
}

/** Show the view the address names. Called once a key has been accepted. */
export function start() {
  show(parse(location.hash), false);
}
