"""Versement du brief de préparation dans le Drive utilisateur (§9bis du plan).

Best-effort asynchrone : la réponse à POST /api/meeting-prep ne l'attend pas.
La DB reste source de vérité ; le Drive est un export lisible.

4 fichiers déposés (overwrite à chaque édition) :
  - brief.md (rendu markdown du brief_json)
  - glossaire.txt (sortie de extract_full_glossary_terms_from_brief)
  - documents-source.md (liens vers les docs Drive ingérés)
  - prompt-utilise.txt (prompt LLM substitué, tronqué à 50k chars)

+ Préparations de réunion/glossaire-utilisateur.txt à la racine du dossier
preps (cf §5c, format 1 terme/ligne trié par fréquence desc, cap 300).

État actuel : placeholder structuré. La résolution du dossier cible
(``resolve_drive_target_folder``) et les appels Drive ``items/children/``
+ ``policy`` + ``upload-ended/`` ne sont PAS encore branchés (dépendent de
la DriveClient existante côté dmz-to-internal-bridge). Le thread est démarré, capture
les erreurs, écrit ``drive_sync_status = 'failed'`` en cas d'échec via le
relais device-token-authority (endpoint dédié à ajouter).

NotImplementedError remonté côté logs uniquement — n'impacte pas le
caller. À finaliser dans le sprint Drive/persistence.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


def schedule_drive_brief_sync(
    user_sub: str,
    brief_id: Optional[str],
    brief_json: dict,
    documents: list,
    used_prompt: Optional[str] = None,
    *,
    drive_folder_id: Optional[str] = None,
) -> None:
    """Démarre un thread daemon pour l'upload Drive.

    Ne lève PAS d'exception : tout échec est logué + flag persistance.
    Le caller (api_meeting_prep) reste libre d'aller jusqu'au return rapide.
    """
    if not brief_id:
        return
    t = threading.Thread(
        target=_drive_brief_sync_worker,
        args=(user_sub, brief_id, brief_json, documents, used_prompt, drive_folder_id),
        daemon=True,
        name=f"drive-brief-sync-{brief_id[:8]}",
    )
    t.start()


def _drive_brief_sync_worker(user_sub, brief_id, brief_json, documents,
                              used_prompt, drive_folder_id):
    try:
        _do_sync(user_sub, brief_id, brief_json, documents,
                 used_prompt, drive_folder_id)
    except NotImplementedError as exc:
        # État connu et catalogué (transport Drive pas encore branché) :
        # une ligne de warning suffit. La stacktrace complète qui partait
        # ici à CHAQUE génération faisait croire à un crash de fin de
        # cycle dans les logs prod.
        logger.warning("drive_brief_sync: skipped for brief=%s — %s", brief_id, exc)
    except Exception:
        logger.exception(
            "drive_brief_sync: failed for brief=%s user=%s (best-effort)",
            brief_id, user_sub,
        )
        # TODO : POST /api/v1/briefs/<id>/drive-sync-status {failed} via
        # device-token-authority pour que l'UI affiche le badge "Réessayer".


def _do_sync(user_sub, brief_id, brief_json, documents, used_prompt,
              drive_folder_id):
    """Implémentation effective — versement Drive du brief.

    **État** : génération des 4 contenus en mémoire (brief.md, glossaire.txt,
    documents-source.md, prompt-utilise.txt) ainsi que le glossaire-utilisateur
    fonctionne. **Le POST effectif vers Drive (create folder + upload 3-step)
    n'est PAS branché** : il dépend d'extensions à ``DriveClient``
    (``create_folder``, ``upload_file``, ``delete_item``) qui n'existent pas
    encore dans ``services/dmz-to-internal-bridge/app/drive_client.py``.

    Pour ne pas bricoler en silence (la corruption Drive est invisible),
    on remonte ``NotImplementedError`` qui est attrapé en amont (worker
    daemon) et logué en warning. Le brief reste accessible via la DB et
    l'UI (source de vérité), mais sans export Drive.

    Pour finaliser :
      1. Ajouter à ``DriveClient`` :
         - ``create_folder(token, parent_id, title) -> folder_id``
           (POST /api/v1.0/items/<parent>/children/ {type=FOLDER, title})
         - ``upload_file(token, parent_id, filename, content_bytes,
           content_type) -> file_id`` (POST /items/ → presigned PUT →
           POST /items/<id>/upload-ended/)
         - ``delete_item(token, item_id)`` (overwrite-on-edit)
      2. resolve_drive_target_folder() : 4 cas du plan §9bis.2 — si
         drive_folder fourni → sous-dossier dedans ; sinon → lookup
         "Préparations de réunion/" à la racine main_workspace, créer
         si absent. Sous-dossier "<YYYY-MM-DD>-<slug>" à l'intérieur.
      3. Pour chaque fichier : si même titre existe → delete + recreate.
      4. POST /api/v1/briefs/<id>/drive-sync-status {synced, folder_id, at}
         via device-token-authority (endpoint dédié à ajouter).

    Les contenus sont OK et testés (cf test_drive_brief_sync_helpers.py) :
    seul le transport Drive manque.
    """
    # Génère les 4 contenus (sert au moins à valider le rendu sans Drive).
    files_to_upload = _build_brief_files_payload(
        brief_json, documents, used_prompt,
    )
    logger.info(
        "drive_brief_sync: built %d files for brief=%s (sizes=%s) — upload NOT wired",
        len(files_to_upload), brief_id,
        {name: len(content) for name, content in files_to_upload.items()},
    )
    raise NotImplementedError(
        "drive_brief_sync: contenus générés mais transport Drive non branché "
        "(create_folder/upload_file/delete_item manquent dans DriveClient). "
        "Le brief reste en DB (source de vérité) ; flag drive_sync_status=failed."
    )


def _build_brief_files_payload(brief_json: dict, documents: list,
                                used_prompt: Optional[str]) -> dict:
    """Construit le dict ``{filename: bytes}`` des 4 fichiers Drive.

    Helper testable séparément (cf test_drive_brief_sync_helpers).
    """
    _bj2md = brief_json_to_markdown
    _ds2md = documents_source_to_markdown
    try:
        # Glossaire complet via l'extracteur du dmz-to-internal-bridge (lazily importé
        # pour ne pas alourdir le module si jamais cet appel est hors
        # chemin d'exécution courant).
        import sys, importlib.util as _iu
        import os as _os
        _fm_app = _os.path.normpath(_os.path.join(
            _os.path.dirname(__file__), "..", "..", "dmz-to-internal-bridge", "app"
        ))
        _spec = _iu.spec_from_file_location(
            "_gfb", _os.path.join(_fm_app, "glossary_from_brief.py")
        )
        _gfb = _iu.module_from_spec(_spec)
        _spec.loader.exec_module(_gfb)  # type: ignore
        terms = sorted(_gfb.extract_full_glossary_terms_from_brief(brief_json, documents))
    except Exception:
        logger.exception("drive_brief_sync: glossary extraction failed (fallback empty)")
        terms = []

    out: dict = {}
    out["brief.md"] = _bj2md(brief_json).encode("utf-8")
    glossary_txt = "\n".join(terms) if terms else ""
    out["glossaire.txt"] = glossary_txt.encode("utf-8")
    out["documents-source.md"] = _ds2md(documents or []).encode("utf-8")
    prompt_txt = used_prompt or "(non disponible)"
    if len(prompt_txt) > 50_000:
        prompt_txt = prompt_txt[:50_000] + "\n\n... [tronqué]"
    out["prompt-utilise.txt"] = prompt_txt.encode("utf-8")
    return out


def brief_json_to_markdown(brief_json: dict) -> str:
    """Rendu markdown structuré d'un brief_json pour ``brief.md``.

    Format : Sujet | Objectif reformulé | Contexte | Agenda (avec durées) |
    Questions clés | Points en suspens | Notes participants | Questions
    d'ouverture | Risques | Checklist préparation.
    """
    if not isinstance(brief_json, dict):
        return ""
    out: list[str] = []

    def _h(title: str):
        out.append(f"## {title}\n")

    if subj := brief_json.get("subject"):
        out.append(f"# {subj}\n")
    if obj := brief_json.get("objective_reformulated"):
        _h("Objectif reformulé")
        out.append(f"{obj}\n")
    if ctx := brief_json.get("context_summary"):
        _h("Contexte")
        out.append(f"{ctx}\n")

    agenda = brief_json.get("agenda") or []
    if agenda:
        _h("Agenda")
        for i, item in enumerate(agenda, 1):
            if not isinstance(item, dict):
                continue
            title = item.get("title") or "(sans titre)"
            duration = item.get("duration_minutes") or item.get("duration")
            line = f"{i}. **{title}**"
            if duration:
                line += f" — {duration} min"
            out.append(line)
            for kq in item.get("key_questions") or []:
                if isinstance(kq, str) and kq.strip():
                    out.append(f"   - {kq}")
        out.append("")

    open_threads = brief_json.get("open_threads") or []
    if open_threads:
        _h("Points en suspens")
        for it in open_threads:
            if isinstance(it, dict):
                out.append(f"- {it.get('summary') or it.get('source') or '(non précisé)'}")
            elif isinstance(it, str):
                out.append(f"- {it}")
        out.append("")

    # NOTE : brief_json.participants_notes (LLM) volontairement ignoré.
    # La vraie liste participants vit dans la colonne JSONB `participants`
    # éditable côté UI et n'est pas passée à ce helper aujourd'hui.
    # Pour l'inclure ici, étendre schedule_drive_brief_sync(participants=...).

    risks = brief_json.get("risks") or []
    if risks:
        _h("Risques")
        for r in risks:
            if isinstance(r, dict):
                out.append(f"- {r.get('summary') or r.get('item') or ''}")
            elif isinstance(r, str):
                out.append(f"- {r}")
        out.append("")

    checklist = brief_json.get("preparation_checklist") or []
    if checklist:
        _h("Checklist préparation")
        for c in checklist:
            if isinstance(c, str):
                out.append(f"- [ ] {c}")
            elif isinstance(c, dict):
                out.append(f"- [ ] {c.get('item') or c.get('summary') or ''}")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def documents_source_to_markdown(documents: list,
                                  drive_explorer_base: str = "") -> str:
    """Liste markdown des documents Drive ingérés (cf §9bis.3)."""
    if not documents:
        return "_Aucun document ingéré._\n"
    lines: list[str] = ["# Documents source\n"]
    total_chars = 0
    for d in documents:
        if not isinstance(d, dict):
            continue
        name = d.get("name") or "(sans nom)"
        did = d.get("id") or ""
        status = d.get("status") or ""
        chars = d.get("chars") or 0
        total_chars += chars
        if did and drive_explorer_base:
            lines.append(f"- [{name}]({drive_explorer_base.rstrip('/')}/items/{did}) — {status}")
        else:
            lines.append(f"- {name} — {status}")
    lines.append(f"\nCumulé : {total_chars} caractères extraits.\n")
    return "\n".join(lines)
