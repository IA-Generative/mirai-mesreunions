# Provisioning du secret SMTP (Lot 8 — envoi CR par email)

Ce document décrit la procédure manuelle d'application du secret K8s `smtp-secret`
dans le namespace `audio-internal`, requis par le module `mydevices-web/app/mailer.py`
pour l'envoi automatique du CR par email aux participants en fin de transcription
(et pour le bouton manuel "Envoyer le CR maintenant" côté fiche meeting).

**Aucune action automatique n'est réalisée par le pipeline CI.** Le secret doit
être appliqué manuellement par un opérateur ayant accès au cluster.

## Variables d'environnement attendues par `mydevices-web`

| Variable | Obligatoire | Description | Exemple |
|---|---|---|---|
| `SMTP_HOST` | oui | Serveur SMTP (FQDN ou IP). | `smtp.numerique-fake-domain.name` |
| `SMTP_PORT` | non | Port (défaut 587). | `587` |
| `SMTP_USER` | non* | Login SMTP. *(*) requis si auth.* | `mes-reunions@example.fr` |
| `SMTP_PASSWORD` | non* | Mot de passe SMTP. *(*) requis si auth.* | `<secret>` |
| `SMTP_FROM` | recommandé | Adresse expéditrice (défaut = `SMTP_USER`). | `Mes Réunions <noreply@numerique-fake-domain.name>` |
| `SMTP_USE_TLS` | non | `true` (défaut) = STARTTLS, `false` = plain. | `true` |
| `SMTP_TIMEOUT` | non | Timeout réseau en secondes (défaut 20). | `20` |
| `PUBLIC_BASE_URL` | recommandé | URL racine publique pour les liens du CR. | `https://<mydevices-host>` |

## Création du secret K8s

```bash
KC_INT=/Users/etiquet/Documents/GitHub/mirai-mesreunions/deploy/kubernetes/kubeconfigs/kubeconfig-internal-gw.yaml

kubectl --kubeconfig "$KC_INT" -n audio-internal create secret generic smtp-secret \
  --from-literal=SMTP_HOST=smtp.example.org \
  --from-literal=SMTP_PORT=587 \
  --from-literal=SMTP_USER=mes-reunions@example.org \
  --from-literal=SMTP_PASSWORD='<password>' \
  --from-literal=SMTP_FROM='Mes Réunions <noreply@example.org>' \
  --from-literal=SMTP_USE_TLS=true \
  --dry-run=client -o yaml | kubectl --kubeconfig "$KC_INT" -n audio-internal apply -f -
```

## Câblage dans le déploiement `mydevices-web`

Ajouter dans `deploy/kubernetes/environments/prod-beta/internal/mydevices-web.yaml`
(ou en patch kustomize) :

```yaml
spec:
  template:
    spec:
      containers:
      - name: mydevices-web
        envFrom:
          - secretRef:
              name: smtp-secret
              optional: true   # tolère l'absence (mode dry-run)
        env:
          - name: PUBLIC_BASE_URL
            value: "https://<mydevices-host>"
```

Puis :

```bash
kubectl --kubeconfig "$KC_INT" -n audio-internal rollout restart deployment/mydevices-web
kubectl --kubeconfig "$KC_INT" -n audio-internal rollout status deployment/mydevices-web --timeout=180s
```

## Variable côté pipeline ingester (hook send-cr)

Pour que le hook post-transcription (`dmz-to-internal-bridge/app/puller.py`) puisse
appeler `mydevices-web /api/meetings/<id>/send-cr`, il faut exposer l'URL interne :

```yaml
# deployment dmz-to-internal-bridge (zone interne)
env:
  - name: MYDEVICES_WEB_INTERNAL_BASE_URL
    value: "http://mydevices-web.audio-internal.svc.cluster.local"
  - name: MEETING_CR_EMAIL_HOOK_ENABLED
    value: "true"   # défaut true ; mettre "false" pour désactiver
```

Le bearer utilisé est `INTERNAL_API_TOKEN` (déjà partagé entre tous les services
internes).

## Mode dry-run (sans SMTP configuré)

Si aucun de ces secrets n'est défini :

- `mailer.is_configured()` renvoie `False`.
- `POST /api/meetings/<id>/send-cr` répond `503 {"error": "smtp_not_configured"}`.
- Le hook puller log un debug `MYDEVICES_WEB_INTERNAL_BASE_URL not set, skipping`
  et passe son chemin sans crash.
- Le toggle UI "Envoyer le CR aux participants" peut être activé/persisté en
  base sans effet observable côté inbox.

## Validation rapide

```bash
# Smoke "configured?"
kubectl --kubeconfig "$KC_INT" -n audio-internal exec deploy/mydevices-web -- \
  python -c "from app.mailer import is_configured, get_config; \
             import json; print(json.dumps({'ok': is_configured(), 'cfg': get_config()}))"
```

## Prochaines étapes après application

1. Activer le toggle "Envoyer le CR aux participants" sur une préparation de test.
2. Lier un audio à cette préparation.
3. Attendre la fin du pipeline de transcription.
4. Vérifier l'inbox de l'adresse participant + les logs :
   ```bash
   kubectl --kubeconfig "$KC_INT" -n audio-internal logs deploy/dmz-to-internal-bridge | grep send-cr
   kubectl --kubeconfig "$KC_INT" -n audio-internal logs deploy/mydevices-web | grep mailer
   ```
