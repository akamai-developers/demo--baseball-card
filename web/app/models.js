/* What runs for a card, from GET /models, and where each model is now.
 *
 * The names, the repos, the pinned commits and the licences are the api's,
 * read from deploy/models.env. Where each one runs comes from /healthz, which
 * node.js polls: the label a service reports, akamai-gpu0 or mock-gpu0, and
 * whether it has loaded.
 */

import * as api from "./api.js";
import { state, update } from "./state.js";

let loading = null;

/** The catalogue, fetched once. A failure leaves it null, and the next view
 *  that wants it asks again. */
export function load() {
  if (state.models) return Promise.resolve(state.models);
  if (!loading) {
    loading = api
      .models()
      .then((catalog) => {
        state.models = catalog;
        update("models");
        return catalog;
      })
      .catch(() => null)
      .finally(() => {
        loading = null;
      });
  }
  return loading;
}

/** The model a node service runs: "cutout", "art" or "llm". */
export function forService(name) {
  return state.models?.models.find((model) => model.service === name);
}

/** "akamai-gpu1" and "mock-gpu1" are GPU 1, "akamai-cpu" is the CPU, and a
 *  mock says so. Anything else keeps its label. */
export function placeName(ranOn) {
  const label = String(ranOn || "");
  const gpu = /-gpu(\d+)$/.exec(label);
  const place = gpu ? `GPU ${gpu[1]}` : label.endsWith("-cpu") ? "CPU" : label;
  return label.startsWith("mock-") ? `${place}, mock` : place;
}

/** "black-forest-labs/FLUX.2-klein-4B" is an owner and a model. A narrow cell
 *  has room for the model, and the whole id goes on its title. */
export function shortModel(name) {
  return name ? name.slice(name.lastIndexOf("/") + 1) : "";
}
