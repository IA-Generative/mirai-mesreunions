# mydevices-web — frontend (Vite)

Toolchain de build front pour `mydevices-web`. Introduite en **PR0** comme
préalable à **PR4** (modularisation du JS inline).

## État actuel (PR0)

- `shell.js` est un placeholder minimal (un `console.log`) qui sert
  uniquement à valider la chaîne : `vite build` → `app/static/dist/shell.js`
  → servi par Flask via `/static/dist/shell.js`.
- `app/templates/index.html` n'inclut PAS encore le bundle (touché par PR3
  en parallèle). Le template partiel `_vite_shell_loader.html` est prêt à
  être inclus en PR4.

## À venir (PR4)

- Extraction des ~5000 lignes de JS vanilla inline d'`index.html` en
  modules ES (`tabs/`, `api/`, `state/`, `ui/`).
- `shell.js` deviendra un router minimal + lazy-load des modules par
  onglet via `import()` dynamique (chunks séparés par Vite).
- Le partial `_vite_shell_loader.html` sera inclus depuis `index.html`.

## Commandes

```bash
cd services/mydevices-web
npm install
npm run build   # → app/static/dist/shell.js
npm run dev     # serveur Vite local (dev only, pas utilisé en prod)
```

## Build container

Le `deploy/docker/Dockerfile` est multi-stage : un stage `node-builder`
exécute `npm install && npm run build` puis le `dist/` est copié dans
l'image Python finale via `COPY --from=node-builder`.
