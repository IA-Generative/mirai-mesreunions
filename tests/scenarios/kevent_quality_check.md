# Kevent quality check — humain

Checklist non-scriptable de revue de la qualité des sorties LLM produites
par le pipeline Kevent. À exécuter sur **3 fichiers de test** typiques :

| Fichier | Type | Pourquoi tester |
|---|---|---|
| `dialogue_2p.mp3` | Dialogue clair entre 2 personnes nommées | Cas nominal — naming + reformulation doivent être nets |
| `monologue.mp3` | Une seule voix, lecture continue | Évite que le système hallucine plusieurs speakers |
| `bruite.mp3` | Enregistrement avec parasites, faux départs | Stresse l'OOB cleaning + résilience |

Pour chaque fichier, uploader via le flow mobile, attendre que la pipeline
Kevent finisse (cf `kevent_smoke.sh`), puis remplir la grille ci-dessous.

## 1. Transcription (`transcription_text`)

- [ ] Le texte produit reflète fidèlement le contenu audio (pas de
      hallucination, pas de coupure inattendue)
- [ ] La langue détectée (`transcription_language`) est correcte
- [ ] Sur le fichier bruité : pas de "Thank you" / phrases parasites ajoutées
      par Whisper

## 2. Diarisation (`diarization_json`)

- [ ] Le nombre de speakers détectés est plausible (2 pour le dialogue,
      1 pour le monologue)
- [ ] Les segments couvrent toute la durée sans grands trous
- [ ] Pas de speakers fantômes au milieu d'une longue intervention
      d'une seule personne

## 3. Speaker-tagged (`speaker_tagged_text`)

- [ ] Format Markdown lisible (`**SPEAKER_00** _(0:00 → 0:14)_`)
- [ ] L'attribution texte→speaker est globalement cohérente avec l'écoute
- [ ] Quand naming activé : noms réels présents pour les speakers
      qui se sont identifiés ; `SPEAKER_NN` pour ceux non identifiables

## 4. Out-of-band cleaning (`cleaned_text`)

- [ ] Sur le fichier bruité : moins de "euh", "hum", répétitions parasites
- [ ] Le sens du dialogue est préservé (rien d'important supprimé)
- [ ] Format préservé (les marqueurs SPEAKER restent intacts)

## 5. Reformulation (`reformulated_text`)

- [ ] Lecture fluide en discours indirect ("Jean a dit que…, Marie a
      répondu que…")
- [ ] Identité des intervenants préservée
- [ ] Chiffres, dates, noms propres exacts
- [ ] Pas d'opinion ni d'analyse ajoutée

## 6. Meeting analysis (`meeting_analysis_json`)

Parser le JSON et vérifier :

- [ ] **`actors`** : tous les intervenants identifiés (avec rôle si
      déductible)
- [ ] **`themes`** : 3-8 thématiques pertinentes, pas de redondance,
      pas de thème générique vide ("réunion en cours")
- [ ] **`decisions`** : items réellement énoncés, owner+due renseignés
      quand disponibles
- [ ] **`gaps`** : pertinent — pas d'éléments inventés, mais oui des
      questions ouvertes ou sujets non traités
- [ ] **`recommendations`** : 2-5 actions concrètes, fondées sur le
      contenu de la réunion (pas des conseils génériques)

## 7. Robustesse / coût

- [ ] Temps total `kevent_transcribing → kevent_completed` raisonnable
      (~5-30 s pour un fichier court, ~minutes pour un fichier long)
- [ ] Pas d'erreurs récurrentes dans les logs internal-ingester pour les LLM
      calls (sinon ajuster les prompts ou les modèles)
- [ ] `transcription_status='kevent_completed'` (pas
      `kevent_partially_completed` à répétition — sinon une étape
      échoue systématiquement)

## Si une étape produit du contenu de qualité insuffisante

1. Identifier l'étape (logs internal-ingester : "speaker_names: …", "oob_cleaning: …", etc.)
2. Itérer sur le prompt correspondant dans
   [`services/dmz-to-internal-bridge/app/prompts/`](../../services/dmz-to-internal-bridge/app/prompts/)
3. Rebuild + push image, rolling restart internal-ingester
4. Re-uploader le même fichier, comparer

## Décisions à acter après cette revue

- [ ] Les modèles par défaut (`chat-small` / `mistral-small-24b` /
      `gptoss-120b`) sont-ils adaptés ? Sinon les changer via
      `LLM_MODEL_SMALL` / `_MEDIUM` / `_LARGE`.
- [ ] Activer ou pas chaque sous-toggle en production selon la qualité
      observée.
