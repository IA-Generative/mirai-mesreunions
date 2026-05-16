// Entrypoint Vite — bundle racine de mydevices-web.
//
// Ordre de chargement strict :
//   1. lib/bootstrap.js → publie window.ALLOWED_AUDIO_EXTENSIONS et
//      window.DEVICE_RETENTION_DAYS depuis le <script id="bootstrap-data">
//      injecté par Flask.
//   2. lib/* (api, auth, toast) → publient leurs helpers sur window.
//   3. legacy.js → exécute tout le code historique (setupTabs(),
//      loadDevices().then(loadSessions) en fin de fichier).
//   4. tabs/*.js → wrappers documentaires (ré-exports depuis window).
//
// PR4 : pas de lazy-load (les onclick="" du template HTML s'attendent à
// trouver les handlers globalement résolus). PR5 introduira l'event
// delegation et permettra le lazy-loading par tab via import() dynamique.

import './lib/bootstrap.js';
import './lib/api.js';
import './lib/auth.js';
import './lib/toast.js';
import './legacy.js';
import './tabs/devices.js';
import './tabs/meetings.js';
import './tabs/preparations.js';
import './tabs/useful-data.js';
import './tabs/admin.js';

// Sentinelle utile au test e2e (vérifier que le bundle a bien initialisé).
window.__MYDEVICES_SHELL_READY__ = true;
