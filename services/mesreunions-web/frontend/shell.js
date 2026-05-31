// Entrypoint Vite — bundle racine de mesreunions-web.
//
// Ordre de chargement strict :
//   1. lib/bootstrap.js → publie window.ALLOWED_AUDIO_EXTENSIONS et
//      window.DEVICE_RETENTION_DAYS depuis le <script id="bootstrap-data">
//      injecté par Flask.
//   2. lib/* (api, auth, toast) → publient leurs helpers sur window.
//   3. legacy.js → exécute tout le code historique (setupTabs(),
//      loadDevices().then(loadSessions) en fin de fichier).
//   4. tabs/*.js → wrappers + nouveaux modules (admin, useful-data,
//      preparations, meetings, devices). Les onglets "lourds" délèguent
//      encore à legacy.js pour leur métier ; les onglets nouveaux
//      (admin, useful-data) ont leur propre mount() lazy-load via
//      lib/tab-manager.js.
//   5. lib/tab-manager.js → init() : pose la délégation globale qui
//      synchronise la nav `fr-tabs` DSFR avec activateTab legacy +
//      lazy-load les modules tabs/admin et tabs/useful-data.

import './lib/bootstrap.js';
import './lib/api.js';
import './lib/auth.js';
import './lib/toast.js';
import './lib/detail-cache.js';  // publie window.__detailCache (skeleton/cache)
// tabs/devices.js (PR6) doit être importé AVANT legacy.js : legacy.js
// termine par `loadDevices().then(loadSessions)` via un trampoline qui
// résout sur `window.loadDevices`. Sans cet ordre, le 1er fetch
// /api/my-devices au boot ne part jamais.
import './tabs/devices.js';
import './legacy.js';
import * as meetingsTab from './tabs/meetings.js';
import './tabs/preparations.js';
import './tabs/wizard.js';  // modale fullscreen "Nouvelle préparation"
import './tabs/useful-data.js';
import * as adminTab from './tabs/admin.js';
import { initTabManager } from './lib/tab-manager.js';
import { initChatWidget } from './lib/chat-widget.js';

// Affichage conditionnel de l'onglet Admin dans la nav (selon claim OIDC).
try { adminTab.revealNavIfAdmin && adminTab.revealNavIfAdmin(); } catch (e) {}

// Orchestrateur global des onglets (DSFR ↔ legacy ↔ lazy-load).
initTabManager();

// Mount de l'onglet meetings : panel-transfers est sélectionné par défaut
// au boot — on déclenche immédiatement son mount() pour brancher la
// délégation `data-action="meetings:*"` (nouveau pattern DSFR-friendly,
// remplace progressivement les onclick="" inline).
try {
  const panel = document.getElementById('panel-transfers');
  if (panel && meetingsTab.mount) meetingsTab.mount(panel);
} catch (e) { /* ignore */ }

// Délégation tab-manager → meetings.unmount() quand on quitte transfers,
// meetings.mount() quand on y revient. Le tab-manager n'orchestre que les
// LAZY_TABS aujourd'hui ; pour transfers (eager-loaded), on écoute le
// click directement sur la nav DSFR.
document.addEventListener('click', (ev) => {
  const btn = ev.target && ev.target.closest && ev.target.closest('.fr-tabs__tab[data-tab]');
  if (!btn) return;
  const tabId = btn.getAttribute('data-tab');
  if (tabId === 'transfers') {
    try { meetingsTab.mount(document.getElementById('panel-transfers')); } catch (e) {}
  } else {
    try { meetingsTab.unmount && meetingsTab.unmount(); } catch (e) {}
  }
}, true);

// Widget « Interroger mes réunions » (agent conversationnel RAG, OpenRAG).
// Persistant, indépendant des onglets. Ne se révèle que si le RAG est
// configuré côté serveur (sinon inerte → aucun impact).
try { initChatWidget(); } catch (e) { /* widget non bloquant */ }

// Sentinelle utile au test e2e (vérifier que le bundle a bien initialisé).
window.__MESREUNIONS_SHELL_READY__ = true;
