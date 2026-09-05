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

Trois contraintes gouvernent ce module :

- **Le Drive renomme silencieusement** un titre déjà pris (« brief_01.md »,
  avec un 201 quand même). D'où l'écrasement explicite — lookup, suppression,
  dépôt — fichier par fichier et juste avant le dépôt du même nom.
- **Rien ne doit remonter au caller.** La préparation est persistée AVANT que
  la synchro soit planifiée ; le thread est daemon et ne lève jamais. Un Drive
  absent donne ``skipped``, pas ``failed`` : proposer un « Réessayer » qui ne
  peut pas aboutir est pire que ne rien afficher.
- **Le worker tient un refresh token déchiffré en mémoire.** D'où la deadline :
  un Drive qui pend ne doit pas garder ce secret — ni un thread — indéfiniment.

Tous les imports applicatifs sont paresseux : ce fichier est chargé par chemin
absolu dans les tests (sans package ``app``), et l'échec d'import doit donner
un ``skipped`` propre plutôt qu'un crash au chargement du module.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import unicodedata
from datetime import date, datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Budget de temps du worker. Au-delà, on abandonne : le versement Drive est un
# export best-effort, il ne justifie ni un thread immortel ni la rétention d'un
# refresh token déchiffré.
_SYNC_DEADLINE_SECONDS = 120

# Dossier géré, créé à la racine du workspace personnel quand l'utilisateur
# n'a pas désigné de dossier source.
_ROOT_FOLDER_TITLE = "Préparations de réunion"
_USER_GLOSSARY_FILENAME = "glossaire-utilisateur.txt"
_USER_GLOSSARY_CAP = 300

# Verrou par utilisateur autour de la résolution des dossiers : deux briefs
# générés coup sur coup créeraient sinon deux « Préparations de réunion ».
# Il ne couvre QUE ce process (mesreunions-web tourne en plusieurs replicas) —
# la réconciliation lecture-après-écriture traite le reste.
_user_locks: "dict[str, threading.Lock]" = {}
_user_locks_guard = threading.Lock()

_CONTENT_TYPES = {".md": "text/markdown", ".txt": "text/plain"}


class DriveSyncSkipped(Exception):
    """Aucun Drive exploitable pour cet utilisateur — état normal, pas un échec."""


class DriveSyncDeadline(Exception):
    """Budget de temps épuisé — le versement est abandonné, la DB fait foi."""


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
    except DriveSyncSkipped as exc:
        # Pas de Drive configuré, pas de jeton, pas de workspace : rien à
        # réessayer. `skipped` le dit à l'UI, `failed` lui ferait afficher un
        # bouton qui n'aboutirait jamais.
        logger.info("drive_brief_sync: versement ignoré pour brief=%s — %s", brief_id, exc)
        _report_status(user_sub, brief_id, "skipped")
    except DriveSyncDeadline as exc:
        logger.warning("drive_brief_sync: brief=%s — %s", brief_id, exc)
        _report_status(user_sub, brief_id, "failed")
    except Exception as exc:
        if _is_drive_auth_error(exc):
            # État catalogué (jeton Drive expiré ou refusé) : une ligne suffit,
            # la stacktrace ferait croire à un crash de fin de cycle.
            logger.warning(
                "drive_brief_sync: accès Drive refusé pour brief=%s — %s", brief_id, exc,
            )
        else:
            logger.exception(
                "drive_brief_sync: failed for brief=%s user=%s (best-effort)",
                brief_id, user_sub,
            )
        _report_status(user_sub, brief_id, "failed")


def _do_sync(user_sub, brief_id, brief_json, documents, used_prompt,
              drive_folder_id):
    """Versement effectif : résolution du dossier puis dépôt fichier par fichier."""
    deadline = time.monotonic() + _SYNC_DEADLINE_SECONDS
    files_to_upload = _build_brief_files_payload(brief_json, documents, used_prompt)

    drive, access_token = _open_drive_session(user_sub)
    state = _load_prep_drive_state(user_sub, brief_id)
    _report_status(user_sub, brief_id, "pending")

    def _persist(**ids):
        # Chaque id est écrit DÈS qu'il est connu : une interruption après la
        # création d'un dossier ne doit pas conduire à en créer un second au
        # versement suivant.
        _report_status(user_sub, brief_id, "pending", **ids)

    prep_folder_id, root_folder_id = _resolve_target_folder(
        drive, access_token, user_sub, brief_json, state, drive_folder_id,
        persist=_persist, deadline=deadline,
    )

    for filename, content in files_to_upload.items():
        _check_deadline(deadline)
        _overwrite_file(drive, access_token, prep_folder_id, filename, content)

    if root_folder_id:
        _sync_user_glossary(drive, access_token, root_folder_id, user_sub, deadline)

    logger.info(
        "drive_brief_sync: %d fichiers versés pour brief=%s dans folder=%s",
        len(files_to_upload), brief_id, prep_folder_id,
    )
    _report_status(
        user_sub, brief_id, "synced",
        drive_prep_folder_id=prep_folder_id,
        drive_prep_root_folder_id=root_folder_id,
    )


# ─── Résolution du dossier cible ────────────────────────────────────


def _resolve_target_folder(drive, access_token, user_sub, brief_json, state,
                           drive_folder_id, *, persist, deadline):
    """Retourne ``(dossier de la préparation, dossier géré ou None)``.

    Ordre : cache → dossier source fourni par l'utilisateur → dossier géré
    « Préparations de réunion » sous le workspace personnel.
    """
    cached_prep = (state.get("drive_prep_folder_id") or "").strip()
    cached_root = (state.get("drive_prep_root_folder_id") or "").strip() or None
    if cached_prep and _folder_still_there(drive, access_token, cached_prep):
        return cached_prep, cached_root

    title = _prep_folder_title(state, brief_json)
    with _lock_for(user_sub):
        _check_deadline(deadline)
        if drive_folder_id:
            try:
                prep_id = _lookup_or_create_folder(
                    drive, access_token, drive_folder_id, title,
                )
                persist(drive_prep_folder_id=prep_id)
                return prep_id, cached_root
            except Exception as exc:
                if not _is_drive_access_refused(exc):
                    raise
                # Dossier source partagé en lecture seule (ou disparu) :
                # l'utilisateur veut son export, pas un message d'erreur sur
                # un dossier qui n'est pas le sien.
                logger.info(
                    "drive_brief_sync: écriture refusée dans le dossier source %s "
                    "(%s) — bascule sur le dossier géré", drive_folder_id, exc,
                )

        _check_deadline(deadline)
        root_id = _resolve_managed_root(drive, access_token, cached_root)
        persist(drive_prep_root_folder_id=root_id)
        _check_deadline(deadline)
        prep_id = _lookup_or_create_folder(drive, access_token, root_id, title)
        persist(drive_prep_folder_id=prep_id, drive_prep_root_folder_id=root_id)
        return prep_id, root_id


def _resolve_managed_root(drive, access_token, cached_root):
    """Id du dossier « Préparations de réunion », créé au besoin."""
    if cached_root and _folder_still_there(drive, access_token, cached_root):
        return cached_root
    workspace_id = _main_workspace_id(drive, access_token)
    return _lookup_or_create_folder(
        drive, access_token, workspace_id, _ROOT_FOLDER_TITLE,
    )


def _main_workspace_id(drive, access_token) -> str:
    """Racine personnelle de l'utilisateur (``main_workspace``).

    Les autres racines sont des partages : y créer un dossier peut être
    refusé, et le résultat n'appartiendrait pas à l'utilisateur.
    """
    roots = drive.list_roots(access_token) or []
    for item in roots:
        if item.get("main_workspace") and item.get("id"):
            return item["id"]
    raise DriveSyncSkipped(
        "aucun workspace personnel dans les racines du Drive"
    )


def _lookup_or_create_folder(drive, access_token, parent_id: str, title: str) -> str:
    """Cherche le dossier ``title`` sous ``parent_id``, le crée sinon.

    Réconciliation lecture-après-écriture derrière la création : le verrou
    par utilisateur ne couvre qu'un process, or le service tourne en
    plusieurs replicas. Deux créations concurrentes du même titre donnent
    deux dossiers sans la moindre erreur — le second est renommé
    « <titre>_01 ». Relire par titre exact désigne donc le survivant, et le
    perdant supprime le sien.
    """
    existing = drive.find_child_by_title(access_token, parent_id, title,
                                          item_type="folder")
    if existing and existing.get("id"):
        return existing["id"]

    created = drive.create_folder(access_token, parent_id, title)
    created_id = created.get("id")
    winner = drive.find_child_by_title(access_token, parent_id, title,
                                        item_type="folder")
    winner_id = (winner or {}).get("id")
    if winner_id and winner_id != created_id:
        logger.info(
            "drive_brief_sync: création concurrente de '%s' — on garde %s et "
            "on supprime %s", title, winner_id, created_id,
        )
        try:
            drive.delete_item(access_token, created_id)
        except Exception as exc:
            logger.warning("drive_brief_sync: doublon %s non supprimé: %s", created_id, exc)
        return winner_id
    return created_id


def _folder_still_there(drive, access_token, item_id: str) -> bool:
    """Le dossier mémorisé existe-t-il encore ? (l'utilisateur peut l'avoir jeté)"""
    try:
        drive.get_item(access_token, item_id)
        return True
    except Exception as exc:
        if _is_drive_access_refused(exc):
            logger.info("drive_brief_sync: dossier mémorisé %s inutilisable (%s)", item_id, exc)
            return False
        raise


# ─── Dépôt des fichiers ─────────────────────────────────────────────


def _overwrite_file(drive, access_token, folder_id: str, filename: str,
                    content: bytes) -> None:
    """Remplace ``filename`` dans ``folder_id`` : suppression puis dépôt.

    La suppression a lieu juste avant le dépôt du même nom, jamais en passe
    groupée : le Drive renomme silencieusement un homonyme en
    « brief_01.md », et après quelques éditions l'utilisateur ne sait plus
    lequel fait foi. La suppression est douce (corbeille 30 j) et ces
    fichiers sont intégralement re-dérivables de la base.
    """
    existing = drive.find_child_by_title(access_token, folder_id, filename,
                                          item_type="file")
    if existing and existing.get("id"):
        drive.delete_item(access_token, existing["id"])
    drive.upload_file(access_token, folder_id, filename, content,
                      content_type=_content_type_for(filename))


def _sync_user_glossary(drive, access_token, root_folder_id: str, user_sub: str,
                        deadline) -> None:
    """Dépose le glossaire global de l'utilisateur à la racine des préparations.

    Best-effort dans le best-effort : ce fichier transverse ne doit pas faire
    échouer le versement du brief lui-même, déjà déposé à ce stade.
    """
    try:
        _check_deadline(deadline)
        terms = _load_user_glossary_terms(user_sub)
        if not terms:
            return
        payload = "\n".join(terms).encode("utf-8")
        _overwrite_file(drive, access_token, root_folder_id,
                        _USER_GLOSSARY_FILENAME, payload)
    except Exception:
        logger.warning(
            "drive_brief_sync: %s non mis à jour (non bloquant)",
            _USER_GLOSSARY_FILENAME, exc_info=True,
        )


# ─── Accès à l'environnement mesreunions-web (imports paresseux) ────


_drive_module_cache: list = []


def _drive_client_module():
    """Charge ``drive_client`` du dmz-to-internal-bridge, par chemin absolu.

    Même mécanique que ``meeting_prep`` : l'image de production co-localise
    tous les services sous ``/app/services``.
    """
    if _drive_module_cache:
        return _drive_module_cache[0]
    import importlib.util as _iu
    path = os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "dmz-to-internal-bridge", "app",
        "drive_client.py",
    ))
    spec = _iu.spec_from_file_location("_drive_brief_sync_drive_client", path)
    if spec is None or spec.loader is None:
        raise DriveSyncSkipped(f"drive_client introuvable ({path})")
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _drive_module_cache.append(mod)
    return mod


def _open_drive_session(user_sub: str):
    """Retourne ``(DriveClient, access_token)``.

    ``DriveSyncSkipped`` dès qu'il manque une pièce non fautive (Drive non
    configuré, utilisateur sans jeton). Un refus de Keycloak, lui, remonte en
    ``DriveAuthError`` → statut ``failed``.
    """
    mod = _drive_client_module()
    try:
        from libs.shared.app.config import DRIVE_BASE_URL, OIDC_TOKEN_ENDPOINT
        from libs.shared.app.oidc_refresh_store import fetch_ciphertext
        from libs.shared.app.secrets_crypto import decrypt as decrypt_secret
        from app.main import oidc_cfg
    except Exception as exc:
        raise DriveSyncSkipped(f"environnement mesreunions-web indisponible: {exc}") from exc

    if not DRIVE_BASE_URL or not OIDC_TOKEN_ENDPOINT:
        raise DriveSyncSkipped("DRIVE_BASE_URL ou OIDC_TOKEN_ENDPOINT non configuré")
    ciphertext = fetch_ciphertext(user_sub)
    if not ciphertext:
        raise DriveSyncSkipped("aucun jeton Drive enregistré pour cet utilisateur")

    refresh_token = decrypt_secret(ciphertext)
    drive = mod.DriveClient(
        base_url=DRIVE_BASE_URL,
        oidc_token_endpoint=OIDC_TOKEN_ENDPOINT,
        oidc_client_id=oidc_cfg.client_id,
        oidc_client_secret=oidc_cfg.client_secret,
    )
    return drive, drive.exchange_refresh(refresh_token)


def _load_prep_drive_state(user_sub: str, brief_id: str) -> dict:
    """Ids de dossiers déjà connus + titre/date de la préparation.

    ``track_view=false`` est impératif : lire une préparation pour la verser
    ne doit pas compter comme une consultation, sans quoi le scoring
    d'auto-link avec un audio serait faussé par un robot.
    """
    try:
        from app.modules.preparations.service import get_preparation_drive_state
        return get_preparation_drive_state(user_sub, brief_id) or {}
    except Exception:
        logger.warning(
            "drive_brief_sync: état Drive de brief=%s illisible — on repart du cache vide",
            brief_id, exc_info=True,
        )
        return {}


def _report_status(user_sub: str, brief_id: str, status: str, **ids) -> None:
    """Publie l'état de synchro. Best-effort : un échec ici n'annule rien."""
    try:
        from app.modules.preparations.service import set_drive_sync_status
        set_drive_sync_status(user_sub, brief_id, status, **ids)
    except Exception:
        logger.warning(
            "drive_brief_sync: statut '%s' non publié pour brief=%s",
            status, brief_id, exc_info=True,
        )


def _load_user_glossary_terms(user_sub: str) -> list:
    """Glossaire global de l'utilisateur, 1 terme par ligne, fréquence desc."""
    from app.shared import request_internal_device_api
    body = request_internal_device_api(
        "GET", "/api/v1/user-glossary",
        params={"user_sub": user_sub, "limit": _USER_GLOSSARY_CAP},
    ) or {}
    return [
        t["term"] for t in (body.get("terms") or [])
        if isinstance(t, dict) and t.get("term")
    ]


# ─── Petits utilitaires ─────────────────────────────────────────────


def _lock_for(user_sub: str) -> threading.Lock:
    with _user_locks_guard:
        return _user_locks.setdefault(user_sub or "-", threading.Lock())


def _check_deadline(deadline) -> None:
    if time.monotonic() > deadline:
        raise DriveSyncDeadline(
            f"versement Drive abandonné après {_SYNC_DEADLINE_SECONDS}s"
        )


def _is_drive_auth_error(exc) -> bool:
    mod = _drive_module_cache[0] if _drive_module_cache else None
    return mod is not None and isinstance(exc, mod.DriveAuthError)


def _is_drive_access_refused(exc) -> bool:
    """Refus portant sur UNE ressource : dossier d'autrui, ou disparu.

    Un 401 en est exclu : il dit que notre jeton ne vaut rien du tout, et
    basculer ailleurs ne ferait qu'échouer à nouveau, plus loin.
    """
    mod = _drive_module_cache[0] if _drive_module_cache else None
    if mod is None:
        return False
    if isinstance(exc, mod.DriveApplicativeError):
        return True
    return isinstance(exc, mod.DriveAuthError) and getattr(exc, "status_code", None) == 403


def _content_type_for(filename: str) -> str:
    _, ext = os.path.splitext(filename.lower())
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def _slugify(value: str, *, max_length: int = 60) -> str:
    """Slug ASCII pour un nom de dossier Drive."""
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    return slug[:max_length].strip("-") or "brief"


def _prep_folder_title(state: dict, brief_json: dict) -> str:
    """« <YYYY-MM-DD>-<slug> » — la date de la réunion, pas celle du versement."""
    raw_date = (state.get("target_meeting_date") or state.get("created_at") or "")
    day = raw_date[:10] if isinstance(raw_date, str) and len(raw_date) >= 10 else ""
    try:
        date.fromisoformat(day)
    except ValueError:
        day = datetime.now(timezone.utc).date().isoformat()
    subject = (
        state.get("title") or state.get("subject")
        or (brief_json or {}).get("subject") or ""
    )
    return f"{day}-{_slugify(subject)}"


def _build_brief_files_payload(brief_json: dict, documents: list,
                                used_prompt: Optional[str]) -> dict:
    """Construit le dict ``{filename: bytes}`` des fichiers Drive.

    Helper testable séparément (cf test_drive_brief_sync_helpers).

    ``prompt-utilise.txt`` n'est émis que si le prompt est disponible : il ne
    l'est pas au réessai (il n'est pas persisté), et un placeholder écraserait
    alors le fichier valide déposé à la première génération.
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
    if used_prompt:
        prompt_txt = used_prompt
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

    recos = brief_json.get("ai_recommendations") or []
    reco_lines: list[str] = []
    for r in recos:
        if not isinstance(r, dict):
            continue
        suggestion = (r.get("suggestion") or "").strip()
        if not suggestion:
            continue
        rationale = (r.get("rationale") or "").strip()
        reco_lines.append(f"- {suggestion} _({rationale})_" if rationale else f"- {suggestion}")
    if reco_lines:
        _h("Recommandations")
        out.append("_Suggestions issues de la recherche sur les réunions — à prendre ou à laisser._")
        out.extend(reco_lines)
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
