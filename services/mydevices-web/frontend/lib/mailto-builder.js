// Helper "mailto:" pour l'invitation des participants (Lot 8 PR-6).
//
// API : buildMailtoForPreparation(prep) → string (URI mailto:…)
//
// Convention : le destinataire principal est la 1re adresse, les autres
// passent en CC (les clients mail séparent normalement to/cc proprement).
// Si une seule adresse → uniquement "to". Si aucune → pas de "to" du tout
// (l'utilisateur les remplira manuellement dans son client mail).

function _formatDateFR(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString('fr-FR', {
      weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  } catch (e) {
    return iso;
  }
}

function _validEmails(participants) {
  const out = [];
  const seen = new Set();
  (participants || []).forEach((p) => {
    if (!p) return;
    const e = String(p.email || '').trim();
    if (!e || e.indexOf('@') < 0) return;
    const lc = e.toLowerCase();
    if (seen.has(lc)) return;
    seen.add(lc);
    out.push(e);
  });
  return out;
}

function _buildBody(prep) {
  const lines = [];
  const title = (prep.title || prep.subject || 'Réunion').toString();
  const when = _formatDateFR(prep.target_meeting_date);
  lines.push('Bonjour,');
  lines.push('');
  lines.push(`Je vous invite à participer à la réunion « ${title} ».`);
  if (when) lines.push(`Date : ${when}`);
  if (prep.duration_minutes) lines.push(`Durée prévue : ${prep.duration_minutes} min`);
  lines.push('');
  // Sujet / contexte court (1 paragraphe).
  const subject = (prep.subject || '').trim();
  if (subject && subject !== title) {
    lines.push('Objet :');
    lines.push(subject);
    lines.push('');
  }
  const ctx = (prep.context || '').trim();
  if (ctx) {
    lines.push('Contexte :');
    lines.push(ctx);
    lines.push('');
  }
  // Focus (axes pré-définis) + thématiques additionnelles.
  const focus = Array.isArray(prep.focus) ? prep.focus.filter(Boolean) : [];
  const themes = Array.isArray(prep.themes) ? prep.themes.filter(Boolean) : [];
  const all = focus.concat(themes);
  if (all.length) {
    lines.push('Points à aborder :');
    all.forEach((x) => lines.push('- ' + x));
    lines.push('');
  }
  // Lien Drive (si configuré).
  const driveId = prep.drive_folder_id || prep.drive_prep_folder_id || '';
  if (driveId) {
    lines.push(`Dossier de travail : https://drive.google.com/drive/folders/${driveId}`);
    lines.push('');
  }
  lines.push('Cordialement,');
  return lines.join('\r\n');
}

export function buildMailtoForPreparation(prep) {
  if (!prep) return 'mailto:';
  const emails = _validEmails(prep.participants);
  const subjectLine = (() => {
    const title = (prep.title || prep.subject || 'Réunion').toString();
    const when = _formatDateFR(prep.target_meeting_date);
    return when ? `Réunion : ${title} le ${when}` : `Réunion : ${title}`;
  })();
  const body = _buildBody(prep);
  let to = '';
  const params = [];
  if (emails.length >= 1) to = encodeURIComponent(emails[0]);
  if (emails.length > 1) {
    params.push('cc=' + encodeURIComponent(emails.slice(1).join(',')));
  }
  params.push('subject=' + encodeURIComponent(subjectLine));
  params.push('body=' + encodeURIComponent(body));
  return 'mailto:' + to + '?' + params.join('&');
}

export default { buildMailtoForPreparation };
