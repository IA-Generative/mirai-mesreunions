// Panier de sources du brief — état JS + rendu en chips.
//
// Pourquoi un state JS et pas du DOM-as-truth (le pattern majoritaire du
// reste de l'app) : une entrée n'est pas une chaîne, c'est un objet riche
// (type, identifiants, origine, poids), et une source `inline` porte jusqu'à
// 20 000 caractères de texte collé. Stocker ça dans un attribut DOM serait
// à la fois illisible et coûteux, et rien n'y est éditable en place. On
// emprunte donc à `lib/themes-chips.js` son contrat : un state privé, exposé
// via des propriétés `_...` posées sur le container, et un `onChange`.
//
// En revanche, tout ce qui est *décidable sans DOM* est sorti en fonctions
// pures exportées — `toApiSources`, `computeBudget`, `isReadableFile`,
// `estimateSourceChars`, `mergeEntries`, `normalizeEntry`. C'est la seule
// chose testable : il n'y a ni vitest ni jsdom dans ce dépôt, les tests JS
// tournent sur `node:test` (cf. tests/unit/test_source_basket.js).
//
// ─── Vocabulaire ───────────────────────────────────────────────────
//
// Une *entrée* est ce que manipule l'UI : un objet par chip affichée. Une
// *source* est ce que reçoit le serveur (`app/modules/preparations/sources.py`).
// Les deux ne se correspondent pas un pour un : plusieurs fichiers Drive
// choisis dans la même instance forment UNE source `drive_files`. C'est
// `toApiSources` qui fait ce regroupement, et lui seul.
//
// Types d'entrée : 'drive_folder' | 'drive_file' | 'preparation' | 'inline'
// Types de source : 'drive_folder' | 'drive_files' | 'preparation' | 'inline'

// ─── Plafonds — miroir de app/modules/preparations/sources.py ──────
//
// Dupliqués ici volontairement : le serveur reste l'autorité (il répond 400),
// mais une UI qui laisse construire une sélection vouée au refus est une UI
// qui fait perdre du temps. Toute évolution doit toucher les deux fichiers.
export const CAPS = {
  sources: 20,          // MAX_SOURCES
  driveFolders: 5,      // MAX_DRIVE_FOLDERS
  driveFiles: 20,       // MAX_DRIVE_FILES
  preparations: 5,      // MAX_PREPARATIONS
  inline: 20,           // MAX_INLINE
  inlineChars: 20000,   // MAX_INLINE_CHARS
  inlineTotalChars: 40000, // MAX_INLINE_TOTAL_CHARS
  labelChars: 200,      // MAX_LABEL_CHARS
};

// Budgets de corpus par emplacement du prompt — miroir de
// app/meeting_prep.py (DEFAULT_TOTAL_MAX_CHARS et les budgets par bucket).
// Trois budgets séparés et non un global : {PREP_DOCS}, {PRIOR_MEETINGS} et
// {INLINE_MESSAGES} sont trois sections distinctes du prompt, un gros dossier
// Drive ne doit pas évincer les réunions passées.
export const BUDGETS = {
  documents: 80000,
  meetings: 30000,
  messages: 20000,
};

// Un dossier Drive n'a pas de poids connu côté client (il faudrait le lister).
// On le compte pour la moitié du budget documents : la jauge passe donc à
// l'orange dès deux dossiers, ce qui est la vérité — le serveur s'arrête à
// 8 documents et 80 000 caractères, le reste est silencieusement ignoré.
const FOLDER_ESTIMATE_CHARS = 40000;
const PER_DOC_CHAR_CAP = 20000;      // DEFAULT_PER_DOC_MAX_CHARS

// Poids d'une réunion passée, par section demandée. Ordres de grandeur tirés
// des briefs réellement générés (un CR riche pèse plusieurs fois un brief).
const PREPARATION_PART_CHARS = { brief: 6000, key_points: 1500, cr: 12000 };

// Ratio « octets du fichier → caractères de texte extractible », par format.
// Un .txt fait 1 caractère par octet ; un .docx est un zip d'XML, un .pdf
// embarque polices et images. Ces ratios ne servent qu'à la jauge : ils sont
// approximatifs et assumés comme tels.
const CHARS_PER_BYTE = {
  text: 1, docx: 0.15, odt: 0.15, pdf: 0.10, pptx: 0.05, odp: 0.05,
};

// ─── Formats lisibles ──────────────────────────────────────────────
//
// Table miroir de services/dmz-to-internal-bridge/app/doc_extractor.py
// (_MIME_TO_KIND / _EXT_TO_KIND / _GENERIC_MIMES). Elle vit ici pour que le
// navigateur puisse griser un fichier non lisible AVANT de le télécharger.
// ⚠ Les deux tables doivent évoluer ensemble : ajouter un format à
// doc_extractor.py sans l'ajouter ici le rend inaccessible depuis le picker.
const MIME_TO_KIND = {
  'application/pdf': 'pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
  'application/vnd.oasis.opendocument.text': 'odt',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
  'application/vnd.oasis.opendocument.presentation': 'odp',
  'text/plain': 'text',
  'text/markdown': 'text',
  'text/x-markdown': 'text',
};
const EXT_TO_KIND = {
  '.pdf': 'pdf', '.docx': 'docx', '.odt': 'odt', '.pptx': 'pptx',
  '.odp': 'odp', '.txt': 'text', '.md': 'text', '.markdown': 'text',
};
const GENERIC_MIMES = new Set(['application/octet-stream', 'binary/octet-stream', '']);

/**
 * Type de contenu extractible d'un fichier, ou null s'il est illisible.
 * Reproduit `_detect_kind` : le MIME l'emporte quand il est spécifique ;
 * l'extension ne sert de repli que si le MIME est générique ou absent.
 */
export function detectFileKind(name, mime) {
  const bare = String(mime == null ? '' : mime).split(';')[0].trim().toLowerCase();
  if (bare && !GENERIC_MIMES.has(bare)) {
    // MIME spécifique mais inconnu : on ne retombe PAS sur l'extension —
    // c'est exactement ce que fait doc_extractor, qui préfère un refus franc
    // à une extraction hasardeuse.
    return MIME_TO_KIND[bare] || null;
  }
  const lower = String(name == null ? '' : name).toLowerCase();
  const keys = Object.keys(EXT_TO_KIND);
  for (let i = 0; i < keys.length; i += 1) {
    if (lower.endsWith(keys[i])) return EXT_TO_KIND[keys[i]];
  }
  return null;
}

/**
 * Ce fichier sera-t-il lu par le pipeline d'extraction ?
 * Accepte soit un objet `{name, mime}`, soit `(name, mime)`.
 */
export function isReadableFile(fileOrName, maybeMime) {
  if (fileOrName && typeof fileOrName === 'object') {
    return detectFileKind(fileOrName.name, fileOrName.mime || fileOrName.mime_type) !== null;
  }
  return detectFileKind(fileOrName, maybeMime) !== null;
}

// ─── Normalisation d'une entrée ────────────────────────────────────

function _str(v, max) {
  const s = String(v == null ? '' : v).trim();
  return max ? s.slice(0, max) : s;
}

// Hash court et stable d'un texte — sert uniquement de clé de déduplication
// pour les textes collés (deux collages identiques sont bien un doublon).
function _hash(text) {
  let h = 5381;
  const s = String(text || '');
  for (let i = 0; i < s.length; i += 1) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
  return h.toString(36);
}

const VALID_PARTS = ['brief', 'key_points', 'cr'];

/** Clé de déduplication d'une entrée (jamais envoyée au serveur). */
export function entryKey(entry) {
  if (!entry) return '';
  const scope = `${entry.drive || ''}|${entry.host || ''}`;
  switch (entry.type) {
    case 'drive_folder': return `drive_folder|${scope}|${entry.id}`;
    case 'drive_file': return `drive_file|${scope}|${entry.id}`;
    // Deux fois la même réunion avec des sections différentes = une seule
    // entrée : on fusionne les sections plutôt que d'empiler deux chips.
    case 'preparation': return `preparation|${entry.id}`;
    default: return `inline|${_hash(entry.text)}`;
  }
}

/**
 * Rend une entrée canonique à partir d'un objet lâche (payload de picker ou
 * snapshot de brouillon). Retourne `null` si l'entrée est inexploitable —
 * l'appelant décide s'il signale ou s'il ignore.
 */
export function normalizeEntry(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const type = _str(raw.type);
  const origin = _str(raw.origin) || (type === 'preparation' ? 'meetings' : 'drive');
  const base = {
    type,
    origin,
    label: _str(raw.label || raw.name || raw.title, CAPS.labelChars),
    meta: _str(raw.meta, CAPS.labelChars),
  };

  if (type === 'drive_folder' || type === 'drive_file') {
    const id = _str(raw.id);
    if (!id) return null;
    const entry = Object.assign(base, {
      id,
      drive: _str(raw.drive) || null,
      host: _str(raw.host) || null,
    });
    if (!entry.label) entry.label = id;
    if (type === 'drive_file') {
      const bytes = Number(raw.bytes != null ? raw.bytes : raw.size);
      entry.bytes = Number.isFinite(bytes) && bytes > 0 ? Math.round(bytes) : 0;
      entry.mime = _str(raw.mime || raw.mime_type);
    }
    entry.key = entryKey(entry);
    entry.estimated = estimateSourceChars(entry);
    return entry;
  }

  if (type === 'preparation') {
    const id = _str(raw.id);
    if (!id) return null;
    let include = Array.isArray(raw.include)
      ? raw.include.filter((p) => VALID_PARTS.indexOf(p) !== -1)
      : [];
    // Défaut aligné sur le serveur : une réunion cochée sans précision veut
    // dire « son contenu exploitable », donc brief + points clés.
    if (!include.length) include = ['brief', 'key_points'];
    // Ordre canonique : deux entrées portant les mêmes sections dans un ordre
    // différent doivent produire le même payload.
    include = VALID_PARTS.filter((p) => include.indexOf(p) !== -1);
    const entry = Object.assign(base, { id, include });
    if (!entry.label) entry.label = id;
    entry.origin = 'meetings';
    entry.key = entryKey(entry);
    entry.estimated = estimateSourceChars(entry);
    return entry;
  }

  if (type === 'inline') {
    const text = String(raw.text == null ? '' : raw.text);
    if (!text.trim()) return null;
    const entry = Object.assign(base, {
      kind: _str(raw.kind, CAPS.labelChars) || 'note',
      title: _str(raw.title, CAPS.labelChars) || 'Texte collé',
      // Troncature défensive au plafond serveur : mieux vaut une source
      // amputée qu'un 400 après avoir rempli tout le wizard.
      text: text.slice(0, CAPS.inlineChars),
    });
    entry.label = entry.title;
    entry.origin = origin === 'drive' ? 'mail' : origin;
    entry.key = entryKey(entry);
    entry.estimated = estimateSourceChars(entry);
    return entry;
  }

  return null;
}

// ─── Poids estimé ──────────────────────────────────────────────────

/** Nombre de caractères que cette entrée pèsera (estimé) dans le corpus. */
export function estimateSourceChars(entry) {
  if (!entry) return 0;
  if (entry.type === 'inline') return String(entry.text || '').length;
  if (entry.type === 'drive_folder') return FOLDER_ESTIMATE_CHARS;
  if (entry.type === 'preparation') {
    return (entry.include || []).reduce(
      (acc, part) => acc + (PREPARATION_PART_CHARS[part] || 0), 0,
    );
  }
  if (entry.type === 'drive_file') {
    const kind = detectFileKind(entry.label || entry.name, entry.mime);
    if (!kind) return 0; // illisible = ne pèse rien, il ne sera jamais lu
    const bytes = Number(entry.bytes) || 0;
    // Sans taille connue, on prend un document « moyen » plutôt que zéro :
    // une jauge qui reste verte alors qu'on a coché 20 fichiers ment.
    const chars = bytes > 0 ? bytes * (CHARS_PER_BYTE[kind] || 0.1) : 4000;
    return Math.min(Math.round(chars), PER_DOC_CHAR_CAP);
  }
  return 0;
}

const BUCKET_BY_TYPE = {
  drive_folder: 'documents',
  drive_file: 'documents',
  preparation: 'meetings',
  inline: 'messages',
};

function _level(ratio) {
  if (ratio > 1) return 'over';
  if (ratio >= 0.7) return 'warn';
  return 'ok';
}

/**
 * Budget consommé par bucket + décompte des plafonds.
 *
 * Retourne `{ buckets, counts, level, warnings }` où chaque bucket porte
 * `{ used, budget, ratio, level }` avec `level` ∈ ok|warn|over (vert sous
 * 70 %, orange ensuite, rouge au-delà de 100 %).
 */
export function computeBudget(entries) {
  const list = Array.isArray(entries) ? entries : [];
  const buckets = {};
  Object.keys(BUDGETS).forEach((b) => {
    buckets[b] = { used: 0, budget: BUDGETS[b], ratio: 0, level: 'ok' };
  });
  const counts = {
    drive_folder: 0, drive_file: 0, preparation: 0, inline: 0, sources: 0,
  };

  list.forEach((entry) => {
    if (!entry || !BUCKET_BY_TYPE[entry.type]) return;
    counts[entry.type] += 1;
    const chars = (typeof entry.estimated === 'number')
      ? entry.estimated
      : estimateSourceChars(entry);
    buckets[BUCKET_BY_TYPE[entry.type]].used += chars;
  });

  Object.keys(buckets).forEach((b) => {
    const bucket = buckets[b];
    bucket.ratio = bucket.budget > 0 ? bucket.used / bucket.budget : 0;
    bucket.level = _level(bucket.ratio);
  });

  // Le nombre de *sources* envoyées n'est pas le nombre d'entrées : les
  // fichiers Drive d'une même instance sont regroupés en une seule source.
  counts.sources = toApiSources(list).length;

  const warnings = [];
  if (counts.sources > CAPS.sources) warnings.push(`Trop de sources (maximum ${CAPS.sources}).`);
  if (counts.drive_folder > CAPS.driveFolders) warnings.push(`Trop de dossiers Drive (maximum ${CAPS.driveFolders}).`);
  if (counts.drive_file > CAPS.driveFiles) warnings.push(`Trop de fichiers Drive (maximum ${CAPS.driveFiles}).`);
  if (counts.preparation > CAPS.preparations) warnings.push(`Trop de réunions précédentes (maximum ${CAPS.preparations}).`);
  if (counts.inline > CAPS.inline) warnings.push(`Trop de textes collés (maximum ${CAPS.inline}).`);
  if (buckets.messages.used > CAPS.inlineTotalChars) {
    warnings.push(`Les textes collés dépassent ${CAPS.inlineTotalChars} caractères au total.`);
  }

  const order = { ok: 0, warn: 1, over: 2 };
  const level = Object.keys(buckets)
    .reduce((worst, b) => (order[buckets[b].level] > order[worst] ? buckets[b].level : worst), 'ok');

  return { buckets, counts, level, warnings };
}

// ─── Fusion (ajout au panier) ──────────────────────────────────────

/**
 * Fusionne `incoming` dans `existing` en respectant les plafonds serveur.
 *
 * Retourne `{ entries, added, rejected }` — `rejected` porte un motif lisible
 * par entrée refusée, pour que l'appelant puisse le remonter en toast plutôt
 * que de laisser disparaître silencieusement un choix de l'utilisateur.
 */
export function mergeEntries(existing, incoming) {
  const out = [];
  const seen = Object.create(null);
  const rejected = [];
  let added = 0;

  (Array.isArray(existing) ? existing : []).forEach((raw) => {
    const entry = raw && raw.key ? raw : normalizeEntry(raw);
    if (!entry) return;
    if (seen[entry.key]) return;
    seen[entry.key] = entry;
    out.push(entry);
  });

  const count = (type) => out.filter((e) => e.type === type).length;
  const inlineChars = () => out
    .filter((e) => e.type === 'inline')
    .reduce((acc, e) => acc + String(e.text || '').length, 0);

  (Array.isArray(incoming) ? incoming : []).forEach((raw) => {
    const entry = normalizeEntry(raw);
    if (!entry) {
      rejected.push({ entry: raw, reason: 'Source inexploitable — ignorée.' });
      return;
    }
    const known = seen[entry.key];
    if (known) {
      // Même réunion re-cochée avec d'autres sections : on fusionne les
      // sections au lieu de refuser (l'utilisateur a exprimé un ajout).
      if (entry.type === 'preparation') {
        const merged = VALID_PARTS.filter(
          (p) => known.include.indexOf(p) !== -1 || entry.include.indexOf(p) !== -1,
        );
        if (merged.length !== known.include.length) {
          known.include = merged;
          known.estimated = estimateSourceChars(known);
          added += 1;
        }
      }
      return;
    }
    if (toApiSources(out.concat([entry])).length > CAPS.sources) {
      rejected.push({ entry, reason: `Maximum ${CAPS.sources} sources.` });
      return;
    }
    if (entry.type === 'drive_folder' && count('drive_folder') >= CAPS.driveFolders) {
      rejected.push({ entry, reason: `Maximum ${CAPS.driveFolders} dossiers Drive.` });
      return;
    }
    if (entry.type === 'drive_file' && count('drive_file') >= CAPS.driveFiles) {
      rejected.push({ entry, reason: `Maximum ${CAPS.driveFiles} fichiers Drive.` });
      return;
    }
    if (entry.type === 'preparation' && count('preparation') >= CAPS.preparations) {
      rejected.push({ entry, reason: `Maximum ${CAPS.preparations} réunions précédentes.` });
      return;
    }
    if (entry.type === 'inline') {
      if (count('inline') >= CAPS.inline) {
        rejected.push({ entry, reason: `Maximum ${CAPS.inline} textes collés.` });
        return;
      }
      if (inlineChars() + entry.text.length > CAPS.inlineTotalChars) {
        rejected.push({
          entry,
          reason: `Les textes collés dépassent ${CAPS.inlineTotalChars} caractères au total.`,
        });
        return;
      }
    }
    seen[entry.key] = entry;
    out.push(entry);
    added += 1;
  });

  return { entries: out, added, rejected };
}

// ─── Contrat de fil ────────────────────────────────────────────────

/**
 * Convertit les entrées d'UI en tableau `sources[]` accepté par
 * `POST /api/preparations` (cf. app/modules/preparations/sources.py).
 *
 * Deux responsabilités, et seulement celles-là :
 *   - **regrouper** les fichiers Drive d'une même instance en une source
 *     `drive_files` unique (le serveur compte les fichiers, pas les sources) ;
 *   - **ne rien laisser fuir** des clés d'affichage (`key`, `origin`, `meta`,
 *     `bytes`, `estimated`, `mime`) : elles n'ont aucun sens pour le serveur,
 *     qui les ignorerait, et gonfleraient un corps de requête déjà borné à
 *     2 Mio par les textes collés.
 */
export function toApiSources(entries) {
  const list = Array.isArray(entries) ? entries : [];
  const out = [];
  const fileGroups = new Map();

  list.forEach((raw) => {
    const entry = raw && raw.key ? raw : normalizeEntry(raw);
    if (!entry) return;
    if (entry.type === 'drive_folder') {
      out.push({
        type: 'drive_folder',
        id: entry.id,
        drive: entry.drive || null,
        host: entry.host || null,
        label: entry.label || '',
      });
    } else if (entry.type === 'drive_file') {
      const scope = `${entry.drive || ''}|${entry.host || ''}`;
      let group = fileGroups.get(scope);
      if (!group) {
        group = {
          type: 'drive_files',
          items: [],
          drive: entry.drive || null,
          host: entry.host || null,
        };
        fileGroups.set(scope, group);
        out.push(group);
      }
      group.items.push({ id: entry.id, name: entry.label || '' });
    } else if (entry.type === 'preparation') {
      out.push({
        type: 'preparation',
        id: entry.id,
        include: (entry.include || []).slice(),
        label: entry.label || '',
      });
    } else if (entry.type === 'inline') {
      out.push({
        type: 'inline',
        kind: entry.kind || 'note',
        title: entry.title || 'Texte collé',
        text: entry.text || '',
      });
    }
  });

  return out;
}

// ─── Brouillons localStorage ───────────────────────────────────────

// Budget total accordé aux textes collés DANS UN BROUILLON. Volontairement
// bien plus bas que le plafond serveur : `_writeAllDrafts` avale un
// `QuotaExceededError` en silence, et le quota localStorage (~5 Mo, partagé
// entre TOUS les brouillons) est atteint avec quelques textes de 20 k. Un
// brouillon perdu sans signal coûte plus cher qu'un texte tronqué et signalé.
export const DRAFT_INLINE_TOTAL_CHARS = 12000;

/**
 * Version allégée des entrées, destinée au brouillon localStorage.
 *
 * Retourne `{ entries, degraded }` — `degraded` vaut true si au moins un
 * texte collé a été amputé, pour que l'appelant puisse le dire.
 */
export function snapshotEntries(entries, opts) {
  const budget = (opts && opts.inlineTotalChars) || DRAFT_INLINE_TOTAL_CHARS;
  let remaining = budget;
  let degraded = false;
  const out = (Array.isArray(entries) ? entries : []).map((entry) => {
    if (!entry || entry.type !== 'inline') return entry;
    const text = String(entry.text || '');
    if (text.length <= remaining) {
      remaining -= text.length;
      return entry;
    }
    degraded = true;
    const kept = Math.max(0, remaining);
    remaining = 0;
    return Object.assign({}, entry, {
      text: text.slice(0, kept),
      truncatedInDraft: true,
    });
  });
  return { entries: out, degraded };
}

// ─── Rendu (DOM) ───────────────────────────────────────────────────

const _SAFE_RE = /[&<>"']/g;
function _esc(s) {
  return String(s == null ? '' : s).replace(_SAFE_RE, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

const ORIGIN_BADGES = {
  drive: { label: 'Drive', color: '#0c4498', bg: '#e8f0fe' },
  meetings: { label: 'Réunion', color: '#7c2d12', bg: '#ffedd5' },
  mail: { label: 'Message', color: '#166534', bg: '#dcfce7' },
  link: { label: 'Lien', color: '#3730a3', bg: '#e0e7ff' },
};

const LEVEL_COLORS = { ok: '#15803d', warn: '#b45309', over: '#b91c1c' };

function _formatChars(n) {
  const v = Math.round(n || 0);
  if (v >= 1000) return `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)} k car.`;
  return `${v} car.`;
}

function _gauge(name, bucket) {
  const pct = Math.min(100, Math.round(bucket.ratio * 100));
  const color = LEVEL_COLORS[bucket.level];
  return `
    <div class="source-budget-row" style="display:flex;align-items:center;gap:0.5rem;font-size:0.78rem;">
      <span style="flex:0 0 5.5rem;color:#64748b;">${_esc(name)}</span>
      <span style="flex:1 1 auto;background:#e2e8f0;border-radius:999px;height:6px;overflow:hidden;">
        <span style="display:block;height:100%;width:${pct}%;background:${color};"></span>
      </span>
      <span style="flex:0 0 auto;color:${color};font-variant-numeric:tabular-nums;">
        ${Math.round(bucket.ratio * 100)}&nbsp;%
      </span>
    </div>`;
}

/**
 * Monte le panier dans `rootEl`.
 *
 * `opts.onChange(entries)` est appelé à chaque mutation. Le wizard DOIT le
 * fournir : les pickers vivent dans `document.body`, donc hors du nœud sur
 * lequel `_bindAutosave` délègue (`#wizard-modal-backdrop`) — sans ce rappel,
 * rien de ce que l'utilisateur choisit ne survit à une fermeture du wizard.
 */
export function mountSourceBasket(rootEl, opts) {
  if (!rootEl) return;
  opts = opts || {};
  const onChange = typeof opts.onChange === 'function' ? opts.onChange : null;
  const state = { entries: [] };

  rootEl.classList.add('source-basket');
  rootEl.innerHTML = `
    <div class="source-basket-empty" style="color:#64748b;font-size:0.85rem;padding:0.6rem 0;">
      Aucune source pour l'instant — le brief sera généré à partir de vos seules réponses.
    </div>
    <div class="source-basket-list" style="display:flex;flex-wrap:wrap;gap:0.4rem;"></div>
    <div class="source-basket-budget" style="margin-top:0.7rem;display:none;
         border-top:1px solid #e2e8f0;padding-top:0.6rem;"></div>`;

  const emptyEl = rootEl.querySelector('.source-basket-empty');
  const listEl = rootEl.querySelector('.source-basket-list');
  const budgetEl = rootEl.querySelector('.source-basket-budget');

  function _render() {
    const entries = state.entries;
    emptyEl.style.display = entries.length ? 'none' : '';
    listEl.innerHTML = entries.map((entry, idx) => {
      const badge = ORIGIN_BADGES[entry.origin] || ORIGIN_BADGES.drive;
      const weight = _formatChars(entry.estimated);
      return `
        <span class="source-chip" data-source-key="${_esc(entry.key)}"
              style="display:inline-flex;align-items:center;gap:0.4rem;max-width:100%;
                     background:#f8fafc;border:1px solid #e2e8f0;border-radius:999px;
                     padding:0.15rem 0.2rem 0.15rem 0.5rem;font-size:0.82rem;">
          <span style="background:${badge.bg};color:${badge.color};border-radius:999px;
                       padding:0.05rem 0.4rem;font-size:0.7rem;font-weight:600;">${_esc(badge.label)}</span>
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:16rem;"
                title="${_esc(entry.meta || entry.label)}">${_esc(entry.label)}</span>
          <span style="color:#94a3b8;font-size:0.72rem;white-space:nowrap;">${_esc(weight)}</span>
          <button type="button" class="source-chip-remove" data-source-remove="${idx}"
                  aria-label="Retirer ${_esc(entry.label)}"
                  style="background:none;border:0;cursor:pointer;color:#b91c1c;font-weight:700;
                         padding:0 0.35rem;line-height:1;">×</button>
        </span>`;
    }).join('');

    const budget = computeBudget(entries);
    if (!entries.length) {
      budgetEl.style.display = 'none';
      budgetEl.innerHTML = '';
    } else {
      budgetEl.style.display = '';
      budgetEl.innerHTML = `
        <div style="font-size:0.78rem;color:#475569;margin-bottom:0.35rem;">
          Budget du corpus — ${budget.counts.sources} / ${CAPS.sources} sources
        </div>
        ${_gauge('Documents', budget.buckets.documents)}
        ${_gauge('Réunions', budget.buckets.meetings)}
        ${_gauge('Messages', budget.buckets.messages)}
        ${budget.warnings.length
          ? `<div class="fr-alert fr-alert--warning fr-alert--sm" style="margin-top:0.5rem;">
               <p>${budget.warnings.map(_esc).join('<br>')}</p></div>`
          : ''}
        ${budget.level === 'over'
          ? `<div style="margin-top:0.4rem;font-size:0.76rem;color:${LEVEL_COLORS.over};">
               Au-delà du budget, le contenu excédentaire ne sera pas lu.</div>`
          : ''}`;
    }

    listEl.querySelectorAll('[data-source-remove]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const i = parseInt(btn.getAttribute('data-source-remove'), 10);
        if (Number.isNaN(i)) return;
        state.entries.splice(i, 1);
        _render();
        _notify();
      });
    });
  }

  function _notify() {
    if (onChange) {
      try { onChange(state.entries.slice()); } catch (e) { /* non-fatal */ }
    }
  }

  // API publique posée sur le container, façon lib/themes-chips.js.
  rootEl._sourceBasketState = state;
  rootEl._sourceBasketRender = _render;
  rootEl._sourceBasketSet = (entries) => {
    const merged = mergeEntries([], entries || []);
    state.entries = merged.entries;
    _render();
    _notify();
    return merged;
  };
  rootEl._sourceBasketAdd = (entries) => {
    const merged = mergeEntries(state.entries, entries || []);
    state.entries = merged.entries;
    _render();
    _notify();
    return merged;
  };

  _render();
}

/** Entrées courantes du panier (copie). */
export function getSourceBasketEntries(rootEl) {
  if (!rootEl || !rootEl._sourceBasketState) return [];
  return rootEl._sourceBasketState.entries.slice();
}

/** Remplace le contenu du panier. Retourne le résultat de `mergeEntries`. */
export function setSourceBasketEntries(rootEl, entries) {
  if (!rootEl || !rootEl._sourceBasketSet) return { entries: [], added: 0, rejected: [] };
  return rootEl._sourceBasketSet(entries);
}

/** Ajoute des entrées au panier. Retourne le résultat de `mergeEntries`. */
export function addSourceBasketEntries(rootEl, entries) {
  if (!rootEl || !rootEl._sourceBasketAdd) return { entries: [], added: 0, rejected: [] };
  return rootEl._sourceBasketAdd(entries);
}
