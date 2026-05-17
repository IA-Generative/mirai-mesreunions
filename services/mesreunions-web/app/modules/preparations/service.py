"""Service preparations — logique métier brief amont.

Encapsule les appels device-token-authority spécifiques aux préparations.
Garde un wrapper sémantique au-dessus de ``request_internal_device_api`` pour
faciliter le test unitaire (un seul point de mock par module).
"""

from __future__ import annotations

import logging

from ...shared import request_internal_preparation_api

logger = logging.getLogger("mesreunions_web.preparations")


def list_preparations(user_sub: str, *, with_counts: bool = False) -> dict:
    """Liste les préparations actives (50 dernières)."""
    if with_counts:
        return request_internal_preparation_api(
            "GET", "/api/v1/preparations/list-with-counts",
            params={"user_sub": user_sub, "limit": 50},
        )
    return request_internal_preparation_api(
        "GET", "/api/v1/preparations",
        params={"user_sub": user_sub, "limit": 50, "trashed": "false"},
    )


def get_preparation(user_sub: str, preparation_id: str) -> dict:
    return request_internal_preparation_api(
        "GET", f"/api/v1/preparations/{preparation_id}",
        params={"user_sub": user_sub},
    )


def create_preparation(payload: dict) -> dict:
    return request_internal_preparation_api(
        "POST", "/api/v1/preparations", json_body=payload,
    )


_SENTINEL = object()


def amend_preparation(
    user_sub: str,
    preparation_id: str,
    content: dict | None = None,
    *,
    participants: list | None = None,
    glossary_source: list | None = None,
    is_recurring: bool | None = None,
    recurrence_rule: dict | None | object = _SENTINEL,
    target_meeting_date: str | None | object = _SENTINEL,
    themes: list | None = None,
    send_cr_email: bool | None = None,
    drive_main_courante_doc_id: str | None | object = _SENTINEL,
) -> dict:
    """Amend une préparation — `content`, `participants`, `glossary_source`
    et/ou les champs de récurrence (Lot 6).

    Au moins un champ doit être fourni. ``recurrence_rule`` accepte
    explicitement ``None`` (désactivation) ; on utilise un sentinel pour
    différencier "non fourni" de "fourni à None".
    """
    body: dict = {"user_sub": user_sub}
    if content is not None:
        body["content"] = content
    if participants is not None:
        body["participants"] = participants
    if glossary_source is not None:
        body["glossary_source"] = glossary_source
    if is_recurring is not None:
        body["is_recurring"] = bool(is_recurring)
    if recurrence_rule is not _SENTINEL:
        body["recurrence_rule"] = recurrence_rule
    if target_meeting_date is not _SENTINEL:
        body["target_meeting_date"] = target_meeting_date
    if themes is not None:
        body["themes"] = themes
    if send_cr_email is not None:
        body["send_cr_email"] = bool(send_cr_email)
    if drive_main_courante_doc_id is not _SENTINEL:
        body["drive_main_courante_doc_id"] = drive_main_courante_doc_id
    return request_internal_preparation_api(
        "POST", f"/api/v1/preparations/{preparation_id}/amend",
        json_body=body,
    )


def rename_preparation(user_sub: str, preparation_id: str, title: str) -> dict:
    return request_internal_preparation_api(
        "POST", f"/api/v1/preparations/{preparation_id}/rename",
        json_body={"user_sub": user_sub, "title": title},
    )


def trash_preparation(user_sub: str, preparation_id: str) -> dict:
    return request_internal_preparation_api(
        "DELETE", f"/api/v1/preparations/{preparation_id}",
        json_body={"user_sub": user_sub},
    )


def restore_preparation(user_sub: str, preparation_id: str) -> dict:
    return request_internal_preparation_api(
        "POST", f"/api/v1/preparations/{preparation_id}/restore",
        json_body={"user_sub": user_sub},
    )


def hard_delete_preparation(user_sub: str, preparation_id: str) -> dict:
    return request_internal_preparation_api(
        "DELETE", f"/api/v1/preparations/{preparation_id}/permanently",
        json_body={"user_sub": user_sub},
    )


def audio_files_for(user_sub: str, preparation_id: str) -> dict:
    return request_internal_preparation_api(
        "GET", f"/api/v1/preparations/{preparation_id}/audio-files",
        params={"user_sub": user_sub},
    )


def series_for(user_sub: str, preparation_id: str) -> dict:
    return request_internal_preparation_api(
        "GET", f"/api/v1/preparations/{preparation_id}/series",
        params={"user_sub": user_sub},
    )
