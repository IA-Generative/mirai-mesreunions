"""Tests d'orchestration du versement Drive (lot D du chantier « sources du brief »).

Le rendu des contenus est couvert par ``test_drive_brief_sync_helpers`` ; on
teste ici la séquence : résolution du dossier cible, écrasement fichier par
fichier, publication des statuts, et les modes dégradés (pas de Drive, jeton
refusé, deadline).

Le Drive est remplacé par un faux client en mémoire qui reproduit les deux
comportements décisifs de l'instance réelle : un titre déjà pris est renommé
« <titre>_01 » sans erreur, et un dossier d'autrui refuse l'écriture en 403.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DBS_PATH = os.path.join(ROOT, "services", "mesreunions-web", "app", "drive_brief_sync.py")
DRIVE_CLIENT_PATH = os.path.join(
    ROOT, "services", "dmz-to-internal-bridge", "app", "drive_client.py",
)


def _load(alias, path):
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dbs = _load("dbs_pipeline_under_test", DBS_PATH)
dc = _load("drive_client_for_pipeline", DRIVE_CLIENT_PATH)


class FakeDrive:
    """Drive en mémoire — conserve les invariants qui font mal en vrai."""

    def __init__(self, *, roots=None):
        self.items: dict = {}
        self.calls: list = []
        self.uploads: list = []
        self.deleted: list = []
        self.readonly_parents: set = set()
        self._seq = 0
        for root in roots or [{"id": "ws", "title": "Mon espace", "main_workspace": True}]:
            self.items[root["id"]] = dict(root, type="folder", parent=None, deleted=False)

    # -- helpers internes ------------------------------------------------
    def _new_id(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def _children(self, parent_id):
        return [it for it in self.items.values()
                if it.get("parent") == parent_id and not it["deleted"]]

    def _unique_title(self, parent_id, title):
        """Le Drive renomme silencieusement un homonyme, avec un 201 quand même."""
        taken = {it["title"] for it in self._children(parent_id)}
        if title not in taken:
            return title
        base, ext = os.path.splitext(title)
        return f"{base}_01{ext}"

    # -- surface DriveClient --------------------------------------------
    def list_roots(self, _token):
        self.calls.append(("list_roots",))
        return [it for it in self.items.values() if it.get("parent") is None]

    def get_item(self, _token, item_id):
        self.calls.append(("get_item", item_id))
        item = self.items.get(item_id)
        if not item or item["deleted"]:
            raise dc.DriveApplicativeError(f"items/{item_id} → 404")
        return item

    def find_child_by_title(self, _token, parent_id, title, *, item_type=None):
        self.calls.append(("find", parent_id, title))
        for item in self._children(parent_id):
            if item["title"] == title and (item_type is None or item["type"] == item_type):
                return item
        return None

    def create_folder(self, _token, parent_id, title):
        self.calls.append(("create_folder", parent_id, title))
        if parent_id in self.readonly_parents:
            raise dc.DriveAuthError("read only", status_code=403)
        item = {"id": self._new_id("folder"), "title": self._unique_title(parent_id, title),
                "type": "folder", "parent": parent_id, "deleted": False}
        self.items[item["id"]] = item
        return item

    def upload_file(self, _token, parent_id, filename, content, content_type=None):
        self.calls.append(("upload", parent_id, filename))
        if parent_id in self.readonly_parents:
            raise dc.DriveAuthError("read only", status_code=403)
        item = {"id": self._new_id("file"), "title": self._unique_title(parent_id, filename),
                "type": "file", "parent": parent_id, "deleted": False}
        self.items[item["id"]] = item
        self.uploads.append((parent_id, item["title"], content))
        return item

    def delete_item(self, _token, item_id):
        self.calls.append(("delete", item_id))
        self.deleted.append(item_id)
        if item_id in self.items:
            self.items[item_id]["deleted"] = True
        return True


@pytest.fixture
def wired(monkeypatch):
    """Câble le module sur un faux Drive et capture les statuts publiés."""
    drive = FakeDrive()
    statuses: list = []
    state: dict = {}

    monkeypatch.setattr(dbs, "_drive_module_cache", [dc])
    monkeypatch.setattr(dbs, "_open_drive_session", lambda user_sub: (drive, "AT"))
    monkeypatch.setattr(dbs, "_load_prep_drive_state", lambda user_sub, brief_id: state)
    monkeypatch.setattr(dbs, "_load_user_glossary_terms", lambda user_sub: [])
    monkeypatch.setattr(
        dbs, "_report_status",
        lambda user_sub, brief_id, status, **ids: statuses.append((status, ids)),
    )
    return drive, statuses, state


BRIEF = {"subject": "COPIL DTNUM Été 2026"}


def _sync(**kwargs):
    params = {
        "user_sub": "user-a",
        "brief_id": "brief-1",
        "brief_json": BRIEF,
        "documents": [],
        "used_prompt": "PROMPT",
        "drive_folder_id": None,
    }
    params.update(kwargs)
    return dbs._do_sync(
        params["user_sub"], params["brief_id"], params["brief_json"],
        params["documents"], params["used_prompt"], params["drive_folder_id"],
    )


# ─── Résolution du dossier ──────────────────────────────────────────


def test_creates_the_managed_root_then_the_prep_folder(wired):
    drive, statuses, state = wired
    state.update({"title": "COPIL DTNUM", "target_meeting_date": "2026-09-15T09:00:00+00:00"})

    _sync()

    folders = {it["title"]: it for it in drive.items.values() if it["type"] == "folder"}
    assert "Préparations de réunion" in folders
    assert "2026-09-15-copil-dtnum" in folders
    prep = folders["2026-09-15-copil-dtnum"]
    assert prep["parent"] == folders["Préparations de réunion"]["id"]


def test_reuses_the_cached_prep_folder_without_touching_the_root(wired):
    """Sans ce cache, chaque versement recréerait un dossier — ou pire, un doublon."""
    drive, statuses, state = wired
    cached = drive.create_folder("AT", "ws", "2026-09-15-copil")
    drive.calls.clear()
    state.update({"drive_prep_folder_id": cached["id"],
                  "drive_prep_root_folder_id": "ws"})

    _sync()

    assert not any(c[0] == "create_folder" for c in drive.calls)
    assert all(parent == cached["id"] for parent, _name, _c in drive.uploads)


def test_falls_back_to_the_managed_root_when_the_source_folder_is_read_only(wired):
    """L'utilisateur veut son export, pas une erreur sur un dossier d'autrui."""
    drive, statuses, state = wired
    drive.items["shared"] = {"id": "shared", "title": "Partagé", "type": "folder",
                             "parent": None, "deleted": False}
    drive.readonly_parents.add("shared")

    _sync(drive_folder_id="shared")

    titles = {it["title"] for it in drive.items.values() if it["type"] == "folder"}
    assert "Préparations de réunion" in titles
    assert drive.uploads and all(parent != "shared" for parent, _n, _c in drive.uploads)


def test_creates_the_prep_folder_inside_the_source_folder_when_writable(wired):
    drive, statuses, state = wired
    drive.items["src"] = {"id": "src", "title": "Dossier projet", "type": "folder",
                          "parent": None, "deleted": False}

    _sync(drive_folder_id="src")

    prep_parents = {it["parent"] for it in drive.items.values()
                    if it["type"] == "folder" and it["parent"] == "src"}
    assert prep_parents == {"src"}
    assert "Préparations de réunion" not in {it["title"] for it in drive.items.values()}


def test_concurrent_creation_keeps_the_survivor_and_deletes_its_own(wired):
    """Deux replicas créant le même titre donnent deux dossiers, sans erreur."""
    drive, statuses, state = wired
    # Le concurrent gagne la course juste après notre lookup : notre création
    # est donc renommée « <titre>_01 » par le Drive.
    original_find = drive.find_child_by_title
    seen = {"n": 0}

    def racing_find(token, parent_id, title, *, item_type=None):
        seen["n"] += 1
        if seen["n"] == 1 and title == "Préparations de réunion":
            # Le concurrent crée le dossier entre notre lookup et notre
            # création : la nôtre sera renommée « _01 » sans erreur.
            drive.items["concurrent"] = {"id": "concurrent", "title": title,
                                         "type": "folder", "parent": "ws",
                                         "deleted": False}
            return None
        return original_find(token, parent_id, title, item_type=item_type)

    drive.find_child_by_title = racing_find

    _sync()

    ours = [it for it in drive.items.values()
            if it["title"] == "Préparations de réunion_01"]
    assert ours and ours[0]["deleted"], "notre doublon doit être supprimé"
    prep_parents = {drive.items[parent]["parent"] for parent, _n, _c in drive.uploads}
    assert prep_parents == {"concurrent"}, "le versement doit viser le survivant"


def test_recreates_the_prep_folder_when_the_cached_one_was_trashed(wired):
    drive, statuses, state = wired
    state.update({"drive_prep_folder_id": "disparu"})

    _sync()

    assert drive.uploads
    assert all(parent != "disparu" for parent, _n, _c in drive.uploads)


# ─── Dépôt des fichiers ─────────────────────────────────────────────


def test_uploads_the_generated_files(wired):
    drive, statuses, state = wired

    _sync()

    names = {name for _parent, name, _content in drive.uploads}
    assert {"brief.md", "glossaire.txt", "documents-source.md",
            "prompt-utilise.txt"} <= names


def test_overwrites_by_deleting_the_previous_file_first(wired):
    """Sans suppression, le Drive renomme en brief_01.md et personne ne sait plus."""
    drive, statuses, state = wired
    _sync()
    first_ids = [i for i, it in drive.items.items() if it["title"] == "brief.md"]
    drive.deleted.clear()

    _sync()

    assert first_ids[0] in drive.deleted
    briefs = [it for it in drive.items.values()
              if it["title"].startswith("brief") and not it["deleted"]]
    assert [it["title"] for it in briefs] == ["brief.md"]


def test_retry_without_prompt_does_not_overwrite_the_stored_prompt(wired):
    """`used_prompt` est None au réessai : ne rien émettre plutôt qu'un placeholder."""
    drive, statuses, state = wired
    _sync()
    state.update({"drive_prep_folder_id": drive.uploads[0][0]})
    drive.uploads.clear()

    _sync(used_prompt=None)

    assert "prompt-utilise.txt" not in {name for _p, name, _c in drive.uploads}
    stored = [it for it in drive.items.values()
              if it["title"] == "prompt-utilise.txt" and not it["deleted"]]
    assert len(stored) == 1


def test_user_glossary_is_written_at_the_root_of_the_preparations_folder(wired, monkeypatch):
    drive, statuses, state = wired
    monkeypatch.setattr(dbs, "_load_user_glossary_terms", lambda user_sub: ["DTNUM", "MirAI"])

    _sync()

    root = next(it for it in drive.items.values()
                if it["title"] == "Préparations de réunion")
    glossary = [(parent, content) for parent, name, content in drive.uploads
                if name == "glossaire-utilisateur.txt"]
    assert glossary == [(root["id"], b"DTNUM\nMirAI")]


def test_user_glossary_failure_does_not_fail_the_sync(wired, monkeypatch):
    drive, statuses, state = wired

    def boom(_user_sub):
        raise RuntimeError("glossaire indisponible")

    monkeypatch.setattr(dbs, "_load_user_glossary_terms", boom)

    _sync()

    assert statuses[-1][0] == "synced"


# ─── Statuts publiés ────────────────────────────────────────────────


def test_publishes_pending_then_synced_with_the_folder_ids(wired):
    drive, statuses, state = wired

    _sync()

    assert statuses[0] == ("pending", {})
    final_status, ids = statuses[-1]
    assert final_status == "synced"
    assert ids["drive_prep_folder_id"]
    assert ids["drive_prep_root_folder_id"]
    # Les ids sont publiés AVANT le dépôt, pas seulement à la fin.
    assert any(s == "pending" and ids_.get("drive_prep_root_folder_id")
               for s, ids_ in statuses)


# ─── Modes dégradés ─────────────────────────────────────────────────


def test_no_drive_token_reports_skipped_not_failed(monkeypatch):
    """Un « Réessayer » sur un utilisateur sans Drive n'aboutirait jamais."""
    statuses = []
    monkeypatch.setattr(dbs, "_report_status",
                        lambda u, b, s, **ids: statuses.append((s, ids)))

    def no_session(_user_sub):
        raise dbs.DriveSyncSkipped("aucun jeton Drive enregistré pour cet utilisateur")

    monkeypatch.setattr(dbs, "_open_drive_session", no_session)

    dbs._drive_brief_sync_worker("user-a", "brief-1", BRIEF, [], "P", None)

    assert statuses == [("skipped", {})]


def test_drive_auth_error_reports_failed_without_a_stacktrace(monkeypatch, caplog):
    statuses = []
    monkeypatch.setattr(dbs, "_drive_module_cache", [dc])
    monkeypatch.setattr(dbs, "_report_status",
                        lambda u, b, s, **ids: statuses.append((s, ids)))

    def refused(_user_sub):
        raise dc.DriveAuthError("refresh rejeté", status_code=401)

    monkeypatch.setattr(dbs, "_open_drive_session", refused)

    with caplog.at_level("WARNING"):
        dbs._drive_brief_sync_worker("user-a", "brief-1", BRIEF, [], "P", None)

    assert statuses == [("failed", {})]
    assert not any(rec.exc_info for rec in caplog.records)


def test_deadline_stops_the_sync_and_reports_failed(wired, monkeypatch):
    """Un Drive qui pend garderait sinon un thread ET un refresh token en mémoire."""
    drive, statuses, state = wired
    monkeypatch.setattr(dbs, "_SYNC_DEADLINE_SECONDS", -1)

    dbs._drive_brief_sync_worker("user-a", "brief-1", BRIEF, [], "P", None)

    assert statuses[-1] == ("failed", {})
    assert not drive.uploads


def test_schedule_never_raises_and_returns_immediately():
    """La préparation est déjà persistée : rien ici ne doit remonter au caller."""
    assert dbs.schedule_drive_brief_sync(
        user_sub="user-a", brief_id=None, brief_json={}, documents=[],
    ) is None


# ─── Nommage du dossier ─────────────────────────────────────────────


def test_prep_folder_title_uses_the_meeting_date_and_a_slug():
    title = dbs._prep_folder_title(
        {"title": "Réunion d'équipe — Été", "target_meeting_date": "2026-09-15T09:00:00+00:00"},
        {},
    )
    assert title == "2026-09-15-reunion-d-equipe-ete"


def test_prep_folder_title_falls_back_to_today_and_a_default_slug():
    from datetime import datetime, timezone

    title = dbs._prep_folder_title({}, {})
    assert title.startswith(datetime.now(timezone.utc).date().isoformat())
    assert title.endswith("-brief")
