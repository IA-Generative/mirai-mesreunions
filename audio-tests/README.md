# audio-tests/

Jeu minimal d'audio embarqué dans le repo (≈200 Ko) pour les tests qui ont
besoin d'un fichier sous la main, sans dépendre d'un fetch réseau.

| Fichier | Usage |
|---|---|
| `eicar_test_virus.mp3` / `.wav` | Chaîne de test antivirus **EICAR** (inoffensive) — vérifie que le scanner ClamAV rejette bien un fichier infecté. |
| `test_audio_reunion_12s.mp3` | Petit exemple (12 s) pour un smoke test rapide du pipeline. |

Le jeu **complet** de fixtures (poèmes domaine public, etc.) vit dans le repo
dédié `mirai-mesreunions-fixtures` et se récupère via
`deploy/scripts/setup.sh` dans `tests/fixtures/audio/`.
