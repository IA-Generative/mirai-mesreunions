#!/usr/bin/env node
// Copie les assets statiques DSFR (CSS minifié, JS, fonts Marianne/Spectral,
// icônes système, favicon) depuis node_modules/@gouvfr/dsfr/dist vers
// app/static/dsfr/ pour qu'ils soient servis par Flask via /static/dsfr/...
//
// Pourquoi pas un import Vite ?
//   - Le CSS DSFR fait référence aux fonts par chemins relatifs (../fonts/…).
//     Les bundler obligerait à hasher les fonts, ce qui casse les références
//     relatives sans config supplémentaire et alourdit le bundle JS.
//   - Le DSFR est conçu pour être servi tel quel (CSP-friendly, cache long,
//     intégrité prévisible). Le sortir du flux Vite respecte ce contrat.
//
// Lancé automatiquement après `vite build` via le hook npm "postbuild" dans
// package.json. En dev (vite dev) il faut lancer ce script une fois à la main
// avant le premier `flask run` (ou via `npm run build:assets`).
//
// Idempotent : peut être relancé sans risque, copie strictement ce qui change.

import { cpSync, mkdirSync, existsSync, rmSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const ROOT = resolve(__dirname, '..');
const SRC = resolve(ROOT, 'node_modules/@gouvfr/dsfr/dist');
const DEST = resolve(ROOT, 'app/static/dsfr');

if (!existsSync(SRC)) {
  console.error(`[copy-dsfr-assets] introuvable: ${SRC}`);
  console.error('Lance d\'abord `npm install` pour récupérer @gouvfr/dsfr.');
  process.exit(1);
}

// Sous-arbres à copier. On omet les .map (gain ~50%) et les CSS legacy
// (compat IE11, on est moderne-only). Tout le reste est utile : DSFR fait
// du dynamique sur ces chemins (icônes utilitaires, fonts via @font-face).
const SUBPATHS = [
  // Coeur DSFR (CSS + JS principal)
  'dsfr/dsfr.min.css',
  'dsfr/dsfr.module.min.js',
  'dsfr/dsfr.nomodule.min.js',
  // Utilitaires (icônes système, classes utilitaires)
  'utility/utility.min.css',
  'utility/icons',
  // Fonts Marianne + Spectral (référencées par dsfr.min.css en relatif)
  'fonts',
  // Icônes du composant (référencées par dsfr.min.css)
  'icons',
  // Favicon DSFR officiel
  'favicon',
  // Artwork (pictogrammes utilisés par fr-card, fr-notice, etc.)
  'artwork',
  // Schemes (light/dark CSS variables)
  'scheme',
];

mkdirSync(DEST, { recursive: true });

let copied = 0;
for (const sub of SUBPATHS) {
  const src = join(SRC, sub);
  const dest = join(DEST, sub);
  if (!existsSync(src)) {
    console.warn(`[copy-dsfr-assets] absent (ignoré): ${sub}`);
    continue;
  }
  // Pour les répertoires, on nettoie d'abord pour éviter les fichiers
  // résiduels d'une ancienne version DSFR.
  if (existsSync(dest)) rmSync(dest, { recursive: true, force: true });
  mkdirSync(dirname(dest), { recursive: true });
  cpSync(src, dest, { recursive: true, errorOnExist: false });
  copied += 1;
}

console.log(`[copy-dsfr-assets] ${copied}/${SUBPATHS.length} chemins copiés vers ${DEST}`);
