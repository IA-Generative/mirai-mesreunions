// Les quatre sélecteurs de sources du brief (étape 3 du wizard).
//
//   drive        — navigateur de dossiers/fichiers du Drive
//   preparations — réunions précédentes déjà préparées
//   mail         — collage du contenu d'un message
//   link         — collage d'une URL de dossier Drive (le geste historique)
//
// Chaque picker s'ouvre dans une modale empilable (`lib/stacked-modal.js`),
// donc dans `document.body` : hors du `<form id="wizard-form">`, ce qui
// garantit qu'aucun de ses boutons ne peut soumettre le wizard, et au-dessus
// du voile du wizard (`z-index:9000`).
//
// Trois états sont rendus explicitement pour chacun — chargement, vide,
// erreur avec « Réessayer ». Un picker qui reste blanc parce qu'un fetch a
// échoué est indiscernable d'un picker vide, et l'utilisateur conclut que la
// fonctionnalité ne marche pas.

import { openStackedModal } from './stacked-modal.js';
import { renderGenericSkeleton } from './skeleton.js';
import { showToast } from './toast.js';
import { isReadableFile, CAPS } from './source-basket.js';

const _SAFE_RE = /[&<>"']/g;
function _esc(s) {
  return String(s == null ? '' : s).replace(_SAFE_RE, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function _bytes(n) {
  const v = Number(n) || 0;
  if (v <= 0) return '';
  if (v < 1024) return `${v} o`;
  if (v < 1024 * 1024) return `${Math.round(v / 1024)} Ko`;
  return `${(v / (1024 * 1024)).toFixed(1)} Mo`;
}

// ─── États génériques ──────────────────────────────────────────────

function _renderLoading(el, lines) {
  el.innerHTML = renderGenericSkeleton(lines || 4);
}

function _renderEmpty(el, message, hint) {
  el.innerHTML = `
    <div style="text-align:center;color:#64748b;padding:1.4rem 0.5rem;font-size:0.88rem;">
      <div style="font-size:1.6rem;margin-bottom:0.3rem;" aria-hidden="true">📭</div>
      <div>${_esc(message)}</div>
      ${hint ? `<div style="font-size:0.8rem;margin-top:0.3rem;">${hint}</div>` : ''}
    </div>`;
}

/** Erreur explicite + bouton Réessayer câblé sur `onRetry`. */
function _renderError(el, message, onRetry) {
  el.innerHTML = `
    <div class="fr-alert fr-alert--error fr-alert--sm">
      <p>${_esc(message)}</p>
    </div>
    ${onRetry ? `<div style="margin-top:0.6rem;">
      <button type="button" class="fr-btn fr-btn--sm fr-btn--secondary" data-picker-retry>Réessayer</button>
    </div>` : ''}`;
  if (onRetry) {
    const btn = el.querySelector('[data-picker-retry]');
    if (btn) btn.addEventListener('click', onRetry);
  }
}

/** Ajoute au panier et rend compte : ce qui est entré, ce qui a été refusé. */
function _commit(ctx, entries, okMessage) {
  if (!entries.length) return;
  const res = (ctx.addEntries ? ctx.addEntries(entries) : { added: 0, rejected: [] }) || {};
  if (res.rejected && res.rejected.length) {
    // Un refus silencieux est le pire cas : l'utilisateur croit avoir ajouté
    // une source et découvre son absence à la génération.
    showToast(res.rejected[0].reason || 'Certaines sources ont été refusées.', 'error');
  }
  if (res.added) showToast(okMessage || `${res.added} source(s) ajoutée(s).`, 'success');
}

function _footerButtons(specs) {
  const foot = document.createElement('div');
  foot.style.cssText = 'display:flex;gap:0.5rem;align-items:center;width:100%;';
  foot.innerHTML = specs.map((s) => (
    s.spacer
      ? '<span style="flex:1 1 auto;"></span>'
      : `<button type="button" class="fr-btn fr-btn--sm ${s.cls || 'fr-btn--secondary'}"
                 data-picker-btn="${_esc(s.key)}">${_esc(s.label)}</button>`
  )).join('');
  return foot;
}

// ─── 1. Navigateur Drive ───────────────────────────────────────────

// Contrat servi par app/modules/preparations/routes.py :
//   GET /drive/instances → {drives: [{key, host, label, is_default}]}
//   GET /drive/browse?folder_id=&drive=
//        → {drive, folder_id, is_root, folders: [item], files: [item]}
//     item = {id, name, is_folder, mime_type, size, updated_at}
// La racine est renvoyée quand `folder_id` est absent. Il n'y a pas de fil
// d'Ariane côté serveur : on le tient localement, au fil de la descente.
const BROWSE_URL = '/api/preparations/drive/browse';
const INSTANCES_URL = '/api/preparations/drive/instances';

// Repli si le serveur déployé est antérieur à ces endpoints : la route
// n'existe pas et Flask répond un 404 sans `code`. On le distingue du 404
// applicatif « dossier introuvable », qui, lui, porte un code.
const BROWSE_UNAVAILABLE = 'La navigation dans le Drive n\'est pas disponible sur ce '
  + 'serveur. En attendant, utilisez « Coller un lien » : collez l\'URL du dossier depuis '
  + 'votre Drive, le résultat est identique.';

function openDrivePicker(ctx) {
  const state = {
    instances: [],
    drive: null,
    folderId: '',
    breadcrumb: [],
    // Conservée d'un dossier à l'autre : on ne perd pas la sélection en
    // remontant d'un cran pour aller chercher un document dans un frère.
    selection: new Map(),
  };

  const modal = openStackedModal({
    title: '📁 Choisir dans le Drive',
    trigger: ctx.trigger,
    width: '820px',
    bodyHtml: `
      <div data-drive-instances style="margin-bottom:0.5rem;"></div>
      <div data-drive-crumbs style="font-size:0.82rem;color:#475569;margin-bottom:0.5rem;
           display:flex;flex-wrap:wrap;gap:0.25rem;align-items:center;"></div>
      <div data-drive-list></div>`,
  });

  const instancesEl = modal.body.querySelector('[data-drive-instances]');
  const crumbsEl = modal.body.querySelector('[data-drive-crumbs]');
  const listEl = modal.body.querySelector('[data-drive-list]');

  const foot = _footerButtons([
    { key: 'count', spacer: true },
    { key: 'folder', label: 'Ajouter ce dossier entier', cls: 'fr-btn--secondary' },
    { key: 'files', label: 'Ajouter la sélection', cls: '' },
  ]);
  modal.footer.style.display = '';
  const counter = document.createElement('span');
  counter.style.cssText = 'flex:1 1 auto;font-size:0.8rem;color:#64748b;';
  foot.insertBefore(counter, foot.firstChild);
  modal.footer.appendChild(foot);

  function _refreshCounter() {
    const n = state.selection.size;
    counter.textContent = n
      ? `${n} fichier(s) sélectionné(s) — maximum ${CAPS.driveFiles}`
      : 'Cochez des fichiers, ou ajoutez le dossier entier.';
    const btn = foot.querySelector('[data-picker-btn="files"]');
    if (btn) btn.disabled = n === 0;
  }

  function _currentDriveMeta() {
    return state.instances.find((i) => i.key === state.drive) || null;
  }

  /**
   * Portée à inscrire sur les entrées produites — `{drive, host}`.
   *
   * Quand le serveur n'expose qu'une seule instance (le cas courant), on
   * n'inscrit RIEN : l'entrée devient alors indiscernable d'un dossier collé,
   * ce qui lui permet d'alimenter le champ historique `drive_folder` et
   * d'être dédupliquée par le serveur. Dès qu'il y a un choix d'instance, la
   * portée devient une information qu'on ne peut plus taire.
   */
  function _scope() {
    if (state.instances.length < 2) return { drive: null, host: null };
    const meta = _currentDriveMeta();
    return { drive: state.drive || null, host: (meta && meta.host) || null };
  }

  function _scopeLabel(what) {
    const meta = _currentDriveMeta();
    return (state.instances.length > 1 && meta)
      ? `${what} — ${meta.label || meta.key}` : `${what} Drive`;
  }

  function _renderCrumbs() {
    const parts = [{ id: '', name: 'Racine' }].concat(state.breadcrumb || []);
    crumbsEl.innerHTML = parts.map((p, i) => {
      const last = i === parts.length - 1;
      const label = _esc(p.name || '…');
      return last
        ? `<span style="font-weight:600;color:#0f172a;">${label}</span>`
        : `<a href="#" data-drive-crumb="${_esc(p.id || '')}" class="fr-link">${label}</a>
           <span aria-hidden="true" style="color:#94a3b8;">›</span>`;
    }).join('');
    crumbsEl.querySelectorAll('[data-drive-crumb]').forEach((a) => {
      a.addEventListener('click', (ev) => {
        ev.preventDefault();
        _load(a.getAttribute('data-drive-crumb') || '');
      });
    });
  }

  function _renderInstances() {
    if (state.instances.length < 2) { instancesEl.innerHTML = ''; return; }
    instancesEl.innerHTML = `
      <label style="font-size:0.82rem;color:#475569;display:flex;gap:0.4rem;align-items:center;">
        Instance :
        <select class="fr-select fr-select--sm" data-drive-instance style="width:auto;">
          ${state.instances.map((i) => (
            `<option value="${_esc(i.key)}" ${i.key === state.drive ? 'selected' : ''}>${_esc(i.label || i.key)}</option>`
          )).join('')}
        </select>
      </label>`;
    const sel = instancesEl.querySelector('[data-drive-instance]');
    if (sel) {
      sel.addEventListener('change', () => {
        state.drive = sel.value;
        state.breadcrumb = [];
        _load('');
      });
    }
  }

  function _renderItems(data) {
    const folders = (data.folders || []).filter(Boolean);
    const files = (data.files || []).filter(Boolean);
    if (!folders.length && !files.length) {
      _renderEmpty(listEl, 'Ce dossier est vide.');
      return;
    }
    const rows = [];
    folders.forEach((f) => {
      rows.push(`
        <li style="display:flex;align-items:center;gap:0.5rem;padding:0.35rem 0.2rem;
                   border-bottom:1px solid #f1f5f9;">
          <span style="width:1.2rem;" aria-hidden="true">📁</span>
          <a href="#" class="fr-link" data-drive-open="${_esc(f.id)}"
             data-drive-name="${_esc(f.name || '')}"
             style="flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;">${_esc(f.name || f.id)}</a>
          <button type="button" class="fr-btn fr-btn--sm fr-btn--tertiary-no-outline"
                  data-drive-add-folder="${_esc(f.id)}" data-drive-name="${_esc(f.name || '')}">Ajouter</button>
        </li>`);
    });
    files.forEach((f) => {
      // `mime_type` est la clé normalisée par le serveur (`normalize_drive_item`) ;
      // `mime` reste accepté au cas où une instance la renverrait telle quelle.
      const mime = f.mime_type || f.mime || '';
      const readable = isReadableFile({ name: f.name, mime });
      const scope = _scope();
      const key = `drive_file|${scope.drive || ''}|${scope.host || ''}|${f.id}`;
      const checked = state.selection.has(key) ? 'checked' : '';
      const size = _bytes(f.size);
      rows.push(`
        <li style="display:flex;align-items:center;gap:0.5rem;padding:0.35rem 0.2rem;
                   border-bottom:1px solid #f1f5f9;${readable ? '' : 'opacity:0.5;'}">
          <input type="checkbox" data-drive-file="${_esc(f.id)}"
                 data-drive-name="${_esc(f.name || '')}"
                 data-drive-mime="${_esc(mime)}"
                 data-drive-size="${_esc(f.size == null ? '' : f.size)}"
                 ${readable ? '' : 'disabled'} ${checked}
                 aria-label="Sélectionner ${_esc(f.name || f.id)}">
          <span style="width:1.2rem;" aria-hidden="true">📄</span>
          <span style="flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
                title="${_esc(f.name || '')}">${_esc(f.name || f.id)}</span>
          <span style="color:#94a3b8;font-size:0.75rem;white-space:nowrap;">${_esc(size)}</span>
          ${readable ? '' : `<span style="color:#b45309;font-size:0.72rem;white-space:nowrap;"
                title="Formats lus : PDF, DOCX, ODT, PPTX, ODP, TXT, MD">format non lu</span>`}
        </li>`);
    });
    listEl.innerHTML = `<ul style="list-style:none;margin:0;padding:0;">${rows.join('')}</ul>`;

    listEl.querySelectorAll('[data-drive-open]').forEach((a) => {
      a.addEventListener('click', (ev) => {
        ev.preventDefault();
        state.breadcrumb = (state.breadcrumb || []).concat([{
          id: a.getAttribute('data-drive-open'),
          name: a.getAttribute('data-drive-name') || '',
        }]);
        _load(a.getAttribute('data-drive-open'), { keepCrumbs: true });
      });
    });
    listEl.querySelectorAll('[data-drive-add-folder]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const sc = _scope();
        _commit(ctx, [Object.assign({
          type: 'drive_folder',
          id: btn.getAttribute('data-drive-add-folder'),
          label: btn.getAttribute('data-drive-name') || '',
          origin: 'drive',
          meta: _scopeLabel('Dossier'),
        }, sc)], 'Dossier ajouté.');
      });
    });
    listEl.querySelectorAll('[data-drive-file]').forEach((cb) => {
      cb.addEventListener('change', () => {
        const sc = _scope();
        const id = cb.getAttribute('data-drive-file');
        const fileKey = `drive_file|${sc.drive || ''}|${sc.host || ''}|${id}`;
        if (cb.checked) {
          state.selection.set(fileKey, Object.assign({
            type: 'drive_file',
            id,
            label: cb.getAttribute('data-drive-name') || id,
            mime: cb.getAttribute('data-drive-mime') || '',
            bytes: parseInt(cb.getAttribute('data-drive-size') || '0', 10) || 0,
            origin: 'drive',
            meta: _scopeLabel('Fichier'),
          }, sc));
        } else {
          state.selection.delete(fileKey);
        }
        _refreshCounter();
      });
    });
  }

  async function _load(folderId, opts) {
    state.folderId = folderId || '';
    if (!opts || !opts.keepCrumbs) {
      // Navigation par le fil d'Ariane : on tronque à l'ancêtre cliqué.
      const idx = (state.breadcrumb || []).findIndex((c) => c.id === state.folderId);
      state.breadcrumb = state.folderId
        ? (idx === -1 ? state.breadcrumb : state.breadcrumb.slice(0, idx + 1))
        : [];
    }
    _renderCrumbs();
    _renderLoading(listEl, 5);
    try {
      const params = new URLSearchParams();
      if (state.folderId) params.set('folder_id', state.folderId);
      if (state.drive) params.set('drive', state.drive);
      const r = await fetch(`${BROWSE_URL}?${params.toString()}`);
      if (!r.ok) {
        const err = await r.json().catch(() => null);
        // Un 404 SANS corps JSON, c'est la route qui n'existe pas (serveur
        // antérieur au lot) ; avec un `code`, c'est le dossier qui n'existe
        // pas. Confondre les deux enverrait l'utilisateur chercher un dossier
        // parfaitement valide.
        if (r.status === 404 && !(err && err.code)) {
          _renderError(listEl, BROWSE_UNAVAILABLE, null);
          return;
        }
        const retry = () => _load(state.folderId, { keepCrumbs: true });
        _renderError(listEl, (err && err.error) || `Erreur ${r.status} du Drive.`, retry);
        return;
      }
      const d = await r.json().catch(() => ({}));
      // Le serveur fait autorité sur le fil d'Ariane quand il en renvoie un
      // (`breadcrumb` est natif côté Drive) — notre pile locale n'est qu'un
      // repli pour les instances qui ne l'exposeraient pas.
      if (Array.isArray(d.breadcrumb) && d.breadcrumb.length) state.breadcrumb = d.breadcrumb;
      _renderCrumbs();
      _renderItems(d);
    } catch (err) {
      _renderError(listEl, `Erreur réseau : ${(err && err.message) || err}`,
        () => _load(state.folderId, { keepCrumbs: true }));
    }
  }

  async function _loadInstances() {
    try {
      const r = await fetch(INSTANCES_URL);
      if (!r.ok) return; // 404 = registre pas encore livré : instance unique
      const d = await r.json().catch(() => ({}));
      const items = Array.isArray(d.drives) ? d.drives
        : (Array.isArray(d.instances) ? d.instances : []);
      state.instances = items;
      const def = items.find((i) => i.is_default || i.default) || items[0];
      if (def) state.drive = def.key;
      _renderInstances();
    } catch (e) { /* repli silencieux : une seule instance */ }
  }

  foot.querySelector('[data-picker-btn="folder"]').addEventListener('click', () => {
    if (!state.folderId) {
      showToast('Ouvrez d\'abord un dossier pour l\'ajouter entier.', 'error');
      return;
    }
    const crumb = (state.breadcrumb || [])[state.breadcrumb.length - 1] || {};
    _commit(ctx, [Object.assign({
      type: 'drive_folder',
      id: state.folderId,
      label: crumb.name || state.folderId,
      origin: 'drive',
      meta: _scopeLabel('Dossier'),
    }, _scope())], 'Dossier ajouté.');
  });
  foot.querySelector('[data-picker-btn="files"]').addEventListener('click', () => {
    _commit(ctx, Array.from(state.selection.values()), 'Fichiers ajoutés.');
    state.selection.clear();
    _refreshCounter();
    modal.close();
  });

  _refreshCounter();
  _loadInstances().then(() => _load(''));
  return modal;
}

// ─── 2. Réunions précédentes ───────────────────────────────────────

function openPreparationsPicker(ctx) {
  const modal = openStackedModal({
    title: '🗓️ Réunions précédentes',
    trigger: ctx.trigger,
    width: '760px',
    bodyHtml: `
      <p style="font-size:0.85rem;color:#475569;margin:0 0 0.6rem;">
        Le brief et le compte-rendu des réunions cochées seront versés au contexte
        (maximum ${CAPS.preparations} réunions).
      </p>
      <div data-preps-list></div>`,
  });
  const listEl = modal.body.querySelector('[data-preps-list]');

  const foot = _footerButtons([
    { key: 'spacer', spacer: true },
    { key: 'add', label: 'Ajouter au panier', cls: '' },
  ]);
  modal.footer.style.display = '';
  modal.footer.appendChild(foot);
  const addBtn = foot.querySelector('[data-picker-btn="add"]');
  addBtn.disabled = true;

  function _refreshAddState() {
    addBtn.disabled = listEl.querySelectorAll('input[data-prep-part]:checked').length === 0;
  }

  function _render(preps) {
    if (!preps.length) {
      _renderEmpty(listEl, 'Aucune réunion préparée pour l\'instant.',
        'Vos prochains briefs apparaîtront ici.');
      addBtn.disabled = true;
      return;
    }
    // Pré-cochage : si le wizard prépare la suite d'une série, le brief parent
    // est presque toujours la source voulue — autant l'offrir coché.
    const parentEl = document.getElementById('wizard-series-parent');
    const parentId = (parentEl && parentEl.value) || '';
    listEl.innerHTML = `
      <ul style="list-style:none;margin:0;padding:0;">
        ${preps.map((p) => {
          const title = p.title || p.subject || '(sans titre)';
          const date = (p.target_meeting_date || p.created_at || '').slice(0, 10);
          const isParent = parentId && p.id === parentId;
          return `
            <li style="display:flex;align-items:center;gap:0.6rem;padding:0.4rem 0.2rem;
                       border-bottom:1px solid #f1f5f9;">
              <span style="flex:1 1 auto;overflow:hidden;">
                <span style="display:block;overflow:hidden;text-overflow:ellipsis;
                             white-space:nowrap;">${_esc(title)}</span>
                <span style="color:#94a3b8;font-size:0.75rem;">${_esc(date)}</span>
              </span>
              <label style="font-size:0.8rem;display:flex;gap:0.25rem;align-items:center;white-space:nowrap;">
                <input type="checkbox" data-prep-part="brief" data-prep-id="${_esc(p.id)}"
                       data-prep-title="${_esc(title)}" ${isParent ? 'checked' : ''}> Brief
              </label>
              <label style="font-size:0.8rem;display:flex;gap:0.25rem;align-items:center;white-space:nowrap;">
                <input type="checkbox" data-prep-part="cr" data-prep-id="${_esc(p.id)}"
                       data-prep-title="${_esc(title)}"> Compte-rendu
              </label>
            </li>`;
        }).join('')}
      </ul>`;
    listEl.querySelectorAll('input[data-prep-part]').forEach((cb) => {
      cb.addEventListener('change', _refreshAddState);
    });
    _refreshAddState();
  }

  async function _load() {
    _renderLoading(listEl, 4);
    try {
      const r = await fetch('/api/preparations');
      if (!r.ok) {
        _renderError(listEl, `Impossible de charger vos réunions (erreur ${r.status}).`, _load);
        return;
      }
      const d = await r.json().catch(() => ({}));
      _render((d && (d.preparations || d.briefs)) || []);
    } catch (err) {
      _renderError(listEl, `Erreur réseau : ${(err && err.message) || err}`, _load);
    }
  }

  addBtn.addEventListener('click', () => {
    // Une réunion = une entrée, ses sections cumulées : « Brief » coche
    // brief + points clés (ils ne se lisent pas l'un sans l'autre).
    const byId = new Map();
    listEl.querySelectorAll('input[data-prep-part]:checked').forEach((cb) => {
      const id = cb.getAttribute('data-prep-id');
      const parts = cb.getAttribute('data-prep-part') === 'brief'
        ? ['brief', 'key_points'] : ['cr'];
      const cur = byId.get(id) || {
        type: 'preparation',
        id,
        include: [],
        label: cb.getAttribute('data-prep-title') || id,
        origin: 'meetings',
        meta: 'Réunion précédente',
      };
      parts.forEach((p) => { if (cur.include.indexOf(p) === -1) cur.include.push(p); });
      byId.set(id, cur);
    });
    _commit(ctx, Array.from(byId.values()), 'Réunions ajoutées.');
    modal.close();
  });

  _load();
  return modal;
}

// ─── 3. Message collé (repli du lot C) ─────────────────────────────

// Le mode « connecté » (extension Thunderbird locale sur 127.0.0.1) n'est pas
// livré : son protocole n'est documenté nulle part et le code vit sur le poste
// de son propriétaire. Le repli, lui, s'appuie sur un contrat serveur qui
// existe déjà (`sources[].inline`) et est utilisable dès le premier jour —
// c'est pourquoi il passe AVANT toute détection d'extension.
function openMailPicker(ctx) {
  const modal = openStackedModal({
    title: '✉️ Coller le contenu d\'un message',
    trigger: ctx.trigger,
    width: '720px',
    bodyHtml: `
      <div class="fr-alert fr-alert--info fr-alert--sm" style="margin-bottom:0.7rem;">
        <p>Le texte collé est envoyé au serveur puis au modèle qui rédige le brief.
        Il n'est ni journalisé, ni conservé dans la fiche de la préparation.</p>
      </div>
      <div class="fr-input-group" style="margin-bottom:0.6rem;">
        <label class="fr-label" for="picker-mail-title">Intitulé (facultatif)</label>
        <input class="fr-input fr-input--sm" id="picker-mail-title" type="text"
               maxlength="200" placeholder="Ex. : échange avec la DAF sur le budget">
      </div>
      <div class="fr-input-group">
        <label class="fr-label" for="picker-mail-text">Contenu du message</label>
        <textarea class="fr-input" id="picker-mail-text" rows="12"
                  maxlength="${CAPS.inlineChars}"
                  placeholder="Collez ici le corps du message…"></textarea>
      </div>
      <div data-mail-counter style="font-size:0.78rem;color:#64748b;margin-top:0.25rem;"></div>
      <p style="font-size:0.78rem;color:#94a3b8;margin-top:0.8rem;">
        La recherche directe dans votre messagerie n'est pas encore disponible :
        le collage reste le moyen d'apporter un échange au brief.
      </p>`,
  });

  const titleEl = modal.body.querySelector('#picker-mail-title');
  const textEl = modal.body.querySelector('#picker-mail-text');
  const counterEl = modal.body.querySelector('[data-mail-counter]');

  const foot = _footerButtons([
    { key: 'spacer', spacer: true },
    { key: 'add', label: 'Ajouter au panier', cls: '' },
  ]);
  modal.footer.style.display = '';
  modal.footer.appendChild(foot);
  const addBtn = foot.querySelector('[data-picker-btn="add"]');
  addBtn.disabled = true;

  function _refresh() {
    const n = textEl.value.length;
    counterEl.textContent = `${n} / ${CAPS.inlineChars} caractères`;
    counterEl.style.color = n > CAPS.inlineChars * 0.9 ? '#b45309' : '#64748b';
    addBtn.disabled = !textEl.value.trim();
  }
  textEl.addEventListener('input', _refresh);
  _refresh();

  addBtn.addEventListener('click', () => {
    const text = textEl.value;
    if (!text.trim()) return;
    _commit(ctx, [{
      type: 'inline',
      kind: 'mail',
      title: (titleEl.value || '').trim() || 'Message collé',
      text,
      origin: 'mail',
      // Compte exact : c'est du texte, il n'y a rien à estimer.
      meta: `${text.length} caractères`,
    }], 'Message ajouté.');
    modal.close();
  });

  return modal;
}

// ─── 4. Coller un lien Drive ───────────────────────────────────────

/**
 * Test d'accès à un dossier Drive — anciennement `_testDriveAccess` dans
 * `tabs/wizard.js`, paramétré par ses éléments puisque le markup a déménagé
 * dans cette modale (et que le wizard ne peut pas importer les pickers ET
 * l'inverse sans cycle d'import).
 */
export async function testDriveAccess({ input, result, spinner, btn }) {
  if (!input || !result) return;
  const raw = (input.value || '').trim();
  if (!raw) {
    result.innerHTML = '<div class="fr-alert fr-alert--info fr-alert--sm">'
      + '<p>Aucun dossier renseigné — le test est inutile.</p></div>';
    return;
  }
  result.innerHTML = '';
  if (spinner) spinner.style.display = '';
  if (btn) btn.disabled = true;
  try {
    const url = '/api/preparations/test-drive?folder_id=' + encodeURIComponent(raw);
    const r = await fetch(url);
    const d = await r.json().catch(() => ({}));
    if (d && d.ok) {
      const n = d.docs_count || 0;
      const docs = (d.docs || []).filter((x) => !x.is_folder).slice(0, 10);
      let list = '';
      if (docs.length) {
        list = '<ul style="margin:0.3rem 0 0 1.2rem;font-size:0.85rem;">'
          + docs.map((x) => `<li>${_esc(x.name)}</li>`).join('')
          + (n > docs.length ? `<li><em>…et ${n - docs.length} autre(s)</em></li>` : '')
          + '</ul>';
      }
      result.innerHTML = `<div class="fr-alert fr-alert--success fr-alert--sm">
        <p><strong>${n} document(s) trouvé(s)</strong> dans ce dossier.</p>${list}</div>`;
    } else {
      const msg = (d && d.error) ? d.error : 'Accès au Drive impossible.';
      result.innerHTML = `<div class="fr-alert fr-alert--error fr-alert--sm"><p>${_esc(msg)}</p></div>`;
    }
  } catch (err) {
    result.innerHTML = '<div class="fr-alert fr-alert--error fr-alert--sm"><p>Erreur réseau : '
      + _esc((err && err.message) || err) + '</p></div>';
  } finally {
    if (spinner) spinner.style.display = 'none';
    if (btn) btn.disabled = false;
  }
}

function openLinkPicker(ctx) {
  const modal = openStackedModal({
    title: '🔗 Coller un lien de dossier',
    trigger: ctx.trigger,
    width: '680px',
    bodyHtml: `
      <div class="fr-input-group">
        <label class="fr-label" for="picker-link-input">Dossier Drive</label>
        <span class="fr-hint-text">
          Collez l'URL du dossier <em>mesfichiers</em> (ou son identifiant brut).
          Les documents qu'il contient seront lus pour enrichir le brief.
        </span>
        <input class="fr-input" type="text" id="picker-link-input"
               placeholder="https://mesfichiers.…/explorer/items/xxxxxxxx">
      </div>
      <div style="margin-top:0.5rem;display:flex;gap:0.5rem;align-items:center;">
        <button type="button" class="fr-btn fr-btn--sm fr-btn--secondary"
                data-link-test>Vérifier l'accès</button>
        <span data-link-spinner style="display:none;color:#64748b;font-size:0.85rem;">
          Vérification en cours…
        </span>
      </div>
      <div data-link-result style="margin-top:0.6rem;"></div>`,
  });

  const input = modal.body.querySelector('#picker-link-input');
  const testBtn = modal.body.querySelector('[data-link-test]');
  const spinner = modal.body.querySelector('[data-link-spinner]');
  const result = modal.body.querySelector('[data-link-result]');

  const foot = _footerButtons([
    { key: 'spacer', spacer: true },
    { key: 'add', label: 'Ajouter au panier', cls: '' },
  ]);
  modal.footer.style.display = '';
  modal.footer.appendChild(foot);
  const addBtn = foot.querySelector('[data-picker-btn="add"]');

  testBtn.addEventListener('click', () => testDriveAccess({
    input, result, spinner, btn: testBtn,
  }));

  addBtn.addEventListener('click', () => {
    const raw = (input.value || '').trim();
    if (!raw) {
      result.innerHTML = '<div class="fr-alert fr-alert--error fr-alert--sm">'
        + '<p>Renseignez un dossier avant d\'ajouter.</p></div>';
      return;
    }
    // Ni `drive` ni `host` : c'est ce qui permet au wizard de recopier cette
    // entrée dans le champ historique `drive_folder` sans que le serveur ne
    // l'ingère deux fois (la déduplication de sources.py compare le triplet
    // drive/host/id).
    _commit(ctx, [{
      type: 'drive_folder',
      id: raw,
      drive: null,
      host: null,
      label: raw,
      origin: 'link',
      meta: 'Dossier Drive collé',
    }], 'Dossier ajouté.');
    modal.close();
  });

  return modal;
}

// ─── Aiguillage ────────────────────────────────────────────────────

/**
 * Ouvre le picker demandé.
 *
 * `ctx` : `{ trigger, addEntries(entries) → {added, rejected} }`.
 * `addEntries` est fourni par le wizard et branché sur le panier — c'est lui
 * qui applique les plafonds et déclenche la sauvegarde du brouillon.
 */
export function openSourcePicker(kind, ctx) {
  ctx = ctx || {};
  switch (kind) {
    case 'drive': return openDrivePicker(ctx);
    case 'preparations': return openPreparationsPicker(ctx);
    case 'mail': return openMailPicker(ctx);
    case 'link': return openLinkPicker(ctx);
    default: return null;
  }
}
