# Dockerfile autoporteur du service `video-ingest` (cf. README D14).
#
# Construit une image *indépendante* du reste du monorepo : aucun
# `COPY libs/shared`, aucune dépendance hors `services/video_ingest/`
# et `migrations/internal/019_video_ingest_initial.sql`. Cet artefact
# est utilisé lors de l'extraction du composant dans son propre repo.
#
# En prod-bêta interne (cluster brave-bassi / audio-internal), c'est
# l'image monorepo partagée qui tourne aujourd'hui par convention
# (cf. deploy/kubernetes/internal-zone/video-ingest.yaml). Ce
# Dockerfile reste le contrat de portabilité.
#
# Build :
#   docker build -f services/video_ingest/Dockerfile -t video-ingest:dev .
# Run worker :
#   docker run --env-file .env video-ingest:dev python -m app.worker
# Run API :
#   docker run --env-file .env -p 8000:8000 video-ingest:dev \
#       gunicorn -w 2 -b 0.0.0.0:8000 app.api:create_app\(\)

FROM python:3.12-slim

WORKDIR /app

# ffmpeg requis par yt-dlp pour le postprocess FLAC en mode
# force_audio (Kevent gateway refuse .webm + bug pyannote samples
# mismatch sur AAC issue #41 → on remuxe systématiquement en flac).
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq-dev ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY services/video_ingest/requirements.txt /app/requirements.txt
# gunicorn pour servir l'API en prod (Flask dev server interdit).
RUN pip install --no-cache-dir -r /app/requirements.txt gunicorn==23.0.0

COPY services/video_ingest/app /app/app
COPY services/video_ingest/README.md /app/README.md
# La migration est embarquée pour permettre un `init-db` standalone
# (le job init-db côté K8s la rejoue idempotemment au déploiement).
COPY migrations/internal/019_video_ingest_initial.sql /app/migrations/019_video_ingest_initial.sql

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Pas de CMD par défaut : on choisit explicitement worker vs API au run
# (le K8s Deployment passe la commande appropriée). En local interactif,
# `python -m app.api` lance Flask dev.
CMD ["python", "-m", "app.api"]
