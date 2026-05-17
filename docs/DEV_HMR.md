# Boucle d'itération rapide UI (mydevices-web)

## TL;DR

```bash
# Terminal 1 — stack docker locale (Flask + DB + Keycloak + MinIO + …)
cd deploy/docker
VITE_DEV=1 docker compose -f docker-compose.yml -f docker-compose.shared-infra.yml up -d

# Terminal 2 — Vite dev server sur l'hôte (HMR)
cd services/mydevices-web
npm install   # une fois
npm run dev

# Ouvrir http://localhost:8094 (Flask), pas :5173.
# Toute modif dans frontend/**/*.{js,css,html} → HMR <100ms, état conservé.
```

## Comment ça marche

- **Flask (docker, :8080)** sert l'HTML et l'API. Le template `_vite_shell_loader.html`
  branche conditionnellement le chargement du JS :
  - `VITE_DEV=1` → `<script src="http://localhost:5173/shell.js">` + client HMR
  - sinon → `/static/dist/shell.js` (bundle prébuild)
- **Vite (hôte, :5173)** sert les modules ES en direct avec HMR. CORS activé pour
  accepter les requêtes depuis `:8080`.
- Le browser parle aux deux origines en parallèle. Pas de proxy à configurer.

## Niveau intermédiaire — watch rebuild (sans HMR)

Si tu ne veux pas patcher l'env :

```bash
cd services/mydevices-web && npm run build -- --watch
```

Le bundle dans `app/static/dist/` est régénéré à chaque save (~200ms). Cmd+Shift+R
pour recharger. Marche aussi pour itérer contre prod-bêta (uploader `dist/` à la main).

## Pièges connus

- **Service worker PWA** : `feedback_pwa_service_worker_caching.md` — en mode HMR
  c'est sans effet (le SW ne couvre que `mobile-upload-pwa`), mais reste à
  l'esprit si tu testes des modifs côté `:8081`.
- **Production** : ne JAMAIS exporter `VITE_DEV=1` dans les manifestes K8s ou
  `.env` prod. Le warning est loggé au démarrage de Flask.
- **Vite non installé** : `cd services/mydevices-web && npm install` une fois.
  Le `node_modules/` est gitignored, pas inclus dans l'image docker.
