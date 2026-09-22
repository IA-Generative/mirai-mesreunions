// Ouvrir un écran par son nom — depuis le menu commun (lib/menu-hote.js), un
// bouton « ← Mes réunions », la visite guidée ou une fiche.
//
// Depuis le 2026-09-22 la barre n'a que deux entrées (Mes réunions, Préparer
// une réunion). Les autres écrans (téléphones, association, données utiles,
// corbeille, administration) existent toujours comme panneaux `.tab-pane`,
// mais sans bouton : ce module rejoue pour eux ce que faisait un clic
// d'onglet — les chargements (loadTrash, loadDevices, le montage des
// modules différés), puis activateTab (legacy.js) qui affiche le panneau et
// pose aria-current sur la barre.

const DIFFERES = new Set(['useful-data', 'admin']);

export function ouvrirEcran(tab) {
  if (!tab) return;
  // Une entrée de la barre : le clic natif déclenche setupTabs (legacy),
  // shell.js (montage/démontage de l'onglet réunions) et tab-manager.
  const bouton = document.querySelector(`.tab-btn[data-tab="${tab}"]`);
  if (bouton) {
    bouton.click();
    try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch (e) { window.scrollTo(0, 0); }
    return;
  }
  // Un écran sans onglet.
  try { if (typeof window.__quitterReunions === 'function') window.__quitterReunions(); } catch (e) { /* rien */ }
  if (tab === 'trash' && typeof window.loadTrash === 'function') window.loadTrash();
  if ((tab === 'devices' || tab === 'generate') && typeof window.loadDevices === 'function') window.loadDevices();
  if (DIFFERES.has(tab) && window.__tabManager && typeof window.__tabManager.mount === 'function') {
    window.__tabManager.mount(tab);
  }
  if (typeof window.activateTab === 'function') window.activateTab(tab);
  try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch (e) { window.scrollTo(0, 0); }
}

window.ouvrirEcran = ouvrirEcran;
