// Helpers de formatage centralisés (durées + dates) — TKT-103.
//
// Objectif : bannir du DOM les chaînes brutes "NNmNNs" et "DD/MM/YY HH:MM"
// au profit d'un rendu français lisible cohérent dans toute l'interface.
//
// API publique :
//   formatDuration(seconds)         → "45 s" | "5 min 30 s" | "1 h 23 min"
//   formatDate(iso, { withTime? })  → "15 mai 2026" | "15 mai 2026 à 14:30"
//
// Les deux fonctions sont défensives : entrée nulle / NaN / chaîne invalide
// retourne '' (les call-sites peuvent court-circuiter l'affichage).

export function formatDuration(seconds) {
  const n = Number(seconds);
  if (!Number.isFinite(n) || n <= 0) return '';
  const total = Math.round(n);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  // ≥ 1 h : on tronque les secondes (peu utile à ce niveau de granularité).
  if (h > 0) {
    return m > 0 ? `${h} h ${m} min` : `${h} h`;
  }
  if (m > 0) {
    return s > 0 ? `${m} min ${s} s` : `${m} min`;
  }
  return `${s} s`;
}

function _toDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isFinite(d.getTime()) ? d : null;
}

export function formatDate(iso, opts) {
  const d = _toDate(iso);
  if (!d) return '';
  const withTime = !!(opts && opts.withTime);
  const datePart = d.toLocaleDateString('fr-FR', {
    day: 'numeric', month: 'long', year: 'numeric',
  });
  if (!withTime) return datePart;
  const timePart = d.toLocaleTimeString('fr-FR', {
    hour: '2-digit', minute: '2-digit',
  });
  return `${datePart} à ${timePart}`;
}

export default { formatDuration, formatDate };
