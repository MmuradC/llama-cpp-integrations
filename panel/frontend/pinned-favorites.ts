/**
 * Keeps the main chat model dropdown's "Favorite models" hearts in step with
 * the provider pages' "Pin to chat" buttons.
 *
 * The dropdown reads `LlamaUi.favoriteModels` (the fork's
 * tools/ui/src/lib/constants/storage.constants.ts FAVORITE_MODELS_LOCALSTORAGE_KEY;
 * the key string is duplicated here because panel/frontend deliberately
 * imports nothing from the fork's $lib barrels). A pin writes the *registered*
 * id ("<provider>/<sanitized real id>", the same scheme the router's
 * load_remote_model_presets() and panel/backend/server.py's `_registered_id()
 * both use), so the heart shows filled and the model sits in the Favorite
 * models section on the next dropdown open.
 *
 * The provider pages' own ☆ shortlists ("opencode-favorite-models" etc.) stay
 * browser-local cosmetic state and are untouched by this.
 */
const MAIN_FAVORITES_KEY = "LlamaUi.favoriteModels";

export function syncMainFavorites(registeredId: string, active: boolean): void {
  try {
    const raw = localStorage.getItem(MAIN_FAVORITES_KEY);
    const next = new Set(JSON.parse(raw ?? "[]") as string[]);

    if (active) next.add(registeredId);
    else next.delete(registeredId);

    localStorage.setItem(MAIN_FAVORITES_KEY, JSON.stringify([...next]));
  } catch {
    // The favorites list is cosmetic; a failed write must never fail the pin.
  }
}
