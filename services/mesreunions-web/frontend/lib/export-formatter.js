// Lot 4 — Sérialisation TXT + MD côté front d'une préparation.
//
// Symétrique de `services/mesreunions-web/app/modules/preparations/exporters.py`
// (qui couvre DOCX + ODT). On reproduit la même séquence de sections que
// `renderBriefBody` dans `tabs/preparations.js` pour que tous les formats
// d'export restent visuellement cohérents.
//
// Pourquoi pas du backend pour TXT/MD ? Aucun avantage : pas de mise en
// page riche, pas de dépendance lourde, et un export instantané sans
// round-trip réseau améliore l'UX.

function _slugify(s, maxLen = 60) {
  const str = (s || '')
    .normalize('NFKD').replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return (str || 'preparation').slice(0, maxLen).replace(/-+$/, '');
}

function _sections(prep) {
  const p = prep || {};
  const bj = p.content || {};
  const out = [];
  const objective = (bj.objective_reformulated || '').trim();
  const context = (bj.context_recap || '').trim();
  if (objective || context) {
    out.push({ emoji: '🎯', title: 'Objectif & contexte', kind: 'objective',
               payload: { objective, context } });
  }
  const agenda = Array.isArray(bj.agenda) ? bj.agenda.filter(x => x && typeof x === 'object') : [];
  if (agenda.length) out.push({ emoji: '📋', title: 'Ordre du jour', kind: 'agenda', payload: agenda });
  // Participants sourcés depuis prep.participants (liste éditable), pas
  // depuis brief_json.participants_notes (champ LLM ignoré).
  const pn = Array.isArray(p.participants) ? p.participants.filter(x => x && typeof x === 'object') : [];
  if (pn.length) out.push({ emoji: '👥', title: 'Participants', kind: 'participants', payload: pn });
  const threads = Array.isArray(bj.open_threads) ? bj.open_threads.filter(x => x && typeof x === 'object') : [];
  if (threads.length) out.push({ emoji: '🧵', title: 'Points en suspens', kind: 'threads', payload: threads });
  const opening = Array.isArray(bj.opening_questions) ? bj.opening_questions.filter(q => (q || '').trim()) : [];
  if (opening.length) out.push({ emoji: '💬', title: 'Questions d\'ouverture', kind: 'list', payload: opening });
  const risks = Array.isArray(bj.risk_points) ? bj.risk_points.filter(q => (q || '').trim()) : [];
  if (risks.length) out.push({ emoji: '⚠️', title: 'Points de vigilance', kind: 'list', payload: risks });
  const check = Array.isArray(bj.preparation_checklist) ? bj.preparation_checklist.filter(q => (q || '').trim()) : [];
  if (check.length) out.push({ emoji: '✅', title: 'À faire avant la réunion', kind: 'list', payload: check });
  return out;
}

function _header(prep) {
  const title = (prep.title || prep.subject || 'Préparation de réunion').trim();
  const meta = [];
  const created = (prep.created_at || '').slice(0, 16).replace('T', ' ');
  if (created) meta.push(`Créé le ${created}`);
  if (prep.role) meta.push(`Rôle : ${prep.role}`);
  if (prep.duration_minutes) meta.push(`Durée prévue : ${prep.duration_minutes} min`);
  return { title, meta };
}

/** Render text plain (.txt) — sections séparées par ligne vide. */
export function renderTxt(prep) {
  const { title, meta } = _header(prep);
  const out = [];
  out.push(title);
  out.push('='.repeat(Math.min(title.length, 80)));
  if (meta.length) out.push(meta.join(' · '));
  out.push('');
  for (const sec of _sections(prep)) {
    out.push(`${sec.emoji}  ${sec.title}`);
    out.push('-'.repeat(Math.min(sec.title.length + 4, 80)));
    if (sec.kind === 'objective') {
      if (sec.payload.objective) out.push(sec.payload.objective);
      if (sec.payload.context) out.push(sec.payload.context);
    } else if (sec.kind === 'agenda') {
      sec.payload.forEach((it, i) => {
        const dur = it.duration_minutes ? ` (${parseInt(it.duration_minutes, 10) || 0} min)` : '';
        out.push(`${i + 1}. ${(it.title || '(sans titre)').trim()}${dur}`);
        const obj = (it.objective || '').trim();
        if (obj) out.push(`   ${obj}`);
        const kqs = Array.isArray(it.key_questions) ? it.key_questions.filter(q => (q || '').trim()) : [];
        kqs.forEach(q => out.push(`   - ${q}`));
      });
    } else if (sec.kind === 'participants') {
      sec.payload.forEach(p => {
        const name = (p.name || '—').trim();
        const email = (p.email || '').trim();
        const role = (p.role || '').trim();
        const note = (p.note || '').trim();
        const meta = [role, email].filter(Boolean).join(' · ');
        let line = `- ${name}`;
        if (meta) line += ` (${meta})`;
        if (note) line += ` — ${note}`;
        out.push(line);
      });
    } else if (sec.kind === 'threads') {
      sec.payload.forEach(t => {
        const item = (t.item || '').trim();
        if (!item) return;
        const src = (t.source || '').trim();
        out.push(src ? `- ${item} (source : ${src})` : `- ${item}`);
      });
    } else {
      sec.payload.forEach(s => out.push(`- ${s}`));
    }
    out.push('');
  }
  return out.join('\n');
}

/** Render Markdown — h1 titre, h2 sections, listes structurées. */
export function renderMd(prep) {
  const { title, meta } = _header(prep);
  const out = [];
  out.push(`# ${title}`);
  if (meta.length) out.push(`\n_${meta.join(' · ')}_`);
  out.push('');
  for (const sec of _sections(prep)) {
    out.push(`## ${sec.emoji} ${sec.title}`);
    out.push('');
    if (sec.kind === 'objective') {
      if (sec.payload.objective) out.push(sec.payload.objective);
      if (sec.payload.context) out.push(`\n_${sec.payload.context}_`);
    } else if (sec.kind === 'agenda') {
      sec.payload.forEach((it, i) => {
        const dur = it.duration_minutes ? ` _(${parseInt(it.duration_minutes, 10) || 0} min)_` : '';
        out.push(`${i + 1}. **${(it.title || '(sans titre)').trim()}**${dur}`);
        const obj = (it.objective || '').trim();
        if (obj) out.push(`   - ${obj}`);
        const kqs = Array.isArray(it.key_questions) ? it.key_questions.filter(q => (q || '').trim()) : [];
        kqs.forEach(q => out.push(`   - ${q}`));
      });
    } else if (sec.kind === 'participants') {
      sec.payload.forEach(p => {
        const name = (p.name || '—').trim();
        const email = (p.email || '').trim();
        const role = (p.role || '').trim();
        const note = (p.note || '').trim();
        const meta = [role, email].filter(Boolean).join(' · ');
        let line = `- **${name}**`;
        if (meta) line += ` _(${meta})_`;
        if (note) line += ` — ${note}`;
        out.push(line);
      });
    } else if (sec.kind === 'threads') {
      sec.payload.forEach(t => {
        const item = (t.item || '').trim();
        if (!item) return;
        const src = (t.source || '').trim();
        out.push(src ? `- ${item} _(source : ${src})_` : `- ${item}`);
      });
    } else {
      sec.payload.forEach(s => out.push(`- ${s}`));
    }
    out.push('');
  }
  return out.join('\n');
}

/** Déclenche le téléchargement d'un Blob en mémoire. */
export function downloadBlob(content, mimeType, filename) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, 100);
}

/** Filename slugifié symétrique au backend. */
export function buildFilename(prep, ext) {
  const title = prep.title || prep.subject || 'preparation';
  return `prep-${_slugify(title)}.${ext}`;
}

/** Point d'entrée unique : exporte un brief dans le format demandé. */
export async function exportPreparation(prep, fmt, opts = {}) {
  fmt = (fmt || '').toLowerCase();
  if (fmt === 'txt') {
    downloadBlob(renderTxt(prep), 'text/plain;charset=utf-8', buildFilename(prep, 'txt'));
    return;
  }
  if (fmt === 'md') {
    downloadBlob(renderMd(prep), 'text/markdown;charset=utf-8', buildFilename(prep, 'md'));
    return;
  }
  if (fmt === 'docx' || fmt === 'odt') {
    const prepId = opts.prepId || prep.id;
    if (!prepId) throw new Error('prepId required for backend export');
    const url = `/api/preparations/${encodeURIComponent(prepId)}/export?format=${fmt}`;
    const r = await fetch(url, { credentials: 'include' });
    if (!r.ok) {
      let msg = `export_failed (HTTP ${r.status})`;
      try { const j = await r.json(); if (j && j.error) msg = j.error; } catch (e) {}
      throw new Error(msg);
    }
    const blob = await r.blob();
    // Extract filename from Content-Disposition if présent (sinon fallback slug)
    let filename = buildFilename(prep, fmt);
    const cd = r.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="([^"]+)"/);
    if (m) filename = m[1];
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = objUrl; a.download = filename;
    document.body.appendChild(a); a.click();
    setTimeout(() => { document.body.removeChild(a); URL.revokeObjectURL(objUrl); }, 100);
    return;
  }
  throw new Error(`unsupported format: ${fmt}`);
}
