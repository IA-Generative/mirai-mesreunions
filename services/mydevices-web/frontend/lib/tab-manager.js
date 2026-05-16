// Orchestrateur des onglets : intermédiaire entre la nav `fr-tabs` DSFR
// (qui gère son propre aria-selected + .fr-tabs__panel--selected) et le
// code historique de legacy.js (qui s'appuie sur .is-active + activateTab).
//
// Responsabilités :
//   1. Synchroniser les deux mécanismes (legacy + DSFR) lors d'un click sur
//      un onglet. Sans ça, DSFR met `--selected` mais legacy ne déclenche
//      pas ses hooks (loadBriefs / loadTrash / showFilesList) et vice-versa.
//   2. Pour les onglets nouveaux (`useful-data`, `admin`) inexistants dans
//      legacy.js, déléguer à un module ES en lazy-load via dynamic import().
//   3. Exposer un `mount(tabId)` idempotent (ne ré-importe qu'une fois).
//
// Contrat des modules tabs/<id>.js :
//   - export `mount(container, ctx)` : appelé au 1er affichage du panneau.
//   - export `unmount(container)` (optionnel) : appelé quand on quitte
//     l'onglet ; utile si le module a posé des intervals / listeners
//     globaux à nettoyer.

const LAZY_TABS = new Set(['useful-data', 'admin']);
const _loaded = new Map(); // tabId -> { module, mounted }

async function _loadModule(tabId) {
  if (_loaded.has(tabId)) return _loaded.get(tabId);
  // Vite a besoin d'un littéral résolvable statiquement pour le bundle —
  // on énumère explicitement.
  let mod;
  if (tabId === 'useful-data') mod = await import('../tabs/useful-data.js');
  else if (tabId === 'admin') mod = await import('../tabs/admin.js');
  else return null;
  const entry = { module: mod, mounted: false };
  _loaded.set(tabId, entry);
  return entry;
}

async function _mountTab(tabId) {
  const entry = await _loadModule(tabId);
  if (!entry || entry.mounted) return;
  const panel = document.getElementById('panel-' + tabId);
  if (!panel) return;
  const ctx = { user: (window.__BOOTSTRAP__ && window.__BOOTSTRAP__.user) || {} };
  try {
    if (typeof entry.module.mount === 'function') {
      await entry.module.mount(panel, ctx);
    }
    entry.mounted = true;
  } catch (e) {
    console.error('[tab-manager] mount(' + tabId + ') failed:', e);
  }
}

function _onTabClick(ev) {
  const btn = ev.target.closest && ev.target.closest('.fr-tabs__tab[data-tab]');
  if (!btn) return;
  const tabId = btn.getAttribute('data-tab');
  if (!tabId) return;
  // Sync legacy `is-active` : permet aux règles CSS héritées d'agir.
  // (activateTab legacy le fait déjà pour les 5 onglets connus ; pour
  // useful-data et admin il faut le faire ici.)
  if (LAZY_TABS.has(tabId)) {
    document.querySelectorAll('.tab-pane').forEach((p) => {
      p.classList.toggle(
        'is-active',
        p.getAttribute('data-tab') === tabId
      );
    });
    // Met à jour le libellé header (TAB_HEADER_LABELS legacy ne connaît
    // pas nos nouveaux tabs ; on injecte directement).
    const headerLabel = document.getElementById('header-tab-label');
    if (headerLabel) {
      const labels = { 'useful-data': 'Mes données utiles', admin: 'Admin' };
      headerLabel.textContent = labels[tabId] ? ' — ' + labels[tabId] + ' ' : '';
    }
    try { sessionStorage.setItem('mydevices-active-tab', tabId); } catch (e) {}
    _mountTab(tabId);
  }
}

export function initTabManager() {
  // Délégation globale : un seul listener pour tous les boutons d'onglet
  // (les boutons DSFR émettent un click natif avant de pousser leur état).
  document.addEventListener('click', _onTabClick, true);

  // Si on a déjà été activé sur un lazy-tab (via sessionStorage côté
  // legacy.js → activateTab), il faut quand même le monter au boot.
  try {
    const params = new URLSearchParams(window.location.search);
    let target = params.get('tab') || sessionStorage.getItem('mydevices-active-tab');
    if (target && LAZY_TABS.has(target)) {
      _mountTab(target);
    }
  } catch (e) { /* ignore */ }
}

// Exposé pour debug + tests e2e.
window.__tabManager = { mount: _mountTab, init: initTabManager };
