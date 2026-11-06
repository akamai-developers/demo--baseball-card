/* The card, once Generate has posted it.
 *
 * The result takes the form's place on Make a card. While the card is made,
 * the ledger beside it fills in: each step with its model, its licence, where
 * it ran and its milliseconds. The front comes in the moment the node has it,
 * a little before the back. A tap on the card, Space, Enter or Flip turns it
 * over. Save front and Save back write the PNGs at full size, 1500 x 2100.
 * Make another brings the form back as it was, photo and all, so one change
 * makes the next card.
 *
 * GET /cards/{id} every 500 ms until the card has ended. The milliseconds on a
 * finished step are the api's own. The counter on a running step is this
 * browser's, from when it first saw the step running.
 */

import * as api from "./api.js";
import * as models from "./models.js";
import * as motion from "./motion.js";
import { STEPS, state, styleLabel, update } from "./state.js";

const POLL_MS = 500;
const TICK_MS = 100;

/** The files a card has, in the order the app wants them. */
const ASSETS = ["preview.jpg", "front.png", "back.png"];

const $ = (id) => document.getElementById(id);

let pollTimer = null;
let tickTimer = null;
let turned = false;
let onAgain = () => {};

const ended = (card) => card.status === "done" || card.status === "failed";

/** Done with the node: the card has ended, and every file it lists is here or
 *  was refused for good. */
const settled = (card) => ended(card) && card.assets.every((asset) => card.urls[asset] || card.gone.has(asset));

/* ------------------------------------------------------------- the job -- */

/** Post the card and show it. Throws the api's sentence when it refuses. */
export async function generate(photo, stats, style) {
  const { card_id: id } = await api.createCard(photo, stats, style);
  forget(state.card);
  state.card = {
    id,
    name: stats.name,
    style,
    status: "queued",
    steps: STEPS.map((step) => ({ step, status: "queued" })),
    warnings: [],
    error: null,
    assets: [],
    startedAt: Date.now(),
    finishedAt: null,
    // step -> the moment this browser first saw it running
    seen: {},
    // asset -> its Blob and its object URL, once fetched and decoded. `asked`
    // is in flight or here, `gone` refused for good.
    blobs: {},
    urls: {},
    asked: new Set(),
    gone: new Set(),
  };
  state.result = true;
  turned = false;
  update("result");
  // The form had the focus and the scroll, and it has gone. The card's name
  // takes the focus, and the page goes to its top, where the card is.
  window.scrollTo(0, 0);
  $("result-title").focus({ preventScroll: true });
  clearInterval(pollTimer);
  clearInterval(tickTimer);
  pollTimer = setInterval(poll, POLL_MS);
  tickTimer = setInterval(tick, TICK_MS);
}

/** Let an old card's pictures go. */
function forget(card) {
  for (const url of Object.values(card?.urls || {})) URL.revokeObjectURL(url);
}

function stopPolling() {
  clearInterval(pollTimer);
  clearInterval(tickTimer);
  pollTimer = null;
  tickTimer = null;
}

let polling = false;

/** Ask for the job until it has ended, then keep going until its files are
 *  in: a picture whose fetch hit a blip is asked for again. */
async function poll() {
  const card = state.card;
  if (!card) return stopPolling();
  // One answer at a time. A slow node must not stack polls up.
  if (!ended(card) && !polling) {
    polling = true;
    try {
      apply(card, await api.getCard(card.id));
    } catch (error) {
      // A blip leaves the card as it was, and the next poll asks again. A card
      // that is gone will not come back.
      if (error.status === 404) {
        apply(card, { ...card, status: "failed", error: "The card is gone from the node. The sweeper or a delete removed it." });
      }
    } finally {
      polling = false;
    }
  }
  if (state.card !== card) return;
  for (const asset of ASSETS) if (card.assets.includes(asset)) fetchAsset(card, asset);
  update("result");
  if (settled(card)) stopPolling();
}

/** Copy what the node said about the card onto it. */
function apply(card, job) {
  const now = Date.now();
  for (const step of job.steps) {
    if (step.status === "running" && !card.seen[step.step]) card.seen[step.step] = now;
  }
  card.status = job.status;
  card.steps = job.steps;
  card.warnings = job.warnings || [];
  card.error = job.error || null;
  card.assets = job.assets || [];
  if (ended(card) && !card.finishedAt) card.finishedAt = now;
}

/** Fetch one of the card's pictures once, and keep it once it is decoded, so
 *  the swap from the preview to the front at full size shows no empty frame. */
async function fetchAsset(card, asset) {
  if (card.asked.has(asset)) return;
  card.asked.add(asset);
  try {
    const blob = await api.assetBlob(card.id, asset);
    const url = URL.createObjectURL(blob);
    const probe = new Image();
    probe.src = url;
    await probe.decode();
    if (state.card !== card) return URL.revokeObjectURL(url);
    card.blobs[asset] = blob;
    card.urls[asset] = url;
    update("result");
  } catch (error) {
    // A network blip or a restarting api is asked again on the next poll. A
    // file the node does not have, or one the browser cannot decode, will not come.
    if (error.status === 0 || error.status >= 500) card.asked.delete(asset);
    else card.gone.add(asset);
  }
}

/* ------------------------------------------------------------ the ledger -- */

/** Milliseconds up to 9999, then seconds. */
function formatMs(ms) {
  return ms < 10_000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

const ROW = `<td class="lg-step"><span class="dot"></span><span></span></td><td class="lg-model"></td>
  <td class="lg-licence"></td><td class="lg-where"></td><td class="lg-ms num"></td>`;

const DOTS = { done: "dot-ok", failed: "dot-bad", running: "dot-run" };

/** One row per step of `card`, painted in place: step, model, licence, where
 *  it ran, milliseconds. The art step says whether its background came from
 *  the art service's pool or was painted for this card. */
export function paintLedger(body, card) {
  if (body.children.length !== card.steps.length) {
    body.replaceChildren(...card.steps.map(() => Object.assign(document.createElement("tr"), { innerHTML: ROW })));
  }
  card.steps.forEach((step, index) => {
    const row = body.children[index];
    const [name, model, licence, where, ms] = row.children;
    motion.setAttr(row, "data-step", step.step);
    motion.setAttr(row, "data-status", step.status);
    motion.setAttr(name.firstChild, "class", `dot ${DOTS[step.status] || "dot-idle"}`);
    motion.setText(name.lastChild, step.step);
    const said = [models.shortModel(step.model), step.source].filter(Boolean).join(" · ");
    motion.setText(model, step.status === "failed" && step.error ? step.error : said);
    motion.setAttr(model, "title", [step.model, step.source, step.error].filter(Boolean).join(" · ") || null);
    motion.setText(licence, step.license || "");
    motion.setText(where, step.ran_on || "");
    const since = card.seen[step.step] || card.startedAt;
    const live = step.status === "running" ? Date.now() - since : null;
    motion.setText(ms, live !== null ? formatMs(live) : typeof step.ms === "number" ? formatMs(step.ms) : "·");
  });
}

/* ------------------------------------------------------------- the view -- */

function seconds(card) {
  return `${(((card.finishedAt || Date.now()) - card.startedAt) / 1000).toFixed(1)} s`;
}

/** The front on show: the full-size PNG, or the preview until it comes. */
const frontUrl = (card) => card.urls["front.png"] || card.urls["preview.jpg"];

/** What the line under the name says. The front can land as either file
 *  first, and once one is on show only the back is left. */
function statusLine(card) {
  if (card.status === "queued") return "Queued on the node.";
  if (card.status === "running") {
    return frontUrl(card) ? `Writing the back, ${seconds(card)}.` : `Making it, ${seconds(card)}.`;
  }
  if (card.status === "done") return `Made in ${seconds(card)}.`;
  return `Failed after ${seconds(card)}.`;
}

/** What the back of the card says while it has no picture. */
function backNote(card) {
  if (!ended(card)) return "Writing the back";
  return card.assets.includes("back.png") && !card.gone.has("back.png") ? "Loading the back" : "This card has no back.";
}

/** "Sam Rivera" is "sam-rivera" in a file name. */
function slug(text) {
  return text.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "card";
}

export function renderResult() {
  motion.setHidden($("make-form"), state.result);
  motion.setHidden($("result"), !state.result);
  const card = state.card;
  if (!state.result || !card) return;

  motion.setText($("result-title"), `${card.name}, ${styleLabel(card.style)}`);
  motion.setText($("result-status"), statusLine(card));
  paintLedger($("result-ledger"), card);

  const front = frontUrl(card);
  const back = card.urls["back.png"];
  const flip = $("card-flip");
  const had = !flip.hidden;
  motion.setHidden(flip, !front);
  motion.setHidden($("card-wait"), Boolean(front));
  motion.setText($("card-wait-text"), card.status === "failed" ? card.error || "The card failed." : "The front lands here when it is drawn.");
  $("card-wait").classList.toggle("is-failed", card.status === "failed");
  if (front && !had) motion.animate(flip, [{ opacity: 0, transform: "translateY(16px) scale(0.98)" }, { opacity: 1, transform: "none" }]);
  if (front && $("card-front").getAttribute("src") !== front) $("card-front").src = front;
  if (back && $("card-back").getAttribute("src") !== back) $("card-back").src = back;
  motion.setHidden($("card-back"), !back);
  motion.setHidden($("card-back-wait"), Boolean(back));
  motion.setText($("card-back-wait"), backNote(card));
  flip.classList.toggle("is-turned", turned);
  motion.setAttr(flip, "aria-label", `The card's ${turned ? "back" : "front"} is up. Turn it over.`);
  motion.setAttr($("result-flip"), "aria-pressed", String(turned));
  $("result-flip").disabled = !front;
  $("save-front").disabled = !card.blobs["front.png"];
  $("save-back").disabled = !card.blobs["back.png"];

  const warnings = $("result-warnings");
  const key = card.warnings.join("\n");
  if (warnings.dataset.shows !== key) {
    warnings.dataset.shows = key;
    warnings.replaceChildren(
      ...card.warnings.map((text) => Object.assign(document.createElement("li"), { className: "warning", textContent: text })),
    );
  }
  motion.setHidden(warnings, card.warnings.length === 0);
  const error = card.status === "failed" ? card.error || "The card failed." : "";
  motion.setText($("result-error"), error);
  motion.setHidden($("result-error"), !error);
}

/** The counters on running steps, between polls, so they read as live. */
function tick() {
  const card = state.card;
  if (!state.result || !card || ended(card)) return;
  motion.setText($("result-status"), statusLine(card));
  paintLedger($("result-ledger"), card);
}

function turn() {
  if (!$("card-flip").hidden) {
    turned = !turned;
    renderResult();
  }
}

function save(asset, side) {
  const card = state.card;
  const blob = card?.blobs[asset];
  if (blob) api.saveBlob(blob, `${slug(card.name)}-${card.style}-${side}.png`);
}

/** `again()` puts the focus back on step 1 once the form is back. */
export function wireResult(again) {
  onAgain = again;
  $("card-flip").addEventListener("click", turn);
  $("result-flip").addEventListener("click", turn);
  $("save-front").addEventListener("click", () => save("front.png", "front"));
  $("save-back").addEventListener("click", () => save("back.png", "back"));
  $("result-again").addEventListener("click", () => {
    state.result = false;
    update("result");
    onAgain();
  });
}
