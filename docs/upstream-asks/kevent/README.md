# Demandes upstream Kevent

Drafts d'issues et de PRs à soumettre sur le repo amont
[`IA-Generative/kevent-ai`](https://github.com/IA-Generative/kevent-ai/issues).

Contexte : suite à l'incident "watchdog steal" (mai 2026) côté
mirai-mesreunions, on a identifié trois améliorations à porter
côté Kevent gateway pour soutenir la promesse produit "transcription
fiable bout-en-bout, même sur audios longs". Les fichiers de ce dossier
sont rédigés pour être copiés-collés dans des issues GitHub.

## Statut (mai 2026)

**Issue soumise** : [IA-Generative/kevent-ai#66](https://github.com/IA-Generative/kevent-ai/issues/66)
— consolide les 3 propositions ci-dessous en une seule, après
constat que `DELETE /jobs/{type}/{id}` existait déjà côté gateway
(handler `Cancel` dans `internal/handler/jobs.go:447`) mais ne
couvrait que l'état `pending` (rejette 409 sur `processing`,
qui est précisément notre cas réel). L'ask est donc resserré sur
le **niveau 2 (lease auto-renouvelé)** comme défense en profondeur.

Les fichiers ci-dessous restent comme drafts historiques de la
réflexion.

| Fichier | Statut | Note |
|---|---|---|
| [01-issue-job-leak-when-client-republishes.md](01-issue-job-leak-when-client-republishes.md) | **Soumis** (consolidé dans #66) | Bug report factuel |
| [02-feature-delete-jobs-endpoint.md](02-feature-delete-jobs-endpoint.md) | **Caduc** | DELETE existe déjà côté kevent-ai, scope `pending` only |
| [03-feature-lease-based-job-cancellation.md](03-feature-lease-based-job-cancellation.md) | **Soumis** (consolidé dans #66) | Lease auto-renouvelé = ask principal |

Cross-réf : `~/.claude/projects/.../memory/project_kevent_long_audio_limits.md`
(bugs A/B/C connus côté Kevent) — ces propositions sont
complémentaires, pas redondantes.
