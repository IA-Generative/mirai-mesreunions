# Notes d'intégration — video-ingest

> Ce document complète le README et la spec `features-2-build/FEATURE_video-ingest.md`. Il décrit ce qui reste à faire **côté clients** (Mes Réunions, Mes Collections) et **côté ASR** (fallback Whisper), avec les contrats exacts. Rédigé en autonomie pendant que l'utilisateur travaillait sur autre chose — à valider avant exécution.

---

## 1. Slice 6 — Intégration Mes Réunions

### 1.1 Contrat API consommé

Le client `mesreunions-web` n'a qu'à appeler **un seul endpoint** côté `video-ingest` :

```http
POST http://video-ingest.audio-internal.svc.cluster.local:8000/video/import
Authorization: Bearer <JWT_OIDC>
Content-Type: application/json

{
  "url": "https://youtu.be/dQw4w9WgXcQ",
  "language": "fr",
  "force_audio": false,
  "context": "meeting",
  "context_id": "<meeting_id_côté_mesreunions>"
}
```

Réponses possibles :
- `200 OK` + `{"status":"ready","reused":true,"video_source_id":N,"bookmark_id":M,"job_id":null}` → HIT cache, on bascule le `Meeting` directement en `ready`.
- `202 Accepted` + `{"status":"pending","reused":false,"job_id":J,"video_source_id":null}` → MISS, on passe le `Meeting` en `youtube_fetching` et on poll `GET /video/jobs/J`.
- `400` → URL invalide / non routable.
- `401`/`403` → souci de token.

### 1.2 Module backend à créer

Pattern aligné sur `mcr_import/` qui existe déjà sur la branche `feat/import-from-mcr` (donc le canevas est connu).

Fichier suggéré : `services/mesreunions-web/app/modules/youtube_import/routes.py`

```python
from flask import Blueprint, request, jsonify, current_app
import requests  # trust_env=True par défaut → honore HTTP_PROXY (D15)

bp = Blueprint("youtube_import", __name__, url_prefix="/api/youtube")

VIDEO_INGEST_URL = "http://video-ingest.audio-internal.svc.cluster.local:8000"

@bp.post("/import")
def import_from_youtube():
    # 1. Récupérer le JWT de la session (cookie OIDC → Bearer)
    bearer = current_app.oidc.access_token  # à adapter selon ton helper auth
    payload = request.get_json() or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url requise"}), 400

    # 2. Créer le Meeting côté mesreunions-web AVANT l'appel (status=youtube_fetching)
    meeting = create_meeting_youtube_fetching(
        user_sub=g.user_sub,
        source_url=url,
        title=url,  # sera mis à jour avec le vrai titre quand reçu
    )

    # 3. Appeler video-ingest avec context=meeting
    resp = requests.post(
        f"{VIDEO_INGEST_URL}/video/import",
        headers={"Authorization": f"Bearer {bearer}"},
        json={
            "url": url,
            "language": payload.get("language", "fr"),
            "force_audio": payload.get("force_audio", False),
            "context": "meeting",
            "context_id": str(meeting.id),
        },
        timeout=10,
    )
    body = resp.json()

    if resp.status_code == 200:
        # HIT cache → on récupère le titre et on passe ready immédiatement
        meta = fetch_metadata_from_video_ingest(body["video_source_id"], bearer)
        meeting.title = meta.get("title") or meeting.title
        meeting.status = "ready"
        meeting.video_source_id = body["video_source_id"]
        db.session.commit()
        return jsonify({"meeting_id": meeting.id, "reused": True}), 200

    if resp.status_code == 202:
        meeting.video_ingest_job_id = body["job_id"]
        db.session.commit()
        return jsonify({"meeting_id": meeting.id, "job_id": body["job_id"], "reused": False}), 202

    # Erreur upstream → annuler le Meeting
    db.session.delete(meeting)
    db.session.commit()
    return jsonify(body), resp.status_code
```

### 1.3 Migration BDD côté mesreunions-web

Ajouter 2 colonnes au `meetings` :

```sql
ALTER TABLE meetings
    ADD COLUMN IF NOT EXISTS video_source_id     BIGINT,      -- pointeur vers video_ingest.video_sources.id (pas FK, cross-service D14)
    ADD COLUMN IF NOT EXISTS video_ingest_job_id BIGINT;      -- pour le poll

-- Statut youtube_fetching à autoriser dans l'enum si tu en as un
```

### 1.4 Frontend — modale + statut

Onglet `frontend/tabs/meetings.js`, ajouter un bouton « Importer depuis YouTube » qui ouvre une modale :

```html
<dialog id="youtube-import">
  <h2>Importer une vidéo YouTube</h2>
  <input type="url" placeholder="https://youtu.be/..." />
  <label>
    Langue
    <select>
      <option value="fr">Français</option>
      <option value="en">Anglais</option>
    </select>
  </label>
  <label>
    <input type="checkbox" /> Forcer la transcription audio (Whisper)
  </label>
  <p class="mention-legale">
    En important une vidéo publique, vous certifiez disposer du droit
    d'en transcrire le contenu pour un usage de réunion interne. La
    vidéo n'est pas redistribuée ; seul son texte est conservé.
  </p>
  <button type="submit">Importer</button>
</dialog>
```

Polling après réponse `202` :

```js
async function pollJob(jobId) {
  while (true) {
    const j = await fetch(`/api/youtube/jobs/${jobId}`).then(r => r.json());
    if (j.status === "done")   { refreshMeetingsList(); return; }
    if (j.status === "failed") { showError(j.error_message);   return; }
    await new Promise(r => setTimeout(r, 3000));
  }
}
```

(Un endpoint proxy `/api/youtube/jobs/<id>` côté mesreunions-web qui forward vers video-ingest est plus propre que d'exposer video-ingest directement au browser.)

### 1.5 Mention légale (Q6) — résolue ici

La modale ci-dessus contient la mention recommandée. À valider par le service juridique avant mise en ligne réelle.

---

## 2. Slice ASR — fallback Whisper sous `force_audio`

### 2.1 Stratégie

Deux chemins possibles, à trancher :

**Chemin A — Réutiliser la stack Kevent existante** (recommandé).
Mémoire `project_kevent_gateway_api.md` rappelle que la gateway Kevent expose `operation=transcription` + `extension=.m4a`. On peut :
1. Télécharger l'audio YouTube avec yt-dlp (`format=bestaudio[ext=m4a]/bestaudio`).
2. Pousser le fichier vers Kevent via le client existant (à dupliquer dans `video_ingest/` — D14).
3. Récupérer le résultat via le mode async Kevent.
4. **Supprimer le fichier audio immédiatement après** (test E2E DoD §10).

Avantage : pas de GPU à provisionner pour `video-ingest`. Coût : couplage à la disponibilité Kevent.

**Chemin B — Whisper local au worker.**
Embarquer `faster-whisper` + cuDNN dans l'image du worker. Mémoire `feedback_faster_whisper_cuda_libs.md` documente les pièges (libcublas manquant, conflit cuDNN avec torch).

Avantage : autonomie totale. Coût : GPU dédié obligatoire pour rester réactif.

### 2.2 Implémentation côté code (chemin A retenu par défaut)

Module à créer : `services/video_ingest/app/providers/youtube/audio.py`

```python
"""Téléchargement audio + appel ASR via Kevent. Fichier supprimé immédiatement après."""

import os, tempfile
from pathlib import Path
from yt_dlp import YoutubeDL

from ...types import FetchedTranscript, TranscriptSegment

_YDL_AUDIO_OPTS_TEMPLATE = {
    "quiet": True, "no_warnings": True,
    "format": "bestaudio[ext=m4a]/bestaudio",
    "outtmpl": None,  # défini au runtime
}

def fetch_audio_and_transcribe(video_id: str, language: str) -> FetchedTranscript:
    with tempfile.TemporaryDirectory(prefix="video-ingest-") as tmp:
        opts = dict(_YDL_AUDIO_OPTS_TEMPLATE)
        opts["outtmpl"] = str(Path(tmp) / "%(id)s.%(ext)s")
        with YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        audio_paths = list(Path(tmp).iterdir())
        assert len(audio_paths) == 1, f"yt-dlp a produit {audio_paths!r}"
        audio_path = audio_paths[0]

        # Appel Kevent (client à dupliquer ici, pas d'import libs.shared)
        result = call_kevent_transcribe(audio_path, language=language)

        # CRITIQUE : audio NON persisté. Le TemporaryDirectory s'en charge,
        # mais on s'assure que le fichier n'est pas écrit ailleurs.
        # Cf. DoD §10 — test E2E dédié obligatoire.

    return _map_kevent_to_fetched_transcript(result, language)
```

Puis modifier `app/providers/youtube/__init__.py` :

```python
def fetch_audio(self, video_id: str, language: str) -> FetchedTranscript:
    from . import audio as _audio
    return _audio.fetch_audio_and_transcribe(video_id, language)
```

Et dans `app/orchestrator.py` remplacer la branche `force_audio` :

```python
if job.force_audio or transcript_unavailable:
    transcript = provider.fetch_audio(provider_video_id, language=languages[0])
    # … reste identique (chunking + persistance)
```

Le `NeedsAudioFallback` actuel devient une **intermédiaire interceptée** dans `run_and_record` : au lieu de `fail`, on requeue avec `force_audio=True`.

### 2.3 Test E2E « zéro audio résiduel »

Critère DoD §10 obligatoire. À placer dans `tests/regression/` :

```python
def test_no_audio_file_after_ingest(tmp_path, monkeypatch):
    # Compte les fichiers dans /tmp avant
    before = set(Path("/tmp").rglob("*.m4a")) | set(Path("/tmp").rglob("*.webm"))
    # Lancer un import force_audio sur une URL test
    import_video(url="https://youtu.be/xxx", force_audio=True)
    # Compte après — doit être identique
    after = set(Path("/tmp").rglob("*.m4a")) | set(Path("/tmp").rglob("*.webm"))
    assert after == before
```

---

## 3. Activation prod-bêta — état au 2026-08-30

Le service **tourne en prod-bêta** depuis le 2026-05-25 (le serveur MCP depuis le 05-27).
Ce qui suit remplace la checklist d'origine, dont plusieurs cases étaient cochées dans les
faits mais pas dans le document — et dont une ligne était fausse.

### Fait

- [x] Migration appliquée (`019_video_ingest_initial.sql` → `023_video_ingest_job_retry.sql`)
- [x] Secrets posés : `video-ingest-secret`, `kevent-api-key`, `internal-api-secret`
- [x] Trois charges en service : `video-ingest-api` (8000), `-mcp` (8001), `-worker`
- [x] `CiliumNetworkPolicy video-ingest-egress` en place
- [x] Client `mesreunions-web` câblé, slice ASR livrée
- [x] **Manifestes versionnés** — `deploy/kubernetes/environments/prod-beta/internal/`,
      reconstruits le 2026-08-30 et prouvés conformes (`kubectl diff` exit 0)

> ⚠ **La ligne « Apply kustomize » de la checklist d'origine était fausse.** Le chemin
> qu'elle nommait **n'a jamais existé dans git** : les trois charges avaient été posées à la
> main, et leur seule description survivante était l'annotation
> `last-applied-configuration` du cluster. Un correctif appliqué aurait disparu au prochain
> `apply`. Corrigé le 2026-08-30 — voir **ADR-0005** et
> [`docs/RUNBOOK_DEPLOIEMENT_PROD_BETA.md`](../../docs/RUNBOOK_DEPLOIEMENT_PROD_BETA.md).

### Déployer aujourd'hui

```bash
git push origin <branche>                                   # le build part de git
export REGISTRY_NAMESPACE=funcscwnspricelessmontalcinhiacgnzi
deploy/scripts/build-incluster.sh "$(date +%Y%m%d-%H%M%S)" --no-latest

# poser le tag dans internal/kustomization.yaml (bloc images:), puis
kustomize build --load-restrictor=LoadRestrictionsNone \
  deploy/kubernetes/environments/prod-beta/internal/ | kubectl apply -f -
```

La surcharge `patch-hotes.local.yaml` (gitignorée) est **requise** : sans elle
`kustomize build` échoue. Recopier `patch-hotes.local.yaml.example`.

### Reste à faire — trois dettes nommées

1. **Migrer vers l'API MCP 2.x.** Le code est épinglé `mcp>=1.2.0,<2` depuis le 2026-08-30 :
   `2.x` renomme `FastMCP` en `MCPServer` et **déplace `host`/`port` du constructeur vers
   `run()`**. Or `mcp_server.py` bind `0.0.0.0:8001` dans le constructeur. Migrer sans
   déplacer ce bind donnerait un serveur en écoute sur loopback — pod « Running » et
   injoignable. **Prérequis à la migration : un test qui prouve le bind et un aller-retour
   sur un outil.**
2. **Poser une sonde sur `video-ingest-mcp`.** Il n'en a aucune : rien ne distingue
   aujourd'hui un serveur MCP sain d'un serveur qui n'écoute nulle part. C'est ce qui rend
   la dette n°1 dangereuse.
3. **Trancher la sortie vers la passerelle Kevent.** `VIDEO_INGEST_KEVENT_GATEWAY_URL` est
   posée sur les trois charges, mais la `CiliumNetworkPolicy` ne l'autorise **pas** en
   sortie : le repli ASR Whisper ne peut pas aboutir. État du cluster depuis 96 jours.
   Soit on ouvre la sortie, soit on retire la variable qui promet ce qu'elle ne tient pas.

### Contrôles

```bash
kubectl -n audio-internal get pods -l app=video-ingest \
  -o custom-columns='NAME:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount'
kubectl -n audio-internal logs deploy/video-ingest-mcp --tail=5   # « Uvicorn running on http://0.0.0.0:8001 »
kubectl -n audio-internal exec deploy/video-ingest-api -- curl -s http://localhost:8000/health
```

---

_Document du 2026-05-25, actualisé le 2026-08-30 (remise sous git des manifestes, borne
haute sur `mcp`, tags immuables)._
