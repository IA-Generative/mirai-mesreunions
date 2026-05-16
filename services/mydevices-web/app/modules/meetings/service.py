"""Service meetings — wrappers HTTP vers device-token-authority."""

from __future__ import annotations

from ...shared import request_internal_meeting_api


def list_meetings(user_sub: str, *, trashed: str = "false", limit: int = 50) -> dict:
    return request_internal_meeting_api(
        "GET", "/api/v1/meetings",
        params={"user_sub": user_sub, "limit": limit, "trashed": trashed},
    )


def get_meeting(user_sub: str, meeting_id: str) -> dict:
    return request_internal_meeting_api(
        "GET", f"/api/v1/meetings/{meeting_id}",
        params={"user_sub": user_sub},
    )


def create_meeting(payload: dict) -> dict:
    return request_internal_meeting_api(
        "POST", "/api/v1/meetings", json_body=payload,
    )


def amend_meeting(user_sub: str, meeting_id: str, content: dict) -> dict:
    return request_internal_meeting_api(
        "POST", f"/api/v1/meetings/{meeting_id}/amend",
        json_body={"user_sub": user_sub, "content": content},
    )


def rename_meeting(user_sub: str, meeting_id: str, title: str) -> dict:
    return request_internal_meeting_api(
        "POST", f"/api/v1/meetings/{meeting_id}/rename",
        json_body={"user_sub": user_sub, "title": title},
    )


def trash_meeting(user_sub: str, meeting_id: str) -> dict:
    return request_internal_meeting_api(
        "DELETE", f"/api/v1/meetings/{meeting_id}",
        json_body={"user_sub": user_sub},
    )


def restore_meeting(user_sub: str, meeting_id: str) -> dict:
    return request_internal_meeting_api(
        "POST", f"/api/v1/meetings/{meeting_id}/restore",
        json_body={"user_sub": user_sub},
    )


def hard_delete_meeting(user_sub: str, meeting_id: str) -> dict:
    return request_internal_meeting_api(
        "DELETE", f"/api/v1/meetings/{meeting_id}/permanently",
        json_body={"user_sub": user_sub},
    )


def link_preparation(user_sub: str, meeting_id: str, preparation_id: str | None) -> dict:
    return request_internal_meeting_api(
        "POST", f"/api/v1/meetings/{meeting_id}/link-preparation",
        json_body={"user_sub": user_sub, "preparation_id": preparation_id},
    )


def link_audio(user_sub: str, meeting_id: str, file_id: str | None) -> dict:
    return request_internal_meeting_api(
        "POST", f"/api/v1/meetings/{meeting_id}/link-audio",
        json_body={"user_sub": user_sub, "file_id": file_id},
    )
