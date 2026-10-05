"""Tests de la recherche plein texte des réunions (internal-ingester).

Deux niveaux :

- Fonctions pures de ``app/meeting_search.py`` (positions UTF-16, extraits,
  validation) : toujours exécutées.
- Recherche réelle (``POST /api/v1/audio/search``) contre un **Postgres** :
  SQLite n'a ni ``tsvector`` ni ``ts_headline``. Exécutées seulement si
  ``MESREUNIONS_TEST_PG_DSN`` désigne une base jetable, par exemple :

      docker run -d --rm --name pg-recherche -p 127.0.0.1:35433:5432 \\
        -e POSTGRES_DB=recherche_test -e POSTGRES_USER=recherche \\
        -e POSTGRES_PASSWORD=recherche-local postgres:16-alpine
      MESREUNIONS_TEST_PG_DSN=postgresql://recherche:recherche-local@127.0.0.1:35433/recherche_test \\
        python -m pytest tests/unit/test_meeting_search.py

  Chaque exécution travaille dans un schéma créé puis supprimé ; elle applique
  les migrations 019 (video_sources) et 024, puis les jeux d'essai versionnés
  ``deploy/docker/seed/recherche-*.sql``.
"""

import importlib.util
import os
import re
import sys
import types
import uuid
from unittest.mock import MagicMock

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_SPEC = importlib.util.spec_from_file_location(
    "meeting_search_under_test",
    os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "meeting_search.py"),
)
ms = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ms)

PG_DSN = os.getenv("MESREUNIONS_TEST_PG_DSN", "").strip()
needs_pg = pytest.mark.skipif(not PG_DSN, reason="MESREUNIONS_TEST_PG_DSN absent (Postgres requis)")

A = "recherche-test-user-a"
B = "recherche-test-user-b"
UAF = "5e5e0000-0000-4000-8000-0000000000{}".format
# Uploads vivants de A tels que mesreunions-web les liste (zone externe du
# jeu d'essai) : seul SRCH01 — SRCH02/SRCH03 sont à la corbeille, SRCH05 a été
# supprimé définitivement.
LIVE_UPLOADS = [{"simple_code": "SRCH01", "filename": "SRCH01_copil.mp4"}]


# ─── Fonctions pures ────────────────────────────────────────────────────────

def test_utf16_len_compte_les_paires_de_substitution():
    assert ms.utf16_len("abc") == 3
    assert ms.utf16_len("é") == 1
    assert ms.utf16_len("🎯") == 2  # hors plan multilingue de base
    assert ms.utf16_len("🎯 budget") == 9


def test_parse_marked():
    plain, spans = ms.parse_marked("le \x02budget\x03 et le \x02budgets\x03")
    assert plain == "le budget et le budgets"
    assert [plain[s:e] for s, e in spans] == ["budget", "budgets"]


def test_extrait_court_positions_utf16():
    plain, spans = ms.parse_marked("🎯 Le \x02budget\x03 2027")
    snippet, hl = ms.build_snippet(plain, spans)
    assert snippet == "🎯 Le budget 2027"
    # En JavaScript : "🎯 Le budget 2027".slice(6, 12) === "budget"
    assert hl == [[6, 12]]
    js_units = snippet.encode("utf-16-le")
    assert js_units[12:24].decode("utf-16-le") == "budget"


def test_extrait_long_borne_et_coupe_sur_un_blanc():
    words = " ".join(f"mot{i}" for i in range(200))
    marked = words + " le \x02budget\x03 " + words
    plain, spans = ms.parse_marked(marked)
    snippet, hl = ms.build_snippet(plain, spans)
    assert len(snippet) <= ms.SNIPPET_MAX_CHARS + 2
    assert snippet.startswith("…") and snippet.endswith("…")
    (s, e), = hl
    assert snippet.encode("utf-16-le")[2 * s:2 * e].decode("utf-16-le") == "budget"
    # Pas de mot coupé aux bords.
    assert snippet[1:].split(" ")[0].startswith("mot")
    assert re.fullmatch(r"mot\d+", snippet[:-1].split(" ")[-1])


def test_extrait_ne_contient_jamais_de_balise_de_surlignage():
    plain, spans = ms.parse_marked("<b>pas du HTML</b> \x02budget\x03")
    snippet, _ = ms.build_snippet(plain, spans)
    assert "\x02" not in snippet and "\x03" not in snippet
    assert snippet == "<b>pas du HTML</b> budget"  # donnée brute, non interprétée


def test_clean_et_points_cles():
    assert ms.clean_text("a\x02b\n\n c\t") == "a b c"
    kp = ms.flatten_key_points("- **Enveloppe** fixée\n- Lot 2 décalé\n\n")
    assert kp == "Enveloppe fixée · Lot 2 décalé"


def test_blocs_depuis_le_parseur_du_puller():
    reparsed = [{"speaker": "M. B", "start": 1421.0, "end": 1450.0,
                 "body_lines": ["> On part sur", "> une enveloppe"]}]
    assert ms.blocks_from_reparsed(reparsed) == [
        {"speaker": "M. B", "start": 1421.0, "end": 1450.0, "text": "On part sur une enveloppe"}]


def test_participants_presents_et_non_actors():
    analysis = '{"actors": [{"name": "a"}, {"name": "b"}, {"name": "cité"}], ' \
               '"participants_presents": [{"name": "a"}, {"name": "b"}]}'
    assert ms.participants(analysis, [], "upload") == (2, "analysis")


def test_participants_repli_sur_les_locuteurs_puis_nul():
    blocks = [{"speaker": "X"}, {"speaker": "Y"}, {"speaker": "X"}]
    only_actors = '{"actors": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}'
    assert ms.participants(only_actors, blocks, "upload") == (2, "speakers")
    assert ms.participants('{"participants_presents": []}', blocks, "upload") == (2, "speakers")
    assert ms.participants(None, [], "upload") == (None, None)
    assert ms.participants(None, blocks, "youtube_subtitle") == (None, None)


def test_date_kind_valeurs_du_contrat():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert ms.date_and_kind(now, now, False)[1] == "meeting"
    assert ms.date_and_kind(None, now, True)[1] == "import"
    assert ms.date_and_kind(None, now, False)[1] == "upload"
    assert ms.date_and_kind(None, None, False) == (None, None)


@pytest.mark.parametrize("payload", [
    None, "texte", {}, {"user_sub": A}, {"user_sub": A, "q": ""},
    {"user_sub": A, "q": "x" * 1001}, {"user_sub": A, "q": "b", "limit": 0},
    {"user_sub": A, "q": "b", "limit": 51}, {"user_sub": A, "q": "b", "limit": "dix"},
    {"user_sub": A, "q": "b", "from": "hier"},
    {"user_sub": A, "q": "b", "from": "2026-09-01T00:00:00", "uploads": []},  # sans fuseau
    {"user_sub": A, "q": "b"},  # uploads obligatoire : fermé par défaut
    {"user_sub": A, "q": "b", "uploads": "SRCH01"},
    {"user_sub": A, "q": "b", "uploads": ["SRCH01"]},
])
def test_validation_refuse(payload):
    with pytest.raises(ms.SearchValidationError):
        ms.validate_payload(payload)


def test_validation_accepte():
    p = ms.validate_payload({"user_sub": A, "q": " budget ", "from": "2026-09-01T00:00:00+02:00",
                             "uploads": LIVE_UPLOADS})
    assert p["q"] == "budget" and p["limit"] == 20
    assert p["up_codes"] == ["SRCH01"] and p["up_files"] == ["SRCH01_copil.mp4"]
    assert p["date_from"].utcoffset() is not None


# ─── Route Flask (sans base) ────────────────────────────────────────────────

def _load_puller_with_stubs(monkeypatch):
    """Charge puller.py sans DB/S3/RabbitMQ (même principe que
    test_puller_trigger_endpoint), avec le vrai meeting_search. Les bouchons
    sont posés via ``monkeypatch`` : ils disparaissent après chaque test."""
    monkeypatch.setenv("INTERNAL_API_TOKEN", "x" * 48)
    monkeypatch.setenv("SKIP_CREATE_APP", "1")

    def _stub(name, **attrs):
        m = types.ModuleType(name)
        m.__all__ = []
        m.__getattr__ = lambda _n: MagicMock()
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    class _RMQ:
        host = "x"; port = 5672; user = "u"; password = "p"; vhost = "/"

    _stub("libs.shared.app.queue_helper", QUEUE_TRANSCRIPTION="t", QUEUE_INTERNAL_PULL="p",
          RabbitMQConfig=_RMQ)
    cfg = _stub("libs.shared.app.config", RabbitMQConfig=_RMQ, INTERNAL_API_TOKEN="x" * 48,
                INTERNAL_PULL_QUEUE_INTERVAL_SECONDS=30,
                load_int_db=lambda: types.SimpleNamespace(sync_url="sqlite:///:memory:"),
                load_s3_processed=lambda: types.SimpleNamespace(),
                load_s3_internal=lambda: types.SimpleNamespace())
    cfg.__getattr__ = lambda _n: None
    for name in ("libs.shared.app.models", "libs.shared.app.database",
                 "libs.shared.app.s3_helper"):
        _stub(name)
    _stub("libs.shared.app.security",
          require_strong_shared_secret=lambda *_a, **_k: None,
          verify_bearer_token=lambda header, expected: header == f"Bearer {expected}")
    app_pkg = types.ModuleType("app")
    app_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "app", app_pkg)
    for sub in ("mcr_client", "kevent_client", "llm_client", "diarization_merger",
                "glossary_loader", "audio_format", "meeting_intelligence"):
        setattr(app_pkg, sub, _stub(f"app.{sub}"))
    monkeypatch.setitem(sys.modules, "app.meeting_search", ms)
    app_pkg.meeting_search = ms
    spec = importlib.util.spec_from_file_location(
        "internal_ingester_search_under_test",
        os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "puller.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def puller(monkeypatch):
    pytest.importorskip("flask")
    pytest.importorskip("sqlalchemy")
    return _load_puller_with_stubs(monkeypatch)


def test_route_exige_le_jeton_interne(puller):
    c = puller.app.test_client()
    assert c.post("/api/v1/audio/search", json={"user_sub": A, "q": "b", "uploads": []}).status_code == 401


def test_route_400_sur_parametre_invalide(puller):
    c = puller.app.test_client()
    r = c.post("/api/v1/audio/search", json={"user_sub": A, "q": ""},
               headers={"Authorization": "Bearer " + "x" * 48})
    assert r.status_code == 400 and r.get_json()["error"] == "invalid_query"


def test_route_503_sans_base_et_sans_fuite(puller, caplog):
    c = puller.app.test_client()
    puller.SessionLocal = None
    r = c.post("/api/v1/audio/search", json={"user_sub": A, "q": "secret-budget", "uploads": []},
               headers={"Authorization": "Bearer " + "x" * 48})
    assert r.status_code == 503


def test_route_erreur_sql_ne_journalise_pas_la_requete(puller, caplog):
    class _BoomSession:
        def execute(self, *_a, **_k):
            raise RuntimeError("[parameters: {'q': 'secret-budget'}]")

        def rollback(self):
            pass

        def close(self):
            pass

    puller.SessionLocal = _BoomSession
    c = puller.app.test_client()
    with caplog.at_level("INFO"):
        r = c.post("/api/v1/audio/search", json={"user_sub": A, "q": "secret-budget", "uploads": []},
                   headers={"Authorization": "Bearer " + "x" * 48})
    assert r.status_code == 503 and r.get_json()["error"] == "search_unavailable"
    assert "secret-budget" not in caplog.text


# ─── Postgres réel ──────────────────────────────────────────────────────────

def _psql_script(path: str, variables: dict) -> str:
    """Rend un script psql exécutable par un driver : retire les
    méta-commandes (\\if…) et substitue :'var'."""
    out = []
    for line in open(path, encoding="utf-8").read().splitlines():
        if line.lstrip().startswith("\\"):
            continue
        for k, v in variables.items():
            line = line.replace(f":'{k}'", "'" + v.replace("'", "''") + "'")
        out.append(line)
    return "\n".join(out)


@pytest.fixture(scope="module")
def pg():
    if not PG_DSN:
        pytest.skip("MESREUNIONS_TEST_PG_DSN absent")
    sqlalchemy = pytest.importorskip("sqlalchemy")
    pytest.importorskip("psycopg2")
    from sqlalchemy.orm import sessionmaker
    from libs.shared.app.models import ExternalBase, InternalBase

    schema = "recherche_" + uuid.uuid4().hex[:10]
    # Le schéma doit exister AVANT la première connexion du moteur : SQLAlchemy
    # y lit le schéma par défaut une fois pour toutes.
    admin = sqlalchemy.create_engine(PG_DSN, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    admin.dispose()
    engine = sqlalchemy.create_engine(
        PG_DSN, connect_args={"options": f"-csearch_path={schema}"})
    raw = engine.raw_connection()
    raw.autocommit = True
    cur = raw.cursor()
    InternalBase.metadata.create_all(engine)
    ExternalBase.metadata.create_all(engine)
    mig = os.path.join(ROOT, "migrations", "internal")
    cur.execute(open(os.path.join(mig, "019_video_ingest_initial.sql"), encoding="utf-8").read())
    # 024 : CREATE INDEX CONCURRENTLY, hors transaction (autocommit).
    for stmt in _split_sql(open(os.path.join(mig, "024_user_audio_files_search.sql"),
                                encoding="utf-8").read()):
        cur.execute(stmt)
    seed = os.path.join(ROOT, "deploy", "docker", "seed")
    variables = {"user_a": A, "user_b": B}
    cur.execute(_psql_script(os.path.join(seed, "recherche-internal.sql"), variables))
    cur.execute(_psql_script(os.path.join(seed, "recherche-external.sql"), variables))
    Session = sessionmaker(bind=engine)
    try:
        yield types.SimpleNamespace(engine=engine, Session=Session, cur=cur, schema=schema)
    finally:
        cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        raw.close()
        engine.dispose()


def _split_sql(sql: str) -> list[str]:
    """Découpe sur ``;`` en fin de ligne, en respectant les corps ``$$``."""
    stmts, buf, in_dollar = [], [], False
    for line in sql.splitlines():
        if line.strip().startswith("--") and not in_dollar:
            continue
        buf.append(line)
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        if not in_dollar and line.rstrip().endswith(";"):
            stmts.append("\n".join(buf))
            buf = []
    if "".join(buf).strip():
        stmts.append("\n".join(buf))
    return stmts


def _reparse():
    """Le vrai parseur des blocs du puller, sans charger tout puller.py."""
    src = open(os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "puller.py"),
               encoding="utf-8").read()
    start = src.index("def _reparse_speaker_tagged_blocks")
    end = src.index("\ndef _rebuild_speaker_tagged")
    ns = {"Optional": __import__("typing").Optional}
    exec(src[start:end], ns)
    return ns["_reparse_speaker_tagged_blocks"]




def _search(pg, **over):
    payload = {"user_sub": A, "q": "budget", "uploads": LIVE_UPLOADS}
    payload.update(over)
    db = pg.Session()
    try:
        return ms.run_search(db, ms.validate_payload(payload), reparse_blocks=_reparse())
    finally:
        db.rollback()
        db.close()


def _ids(out):
    return {r["uaf_id"] for r in out["results"]}


def _sql(pg, stmt):
    pg.cur.execute(stmt)
    return pg.cur.fetchall() if pg.cur.description else None


@needs_pg
def test_pg_index_utilisable(pg):
    with pg.engine.connect() as conn:
        conn.exec_driver_sql("SET enable_seqscan = off")
        plan = "\n".join(r[0] for r in conn.exec_driver_sql(
            "EXPLAIN SELECT id FROM user_audio_files u WHERE "
            "u.search_tsv @@ websearch_to_tsquery('french_unaccent', 'budget')"))
    assert "ix_uaf_search_tsv" in plan


@needs_pg
def test_pg_visibilite_fermee_par_defaut(pg):
    out = _search(pg)
    ids = _ids(out)
    assert UAF("a1") in ids                      # upload vivant
    assert UAF("a5") in ids                      # import, réunion vivante
    assert UAF("a6") in ids                      # ré-import : nouvelle réunion vivante
    assert UAF("b1") not in ids                  # autre utilisateur
    assert UAF("a2") not in ids                  # fichier à la corbeille
    assert UAF("a3") not in ids                  # session à la corbeille
    assert UAF("a4") not in ids                  # import, réunion à la corbeille
    assert UAF("a7") not in ids                  # upload supprimé définitivement
    assert UAF("a8") not in ids                  # import, réunion purgée
    assert out["total"] == len(out["results"]) == 3
    assert out["total_is_lower_bound"] is False


@needs_pg
def test_pg_total_plafonne(pg, monkeypatch):
    monkeypatch.setattr(ms, "TOTAL_CAP", 2)
    out = _search(pg, limit=1)
    assert out["total"] == 2 and out["total_is_lower_bound"] is True
    monkeypatch.setattr(ms, "TOTAL_CAP", 3)
    out = _search(pg, limit=1)
    assert out["total"] == 3 and out["total_is_lower_bound"] is False


@needs_pg
def test_pg_sans_liste_aucun_upload(pg):
    ids = _ids(_search(pg, uploads=[]))
    assert ids == {UAF("a5"), UAF("a6")}


@needs_pg
def test_pg_appariement_par_code_et_nom_transcode(pg):
    """Ce que liste la zone externe sort, et seulement ça (même appariement que
    /api/v1/audio/lookup : code de session + suffixe du nom stocké)."""
    ids = _ids(_search(pg, uploads=LIVE_UPLOADS + [
        {"simple_code": "SRCH02", "filename": "SRCH02_brouillon.mp4"},
        {"simple_code": "SRCH03", "filename": "autre_nom.mp4"},       # nom différent
        {"simple_code": "AUTRE", "filename": "SRCH05_efface.mp4"},    # code différent
    ]))
    assert UAF("a2") in ids
    assert UAF("a3") not in ids and UAF("a7") not in ids


@needs_pg
def test_pg_reimport_puis_corbeille_de_la_nouvelle_reunion(pg):
    _sql(pg, "UPDATE meetings SET trashed_at = now() "
             f"WHERE id = '{UAF('f7')}'")
    try:
        assert UAF("a6") not in _ids(_search(pg))
    finally:
        _sql(pg, f"UPDATE meetings SET trashed_at = NULL WHERE id = '{UAF('f7')}'")


@needs_pg
def test_pg_reunion_purgee_definitivement(pg):
    """Purge de la réunion vivante d'un import : il disparaît (la ligne
    user_audio_files, elle, n'est jamais supprimée)."""
    _sql(pg, f"UPDATE meetings SET id = '{UAF('f9')}', user_audio_file_id = NULL "
             f"WHERE id = '{UAF('f5')}'")
    try:
        assert UAF("a5") not in _ids(_search(pg))
    finally:
        _sql(pg, f"UPDATE meetings SET id = '{UAF('f5')}', user_audio_file_id = '{UAF('a5')}' "
                 f"WHERE id = '{UAF('f9')}'")


@needs_pg
def test_pg_autre_utilisateur(pg):
    out = _search(pg, user_sub=B, uploads=[{"simple_code": "SRCH04", "filename": "SRCH04_autre.mp4"}])
    assert _ids(out) == {UAF("b1")}
    # Les uploads de B listés pour A ne donnent rien à A.
    assert UAF("b1") not in _ids(_search(pg, uploads=[
        {"simple_code": "SRCH04", "filename": "SRCH04_autre.mp4"}]))


@needs_pg
def test_pg_total_compte_apres_filtres_avant_limite(pg):
    out = _search(pg, limit=1)
    assert out["total"] == 3 and len(out["results"]) == 1


@needs_pg
def test_pg_passages_horodates_et_utf16(pg):
    res = next(r for r in _search(pg)["results"] if r["uaf_id"] == UAF("a1"))
    assert 1 <= len(res["hits"]) <= ms.MAX_HITS
    first = res["hits"][0]
    assert first["field"] == "transcript"
    assert first["location"] == {"start_seconds": 1421, "end_seconds": 1450, "page": None}
    assert first["speaker"] == "M. Bertrand"
    assert first["snippet"].startswith("🎯")
    for h in res["hits"]:
        units = h["snippet"].encode("utf-16-le")
        assert h["highlights"], h
        for s, e in h["highlights"]:
            assert units[2 * s:2 * e].decode("utf-16-le").lower().startswith("budget")
        assert len(h["snippet"]) <= ms.SNIPPET_MAX_CHARS + 2
        assert "\x02" not in h["snippet"]
    # 3 présents, pas les 4 « actors » (qui comptent les personnes citées).
    assert res["context"] == {"duration_seconds": 4320, "participants_count": 3,
                              "participants_basis": "analysis", "source_type": "upload",
                              "date_kind": "meeting"}
    assert res["date"].startswith("2026-09-19")
    assert isinstance(res["score"], float)


@needs_pg
def test_pg_participants_par_locuteurs(pg):
    res = next(r for r in _search(pg)["results"] if r["uaf_id"] == UAF("a6"))
    assert res["context"]["participants_count"] == 2
    assert res["context"]["participants_basis"] == "speakers"
    assert res["context"]["date_kind"] == "import"


@needs_pg
def test_pg_racinisation_francaise(pg):
    assert UAF("a1") in _ids(_search(pg, q="budgets"))


@needs_pg
def test_pg_accents(pg):
    """« reunion » trouve « réunion » quand l'extension unaccent est là."""
    if not _sql(pg, "SELECT 1 FROM pg_extension WHERE extname = 'unaccent'"):
        pytest.skip("extension unaccent indisponible sur ce Postgres")
    assert UAF("a1") in _ids(_search(pg, q="reunion"))
    assert UAF("a1") in _ids(_search(pg, q="réunion"))


@needs_pg
def test_pg_entetes_de_blocs_non_indexes(pg):
    """« intervenant » (libellé des en-têtes) ne trouve pas toutes les réunions."""
    assert _search(pg, q="intervenant")["total"] == 0
    assert _search(pg, q="Bertrand")["total"] == 0


@needs_pg
def test_pg_champs_nuls(pg):
    res = next(r for r in _search(pg)["results"] if r["uaf_id"] == UAF("a5"))
    assert res["context"] == {"duration_seconds": None, "participants_count": None,
                              "participants_basis": None, "source_type": "youtube_subtitle",
                              "date_kind": "import"}
    assert res["title"] == "Conférence « Numérique de l'État 2027 »"
    hit = res["hits"][0]
    assert hit["field"] == "transcript" and hit["location"] is None and hit["speaker"] is None


@needs_pg
def test_pg_titre_d_origine_d_un_import(pg):
    assert UAF("a5") in _ids(_search(pg, q="numérique"))


@needs_pg
def test_pg_filtre_de_dates_bornes_incluses(pg):
    out = _search(pg, **{"from": "2026-09-19T10:00:00+02:00", "to": "2026-09-19T10:00:00+02:00"})
    assert _ids(out) == {UAF("a1")}
    out = _search(pg, **{"to": "2026-07-01T00:00:00+02:00"})
    assert _ids(out) == {UAF("a5")}


@needs_pg
def test_pg_mots_vides_seulement(pg):
    assert _search(pg, q="de la du et") == {"total": 0, "total_is_lower_bound": False,
                                            "results": []}


@needs_pg
def test_pg_syntaxe_libre_sans_erreur(pg):
    for q in ['"budget nexus"', "budget -nexus", "budget OR crédits", "a & | ! ( :*", "'"]:
        _search(pg, q=q)  # websearch_to_tsquery ne lève jamais


@needs_pg
def test_pg_blocs_masques_ignores(pg):
    _sql(pg, f"UPDATE user_audio_files SET hidden_block_indices = '[0]' WHERE id = '{UAF('a1')}'")
    try:
        res = next(r for r in _search(pg)["results"] if r["uaf_id"] == UAF("a1"))
        assert all((h["location"] or {}).get("start_seconds") != 1421 for h in res["hits"])
        assert all(h["location"] is not None for h in res["hits"] if h["field"] == "transcript")
    finally:
        _sql(pg, f"UPDATE user_audio_files SET hidden_block_indices = '[]' WHERE id = '{UAF('a1')}'")


@needs_pg
def test_pg_trigger_ne_recalcule_que_sur_les_textes(pg):
    """Le battement de cœur (last_activity_at) ne recalcule pas le document ;
    une modification de texte, si."""
    row = UAF("a1")
    _sql(pg, f"ALTER TABLE user_audio_files DISABLE TRIGGER trg_uaf_search_tsv_update")
    _sql(pg, f"UPDATE user_audio_files SET search_tsv = NULL WHERE id = '{row}'")
    _sql(pg, f"ALTER TABLE user_audio_files ENABLE TRIGGER trg_uaf_search_tsv_update")
    try:
        _sql(pg, f"UPDATE user_audio_files SET last_activity_at = now() WHERE id = '{row}'")
        assert _sql(pg, f"SELECT search_tsv IS NULL FROM user_audio_files WHERE id = '{row}'")[0][0]
        _sql(pg, f"UPDATE user_audio_files SET key_points_summary = key_points_summary || ' ' "
                 f"WHERE id = '{row}'")
        assert not _sql(pg, f"SELECT search_tsv IS NULL FROM user_audio_files WHERE id = '{row}'")[0][0]
        assert UAF("a1") in _ids(_search(pg))
    finally:
        _sql(pg, f"UPDATE user_audio_files SET key_points_summary = rtrim(key_points_summary) "
                 f"WHERE id = '{row}'")


@needs_pg
def test_pg_migration_rejouable(pg):
    for stmt in _split_sql(open(os.path.join(ROOT, "migrations", "internal",
                                             "024_user_audio_files_search.sql"),
                                encoding="utf-8").read()):
        pg.cur.execute(stmt)
    assert UAF("a1") in _ids(_search(pg))


@needs_pg
def test_pg_uploads_vivants_listes_par_mesreunions_web(pg, monkeypatch):
    """La liste construite par mesreunions-web en zone externe est exactement
    celle des uploads vivants (ni fichier ni session à la corbeille, ligne
    présente)."""
    pytest.importorskip("flask")
    sys.path.insert(0, os.path.join(ROOT, "services", "mesreunions-web"))
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        sys.modules.pop(name, None)
    from app import runtime
    runtime.configure_runtime(session_factory=pg.Session)
    from app.modules.search import routes
    assert routes._live_uploads(A) == LIVE_UPLOADS
    assert routes._live_uploads(B) == [{"simple_code": "SRCH04", "filename": "SRCH04_autre.mp4"}]
