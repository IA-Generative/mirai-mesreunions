// Composant "Participants" — utilisé dans le wizard step 2 ET la fiche
// brief (Lot 5). Une seule source de vérité pour le rendu + validation
// email + serialize/deserialize.
//
// Structure participant : {name: string, email: string|null, role: string|null}
// L'UI affiche 1 ligne par participant avec 3 inputs + bouton suppr.

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function _esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export function isValidEmail(s) {
  if (!s) return true; // email optionnel — vide = OK
  return EMAIL_RE.test(String(s).trim());
}

/**
 * Crée une ligne participant DSFR-styled.
 * @param {object} data {name, email, role}
 * @param {function} onChange callback fired on any input change
 * @returns {HTMLElement} row element with .pa-name/.pa-email/.pa-role/.pa-remove children
 */
export function createParticipantRow(data, onChange) {
  data = data || {};
  const row = document.createElement('div');
  row.className = 'participant-row';
  row.style.cssText = 'display:grid;grid-template-columns:1.2fr 1.6fr 1fr auto;'
    + 'gap:0.35rem;align-items:center;';

  const mk = (type, cls, value, placeholder) => {
    const i = document.createElement('input');
    i.type = type;
    i.className = cls;
    i.value = value || '';
    i.placeholder = placeholder || '';
    i.style.cssText = 'border:1px solid #cbd5e1;border-radius:0.3rem;'
      + 'padding:0.3rem 0.4rem;font-size:0.85rem;width:100%;';
    if (onChange) i.addEventListener('input', onChange);
    return i;
  };

  const nameI = mk('text', 'pa-name', data.name, 'Nom');
  nameI.setAttribute('aria-label', 'Nom');
  const emailI = mk('email', 'pa-email', data.email, 'email@exemple.fr');
  emailI.setAttribute('aria-label', 'Email');
  const roleI = mk('text', 'pa-role', data.role, 'Rôle (optionnel)');
  roleI.setAttribute('aria-label', 'Rôle');

  const rm = document.createElement('button');
  rm.type = 'button';
  rm.className = 'fr-btn fr-btn--sm fr-btn--tertiary-no-outline';
  rm.setAttribute('aria-label', 'Supprimer ce participant');
  rm.textContent = '×';
  rm.addEventListener('click', () => {
    row.remove();
    if (onChange) onChange();
  });

  // Hint visuel pour email invalide
  emailI.addEventListener('input', () => {
    const v = emailI.value.trim();
    if (!v || isValidEmail(v)) {
      emailI.style.borderColor = '#cbd5e1';
    } else {
      emailI.style.borderColor = '#b91c1c';
    }
  });

  row.appendChild(nameI);
  row.appendChild(emailI);
  row.appendChild(roleI);
  row.appendChild(rm);
  return row;
}

/**
 * Lit le DOM d'un conteneur et retourne la liste serializée.
 * Filtre les lignes complètement vides.
 */
export function serializeParticipantsContainer(container) {
  if (!container) return [];
  const rows = Array.from(container.querySelectorAll('.participant-row'));
  const out = [];
  rows.forEach((r) => {
    const name = (r.querySelector('.pa-name')?.value || '').trim();
    const email = (r.querySelector('.pa-email')?.value || '').trim();
    const role = (r.querySelector('.pa-role')?.value || '').trim();
    if (!name && !email && !role) return;
    const entry = {};
    if (name) entry.name = name;
    if (email) entry.email = email;
    if (role) entry.role = role;
    out.push(entry);
  });
  return out;
}

/**
 * Vide le conteneur et le re-remplit depuis une liste de participants.
 */
export function populateParticipantsContainer(container, list, onChange) {
  if (!container) return;
  container.innerHTML = '';
  (list || []).forEach((p) => {
    container.appendChild(createParticipantRow(p, onChange));
  });
}

/**
 * Retourne la liste des emails invalides présents (pour affichage erreur).
 */
export function listInvalidEmails(container) {
  if (!container) return [];
  const out = [];
  container.querySelectorAll('.pa-email').forEach((i) => {
    const v = (i.value || '').trim();
    if (v && !isValidEmail(v)) out.push(v);
  });
  return out;
}

// Pour compat tests / autres modules.
export const __test__ = { EMAIL_RE };
