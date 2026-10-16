// Tests de aplatirSource (services/mesreunions-web/frontend/lib/sources-openrag.js),
// appelée par le widget « Interroger mes réunions » à l'entrée de la boucle des
// puces de source.
//
// Exécutable directement :  node --test tests/unit/test_sources_openrag.js
// Aussi déclenché par      :  pytest tests/unit/test_sources_openrag.py

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const MODULE_PATH = path.resolve(
  __dirname, '..', '..',
  'services', 'mesreunions-web', 'frontend', 'lib', 'sources-openrag.js',
);

async function loadModule() {
  return await import(pathToFileURL(MODULE_PATH).href);
}

const HOTE = 'https://openrag.example';
const A_PLAT = {
  title: 'Comité de pilotage budget', file_id: '0b9c6a52_1d2e', partition: 'perso-x', page: 2,
  _id: '1002', source_type: 'document',
  chunk_url: `${HOTE}/extract/1002`, file_url: `${HOTE}/static/1002`,
  metadata: { uaf_id: '0b9c6a52-1d2e', meeting_title: 'Comité de pilotage budget' },
};
const SOUS_CHUNK = {
  source_type: 'document',
  chunk: { title: 'Comité de pilotage budget', file_id: '0b9c6a52_1d2e', partition: 'perso-x', page: 2, _id: '1002' },
  chunk_url: `${HOTE}/extract/1002`, file_url: `${HOTE}/static/1002`,
  metadata: { uaf_id: '0b9c6a52-1d2e', meeting_title: 'Comité de pilotage budget' },
};

test('2.3.1 : les métadonnées sous chunk remontent au premier niveau', async () => {
  const { aplatirSource } = await loadModule();
  assert.deepEqual(aplatirSource(SOUS_CHUNK), A_PLAT);
});

test('2.2.1 : une source à plat est rendue telle quelle (même objet)', async () => {
  const { aplatirSource } = await loadModule();
  assert.equal(aplatirSource(A_PLAT), A_PLAT);
});

test('source web inchangée', async () => {
  const { aplatirSource } = await loadModule();
  const web = { source_type: 'web', url: 'https://www.example.org/page', title: 'Page', snippet: '…' };
  assert.equal(aplatirSource(web), web);
});

test('les clés du premier niveau gagnent sur celles de chunk', async () => {
  const { aplatirSource } = await loadModule();
  const s = { source_type: 'document', chunk: { page: 1, file_url: 'x' }, file_url: `${HOTE}/static/9` };
  assert.deepEqual(aplatirSource(s), { page: 1, file_url: `${HOTE}/static/9`, source_type: 'document' });
});

test('valeurs dégénérées : rendues telles quelles, sans exception', async () => {
  const { aplatirSource } = await loadModule();
  for (const v of [null, undefined, '', 'texte', 3, { chunk: null }, { chunk: 'x' }]) {
    assert.equal(aplatirSource(v), v);
  }
});
