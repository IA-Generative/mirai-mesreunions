// Lit le contexte Jinja injecté par Flask dans <script type="application/json"
// id="bootstrap-data">. Remplace les anciens {{ ... }} embarqués dans le JS
// inline du template index.html (cf. PR4).
//
// Schéma attendu :
//   {
//     "allowed_audio_extensions": "m4a,mp3,wav,...",
//     "device_retention_days": 15,
//     "user": { "email": "...", "name": "...", "roles": [...] }
//   }

function _readBootstrap() {
  try {
    const el = document.getElementById('bootstrap-data');
    if (!el) return {};
    return JSON.parse(el.textContent || '{}');
  } catch (e) {
    console.warn('bootstrap-data illisible, fallback {} :', e);
    return {};
  }
}

const _BOOT = _readBootstrap();

export const ALLOWED_AUDIO_EXTENSIONS = new Set(
  ((_BOOT.allowed_audio_extensions || '') + '')
    .split(',')
    .map((e) => e.trim().toLowerCase())
    .filter(Boolean)
);

export const DEVICE_RETENTION_DAYS = Number(_BOOT.device_retention_days || 15);

export const CURRENT_USER = _BOOT.user || {};

// Expose au global pour les modules legacy (tabs/*) qui ne sont pas
// encore tree-shakeable.
window.__BOOTSTRAP__ = _BOOT;
window.ALLOWED_AUDIO_EXTENSIONS = ALLOWED_AUDIO_EXTENSIONS;
window.DEVICE_RETENTION_DAYS = DEVICE_RETENTION_DAYS;
