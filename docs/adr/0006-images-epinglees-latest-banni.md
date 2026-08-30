# ADR-0006 — Images épinglées sur un tag immuable, `latest` banni

## Statut
Accepté — 2026-08-30

## Contexte

Le 2026-08-29, une construction a tiré `mcp` 2.x (borne haute manquante, cf.
[ADR-0005](0005-manifestes-versionnes-hotes-expurges.md) pour le chantier voisin) et
`video-ingest-mcp` est mort au démarrage. Un seul pod est tombé — et c'est ce détail qui
est instructif.

Mesuré le 2026-08-30 dans `audio-internal` : **six des huit déploiements** tournaient sur
`secure-audio-upload:latest`, seuls `mesreunions-web` et `internal-ingester` étant épinglés
sur `20260829-204922`. Les six n'avaient simplement **pas encore retiré l'image**. La panne
ne les a pas touchés par chance, pas par conception : au premier redémarrage — une éviction,
un drain de nœud, un `rollout restart` sans rapport — ils auraient basculé sur l'image
cassée, sans rollout, sans trace, et sans que personne ne fasse le lien avec une
construction de la veille.

C'est le mécanisme exact de la panne Drive : « deux versions en service en même temps,
invisibles de tous ». Le dépôt de déploiement de la bêta interdit déjà `latest` ailleurs et
le justifie par cet incident.

`deploy/scripts/build-incluster.sh:117` ajoute `latest` aux destinations **par défaut**, et
accepte `--no-latest`. Le défaut est donc le comportement dangereux.

## L'observation qui rend la bascule sans risque

Les huit déploiements tournaient sur **un seul et même digest**, `sha256:c073ca6713f9…` :
les six étiquetés `latest` et les deux étiquetés `20260829-204922` étaient **la même
image**. Épingler les six n'était donc pas une montée de version déguisée, mais un **pur
ré-étiquetage** — aucun changement de comportement, aucune reconstruction.

Mesurer avant d'écrire a transformé une opération que l'on aurait crue risquée en une
opération démontrablement neutre. Sans ce relevé, le réflexe prudent aurait été de reporter.

## Décision

1. **Aucune image `:latest` dans `audio-internal`.** Tout déploiement porte un tag horodaté
   immuable. Vérifié après bascule : `0` image `latest`, 9/9 répliques prêtes.
2. **Toute construction destinée à corriger un service se fait `--no-latest`**, avec un tag
   explicite, et ne fait rouler que les déploiements concernés.
3. **Le tag est porté par le manifeste**, pas par une commande. Pour les charges versionnées,
   c'est le bloc `images:` de la `kustomization.yaml` — un seul endroit à lire pour savoir
   ce qui tourne. Pour les charges dont le manifeste a dérivé, c'est `kubectl set image`
   (voir le runbook : appliquer leur base écraserait dix-sept variables).

### Le corollaire : ne faire rouler que ce qui doit rouler

La correction du 2026-08-30 a été construite depuis une branche portant **48 fichiers et
7 582 insertions** de travail en cours sur le wizard. Basculer les huit déploiements sur
cette image aurait expédié tout ce travail en production pour un correctif de dépendance
d'une ligne.

Seules les trois charges `video-ingest-*` ont donc basculé sur le tag neuf ; les cinq
autres sont restées sur `20260829-204922`. C'est sûr **parce que c'est mesuré** :
`git log main..HEAD -- services/video_ingest/ requirements.txt` est vide — le composant est
identique entre les deux réfs, comme le garantit sa règle d'isolation D14 (aucun import
croisé). Un tag immuable rend cette dissociation lisible ; avec `latest`, elle est
impossible à exprimer.

## Conséquences

- Une construction ne peut plus modifier un service à son insu. Le lien « cette version
  tourne depuis ce commit » redevient vrai et vérifiable.
- Le coût : un geste de plus à chaque livraison — mettre à jour le tag dans le manifeste.
  C'est le prix de la traçabilité, et il est payé une fois par livraison, pas une fois par
  incident.
- **Reste à faire** : inverser le défaut de `build-incluster.sh` (`latest` sur demande
  explicite plutôt que par défaut), et poser un contrôle qui refuse un manifeste contenant
  `:latest`.
- **Angle mort qui subsiste** : `video-ingest-mcp` n'a **aucune sonde**. Une image épinglée
  garantit *quelle* version tourne, pas qu'elle *fonctionne*. Le serveur MCP bind
  `0.0.0.0:8001` dans le constructeur ; s'il retombait un jour sur le défaut loopback, le
  pod resterait « Running » et injoignable. C'est précisément pourquoi la migration MCP 2.x
  a été refusée dans le même lot (voir plus bas).

## Note liée — pourquoi `mcp<2` plutôt que la migration

Le message d'erreur proposait les deux voies. La migration a été écartée pour ce lot :

- la v2 **déplace `host`/`port`** du constructeur vers `run()`. Le code s'appuie sur le bind
  du constructeur, avec un commentaire disant que sans lui FastMCP écoute en loopback ;
- le serveur MCP **n'a aucun test** (« import-time validé, E2E à faire en intégration ») et
  **aucune sonde** ;
- combinés, ces trois faits font qu'une migration bâclée produirait un pod vert, sans sonde
  et injoignable — une panne **muette**, strictement pire que le plantage d'alors, qui au
  moins se comptait (68 redémarrages en 5 h 27).

Le vrai défaut n'était pas la version, mais la **borne haute absente**. Trois bornes ont été
posées ; la migration 2.x reste à faire, avec un test qui prouve le bind et un aller-retour
sur un outil.

Voir aussi [ADR-0004](0004-build-in-cluster-buildkit.md) et [ADR-0005](0005-manifestes-versionnes-hotes-expurges.md).
