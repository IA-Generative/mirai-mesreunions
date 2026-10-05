# Déploiement — recherche des réunions pour Mon portail (contrat de recherche MirAI)

`GET /api/v1/search` (mesreunions-web) répond à Mon portail (MySearch), appelé
depuis le navigateur avec le jeton de l'utilisateur. internal-ingester porte la
recherche (`POST /api/v1/audio/search`). Recherche seulement : aucun appel à
un modèle de langage.

Contrat : voir le dépôt de Mon portail, `docs/contrat-recherche-mirai.md`.
Écarts assumés côté MesRéunions :

- `context.date_kind` vaut `meeting`, `import` ou `upload` (date de création
  d'un upload sans date de réunion saisie) ;
- `total` est plafonné à 1 000 ; au-delà, `total_is_lower_bound` vaut `true`
  (et `truncated` aussi). Sinon `total_is_lower_bound` vaut `false`.

## 1. Migration 024 — avant le déploiement du code

Vérifier d'abord la disponibilité d'`unaccent` sur postgres-internal :

```sql
SELECT name, installed_version FROM pg_available_extensions WHERE name = 'unaccent';
```

- Ligne présente : la migration crée l'extension (droit requis) et la recherche
  ignore les accents (« reunion » trouve « réunion »).
- Aucune ligne, ou création refusée : la migration passe quand même et l'annonce
  par `NOTICE: unaccent absent : recherche sensible aux accents`. Installer
  l'extension plus tard demande la procédure décrite en tête du fichier de
  migration (modifier la configuration, PUIS recalculer toute la colonne par
  lots — un REINDEX ne suffit pas).

Appliquer avec psql **sans** `-1` (le rattrapage valide par lots, l'index est
construit `CONCURRENTLY`) :

```bash
kubectl exec -i <pod postgres-internal> -- psql -U <utilisateur> -d <base> \
  < migrations/internal/024_user_audio_files_search.sql
```

Effets : colonne `user_audio_files.search_tsv` (ajout sans réécriture de la
table), triggers qui la tiennent à jour sur les seules colonnes de texte,
rattrapage par lots de 200, index GIN `ix_uaf_search_tsv`. Rejouable.

Sans la migration, la route répond `503 search_unavailable`.

## 2. Variables d'environnement de mesreunions-web

| Variable | Rôle | Défaut |
|---|---|---|
| `MESREUNIONS_CORS_ORIGINS` | Origines autorisées (Mon portail), virgules | vide = aucun appel navigateur |
| `PUBLIC_BASE_URL` | Base https des liens renvoyés (`/reunion/<id>`) | hôte de la requête |
| `SEARCH_OIDC_ISSUER` | Émetteur(s) accepté(s) — realm de Mon portail | `OIDC_ISSUER` + `OIDC_INTERNAL_ISSUER` |
| `SEARCH_OIDC_JWKS_URL` | JWKS de ce realm | `<émetteur interne>/protocol/openid-connect/certs` |
| `SEARCH_OIDC_AUDIENCE` | Audience exigée dans `aud` | `mes-reunions` |
| `SEARCH_ALLOWED_AZP` | Clients autorisés (`azp`) | `mysearch` |
| `SEARCH_RATE_LIMIT_PER_MINUTE` | Requêtes par utilisateur et par minute | `30` |
| `SEARCH_INGESTER_TIMEOUT_SECONDS` | Délai d'appel à internal-ingester | `7` |

Ne jamais régler `SEARCH_OIDC_AUDIENCE=mysearch` : ce serait une confusion
d'audience.

La limite de débit est comptée **par processus** : limite effective =
`SEARCH_RATE_LIMIT_PER_MINUTE × workers gunicorn × réplicas` (accepté en V1).

## 3. Ingress : ne pas journaliser `q`

`q` est dans l'URL : l'access log de l'ingress le recopierait. L'application ne
le journalise pas ; il faut un objet Ingress **dédié** au seul chemin de
recherche, sans access log. Les autres chemins gardent le leur.

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: mesreunions-web-recherche
  namespace: <namespace>
  annotations:
    nginx.ingress.kubernetes.io/enable-access-log: "false"
    # Reprendre ici les autres annotations de l'Ingress principal (TLS,
    # taille de corps…), sauf celles qui réécrivent le chemin.
spec:
  ingressClassName: nginx
  tls:
    - hosts: [<hôte-mesréunions>]
      secretName: <secret-tls>
  rules:
    - host: <hôte-mesréunions>
      http:
        paths:
          - path: /api/v1/search
            pathType: Exact
            backend:
              service:
                name: <service mesreunions-web>
                port:
                  number: <port>
```

Le CORS est posé par l'application : ne pas ajouter d'annotation
`enable-cors` sur cet Ingress (elle dupliquerait les en-têtes).

Vérifier aussi que `/reunion/` est servi par mesreunions-web (lien ouvert
depuis un résultat).

## 4. Keycloak (realm de Mon portail)

Artefact de production : la **portée optionnelle** `mes-reunions-recherche`
(mappeur d'audience vers le client `mes-reunions`) affectée au client public
`mysearch`, sans « direct access grants ». Voir le dépôt de Mon portail,
`deploy/keycloak/`. Le client `mes-reunions` doit exister dans ce realm pour
que le mappeur d'audience s'applique.

Le realm local `deploy/docker/keycloak-realm.json` prend un raccourci réservé
au développement : mappeur d'audience posé directement sur le client
`mysearch` (un import de realm qui déclare des `clientScopes` supprime les
portées par défaut de Keycloak) et mot de passe direct ouvert pour obtenir un
jeton en ligne de commande. À ne pas reproduire en production.

## 5. Lien `/reunion/<id>?t=<secondes>`

Ouvre la fiche et se place au moment indiqué. Derrière la connexion : la
destination survit au passage par le SSO dès que `require_auth` transmet
`next=` (mécanisme du lien externe `/preparer`, livré séparément).

## 6. Périmètre : les 30 derniers jours

La purge quotidienne de l'ingester (`run_internal_purge_once`) supprime les lignes `user_audio_files` plus anciennes que `INTERNAL_PURGE_MAX_AGE_DAYS`, transcription comprise, qu'elles soient à la corbeille ou non. En prod-bêta, ce délai est de 30 jours. La recherche ne couvre donc que les réunions des 30 derniers jours. Une réunion plus ancienne introuvable est normale, ce n'est pas une panne. Mon portail l'indique sous le groupe « Réunions ».

La visibilité fermée par défaut reste nécessaire : entre une suppression définitive et la purge, la ligne interne subsiste.

## Vérification locale

Jeu d'essai : `deploy/docker/seed/recherche-internal.sql` et
`recherche-external.sql` (deux utilisateurs, corbeille, suppressions
définitives, ré-import). Tests contre un Postgres jetable : voir l'en-tête de
`tests/unit/test_meeting_search.py` (`MESREUNIONS_TEST_PG_DSN`).
