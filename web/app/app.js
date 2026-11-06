/* Baseball Card.
 *
 * Boot, sign in, the three input panels, Generate, and the wiring between
 * them. The network lives in api.js and the state in state.js. The moving
 * parts have a file each: camera.js with detector.js and framing.js for the
 * Camera tab, photo.js for a chosen photo, results.js for the card once it is
 * posted, node.js for the node panel and the header's pill, how.js for How it
 * works, and views.js switches between the three views the header names.
 * motion.js holds the one motion system they all use.
 *
 * Nothing here is fetched from anywhere but this node: no CDN, no framework, no
 * bundler. What the browser loads is what is in web/app and web/vendor.
 */

import * as api from "./api.js";
import * as camera from "./camera.js";
import * as detector from "./detector.js";
import * as how from "./how.js";
import * as motion from "./motion.js";
import * as node from "./node.js";
import * as photo from "./photo.js";
import * as results from "./results.js";
import * as views from "./views.js";
import {
  FIELDS,
  METRICS,
  POSITIONS,
  SAMPLE,
  STYLES,
  state,
  statsFor,
  subscribe,
  update,
  whyNotReady,
} from "./state.js";

const SIGNED_OUT_REASON = "card-signed-out-reason";

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------- sign in -- */

function showSignIn(message = "") {
  $("app").hidden = true;
  $("signin").hidden = false;
  $("signin-error").hidden = !message;
  $("signin-error").textContent = message;
  $("key-input").focus();
}

async function trySignIn(key) {
  state.health = await api.signIn(key);
  state.signedIn = true;
  $("signin").hidden = true;
  $("app").hidden = false;
  // Every photo is measured for a face before it is sent, on 12 MB of wasm.
  // Start loading it now, while a photo is chosen, so the first one does not
  // wait for the download.
  detector.loadStill();
  node.startPolling();
  views.start();
  update("all");
}

function wireSignIn() {
  $("signin-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("signin-btn");
    const key = $("key-input").value.trim();
    if (!key) return;
    button.disabled = true;
    button.textContent = "Checking";
    try {
      await trySignIn(key);
      $("key-input").value = "";
    } catch (error) {
      showSignIn(
        error.status === 401 ? "That key is wrong. It is CARD_API_KEY in the .env the stack was started with." : error.message,
      );
    } finally {
      button.disabled = false;
      button.textContent = "Sign in";
    }
  });

  $("signout-btn").addEventListener("click", () => {
    api.clearKey();
    location.reload();
  });

  // A key that stops being taken while the tab is open, after make rotate-key.
  // Reloading is the honest reset: the photo and the form are the browser's,
  // not the node's.
  api.setSignedOutHandler(() => {
    if (!state.signedIn) return;
    state.signedIn = false;
    sessionStorage.setItem(SIGNED_OUT_REASON, "The node stopped taking that key. It may have been rotated.");
    api.clearKey();
    location.reload();
  });
}

/* --------------------------------------------------------------- photo -- */

function showPhotoError(message) {
  $("photo-error").hidden = !message;
  $("photo-error").textContent = message;
}

/** Shrink a photo, find its face, and put it in the thumb box, from either tab. */
async function takePhoto(file) {
  if (!file) return;
  showPhotoError("");
  // The first photo can wait on the face detector's download.
  const zone = $("dropzone");
  const lead = zone.querySelector(".dz-lead");
  lead.textContent = "Finding the face";
  zone.setAttribute("aria-busy", "true");
  try {
    const prepared = await photo.prepare(file);
    if (state.photo) URL.revokeObjectURL(state.photo.url);
    state.photo = prepared;
  } catch (failure) {
    showPhotoError(failure.message);
  } finally {
    lead.textContent = "Drop a photo, or choose one";
    zone.removeAttribute("aria-busy");
  }
  update("photo");
}

/** Show one tab's panel. The camera only runs while its own tab is on. */
function goTab(which) {
  for (const tab of document.querySelectorAll("#panel-photo .tab")) {
    const on = tab.dataset.tab === which;
    tab.classList.toggle("is-on", on);
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    $(`tab-${tab.dataset.tab}`).hidden = !on;
  }
  camera.setActive(which === "camera");
}

function wirePhoto() {
  const zone = $("dropzone");
  const input = $("file-input");
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      input.click();
    }
  });
  input.addEventListener("change", () => {
    const [file] = input.files;
    // Emptied once read. A browser sends no change for the file an input
    // holds already, and the same photo may well be chosen again.
    input.value = "";
    takePhoto(file);
  });
  for (const name of ["dragenter", "dragover"]) {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.add("is-over");
    });
  }
  for (const name of ["dragleave", "drop"]) {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.remove("is-over");
    });
  }
  zone.addEventListener("drop", (event) => takePhoto(event.dataTransfer?.files?.[0]));

  $("retake-btn").addEventListener("click", () => {
    if (state.photo) URL.revokeObjectURL(state.photo.url);
    state.photo = null;
    update("photo");
  });

  // role="tab" promises arrow keys. Left and right move and select.
  const tabs = [...document.querySelectorAll("#panel-photo .tab")];
  for (const tab of tabs) {
    tab.addEventListener("click", () => goTab(tab.dataset.tab));
    tab.addEventListener("keydown", (event) => {
      const by = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
      if (!by) return;
      event.preventDefault();
      const next = tabs[(tabs.indexOf(tab) + by + tabs.length) % tabs.length];
      next.focus();
      goTab(next.dataset.tab);
    });
  }
  $("cam-use-upload").addEventListener("click", () => {
    goTab("upload");
    document.querySelector('.tab[data-tab="upload"]').focus();
  });
  camera.wireCamera(takePhoto);
}

/** A photo with no face found says so: the card places the person by the top
 *  of their outline, and the card's warnings say no face was found. */
const NO_FACE = "No face found. The card places you by the top of your outline.";
const NO_DETECTOR = "The face detector did not load, so the card places you by the top of your outline.";

function renderPhoto() {
  const chosen = state.photo;
  motion.setHidden($("thumb-row"), !chosen);
  motion.setHidden($("photo-pick"), Boolean(chosen));
  motion.setHidden($("photo-tabs"), Boolean(chosen));
  if (!chosen) return;
  if ($("thumb").getAttribute("src") !== chosen.url) $("thumb").src = chosen.url;
  motion.setText($("thumb-name"), chosen.name);
  motion.setText($("thumb-size"), `${chosen.width} x ${chosen.height}, ${photo.formatBytes(chosen.bytes)}`);
  const note = chosen.face === undefined ? NO_DETECTOR : chosen.face === null ? NO_FACE : "";
  motion.setText($("thumb-note"), note);
  motion.setHidden($("thumb-note"), !note);
}

/* -------------------------------------------------------------- player -- */

function buildPlayerForm() {
  for (const name of POSITIONS) {
    $("f-position").append(Object.assign(document.createElement("option"), { value: name, textContent: name }));
  }
  // A slider for each number, with its value beside it and a box to tick to
  // use it. Off until ticked, so nothing goes on a card that was not measured.
  for (const metric of METRICS) {
    const element = document.createElement("div");
    element.className = "metric";
    element.dataset.metric = metric.key;
    element.innerHTML = `
      <span class="metric-top"><span class="metric-name">${metric.label}</span>
        <span class="metric-unit">${metric.unit}, ${metric.min} to ${metric.max}</span></span>
      <output class="metric-value num"></output>
      <input class="metric-slider" type="range" aria-label="${metric.label} in ${metric.unit}"
             min="${metric.min}" max="${metric.max}" step="${metric.step}" value="${metric.value}" />
      <label class="tick metric-use"><input type="checkbox" aria-label="Use ${metric.label.toLowerCase()}" /> use</label>`;
    element.querySelector(".metric-slider").addEventListener("input", (event) => {
      state.metrics[metric.key].value = Number(event.target.value);
      update("player");
    });
    element.querySelector(".metric-use input").addEventListener("change", (event) => {
      state.metrics[metric.key].on = event.target.checked;
      update("player");
    });
    $("metrics").append(element);
  }
}

function wirePlayer() {
  for (const key of FIELDS) {
    $(`f-${key}`).addEventListener("input", (event) => {
      state.player[key] = event.target.value;
      update("player");
    });
  }
  // examples/stats.json: every field, and the six numbers ticked on.
  $("sample-btn").addEventListener("click", () => {
    for (const key of FIELDS) {
      state.player[key] = String(SAMPLE[key] ?? "");
      $(`f-${key}`).value = state.player[key];
    }
    for (const metric of METRICS) state.metrics[metric.key] = { value: SAMPLE.metrics[metric.key], on: true };
    update("player");
  });
}

function renderPlayer() {
  const inches = Number(state.player.height_in);
  motion.setText($("height-aux"), inches >= 36 && inches <= 96 ? `${Math.floor(inches / 12)}' ${inches % 12}"` : "in");
  const on = METRICS.filter((metric) => state.metrics[metric.key].on).length;
  motion.setText($("metrics-note"), `${on} of ${METRICS.length} on`);
  for (const metric of METRICS) {
    const element = $("metrics").querySelector(`[data-metric="${metric.key}"]`);
    const entry = state.metrics[metric.key];
    element.classList.toggle("is-off", !entry.on);
    const slider = element.querySelector(".metric-slider");
    slider.value = String(entry.value);
    slider.disabled = !entry.on;
    element.querySelector(".metric-use input").checked = entry.on;
    motion.setText(element.querySelector(".metric-value"), entry.on ? entry.value.toFixed(metric.step < 1 ? 1 : 0) : "off");
  }
}

/* --------------------------------------------------------------- style -- */

function buildStyles() {
  for (const style of STYLES) {
    const label = document.createElement("label");
    label.className = "style";
    label.innerHTML = `
      <input type="radio" name="style" value="${style.name}" />
      <img src="/app/styles/${style.name}.jpg" alt="" width="96" height="134" />
      <span class="style-text"><span class="style-name">${style.label}</span>
        <span class="style-look">${style.look}</span></span>`;
    label.querySelector("input").addEventListener("change", () => {
      state.style = style.name;
      update("style");
    });
    $("styles").append(label);
  }
}

function renderStyles() {
  for (const input of document.querySelectorAll('#styles input[name="style"]')) {
    input.checked = input.value === state.style;
  }
}

/* ------------------------------------------------------------ generate -- */

let generateError = "";

function wireGenerate() {
  $("make-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (whyNotReady()) return;
    const button = $("generate-btn");
    button.disabled = true;
    button.textContent = "Sending";
    generateError = "";
    try {
      await results.generate(state.photo, statsFor(), state.style);
    } catch (error) {
      generateError = error.message;
    } finally {
      button.textContent = "Generate";
      renderGenerate();
    }
  });
}

function renderGenerate() {
  const why = whyNotReady();
  $("generate-btn").disabled = Boolean(why);
  motion.setText($("generate-note"), why || generateError);
  $("generate-note").classList.toggle("is-error", !why && Boolean(generateError));
}

/** Back to step 1 once the form is back: Choose another while there is a
 *  photo, the tab that is on while there is not. */
function focusFirstStep() {
  const first = state.photo ? $("retake-btn") : document.querySelector("#panel-photo .tab.is-on");
  first.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

/* ---------------------------------------------------------------- boot -- */

function onView(view) {
  node.show(view === "node");
  if (view === "how") how.show();
}

function render(what) {
  const all = what === "all";
  if (all || what === "photo") renderPhoto();
  if (all || what === "player") renderPlayer();
  if (all || what === "style") renderStyles();
  if (all || what === "result" || what === "node") results.renderResult();
  if (all || what === "node" || what === "result" || what === "models") node.renderNode();
  if (all || what === "node" || what === "models") how.paint();
  renderGenerate();
}

async function boot() {
  buildPlayerForm();
  buildStyles();
  wireSignIn();
  wirePhoto();
  wirePlayer();
  wireGenerate();
  results.wireResult(focusFirstStep);
  node.wireNode();
  views.wireViews(onView);
  subscribe(render);
  goTab("upload");

  const reason = sessionStorage.getItem(SIGNED_OUT_REASON);
  sessionStorage.removeItem(SIGNED_OUT_REASON);
  // A key already in sessionStorage still has to be taken by the node. It may
  // have been rotated while this tab was in the background.
  if (!reason && api.storedKey()) {
    try {
      await trySignIn(api.storedKey());
      return;
    } catch {
      api.clearKey();
    }
  }
  showSignIn(reason || "");
}

boot();
