# Ouvrir la préparation de réunion depuis votre application

Un lien suffit. Placez-le dans votre interface — bouton, menu, ligne d'agenda —
et l'utilisateur arrive dans l'assistant de préparation de réunion, avec le
contexte que vous lui transmettez.

```
https://<hôte-mesréunions>/preparer
```

Sans aucun paramètre, l'assistant s'ouvre vierge. C'est le cas le plus simple, et
il est parfaitement valable.

## Passer du contexte

Tous les paramètres sont facultatifs et se combinent librement.

| Paramètre | Contenu | Exemple |
|---|---|---|
| `sujet` | L'objet de la réunion | `sujet=Comité budget T4` |
| `duree` | Durée prévue, en minutes | `duree=60` |
| `type` | `general`, `one_on_one`, `project_update`, `steering_committee`, `brainstorm` | `type=steering_committee` |
| `role` | Le rôle de la personne dans la réunion | `role=J'anime la réunion` |
| `attendu` | Ce qu'elle attend du brief | `attendu=Anticiper les objections` |
| `date` | Date de la réunion, au format ISO | `date=2026-09-15T14:00` |
| `serie` | Identifiant d'une préparation précédente, pour chaîner une série | `serie=<uuid>` |
| `ref` | Votre propre identifiant, conservé avec le brief | `ref=agenda-8842` |

Exemple complet, tel qu'on peut le générer côté serveur :

```
https://<hôte-mesréunions>/preparer
  ?sujet=Comit%C3%A9%20budget%20T4
  &duree=60
  &type=steering_committee
  &date=2026-09-15T14:00
  &ref=agenda-8842
```

## Ce à quoi vous pouvez compter

**L'utilisateur garde la main.** Le lien pré-remplit l'assistant ; il ne
déclenche jamais la génération du brief. La personne vérifie ce qui a été
rempli, complète, puis valide. C'est délibéré : un lien ne doit pas pouvoir
provoquer un traitement à l'insu de celui qui clique.

**La connexion ne fait pas perdre le contexte.** Si l'utilisateur n'est pas
encore authentifié, il passe par le SSO et revient sur votre lien, paramètres
compris.

**Votre `ref` revient.** Il est conservé avec le brief produit, ce qui vous
permet de faire le lien avec l'élément qui a déclenché la préparation.

## Bon à savoir

- Les valeurs sont bornées à 300 caractères et nettoyées à l'arrivée : envoyez
  un intitulé, pas un document. Pour fournir des documents, c'est l'étape
  « Sources » de l'assistant qui s'en charge.
- Un `type` inconnu est ignoré plutôt que rejeté : le lien fonctionne quand
  même, l'assistant retombe sur le type général.
- Encodez les paramètres (`encodeURIComponent` ou équivalent) — les accents et
  les espaces sont fréquents dans un intitulé de réunion.
