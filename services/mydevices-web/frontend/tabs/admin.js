// Onglet "Administration" — accessible uniquement si le user a le role
// 'admin' dans ses claims OIDC.
//
// Trois entrées :
//   - revealNavIfAdmin() : exécuté au boot (shell.js). Rend visible le
//     <li id="tab-btn-admin-li"> de la nav `fr-tabs` si user admin.
//     Sinon l'onglet reste invisible dans la nav.
//   - mount(container, ctx) : exécuté au 1er click sur l'onglet (via
//     tab-manager.js lazy-load). Pose une `fr-alert`/`fr-callout` DSFR
//     selon le rôle.
//   - gotoAdmin() : conservé pour compatibilité avec d'éventuels
//     callsites legacy (window.gotoAdmin).

import { isAdmin } from '../lib/auth.js';

export function isAdminAvailable() {
  return isAdmin();
}

export function gotoAdmin() {
  if (!isAdminAvailable()) return false;
  window.location.href = '/admin/';
  return true;
}

export function revealNavIfAdmin() {
  if (!isAdmin()) return;
  const li = document.getElementById('tab-btn-admin-li');
  if (li) li.style.display = '';
}

export function mount(container /*, ctx */) {
  const root = container.querySelector('[data-admin-root]') || container;
  if (isAdmin()) {
    root.innerHTML = `
      <div class="fr-callout fr-icon-information-line">
        <h3 class="fr-callout__title">Console d'administration</h3>
        <p class="fr-callout__text">
          Vous disposez du rôle <strong>admin</strong>. La console
          d'administration permet de gérer l'ensemble des utilisateurs,
          appareils et sessions du service.
        </p>
        <button type="button" class="fr-btn fr-btn--icon-right fr-icon-arrow-right-line"
                data-action="goto-admin">
          Ouvrir la console admin
        </button>
      </div>
    `;
    const btn = root.querySelector('[data-action="goto-admin"]');
    if (btn) btn.addEventListener('click', gotoAdmin);
  } else {
    root.innerHTML = `
      <div class="fr-alert fr-alert--info">
        <h3 class="fr-alert__title">Accès admin non disponible</h3>
        <p>Votre compte n'a pas les droits d'administration sur ce service.
           Si vous pensez qu'il s'agit d'une erreur, contactez l'équipe MIrAI.</p>
      </div>
    `;
  }
}

export function unmount(/* container */) {
  // Rien à nettoyer pour ce module.
}

window.gotoAdmin = gotoAdmin;
