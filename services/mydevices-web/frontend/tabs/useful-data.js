// Onglet "Mes données utiles" — non implémenté dans l'UI actuelle.
// Stub PR4 documentant les futures intégrations Coming Soon :
//   - Glossaire utilisateur (édition + compteur termes à corriger)
//   - Drive sync (état)
//   - Drive perso, Drive DTNUM (à venir)
//   - DINUM, Resana, mescollections (à venir)
//   - Intégration mail/agenda local (à venir)
//
// La corbeille (links vers loadTrash) est déjà accessible via tab
// 'trash' dans tabs/meetings.js.

export const COMING_SOON = [
  { id: 'drive-perso', label: 'Drive personnel', status: 'à venir' },
  { id: 'drive-dtnum', label: 'Drive DTNUM (mesfichiers)', status: 'à venir' },
  { id: 'dinum', label: 'DINUM', status: 'à venir' },
  { id: 'resana', label: 'Resana', status: 'à venir' },
  { id: 'mescollections', label: 'mescollections', status: 'à venir' },
  { id: 'mail-agenda', label: 'Intégration mail/agenda local', status: 'à venir' },
];

// mount() futur : rendra une grille de cards désactivées avec leur label.
export function mount(container) {
  if (!container) return;
  // No-op pour PR4 — l'onglet n'est pas dans la nav HTML actuelle.
}
