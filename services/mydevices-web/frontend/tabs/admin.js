// Onglet "Administration" — accessible uniquement si le user a le role
// 'admin' dans ses claims OIDC. Stub PR4 : expose isAdminAvailable() et
// redirige vers /admin/ (path-based routing à l'ingress, hors scope PR4).
//
// PR5 : remplacer le redirect par un module embarqué + lazy-load via
// import() dynamique.

import { isAdmin } from '../lib/auth.js';

export function isAdminAvailable() {
  return isAdmin();
}

export function gotoAdmin() {
  if (!isAdminAvailable()) return false;
  window.location.href = '/admin/';
  return true;
}

window.gotoAdmin = gotoAdmin;
