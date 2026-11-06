/* The node panel, and the header's line about the node.
 *
 *   /healthz  every 5 s, from sign-in   what each model service says about itself
 *   /gpu      every 2 s, while on show  nvidia-smi on the node, through gpu-stats
 *
 * /gpu is routed by Caddy straight to gpu-stats and never goes through the
 * api, so a panel polling every two seconds cannot queue behind a card render.
 *
 * Three parts: the models, as each service reports itself; the GPUs, each
 * card's load, memory and temperature; and the ledger of the last card this
 * tab made. Rows are made once and painted in place, so a bar at 2 Hz does not
 * flicker, and a failed poll leaves the last reading on screen.
 */

import * as api from "./api.js";
import * as models from "./models.js";
import * as motion from "./motion.js";
import { paintLedger } from "./results.js";
import { state, styleLabel, update } from "./state.js";

const HEALTH_MS = 5000;
const GPU_MS = 2000;

/** Two misses in a row before the page says a source is gone. A single
 *  dropped request during a render is not worth a banner. */
const MISSES = 2;

const $ = (id) => document.getElementById(id);

let healthTimer = null;
let gpuTimer = null;
let healthMisses = 0;
let gpuMisses = 0;

async function pullHealth() {
  try {
    state.health = await api.health();
    healthMisses = 0;
  } catch {
    healthMisses += 1;
    if (healthMisses < MISSES) return;
    state.health = null;
  }
  update("node");
}

async function pullGpus() {
  try {
    state.gpus = await api.gpu();
    gpuMisses = 0;
  } catch {
    gpuMisses += 1;
    if (gpuMisses !== MISSES) return;
  }
  update("node");
}

/** /healthz from sign-in to sign-out. The header's pill reads it on every view. */
export function startPolling() {
  clearInterval(healthTimer);
  healthTimer = setInterval(pullHealth, HEALTH_MS);
}

/** /gpu only while the panel is on show. */
export function show(on) {
  clearInterval(gpuTimer);
  gpuTimer = null;
  if (!on) return;
  pullGpus();
  gpuTimer = setInterval(pullGpus, GPU_MS);
  models.load();
}

/* ----------------------------------------------------------------- rows -- */

/** The row keyed `key` in `host`, built from `markup` on first sight. */
function rowFor(host, key, markup, tag = "tr") {
  let row = host.querySelector(`:scope > [data-row="${key}"]`);
  if (!row) {
    row = document.createElement(tag);
    row.dataset.row = key;
    row.innerHTML = markup;
    host.append(row);
  }
  return row;
}

/** Drop the rows this pass did not claim. */
function settle(host, keys) {
  for (const child of [...host.children]) if (!keys.includes(child.dataset.row)) child.remove();
}

const GB = (mb) => `${(mb / 1024).toFixed(1)} GB`;

/* ------------------------------------------------------------- services -- */

const SERVICE_ROW = `<td class="sv-name"><span class="dot"></span><span></span></td><td class="sv-model"></td>
  <td class="sv-backend"></td><td class="sv-where"></td><td class="sv-memory num"></td>`;

function paintService(row, { name, level, model, title, backend, where, memory }) {
  const [cell, modelCell, backendCell, whereCell, memoryCell] = row.children;
  motion.setAttr(cell.firstChild, "class", `dot dot-${level}`);
  motion.setText(cell.lastChild, name);
  motion.setText(modelCell, model);
  motion.setAttr(modelCell, "title", title || null);
  motion.setText(backendCell, backend);
  motion.setText(whereCell, where);
  motion.setText(memoryCell, memory);
}

function levelOf(service) {
  if (service.loaded) return "ok";
  return service.status === "loading" ? "warn" : "bad";
}

function renderServices() {
  const host = $("node-services");
  const health = state.health;
  const keys = ["api"];
  paintService(rowFor(host, "api", SERVICE_ROW), {
    name: "api",
    level: health ? "ok" : "bad",
    model: health ? `version ${health.version}` : "not answering",
    backend: health?.backend || "",
    where: "CPU",
    memory: "",
  });
  for (const [id, service] of Object.entries(health?.services || {})) {
    keys.push(id);
    const named = models.forService(id)?.name;
    paintService(rowFor(host, id, SERVICE_ROW), {
      name: named ? `${id}, ${named}` : id,
      level: levelOf(service),
      model: service.loaded ? service.model || "" : service.error || service.status,
      title: [service.model, service.gpu, service.error].filter(Boolean).join(" · "),
      backend: service.backend || "",
      where: [models.placeName(service.ran_on), service.gpu].filter(Boolean).join(", "),
      memory: service.vram_mb ? GB(service.vram_mb) : "",
    });
  }
  // gpu-stats is not behind /healthz. Its answer to /gpu is its health.
  keys.push("gpu-stats");
  const report = state.gpus;
  const count = report?.gpus?.length ?? 0;
  paintService(rowFor(host, "gpu-stats", SERVICE_ROW), {
    name: "gpu-stats",
    level: gpuMisses >= MISSES || report?.error ? "bad" : report ? "ok" : "idle",
    model: gpuMisses >= MISSES ? "not answering" : report?.error || (report ? `nvidia-smi, ${count} GPU${count === 1 ? "" : "s"}` : "checking"),
    backend: report?.backend || "",
    where: "CPU",
    memory: "",
  });
  settle(host, keys);
}

/* ----------------------------------------------------------------- gpus -- */

const GPU_CARD = `<p class="gpu-head"><span class="gpu-name"></span><span class="gpu-temp num"></span></p>
  <p class="gpu-line"><span class="gpu-label">load</span><span class="bar"><span class="bar-fill"></span></span><span class="gpu-value num"></span></p>
  <p class="gpu-line"><span class="gpu-label">memory</span><span class="bar"><span class="bar-fill"></span></span><span class="gpu-value num"></span></p>`;

/** The CSS transition on .bar-fill does the moving. This only sets the width. */
function fill(bar, percent) {
  const width = `${Math.max(0, Math.min(100, percent)).toFixed(1)}%`;
  if (bar.style.width !== width) bar.style.width = width;
}

function renderGpus() {
  const host = $("node-gpus");
  const cards = state.gpus?.gpus || [];
  motion.setText($("node-gpus-note"), cards.length ? "" : state.gpus?.error || (state.gpus ? "No GPUs reported." : "Reading /gpu."));
  const keys = [];
  for (const card of cards) {
    const key = String(card.index);
    keys.push(key);
    const element = rowFor(host, key, GPU_CARD, "div");
    element.className = "gpu";
    motion.setText(element.querySelector(".gpu-name"), `GPU ${card.index}, ${card.name}`);
    motion.setText(element.querySelector(".gpu-temp"), `${card.temperature_c}°C`);
    const [load, memory] = element.querySelectorAll(".gpu-line");
    fill(load.querySelector(".bar-fill"), card.utilization_pct);
    motion.setText(load.querySelector(".gpu-value"), `${card.utilization_pct}%`);
    fill(memory.querySelector(".bar-fill"), (card.memory_used_mb / card.memory_total_mb) * 100);
    motion.setText(memory.querySelector(".gpu-value"), `${(card.memory_used_mb / 1024).toFixed(1)} of ${GB(card.memory_total_mb)}`);
  }
  settle(host, keys);
}

/* ------------------------------------------------------------ last card -- */

function renderLast() {
  const card = state.card;
  motion.setHidden($("node-last"), !card);
  motion.setHidden($("node-last-none"), Boolean(card));
  if (!card) return;
  const said = { queued: "queued", running: "being made", done: "made", failed: "failed" }[card.status];
  motion.setText($("node-last-title"), `${card.name}, ${styleLabel(card.style)}, ${said}`);
  paintLedger($("node-ledger"), card);
}

/* -------------------------------------------------------- pill, banner -- */

function renderPill() {
  const health = state.health;
  if (!health) {
    motion.setAttr($("node-dot"), "class", "dot dot-bad");
    motion.setText($("node-pill-text"), "node not answering");
    return;
  }
  const services = Object.values(health.services || {});
  const loaded = services.filter((service) => service.loaded).length;
  const all = loaded === services.length && services.length > 0;
  motion.setAttr($("node-dot"), "class", `dot ${all ? "dot-ok" : "dot-warn"}`);
  motion.setText($("node-pill-text"), `${loaded} of ${services.length} loaded, ${health.backend}`);
}

/** The one error the page raises on its own, not in reply to a click. */
function renderBanner() {
  const gone = state.signedIn && !state.health;
  motion.setHidden($("banner"), !gone);
}

export function renderNode() {
  renderPill();
  renderBanner();
  renderServices();
  renderGpus();
  renderLast();
}

/** Show the last card again on Make a card, from its ledger. */
export function wireNode() {
  $("node-last-show").addEventListener("click", () => {
    state.result = true;
    update("result");
    location.hash = "#make";
  });
}
