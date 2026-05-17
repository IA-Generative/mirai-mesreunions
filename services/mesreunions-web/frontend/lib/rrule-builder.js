// rrule-builder — helper pur pour construire et parser le dict
// `recurrence_rule` (Lot 6, format documenté dans migration 013).
//
// Structure normalisée :
//   {
//     freq: 'DAILY' | 'WEEKLY' | 'MONTHLY',
//     interval: 1..n,
//     byweekday: ['MO','TU','WE','TH','FR','SA','SU'],   // hebdo
//     byhour: 0..23,
//     byminute: 0..59,
//     until: 'YYYY-MM-DD' (optionnel)
//   }
//
// Aucun side-effect — ces helpers sont consommés par wizard.js et
// preparations.js (fiche brief) pour rendre + sérialiser le formulaire.

export const WEEKDAYS = [
  { code: 'MO', label: 'Lundi',    short: 'L'  },
  { code: 'TU', label: 'Mardi',    short: 'Ma' },
  { code: 'WE', label: 'Mercredi', short: 'Me' },
  { code: 'TH', label: 'Jeudi',    short: 'J'  },
  { code: 'FR', label: 'Vendredi', short: 'V'  },
  { code: 'SA', label: 'Samedi',   short: 'S'  },
  { code: 'SU', label: 'Dimanche', short: 'D'  },
];

export const FREQ_LABELS = {
  DAILY: 'Quotidienne',
  WEEKLY: 'Hebdomadaire',
  MONTHLY: 'Mensuelle',
};

/**
 * Sérialise les valeurs du formulaire récurrence (DOM root contenant les
 * champs `data-rrule-*`) vers le dict canonique. Retourne `null` si la
 * fréquence n'est pas reconnue.
 */
export function buildRuleFromForm(root) {
  if (!root) return null;
  const q = (sel) => root.querySelector(sel);
  const qa = (sel) => Array.from(root.querySelectorAll(sel));
  const freq = (q('[data-rrule-freq]') || {}).value || '';
  if (!FREQ_LABELS[freq]) return null;

  const intervalRaw = parseInt((q('[data-rrule-interval]') || {}).value || '1', 10);
  const interval = Number.isFinite(intervalRaw) && intervalRaw > 0 ? Math.min(intervalRaw, 365) : 1;

  const rule = { freq, interval };

  if (freq === 'WEEKLY') {
    const checked = qa('[data-rrule-weekday]:checked').map((el) => el.value);
    const cleaned = checked.filter((c) => WEEKDAYS.some((w) => w.code === c));
    if (cleaned.length) rule.byweekday = cleaned;
  }

  const timeStr = (q('[data-rrule-time]') || {}).value || '';
  if (timeStr && /^\d{1,2}:\d{2}$/.test(timeStr)) {
    const [hh, mm] = timeStr.split(':').map((n) => parseInt(n, 10));
    if (Number.isFinite(hh) && hh >= 0 && hh <= 23) rule.byhour = hh;
    if (Number.isFinite(mm) && mm >= 0 && mm <= 59) rule.byminute = mm;
  }

  const untilStr = (q('[data-rrule-until]') || {}).value || '';
  if (untilStr && /^\d{4}-\d{2}-\d{2}$/.test(untilStr)) {
    rule.until = untilStr;
  }

  return rule;
}

/**
 * Applique un dict `rule` aux champs du DOM root (réciproque de
 * `buildRuleFromForm`). Tolérant : ignore les valeurs absentes.
 */
export function fillFormFromRule(root, rule) {
  if (!root) return;
  const q = (sel) => root.querySelector(sel);
  const qa = (sel) => Array.from(root.querySelectorAll(sel));
  const r = rule || {};
  const freqEl = q('[data-rrule-freq]');
  if (freqEl) freqEl.value = FREQ_LABELS[r.freq] ? r.freq : 'WEEKLY';
  const intervalEl = q('[data-rrule-interval]');
  if (intervalEl) intervalEl.value = String(r.interval || 1);
  const wantedWeekdays = Array.isArray(r.byweekday) ? new Set(r.byweekday) : new Set();
  qa('[data-rrule-weekday]').forEach((cb) => { cb.checked = wantedWeekdays.has(cb.value); });
  const timeEl = q('[data-rrule-time]');
  if (timeEl) {
    if (r.byhour != null || r.byminute != null) {
      const hh = String(r.byhour ?? 9).padStart(2, '0');
      const mm = String(r.byminute ?? 0).padStart(2, '0');
      timeEl.value = `${hh}:${mm}`;
    } else {
      timeEl.value = '';
    }
  }
  const untilEl = q('[data-rrule-until]');
  if (untilEl) untilEl.value = r.until || '';
}

/**
 * Bascule l'affichage des sous-champs dépendant de la fréquence (jours
 * pour hebdo, etc.). À brancher sur `change` du select fréquence.
 */
export function refreshFreqVisibility(root) {
  if (!root) return;
  const freq = (root.querySelector('[data-rrule-freq]') || {}).value || '';
  const weeklyOnly = root.querySelector('[data-rrule-weekly-only]');
  if (weeklyOnly) weeklyOnly.style.display = (freq === 'WEEKLY') ? '' : 'none';
}

/**
 * Petit résumé textuel humain d'une rule, pour afficher dans la fiche
 * brief sans devoir re-rendre tout un formulaire.
 */
export function summarizeRule(rule) {
  if (!rule || typeof rule !== 'object') return '';
  const r = rule;
  const parts = [];
  const freq = r.freq || '';
  const interval = Math.max(1, parseInt(r.interval || 1, 10));
  if (freq === 'DAILY') {
    parts.push(interval === 1 ? 'Tous les jours' : `Tous les ${interval} jours`);
  } else if (freq === 'WEEKLY') {
    const days = Array.isArray(r.byweekday) ? r.byweekday : [];
    const dayLabels = days.map((c) => {
      const w = WEEKDAYS.find((x) => x.code === c);
      return w ? w.label : c;
    });
    const dayPart = dayLabels.length ? ` (${dayLabels.join(', ')})` : '';
    parts.push(interval === 1
      ? `Toutes les semaines${dayPart}`
      : `Toutes les ${interval} semaines${dayPart}`);
  } else if (freq === 'MONTHLY') {
    parts.push(interval === 1 ? 'Tous les mois' : `Tous les ${interval} mois`);
  } else {
    return '';
  }
  if (r.byhour != null || r.byminute != null) {
    const hh = String(r.byhour ?? 0).padStart(2, '0');
    const mm = String(r.byminute ?? 0).padStart(2, '0');
    parts.push(`à ${hh}:${mm}`);
  }
  if (r.until) {
    parts.push(`jusqu'au ${r.until}`);
  }
  return parts.join(' ');
}

/**
 * Formate `next_occurrence_at` (ISO) en libellé FR lisible — `dd/MM/yyyy à HH:mm`.
 */
export function formatNextOccurrence(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} `
      + `à ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch (e) {
    return '';
  }
}
