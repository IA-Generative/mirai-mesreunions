// Cache mémoire pour les fiches détail (chantier UX-Refonte-3 #2).
//
// Objectif : éviter le re-fetch synchrone à chaque switch liste↔détail.
//   - 1er affichage d'une fiche : fetch normal + put(id, data).
//   - réaffichage : get(id) renvoie immédiatement la dernière payload connue,
//     l'appelant peut afficher le contenu puis lancer un refetch silencieux
//     en arrière-plan pour rafraîchir si besoin (pattern stale-while-revalidate).
//   - mutation (rename, delete, link, amend) : invalidate(id) avant le prochain
//     show().
//
// Espace clé par type pour éviter les collisions (briefs vs files).

const _store = {
  brief: new Map(),
  file: new Map(),
};

function _ns(kind) {
  return _store[kind] || (_store[kind] = new Map());
}

export function get(kind, id) {
  if (!id) return null;
  const m = _ns(kind);
  const entry = m.get(String(id));
  if (!entry) return null;
  return entry.data;
}

export function put(kind, id, data) {
  if (!id) return;
  const m = _ns(kind);
  m.set(String(id), { data, ts: Date.now() });
}

export function invalidate(kind, id) {
  if (!id) return;
  const m = _ns(kind);
  m.delete(String(id));
}

export function invalidateAll(kind) {
  if (!kind) {
    Object.values(_store).forEach(m => m.clear());
    return;
  }
  const m = _ns(kind);
  m.clear();
}

// Helpers publiés sur window pour faciliter le debug en console et
// permettre aux modules non-ES (legacy.js) de purger le cache après
// une mutation sans avoir à importer le module.
if (typeof window !== 'undefined') {
  window.__detailCache = {
    get, put, invalidate, invalidateAll,
  };
}
