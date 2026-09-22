// Tests unitaires des fonctions pures du panier de sources
// (services/mesreunions-web/frontend/lib/source-basket.js).
//
// Exécutable directement :  node --test tests/unit/test_source_basket.js
// Aussi déclenché par      :  pytest tests/unit/test_source_basket.py
//
// On reste sur le test runner natif de Node 18+ (`node:test`), comme
// test_format_utils.js : il n'y a ni vitest ni jsdom dans ce dépôt, et on ne
// va pas en ajouter pour quatre fonctions. C'est précisément pour ça que le
// panier expose des fonctions SANS DOM : elles sont la seule partie testable,
// et ce sont aussi celles dont une erreur coûte le plus cher (un contrat de
// fil qui dérive donne un 400 côté serveur, ou pire, une source ignorée).

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const MODULE_PATH = path.resolve(
  __dirname, '..', '..',
  'services', 'mesreunions-web', 'frontend', 'lib', 'source-basket.js',
);

// Le module est en ESM et le runner en CJS → import() dynamique.
async function loadModule() {
  return await import(pathToFileURL(MODULE_PATH).href);
}

// ─── isReadableFile ────────────────────────────────────────────────
// Miroir de services/dmz-to-internal-bridge/app/doc_extractor.py.

test('isReadableFile: MIME spécifique connu → lisible', async () => {
  const { isReadableFile } = await loadModule();
  assert.equal(isReadableFile({ name: 'a.pdf', mime: 'application/pdf' }), true);
  assert.equal(isReadableFile({
    name: 'note.docx',
    mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  }), true);
  assert.equal(isReadableFile({ name: 'x', mime: 'text/markdown' }), true);
  // Les paramètres du Content-Type ne doivent pas empêcher la détection.
  assert.equal(isReadableFile({ name: 'x', mime: 'text/plain; charset=utf-8' }), true);
});

test('isReadableFile: MIME spécifique inconnu → NON lisible, même avec une bonne extension', async () => {
  const { isReadableFile } = await loadModule();
  // doc_extractor ne retombe pas sur l'extension quand le MIME est explicite
  // et inconnu : il préfère un refus franc à une extraction hasardeuse.
  assert.equal(isReadableFile({ name: 'rapport.pdf', mime: 'image/png' }), false);
  assert.equal(isReadableFile({ name: 'a.docx', mime: 'application/zip' }), false);
});

test('isReadableFile: MIME générique ou absent → repli sur l\'extension', async () => {
  const { isReadableFile } = await loadModule();
  assert.equal(isReadableFile({ name: 'a.pdf', mime: 'application/octet-stream' }), true);
  assert.equal(isReadableFile({ name: 'notes.md', mime: '' }), true);
  assert.equal(isReadableFile({ name: 'NOTES.MARKDOWN' }), true);
  assert.equal(isReadableFile({ name: 'archive.zip', mime: '' }), false);
  assert.equal(isReadableFile({ name: 'sans-extension', mime: '' }), false);
});

test('isReadableFile: accepte aussi la forme (name, mime)', async () => {
  const { isReadableFile } = await loadModule();
  assert.equal(isReadableFile('a.odt', 'application/vnd.oasis.opendocument.text'), true);
  assert.equal(isReadableFile('a.exe', ''), false);
});

// ─── toApiSources — le contrat de fil ──────────────────────────────

const DISPLAY_ONLY_KEYS = ['key', 'origin', 'meta', 'bytes', 'estimated', 'mime'];

// Clés autorisées pour chaque type, telles que les valide
// app/modules/preparations/sources.py.
const WIRE_KEYS = {
  drive_folder: ['type', 'id', 'drive', 'host', 'label'],
  drive_files: ['type', 'items', 'drive', 'host'],
  preparation: ['type', 'id', 'include', 'label'],
  inline: ['type', 'kind', 'title', 'text'],
};

test('toApiSources: aucune clé d\'affichage ne franchit le fil', async () => {
  const { toApiSources, normalizeEntry } = await loadModule();
  const entries = [
    normalizeEntry({
      type: 'drive_folder', id: 'f1', label: 'Budget', origin: 'drive', meta: 'Dossier',
    }),
    normalizeEntry({
      type: 'drive_file', id: 'd1', label: 'note.pdf', mime: 'application/pdf',
      bytes: 4096, origin: 'drive', meta: 'Fichier',
    }),
    normalizeEntry({
      type: 'preparation', id: 'p1', include: ['cr'], label: 'CODIR', origin: 'meetings',
    }),
    normalizeEntry({
      type: 'inline', title: 'Mail DAF', text: 'bonjour', origin: 'mail', meta: '7 car.',
    }),
  ];
  const out = toApiSources(entries);
  assert.equal(out.length, 4);
  for (const src of out) {
    for (const forbidden of DISPLAY_ONLY_KEYS) {
      assert.equal(
        Object.prototype.hasOwnProperty.call(src, forbidden), false,
        `clé d'affichage ${forbidden} présente sur une source ${src.type}`,
      );
    }
    assert.deepEqual(
      Object.keys(src).sort(), WIRE_KEYS[src.type].slice().sort(),
      `clés inattendues sur une source ${src.type}`,
    );
  }
});

test('toApiSources: les fichiers Drive d\'une même instance forment UNE source', async () => {
  const { toApiSources } = await loadModule();
  const out = toApiSources([
    { type: 'drive_file', id: 'a', label: 'a.pdf', drive: 'beta' },
    { type: 'drive_file', id: 'b', label: 'b.pdf', drive: 'beta' },
    { type: 'drive_file', id: 'c', label: 'c.pdf', drive: 'dinum' },
  ]);
  assert.equal(out.length, 2);
  const beta = out.find((s) => s.drive === 'beta');
  assert.equal(beta.type, 'drive_files');
  assert.deepEqual(beta.items, [{ id: 'a', name: 'a.pdf' }, { id: 'b', name: 'b.pdf' }]);
  const dinum = out.find((s) => s.drive === 'dinum');
  assert.equal(dinum.items.length, 1);
});

test('toApiSources: drive/host absents sortent en null (et non undefined)', async () => {
  const { toApiSources } = await loadModule();
  const [src] = toApiSources([{ type: 'drive_folder', id: 'coll', label: 'collé' }]);
  // `undefined` disparaîtrait de JSON.stringify : le serveur ne verrait pas
  // la clé, ce qui change la clé de déduplication côté sources.py.
  assert.equal(src.drive, null);
  assert.equal(src.host, null);
});

test('toApiSources: sections d\'une réunion en ordre canonique', async () => {
  const { toApiSources } = await loadModule();
  const [src] = toApiSources([
    { type: 'preparation', id: 'p1', include: ['cr', 'brief'], label: 'CODIR' },
  ]);
  assert.deepEqual(src.include, ['brief', 'cr']);
});

test('toApiSources: une réunion sans sections reçoit le défaut du serveur', async () => {
  const { toApiSources } = await loadModule();
  const [src] = toApiSources([{ type: 'preparation', id: 'p1' }]);
  assert.deepEqual(src.include, ['brief', 'key_points']);
});

test('toApiSources: entrées vides ou inconnues ignorées', async () => {
  const { toApiSources } = await loadModule();
  assert.deepEqual(toApiSources(null), []);
  assert.deepEqual(toApiSources([null, {}, { type: 'zzz' }, { type: 'inline', text: '   ' }]), []);
});

// ─── computeBudget ─────────────────────────────────────────────────

test('computeBudget: trois budgets séparés, pas un global', async () => {
  const { computeBudget, BUDGETS } = await loadModule();
  const b = computeBudget([
    { type: 'inline', text: 'x'.repeat(10000) },
    { type: 'preparation', id: 'p', include: ['brief'] },
  ]);
  assert.equal(b.buckets.messages.used, 10000);
  assert.equal(b.buckets.messages.budget, BUDGETS.messages);
  // Le texte collé ne consomme rien du budget documents : ce sont deux
  // emplacements distincts du prompt.
  assert.equal(b.buckets.documents.used, 0);
  assert.ok(b.buckets.meetings.used > 0);
});

test('computeBudget: seuils vert / orange / rouge', async () => {
  const { computeBudget, BUDGETS } = await loadModule();
  const at = (n) => computeBudget([{ type: 'inline', text: 'x'.repeat(n) }]).buckets.messages;
  assert.equal(at(Math.round(BUDGETS.messages * 0.5)).level, 'ok');
  assert.equal(at(Math.round(BUDGETS.messages * 0.7)).level, 'warn');
  assert.equal(at(BUDGETS.messages).level, 'warn');
  assert.equal(at(BUDGETS.messages + 1).level, 'over');
});

test('computeBudget: le pire bucket donne le niveau global', async () => {
  const { computeBudget, BUDGETS } = await loadModule();
  const b = computeBudget([
    { type: 'inline', text: 'x'.repeat(BUDGETS.messages + 1) },
    { type: 'preparation', id: 'p', include: ['brief'] },
  ]);
  assert.equal(b.level, 'over');
});

test('computeBudget: compte les SOURCES du fil, pas les chips', async () => {
  const { computeBudget } = await loadModule();
  const b = computeBudget([
    { type: 'drive_file', id: 'a', label: 'a.pdf', drive: 'beta' },
    { type: 'drive_file', id: 'b', label: 'b.pdf', drive: 'beta' },
    { type: 'drive_file', id: 'c', label: 'c.pdf', drive: 'beta' },
  ]);
  assert.equal(b.counts.drive_file, 3);
  assert.equal(b.counts.sources, 1);
});

test('computeBudget: dépassement de plafond → avertissement lisible', async () => {
  const { computeBudget, CAPS } = await loadModule();
  const preps = [];
  for (let i = 0; i < CAPS.preparations + 2; i += 1) {
    preps.push({ type: 'preparation', id: `p${i}`, include: ['brief'] });
  }
  const b = computeBudget(preps);
  assert.ok(b.warnings.some((w) => w.includes(String(CAPS.preparations))));
});

test('computeBudget: un fichier illisible ne pèse rien', async () => {
  const { computeBudget } = await loadModule();
  const b = computeBudget([
    { type: 'drive_file', id: 'a', label: 'image.png', mime: 'image/png', bytes: 5000000 },
  ]);
  assert.equal(b.buckets.documents.used, 0);
});

test('computeBudget: panier vide → tout à zéro et vert', async () => {
  const { computeBudget } = await loadModule();
  const b = computeBudget([]);
  assert.equal(b.level, 'ok');
  assert.equal(b.counts.sources, 0);
  assert.deepEqual(b.warnings, []);
});

// ─── mergeEntries ──────────────────────────────────────────────────

test('mergeEntries: doublon exact ignoré', async () => {
  const { mergeEntries } = await loadModule();
  const first = mergeEntries([], [{ type: 'drive_folder', id: 'f1', label: 'Budget' }]);
  const second = mergeEntries(first.entries, [{ type: 'drive_folder', id: 'f1', label: 'Budget' }]);
  assert.equal(second.entries.length, 1);
  assert.equal(second.added, 0);
});

test('mergeEntries: même réunion re-cochée → sections fusionnées, une seule chip', async () => {
  const { mergeEntries } = await loadModule();
  const a = mergeEntries([], [{ type: 'preparation', id: 'p1', include: ['brief'] }]);
  const b = mergeEntries(a.entries, [{ type: 'preparation', id: 'p1', include: ['cr'] }]);
  assert.equal(b.entries.length, 1);
  assert.deepEqual(b.entries[0].include, ['brief', 'cr']);
});

test('mergeEntries: le même dossier sur deux instances reste deux entrées', async () => {
  const { mergeEntries } = await loadModule();
  const res = mergeEntries([], [
    { type: 'drive_folder', id: 'f1', drive: 'beta' },
    { type: 'drive_folder', id: 'f1', drive: 'dinum' },
  ]);
  assert.equal(res.entries.length, 2);
});

test('mergeEntries: plafond de dossiers → refus motivé, rien n\'est perdu en silence', async () => {
  const { mergeEntries, CAPS } = await loadModule();
  const incoming = [];
  for (let i = 0; i < CAPS.driveFolders + 1; i += 1) {
    incoming.push({ type: 'drive_folder', id: `f${i}` });
  }
  const res = mergeEntries([], incoming);
  assert.equal(res.entries.length, CAPS.driveFolders);
  assert.equal(res.rejected.length, 1);
  assert.match(res.rejected[0].reason, /dossiers Drive/);
});

test('mergeEntries: le total des textes collés est plafonné', async () => {
  const { mergeEntries, CAPS } = await loadModule();
  const incoming = [];
  // 3 × 20 000 = 60 000 > 40 000 : la troisième doit être refusée. Le préfixe
  // rend les textes distincts — deux collages identiques sont dédupliqués,
  // pas plafonnés, et on ne testerait alors pas ce qu'on croit.
  for (let i = 0; i < 3; i += 1) {
    incoming.push({ type: 'inline', title: `t${i}`, text: `${i}${'x'.repeat(CAPS.inlineChars - 1)}` });
  }
  const res = mergeEntries([], incoming);
  assert.equal(res.entries.filter((e) => e.type === 'inline').length, 2);
  assert.equal(res.rejected.length, 1);
  assert.match(res.rejected[0].reason, /total/);
});

test('mergeEntries: un texte collé est tronqué au plafond serveur', async () => {
  const { mergeEntries, CAPS } = await loadModule();
  const res = mergeEntries([], [{ type: 'inline', text: 'y'.repeat(CAPS.inlineChars + 500) }]);
  assert.equal(res.entries[0].text.length, CAPS.inlineChars);
});

test('mergeEntries: entrée inexploitable → rejet motivé, pas de crash', async () => {
  const { mergeEntries } = await loadModule();
  const res = mergeEntries([], [{ type: 'drive_folder' }, null]);
  assert.equal(res.entries.length, 0);
  assert.equal(res.rejected.length, 2);
});

test('mergeEntries: plafond global de sources respecté', async () => {
  const { mergeEntries, toApiSources, CAPS } = await loadModule();
  const incoming = [];
  // Que des textes collés courts : une source chacun, donc le plafond global
  // et le plafond `inline` se rencontrent au même endroit.
  for (let i = 0; i < CAPS.sources + 5; i += 1) {
    incoming.push({ type: 'inline', title: `t${i}`, text: `contenu ${i}` });
  }
  const res = mergeEntries([], incoming);
  assert.ok(toApiSources(res.entries).length <= CAPS.sources);
  assert.ok(res.rejected.length > 0);
});

// ─── snapshotEntries (garde-fou brouillon) ─────────────────────────

test('snapshotEntries: ampute les textes collés au-delà du budget brouillon', async () => {
  const { snapshotEntries, DRAFT_INLINE_TOTAL_CHARS } = await loadModule();
  const res = snapshotEntries([
    { type: 'inline', text: 'a'.repeat(DRAFT_INLINE_TOTAL_CHARS) },
    { type: 'inline', text: 'b'.repeat(5000) },
  ]);
  assert.equal(res.degraded, true);
  assert.equal(res.entries[0].text.length, DRAFT_INLINE_TOTAL_CHARS);
  assert.equal(res.entries[1].text.length, 0);
  assert.equal(res.entries[1].truncatedInDraft, true);
});

test('snapshotEntries: sous le budget, rien n\'est touché', async () => {
  const { snapshotEntries } = await loadModule();
  const entries = [{ type: 'inline', text: 'court' }, { type: 'drive_folder', id: 'f1' }];
  const res = snapshotEntries(entries);
  assert.equal(res.degraded, false);
  assert.equal(res.entries[0], entries[0]);
  assert.equal(res.entries[1], entries[1]);
});

// ─── Cohérence des plafonds avec le serveur ────────────────────────

test('CAPS reflète app/modules/preparations/sources.py', async () => {
  const { CAPS, BUDGETS } = await loadModule();
  assert.deepEqual(CAPS, {
    sources: 20,
    driveFolders: 5,
    driveFiles: 20,
    preparations: 5,
    inline: 20,
    inlineChars: 20000,
    inlineTotalChars: 40000,
    labelChars: 200,
  });
  assert.deepEqual(BUDGETS, { documents: 80000, meetings: 30000, messages: 20000 });
});
