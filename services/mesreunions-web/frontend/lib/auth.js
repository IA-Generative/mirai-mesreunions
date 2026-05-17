// Lecture des claims utilisateur. Pour mesreunions-web, les claims sont injectés
// par Flask dans le bootstrap-data (cf. lib/bootstrap.js) — pas d'endpoint
// /api/me dédié. Cette couche est isolée pour pouvoir évoluer (PR5) vers un
// vrai endpoint si on doit rafraîchir les rôles sans reload page.

import { CURRENT_USER } from './bootstrap.js';

export function getUser() {
  return CURRENT_USER || {};
}

export function isAdmin() {
  const u = getUser();
  const roles = Array.isArray(u.roles) ? u.roles : [];
  return roles.some((r) => String(r).toLowerCase() === 'admin');
}

window.getCurrentUser = getUser;
window.isAdmin = isAdmin;
