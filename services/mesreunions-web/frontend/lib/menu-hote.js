// Les entrées de Mes réunions dans le menu commun de la bêta (mirai-apps-menu
// ≥ 1.13.0) — la section « Mes réunions · avancé » du panneau de compte.
//
// C'est là que vivent, depuis le 2026-09-22, les écrans qui n'ont plus
// d'onglet : téléphones, données utiles, corbeille, administration — et la
// visite guidée, pour la revoir. Le menu rend, nous décidons ; il ferme son
// panneau avant d'appeler `action`.
//
// Deux portes, parce que l'ordre de chargement n'est pas garanti : le menu
// lit `window.MIRAI_MENU.entrees` à son montage, et `MirAI.poserEntrees`
// re-rend à tout moment (le compte de téléphones arrive après le boot).

import { ouvrirEcran } from './navigation.js';
import { isAdmin } from './auth.js';
import { demarrerVisite } from './visite-guidee.js';

function _entrees() {
  const n = window.__mesreunionsTelephones;
  const liste = [
    { libelle: 'Mes téléphones', icone: '📱', compte: (n === undefined ? '' : n),
      titre: 'Les téléphones autorisés à envoyer leurs enregistrements ici',
      action: () => ouvrirEcran('devices') },
    { libelle: 'Mes données utiles', icone: '📚', action: () => ouvrirEcran('useful-data') },
    { libelle: 'Corbeille', icone: '🗑', titre: 'Réunions et briefs supprimés, gardés 30 jours',
      action: () => ouvrirEcran('trash') },
  ];
  if (isAdmin()) liste.push({ libelle: 'Administration', icone: '🛠', action: () => ouvrirEcran('admin') });
  liste.push({ libelle: 'Revoir la visite guidée', icone: '🧭', action: () => demarrerVisite({ forcer: true }) });
  return liste;
}

function _poser() {
  const liste = _entrees();
  try {
    window.MIRAI_MENU = window.MIRAI_MENU || {};
    window.MIRAI_MENU.entrees = liste;
    window.MIRAI_MENU.titreEntrees = 'Mes réunions · avancé';
  } catch (e) { /* rien */ }
  if (window.MirAI && typeof window.MirAI.poserEntrees === 'function') {
    window.MirAI.poserEntrees(liste, { titre: 'Mes réunions · avancé' });
  }
}

export function initMenuHote() {
  _poser();
  // Le compte de téléphones change : l'entrée se relit.
  document.addEventListener('mesreunions:telephones', _poser);
}
