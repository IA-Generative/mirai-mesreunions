# Demandes upstream Kevent

Drafts d'issues et de PRs à soumettre sur le repo amont
[`IA-Generative/kevent-ai`](https://github.com/IA-Generative/kevent-ai/issues).

Contexte : suite à l'incident "watchdog steal" (mai 2026) côté
mirai-mesreunions, on a identifié trois améliorations à porter
côté Kevent gateway pour soutenir la promesse produit "transcription
fiable bout-en-bout, même sur audios longs". Les fichiers de ce dossier
sont rédigés pour être copiés-collés dans des issues GitHub.

| Fichier | Type | Cible | Priorité |
|---|---|---|---|
| [01-issue-job-leak-when-client-republishes.md](01-issue-job-leak-when-client-republishes.md) | Bug report | Issue | P1 — fuite GPU avérée |
| [02-feature-delete-jobs-endpoint.md](02-feature-delete-jobs-endpoint.md) | Feature request | PR small | P2 — unblock fix client |
| [03-feature-lease-based-job-cancellation.md](03-feature-lease-based-job-cancellation.md) | Design proposal | Issue + RFC | P3 — défense en profondeur |

Note : ne pas soumettre les 3 ensemble. Commencer par #1 (bug
factuel), discuter avec l'équipe Kevent, puis #2 si l'idée passe, puis
#3 comme évolution structurelle.

Cross-réf : `~/.claude/projects/.../memory/project_kevent_long_audio_limits.md`
(bugs A/B/C connus côté Kevent) — ces propositions sont
complémentaires, pas redondantes.
