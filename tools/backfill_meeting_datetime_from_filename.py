"""Backfill ``meeting_datetime`` depuis ``original_filename`` pour les
UserAudioFile existants sans datetime fixée.

À exécuter UNE FOIS après le déploiement du parseur côté ingester. Idempotent
(filtre ``meeting_datetime IS NULL``), donc rejouable sans effet de bord.

Usage : depuis un pod qui a accès à la DB interne (internal-ingester) ::

    kubectl exec deployment/internal-ingester -n audio-internal -- \\
        python /app/tools/backfill_meeting_datetime_from_filename.py

Par défaut le script est en mode ``--dry-run`` (affiche ce qui serait fait
sans rien modifier). Ajouter ``--apply`` pour persister.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Quand exécuté in-pod, /app est dans le PYTHONPATH ; on garde un fallback
# pour les exécutions hors-pod (dev shell, tests).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import update

from libs.shared.app.config import load_int_db
from libs.shared.app.filename_datetime import extract_meeting_datetime
from libs.shared.app.models import UserAudioFile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_meeting_datetime")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Persiste les changements (sinon dry-run).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap nombre de rows à traiter (debug).")
    args = parser.parse_args()

    eng = create_engine(load_int_db().sync_url)
    Session = sessionmaker(bind=eng)
    db = Session()

    try:
        q = (
            db.query(UserAudioFile)
            .filter(UserAudioFile.meeting_datetime.is_(None),
                    UserAudioFile.original_filename.isnot(None))
        )
        if args.limit:
            q = q.limit(args.limit)
        rows = q.all()
        logger.info("Candidats sans meeting_datetime : %d", len(rows))

        matched = []
        for r in rows:
            dt = extract_meeting_datetime(r.original_filename or "")
            if dt is None:
                continue
            matched.append((r.id, r.original_filename, dt))

        logger.info("Parseable : %d / %d", len(matched), len(rows))

        # Affiche un échantillon pour validation visuelle.
        for rid, name, dt in matched[:20]:
            logger.info("  %s | %s → %s", str(rid)[:8], name, dt.isoformat())
        if len(matched) > 20:
            logger.info("  … et %d autres", len(matched) - 20)

        if not args.apply:
            logger.info("DRY-RUN — rien persisté. Relancer avec --apply pour appliquer.")
            return 0

        # Batch UPDATE en une transaction. Pas d'ORM par row, on fait des
        # UPDATEs ciblés par id (rapide même sur >10000 lignes).
        for rid, _name, dt in matched:
            db.execute(
                update(UserAudioFile)
                .where(UserAudioFile.id == rid,
                       UserAudioFile.meeting_datetime.is_(None))
                .values(meeting_datetime=dt)
            )
        db.commit()
        logger.info("APPLIED : %d rows mises à jour.", len(matched))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
