// Unit tests pour services/mydevices-web/frontend/utils/format.js (TKT-103).
//
// Exécutable directement :  node tests/unit/test_format_utils.js
// Aussi déclenché par      :  pytest tests/unit/test_format_utils.py
//
// On utilise le test runner natif de Node 18+ (`node:test`) pour éviter
// d'ajouter une dépendance JS dev. La timezone des dates est forcée à
// Europe/Paris en début de process pour rendre les assertions reproductibles
// quel que soit l'environnement (CI runners US, dev macOS, etc.).

process.env.TZ = 'Europe/Paris';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const MODULE_PATH = path.resolve(
  __dirname,
  '..',
  '..',
  'services',
  'mydevices-web',
  'frontend',
  'utils',
  'format.js',
);

// L'utilitaire est en ESM (export …) et le test runner est en CJS — on passe
// par import() dynamique. Top-level await indispo en CJS → wrapper async.
async function loadModule() {
  return await import(pathToFileURL(MODULE_PATH).href);
}

test('formatDuration: cas invalides retournent une chaîne vide', async () => {
  const { formatDuration } = await loadModule();
  assert.equal(formatDuration(null), '');
  assert.equal(formatDuration(undefined), '');
  assert.equal(formatDuration(NaN), '');
  assert.equal(formatDuration(0), '');
  assert.equal(formatDuration(-12), '');
  assert.equal(formatDuration('abc'), '');
});

test('formatDuration: < 1 min → secondes seules', async () => {
  const { formatDuration } = await loadModule();
  assert.equal(formatDuration(1), '1 s');
  assert.equal(formatDuration(45), '45 s');
  assert.equal(formatDuration(59), '59 s');
});

test('formatDuration: < 1 h → minutes + secondes lisibles (pas de NNmNNs)', async () => {
  const { formatDuration } = await loadModule();
  assert.equal(formatDuration(60), '1 min');
  assert.equal(formatDuration(90), '1 min 30 s');
  assert.equal(formatDuration(330), '5 min 30 s');
  assert.equal(formatDuration(3599), '59 min 59 s');
  // Garantit que le pattern brut "NNmNNs" est éradiqué.
  for (const s of [45, 90, 330, 3599]) {
    assert.doesNotMatch(formatDuration(s), /\d+m\d+s/);
  }
});

test('formatDuration: ≥ 1 h → "h" + "min", secondes tronquées', async () => {
  const { formatDuration } = await loadModule();
  assert.equal(formatDuration(3600), '1 h');
  assert.equal(formatDuration(3660), '1 h 1 min');
  assert.equal(formatDuration(5025), '1 h 23 min');
  assert.equal(formatDuration(7200), '2 h');
});

test('formatDuration: secondes fractionnaires arrondies', async () => {
  const { formatDuration } = await loadModule();
  assert.equal(formatDuration(90.4), '1 min 30 s');
  assert.equal(formatDuration(90.6), '1 min 31 s');
});

test('formatDate: cas invalides retournent une chaîne vide', async () => {
  const { formatDate } = await loadModule();
  assert.equal(formatDate(null), '');
  assert.equal(formatDate(undefined), '');
  assert.equal(formatDate(''), '');
  assert.equal(formatDate('pas une date'), '');
});

test('formatDate: ISO sans option → "15 mai 2026" (jour mois année français)', async () => {
  const { formatDate } = await loadModule();
  // ISO sans Z → minuit Europe/Paris.
  assert.equal(formatDate('2026-05-15T00:00:00+02:00'), '15 mai 2026');
  // Pattern brut DD/MM/YY explicitement banni.
  assert.doesNotMatch(formatDate('2026-05-15T00:00:00+02:00'), /\d{2}\/\d{2}\/\d{2}/);
});

test('formatDate: withTime=true → "<date> à HH:MM"', async () => {
  const { formatDate } = await loadModule();
  const out = formatDate('2026-05-15T14:30:00+02:00', { withTime: true });
  assert.equal(out, '15 mai 2026 à 14:30');
  assert.doesNotMatch(out, /\d{2}\/\d{2}\/\d{2}/);
});

test('formatDate: withTime=false explicite équivaut à sans option', async () => {
  const { formatDate } = await loadModule();
  const a = formatDate('2026-05-15T14:30:00+02:00');
  const b = formatDate('2026-05-15T14:30:00+02:00', { withTime: false });
  assert.equal(a, b);
});

test('default export expose les deux fonctions', async () => {
  const mod = await loadModule();
  assert.equal(typeof mod.default.formatDuration, 'function');
  assert.equal(typeof mod.default.formatDate, 'function');
});
