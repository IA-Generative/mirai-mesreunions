#!/usr/bin/env bash
# Campagne de non-régression — à exécuter avant ET après chaque slice
# du plan ~/.claude/plans/l-importation-de-fichier-youtube-nifty-frost.md
#
# 2 runs séparés (volontairement) :
#   1. tests video_ingest_* (notre périmètre, doit rester 100% vert)
#   2. tests historiques (dette préexistante = ~20 fails de pollution sys.modules,
#      acceptable tant que le compteur ne se dégrade pas)
#
# Usage :
#   bash tests/run-regression-campaign.sh [tag]
#
# Si `tag` est fourni, le rapport est écrit dans :
#   tests/regression/campaigns/<tag>-<timestamp>.txt
# Sinon le rapport va sur stdout uniquement.
#
# Le script échoue (exit ≠ 0) seulement si :
#   - les tests video_ingest_* ne sont pas 100% verts
#   - OU le compteur "historique passed" diminue strictement vs la baseline
#     (.last-passed-historical sauvegardée dans tests/regression/)

set -euo pipefail

cd "$(dirname "$0")/.."

TAG="${1:-}"
TS="$(date +%Y%m%d-%H%M%S)"
CAMPAIGNS_DIR="tests/regression/campaigns"
BASELINE_FILE="tests/regression/.last-passed-historical"

mkdir -p "$CAMPAIGNS_DIR"

# Build le rapport en mémoire d'abord (pour l'écrire à la fin avec un tag clair).
REPORT=""
log() {
  REPORT="${REPORT}$*"$'\n'
  echo "$*"
}

log "=== Campagne de non-régression — $(date) ==="
[ -n "$TAG" ] && log "Tag : $TAG"
log ""

# ── Run 1 : périmètre video_ingest (notre code, doit être 100% vert) ─────────
log "── Run 1 : tests/unit/test_video_ingest_*.py ──"
VI_OUT=$(python -m pytest tests/unit/test_video_ingest_*.py 2>&1 || true)
VI_LAST=$(echo "$VI_OUT" | tail -3 | tr -d '\n')
log "$VI_LAST"
log ""

VI_PASSED=$(echo "$VI_OUT" | grep -oE '[0-9]+ passed' | head -1 | grep -oE '[0-9]+' || echo 0)
VI_FAILED=$(echo "$VI_OUT" | grep -oE '[0-9]+ failed' | head -1 | grep -oE '[0-9]+' || echo 0)

# ── Run 2 : reste de tests/unit/ (dette préexistante = ~20 fails) ────────────
log "── Run 2 : tests/unit/ (hors video_ingest) ──"
HIST_OUT=$(python -m pytest tests/unit/ \
  --ignore=tests/unit/test_video_ingest_youtube_audio.py \
  --ignore=tests/unit/test_video_ingest_youtube_metadata.py \
  --ignore=tests/unit/test_video_ingest_youtube_provider.py \
  --ignore=tests/unit/test_video_ingest_youtube_subtitles.py \
  --ignore=tests/unit/test_video_ingest_youtube_url.py \
  --ignore=tests/unit/test_video_ingest_youtube_provider.py \
  --ignore=tests/unit/test_video_ingest_audit.py \
  --ignore=tests/unit/test_video_ingest_quotas.py \
  --ignore=tests/unit/test_video_ingest_jobs.py \
  --ignore=tests/unit/test_video_ingest_chunking.py \
  --ignore=tests/unit/test_video_ingest_orchestrator.py \
  --ignore=tests/unit/test_video_ingest_api.py \
  --ignore=tests/unit/test_video_ingest_auth.py \
  2>&1 || true)
HIST_LAST=$(echo "$HIST_OUT" | tail -3 | tr -d '\n')
log "$HIST_LAST"
log ""

HIST_PASSED=$(echo "$HIST_OUT" | grep -oE '[0-9]+ passed' | head -1 | grep -oE '[0-9]+' || echo 0)
HIST_FAILED=$(echo "$HIST_OUT" | grep -oE '[0-9]+ failed' | head -1 | grep -oE '[0-9]+' || echo 0)

# ── Diagnostic ───────────────────────────────────────────────────────────────
log "── Compteurs ──"
log "video_ingest : ${VI_PASSED} passed / ${VI_FAILED} failed"
log "historique   : ${HIST_PASSED} passed / ${HIST_FAILED} failed"
log "total passed : $((VI_PASSED + HIST_PASSED))"

# Critère 1 : video_ingest doit être 100% vert
FAIL_EXIT=0
if [ "$VI_FAILED" -gt 0 ]; then
  log ""
  log "❌ ÉCHEC : ${VI_FAILED} tests video_ingest cassés. Bloquant pour la slice."
  FAIL_EXIT=1
fi

# Critère 2 : compteur historique ne diminue pas vs baseline
if [ -f "$BASELINE_FILE" ]; then
  BASELINE_HIST=$(cat "$BASELINE_FILE")
  if [ "$HIST_PASSED" -lt "$BASELINE_HIST" ]; then
    log ""
    log "❌ ÉCHEC : compteur historique passe de ${BASELINE_HIST} → ${HIST_PASSED}. Régression."
    FAIL_EXIT=1
  else
    log ""
    log "✓ Historique : ${HIST_PASSED} ≥ baseline ${BASELINE_HIST}"
    # Si on augmente, on met à jour la baseline.
    if [ "$HIST_PASSED" -gt "$BASELINE_HIST" ]; then
      echo "$HIST_PASSED" > "$BASELINE_FILE"
      log "  (baseline relevée à ${HIST_PASSED})"
    fi
  fi
else
  # Première exécution : pose la baseline
  echo "$HIST_PASSED" > "$BASELINE_FILE"
  log ""
  log "ℹ️ Baseline historique initialisée à ${HIST_PASSED}"
fi

# ── Écriture du rapport ──────────────────────────────────────────────────────
if [ -n "$TAG" ]; then
  REPORT_FILE="${CAMPAIGNS_DIR}/${TAG}-${TS}.txt"
  echo "$REPORT" > "$REPORT_FILE"
  log ""
  log "Rapport archivé : $REPORT_FILE"
fi

exit $FAIL_EXIT
