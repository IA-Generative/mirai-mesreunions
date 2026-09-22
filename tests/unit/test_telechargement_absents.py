"""« Pour les absents » se télécharge (fiche épurée, 2026-09-22).

Le résumé pour les absents était lu à l'écran, jamais téléchargeable. La
fiche épurée le propose en Word / LibreOffice / Markdown : la route
générique `/api/file/transcript/<kind>/<ext>/<id>` le sert si `absentee`
figure dans la table des genres, l'en-tête du document le nomme, et le nom
du fichier ne se confond pas avec celui du compte-rendu.
"""
import ast
import importlib.util
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WEB = os.path.join(ROOT, "services", "mesreunions-web", "app")


def _table_des_genres():
    arbre = ast.parse(open(os.path.join(WEB, "modules", "sessions", "service.py"), encoding="utf-8").read())
    for noeud in arbre.body:
        if isinstance(noeud, ast.Assign) and any(getattr(c, "id", "") == "TRANSCRIPT_KIND_TO_COLUMN" for c in noeud.targets):
            return ast.literal_eval(noeud.value)
    raise AssertionError("TRANSCRIPT_KIND_TO_COLUMN introuvable")


def test_absents_dans_la_table_des_genres():
    table = _table_des_genres()
    assert table.get("absentee") == "absentee_summary"
    # Les genres historiques ne bougent pas.
    for k in ("transcript", "transcript-tagged", "transcript-corrected", "transcript-cleaned", "transcript-reformulated"):
        assert k in table


def test_en_tete_nomme_le_document():
    spec = importlib.util.spec_from_file_location("tf_absents", os.path.join(WEB, "transcript_formats.py"))
    tf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tf)
    md = tf.build_document_header_md(title="COPIL", kind="absentee", meeting_date_iso=None,
                                     upload_date_iso="2026-09-22T10:00:00", duration_seconds=3600,
                                     key_points=None)
    assert "Pour les absents" in md


def test_nom_de_fichier_distinct_du_compte_rendu():
    src = open(os.path.join(WEB, "modules", "sessions", "routes.py"), encoding="utf-8").read()
    assert 'if kind == "absentee" and "absents" not in stem:' in src
    assert 'stem = f"{stem} - pour les absents"' in src


def test_la_recherche_interne_renvoie_le_resume_pour_les_absents():
    """internal-ingester `/api/v1/audio/lookup` est la SEULE source de la fiche :
    une colonne qu'il ne renvoie pas n'existe pas pour l'écran (vu le 22/09 :
    le résumé était en base, la fiche disait « pas encore rédigé »)."""
    src = open(os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "puller.py"), encoding="utf-8").read()
    debut = src.index('@app.route("/api/v1/audio/lookup"')
    fin = src.index("@app.route(", debut + 10)
    route = src[debut:fin]
    for col in ("meeting_analysis_json", "reformulated_text", "cleaned_text", "absentee_summary"):
        assert f'"{col}": row.{col}' in route, f"{col} absent de la réponse de /api/v1/audio/lookup"
