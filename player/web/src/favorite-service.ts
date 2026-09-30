import { api } from "./api";
import { FavoriteMutations } from "./favorite-mutations";

/** One queue across feed, context and successive large-player instances. */
export const favoriteMutations = new FavoriteMutations(
  (id, enabled) => api.setFavorite(id, enabled),
  () => undefined,
  () => undefined,
);
