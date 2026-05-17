// Skeleton DSFR-light (chantier UX-Refonte-3 #2).
//
// DSFR ne fournit pas de composant skeleton natif. On expose ici 2 helpers
// pour générer un HTML shimmer cohérent visuellement avec l'app, à coller
// dans le conteneur cible pendant un fetch.
//
// Pattern d'usage :
//   import { renderBriefDetailSkeleton, renderFileDetailSkeleton } from './skeleton.js';
//   container.innerHTML = renderBriefDetailSkeleton();
//   const data = await fetch(...);
//   container.innerHTML = renderRealContent(data);

/** Skeleton générique : N lignes shimmer + un bloc. */
export function renderGenericSkeleton(lines = 4) {
  const out = ['<div class="skeleton-fade-in" data-skeleton="1">'];
  out.push('<div class="skeleton-line skeleton-line--title"></div>');
  for (let i = 0; i < lines; i += 1) {
    const cls = (i % 3 === 0) ? 'skeleton-line--mid' :
                (i % 3 === 1) ? '' : 'skeleton-line--short';
    out.push(`<div class="skeleton-line ${cls}"></div>`);
  }
  out.push('<div class="skeleton-line skeleton-line--block"></div>');
  out.push('</div>');
  return out.join('');
}

/** Skeleton pour la fiche détail d'un brief (titre + meta + 6 lignes + bloc). */
export function renderBriefDetailSkeleton() {
  return [
    '<div class="skeleton-fade-in" data-skeleton="1">',
    '  <div class="skeleton-line skeleton-line--title"></div>',
    '  <div class="skeleton-line skeleton-line--short"></div>',
    '  <div class="skeleton-line skeleton-line--mid"></div>',
    '  <div class="skeleton-line"></div>',
    '  <div class="skeleton-line skeleton-line--short"></div>',
    '  <div class="skeleton-line skeleton-line--block"></div>',
    '  <div class="skeleton-line"></div>',
    '  <div class="skeleton-line skeleton-line--mid"></div>',
    '</div>',
  ].join('');
}

/** Skeleton pour la fiche détail d'un fichier audio (titre + 3 lignes meta + bloc). */
export function renderFileDetailSkeleton() {
  return [
    '<div class="skeleton-fade-in" data-skeleton="1" style="padding:0.5rem 0.4rem;">',
    '  <div class="skeleton-line skeleton-line--title"></div>',
    '  <div class="skeleton-line skeleton-line--short"></div>',
    '  <div class="skeleton-line skeleton-line--mid"></div>',
    '  <div class="skeleton-line skeleton-line--block"></div>',
    '</div>',
  ].join('');
}
