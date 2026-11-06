/* How it works: the pipeline in five lines, and the model table.
 *
 * No name or number here is typed into the page. The model names, the repos,
 * the pinned commits and the licences come from GET /models, which reads
 * deploy/models.env. Where each model runs and what memory it holds come from
 * the /healthz poll node.js runs.
 */

import * as models from "./models.js";
import * as motion from "./motion.js";
import { state } from "./state.js";

const $ = (id) => document.getElementById(id);

/** The five lines, given the three models' names. */
function lines({ cutout, art, text }) {
  return [
    "This browser shrinks your photo to 2048 px, finds your face, and posts both with your name and numbers. The api strips the EXIF before any model sees the photo.",
    `${cutout} cuts you out of the photo while ${art} paints the stadium behind you.`,
    `At the same time ${text} writes your nickname and report from the facts you typed, and numpy finds the 2025 hitters whose Statcast numbers sit nearest yours, when you gave numbers. A report that holds a number you did not type is sent back once, then replaced by a template.`,
    "Pillow puts you on the stadium, and Chromium draws the front and the back from HTML at 1500 x 2100.",
    "The back lists each step's model, licence, GPU and milliseconds, as the services reported them. It says the card was made on Akamai Cloud GPUs only when every model step ran on one.",
  ];
}

const ROW = `<td class="mt-step"></td><td class="mt-model"><span class="mt-name"></span>
  <a class="mt-repo" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer"></a></td>
  <td class="mt-licence"></td><td class="mt-pin num"></td><td class="mt-where"><span class="dot"></span><span></span></td>
  <td class="mt-memory num"></td>`;

/** Where `model` is now, from what its service last told /healthz. */
function where(model) {
  if (!state.health) return { level: "bad", said: "the node is not answering" };
  const service = state.health.services?.[model.service];
  if (!service) return { level: "idle", said: "not in this stack" };
  const place = models.placeName(service.ran_on);
  if (service.loaded) return { level: "ok", said: place };
  return service.status === "loading" ? { level: "warn", said: `loading on ${place}` } : { level: "bad", said: service.error || service.status };
}

export function paint() {
  const catalog = state.models;
  motion.setHidden($("how-msg"), Boolean(catalog));
  if (!catalog) return;

  const named = Object.fromEntries(catalog.models.map((model) => [model.step, model.name]));
  const list = $("how-steps");
  const said = lines(named);
  if (list.dataset.shows !== said.join("\n")) {
    list.dataset.shows = said.join("\n");
    list.replaceChildren(...said.map((line) => Object.assign(document.createElement("li"), { textContent: line })));
  }

  const body = $("model-table");
  if (body.children.length !== catalog.models.length) {
    body.replaceChildren(...catalog.models.map(() => Object.assign(document.createElement("tr"), { innerHTML: ROW })));
  }
  catalog.models.forEach((model, index) => {
    const row = body.children[index];
    motion.setText(row.querySelector(".mt-step"), model.step);
    motion.setText(row.querySelector(".mt-name"), model.name);
    const repo = row.querySelector(".mt-repo");
    motion.setText(repo, model.repo);
    motion.setAttr(repo, "href", model.url);
    motion.setText(row.querySelector(".mt-licence"), model.license);
    const pin = row.querySelector(".mt-pin");
    motion.setText(pin, model.revision.slice(0, 12));
    motion.setAttr(pin, "title", model.revision);
    const now = where(model);
    const cell = row.querySelector(".mt-where");
    motion.setAttr(cell.firstChild, "class", `dot dot-${now.level}`);
    motion.setText(cell.lastChild, now.said);
    const vram = state.health?.services?.[model.service]?.vram_mb;
    motion.setText(row.querySelector(".mt-memory"), vram ? `${(vram / 1024).toFixed(1)} GB` : "");
  });

  const mock = state.health?.backend === "mock";
  motion.setHidden($("how-mock"), !mock);
}

export function show() {
  models.load();
  paint();
}
