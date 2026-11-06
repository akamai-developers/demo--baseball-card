/* Every call to the node, in one place.
 *
 * The key is held in sessionStorage and goes on every request as
 * `Authorization: Bearer`. There is no cookie and the node keeps nothing
 * about the tab, so a closed tab is a signed-out tab.
 *
 * Only `/`, `/app/*`, `/vendor/*` and `/livez` are open. Everything else here
 * needs the key, the card's pictures included, which is why a card is fetched
 * as a blob and shown from an object URL instead of an <img src> on the route.
 * The key is never in a URL.
 *
 * Requests go to this page's own origin and nowhere else.
 */

const KEY_NAME = "card-api-key";

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

let key = sessionStorage.getItem(KEY_NAME) || "";

/** Called when a request that used to work starts coming back 401. */
let onSignedOut = () => {};

export function setSignedOutHandler(handler) {
  onSignedOut = handler;
}

/** The key this tab holds, from sessionStorage, or "". */
export function storedKey() {
  return key;
}

export function clearKey() {
  key = "";
  sessionStorage.removeItem(KEY_NAME);
}

/** What went wrong, in words a person can act on. The api's own sentence when
 *  it sent one, as it does for a photo or a number it refused. */
async function explain(response) {
  let detail = "";
  try {
    const body = await response.json();
    detail = typeof body?.detail === "string" ? body.detail : "";
  } catch {
    detail = "";
  }
  if (detail) return detail.charAt(0).toUpperCase() + detail.slice(1);
  if (response.status === 401) return "The node did not take that key.";
  if (response.status === 413) return "That photo is too big for the gateway. Its limit is 20 MB.";
  if (response.status === 429) return "Too many wrong keys from this address. Wait a minute, then try again.";
  if (response.status >= 502) return "The gateway answered, but the api behind it did not. Its logs say why.";
  return `The node answered ${response.status}.`;
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { ...options, headers: { Authorization: `Bearer ${key}` } });
  } catch {
    throw new ApiError(0, "The node is not answering. Check that the stack is up, or that make tunnel is still running.");
  }
  if (response.status === 401) {
    const error = new ApiError(401, await explain(response));
    onSignedOut();
    throw error;
  }
  if (!response.ok) throw new ApiError(response.status, await explain(response));
  return response;
}

const getJSON = async (path) => (await request(path)).json();

/** Try a key against /healthz. Keeps it and returns the report, or throws. */
export async function signIn(candidate) {
  const previous = key;
  key = candidate;
  try {
    const health = await getJSON("/healthz");
    sessionStorage.setItem(KEY_NAME, candidate);
    return health;
  } catch (error) {
    key = previous;
    throw error;
  }
}

export const health = () => getJSON("/healthz");
export const gpu = () => getJSON("/gpu");
export const models = () => getJSON("/models");
export const getCard = (cardId) => getJSON(`/cards/${cardId}`);

/** Post one card. `face` is the box photo.js measured, or null when the
 *  detector looked and found none, so the card says no face was found. When
 *  the detector did not load it is undefined, and no face field is sent: the
 *  browser did not look, and the card says nothing about a face. */
export async function createCard({ blob, name, face }, stats, style) {
  const form = new FormData();
  form.append("photo", blob, name);
  form.append("stats", JSON.stringify(stats));
  form.append("style", style);
  if (face !== undefined) form.append("face", JSON.stringify(face));
  const response = await request("/cards", { method: "POST", body: form });
  return response.json();
}

/** A card's picture, as a Blob: front.png, back.png or preview.jpg. */
export async function assetBlob(cardId, asset) {
  return (await request(`/cards/${cardId}/${asset}`)).blob();
}

/** Hand `blob` to the browser to save under `filename`. */
export function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  // Give the browser a moment to start the save before the blob goes away.
  setTimeout(() => URL.revokeObjectURL(url), 30_000);
}
