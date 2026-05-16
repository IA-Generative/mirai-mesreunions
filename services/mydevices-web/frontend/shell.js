// Entrypoint Vite — bundle racine de mydevices-web.
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
import './legacy.js';
import './tabs/devices.js';
import './tabs/meetings.js';
import './tabs/preparations.js';
import './tabs/useful-data.js';
import * as adminTab from './tabs/admin.js';
import { initTabManager } from './lib/tab-manager.js';

// Affichage conditionnel de l'onglet Admin dans la nav (selon claim OIDC).
try { adminTab.revealNavIfAdmin && adminTab.revealNavIfAdmin(); } catch (e) {}

// Orchestrateur global des onglets (DSFR ↔ legacy ↔ lazy-load).
initTabManager();

// Sentinelle utile au test e2e (vérifier que le bundle a bien initialisé).
window.__MYDEVICES_SHELL_READY__ = true;
