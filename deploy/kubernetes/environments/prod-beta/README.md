# prod-bêta — zone interne

Cible n°3 du tableau de [`../README.md`](../README.md) : deux clusters réseau-isolés, un par
zone. Ce dossier ne décrit que ce qui **manquait** au dépôt, pas l'ensemble de la zone.

## Ce que ce dossier contient, et pourquoi il est né le 2026-08-30

`internal/video-ingest.yaml` — les trois charges du composant d'import vidéo
(`video-ingest-api`, `-mcp`, `-worker`), plus leurs deux Services et leur politique de
sortie Cilium.

Ils tournaient depuis le 2026-05-25 **sans aucun manifeste**. Ni dans git — l'historique
complet du dépôt ne connaît que `environments/README.md` et `environments/staging/` — ni sur
la machine de construction, dont `internal-zone/deployments.yaml` contient zéro occurrence
de « video-ingest ». Ils avaient été posés à la main, et leur seule description survivante
était l'annotation `last-applied-configuration` portée par les objets du cluster. Tant que
c'était vrai, **tout correctif appliqué au cluster disparaissait au prochain `apply`**, sans
un mot. Le contenu de ce dossier est reconstruit depuis cette annotation, puis vérifié par
`kubectl diff` : aucune valeur ne diffère de ce qui tourne.

Les cinq autres déploiements de `audio-internal` (`admin-console`, `device-token-authority`,
`internal-ingester`, `mesreunions-web`, `transcription-relay`) ne sont **pas** ici : leurs
manifestes vivent dans les zones gitignorées `deploy/kubernetes/{internal,external}-zone/`,
synchronisées hors git. On ne déplace pas ce qui a déjà une source ; on remet sous git ce
qui n'en avait aucune.

## Le fichier gitignoré que ce dossier exige

Ce dépôt est **public**. Les deux hôtes réels — SSO ministériel et passerelle de
transcription — n'y figuraient nulle part avant ce jour ; les écrire ici serait une fuite
neuve. `video-ingest.yaml` porte donc la convention maison `*.fake-domain.name` /
`example.com`, déjà employée partout ailleurs (et prescrite pour ces variables précises par
[`../../../../services/video_ingest/SECURITY_REVIEW.md`](../../../../services/video_ingest/SECURITY_REVIEW.md)).

Les valeurs de prod-bêta vivent dans `internal/patch-hotes.local.yaml`, **gitignoré**
(convention `*.local.yaml`), que la kustomization **exige** :

```
kustomize build --load-restrictor=LoadRestrictionsNone internal/ | kubectl apply -f -
```

Sans ce fichier, la construction **échoue** avec « no such file ». C'est voulu : appliquer
ce dossier avec la mauvaise adresse de SSO casserait l'authentification de tous les imports
sans que rien ne le dise. Un refus bruyant vaut mieux qu'un déploiement faux.

Le gabarit à recopier est dans `internal/patch-hotes.local.yaml.example`.

## Deux constats consignés, volontairement non corrigés ici

1. **La passerelle Kevent n'est pas autorisée en sortie.**
   `VIDEO_INGEST_KEVENT_GATEWAY_URL` est posée sur les trois charges, mais la politique
   Cilium n'ouvre que la résolution de noms, la base, `internal-ingester`, les domaines
   YouTube et le SSO. Le repli ASR Whisper ne peut donc pas aboutir. Ce n'est pas une
   régression de ce fichier : c'est l'état du cluster depuis 96 jours (mesuré le
   2026-08-30), remis sous git pour qu'il cesse d'être invisible. À trancher à part —
   ouvrir la sortie, ou retirer la variable qui promet ce qu'elle ne tient pas.

2. **`video-ingest-mcp` n'a aucune sonde.** Il bind `0.0.0.0:8001` dans le constructeur de
   FastMCP ; s'il retombait sur le défaut loopback, le pod resterait « Running » et
   personne ne l'apprendrait. À corriger avec la migration MCP 2.x.

Mêler l'un ou l'autre à la remise sous git aurait empêché de prouver, par `kubectl diff`,
que le manifeste décrit bien ce qui tourne.
