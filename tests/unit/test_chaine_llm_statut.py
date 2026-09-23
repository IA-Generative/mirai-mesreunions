"""Statut de fin de chaîne LLM : qui a le droit de peindre une réunion en orange.

Le 2026-09-23, les 7 réunions de la bêta portaient la pastille (!) orange
« Réunion prête (partiellement) » — et AUCUNE n'était incomplète. Deux causes :

  1. `apply_glossary_correction` rend « rien à corriger » (aucun sigle du
     glossaire dans la réunion, cas parfaitement normal), et l'orchestrateur
     le comptait comme un échec ;
  2. les étapes INTERMÉDIAIRES (correction des sigles, nettoyage des
     hésitations) — dont la sortie n'est jamais affichée telle quelle —
     basculaient le statut au même titre que le compte-rendu lui-même.

Ces tests fixent la règle : seules les étapes PORTEUSES (celles dont l'absence
se voit dans la fiche) font une réunion « partiellement prête », et un drapeau
posé porte toujours son motif.
"""

import os
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def _puller():
    from test_puller_perform_pull import _install_stubs, _load_puller

    _install_stubs()
    return _load_puller()


class _Llm:
    """Doublure : présente, donc la chaîne s'exécute."""


# Réplique fidèle de `mi.NOTHING_TO_DO` : une chaîne VIDE (donc falsy), dont
# c'est l'IDENTITÉ et non la valeur qui porte le sens. `app.meeting_intelligence`
# est doublé dans ce banc, on ne peut donc pas réutiliser la vraie sentinelle
# ici — sa forme est vérifiée côté source par test_meeting_intelligence.py.
NOTHING_TO_DO = type("_NothingToDo", (str,), {"__slots__": ()})()


def _run(puller, monkeypatch, *, steps):
    """Joue la chaîne en remplaçant chaque étape par son résultat voulu.

    `steps` associe un nom d'étape à la valeur que rend le helper de
    meeting_intelligence : une chaîne (succès), None (le moteur n'a rien
    renvoyé), `NOTHING_TO_DO` (rien à faire) ou une exception à lever.
    """
    mi = puller.mi
    monkeypatch.setattr(mi, "NOTHING_TO_DO", NOTHING_TO_DO)

    def _stub(name, default):
        def _fn(*args, **kwargs):
            out = steps.get(name, default)
            if isinstance(out, Exception):
                raise out
            return out
        return _fn

    monkeypatch.setattr(mi, "apply_glossary_correction", _stub("glossary_correction", "corrigé"))
    monkeypatch.setattr(mi, "suggest_metadata", _stub("suggest_metadata", {"title": "T", "key_points": ["a"]}))
    monkeypatch.setattr(mi, "clean_oob", _stub("cleaning", "nettoyé"))
    monkeypatch.setattr(mi, "reformulate", _stub("reformulation", "reformulé"))
    monkeypatch.setattr(mi, "analyse_meeting", _stub("meeting_analysis", {"actors": [], "themes": []}))
    monkeypatch.setattr(mi, "summarise_for_absentee", _stub("absentee_summary", "résumé"))
    # Les drapeaux d'étapes viennent de l'environnement du service ; on les
    # allume tous pour jouer la chaîne COMPLÈTE, sinon le test ne mesure que
    # les étapes que la config de la machine de test a laissées actives.
    for flag in ("KEVENT_GLOSSARY_CORRECTION_ENABLED",
                 "KEVENT_FILENAME_SUGGESTION_ENABLED",
                 "KEVENT_OOB_CLEANING_ENABLED",
                 "KEVENT_REFORMULATION_ENABLED",
                 "KEVENT_MEETING_ANALYSIS_ENABLED",
                 "KEVENT_ABSENTEE_SUMMARY_ENABLED"):
        monkeypatch.setattr(puller, flag, True)
    monkeypatch.setattr(mi, "serialize_analysis", lambda a: "{}")
    monkeypatch.setattr(mi, "serialize_key_points", lambda kp: "- a")
    # Pas d'attente entre tentatives : ces tests ne mesurent pas le backoff.
    monkeypatch.setattr(puller.time, "sleep", lambda *_: None)
    return puller._run_llm_chain_for_audio(
        "texte de départ", None, llm=_Llm(), glossary_terms=["ANSC"],
    )


def test_toutes_les_etapes_ok_rend_la_reunion_prete(_puller, monkeypatch):
    updates, status = _run(_puller, monkeypatch, steps={})
    assert status == "kevent_completed"
    assert updates["last_error_kind"] is None


def test_rien_a_corriger_ne_peint_pas_la_reunion_en_orange(_puller, monkeypatch):
    """Le cas qui a produit les 7 fausses alertes du 2026-09-23."""
    updates, status = _run(
        _puller, monkeypatch,
        steps={"glossary_correction": NOTHING_TO_DO},
    )
    assert status == "kevent_completed"
    assert "glossary_corrected_text" not in updates
    assert updates["last_error_kind"] is None


def test_etape_intermediaire_ratee_laisse_la_reunion_prete(_puller, monkeypatch):
    """Nettoyage KO : la suite repart du texte brut, la fiche est complète."""
    updates, status = _run(_puller, monkeypatch, steps={"cleaning": None})
    assert status == "kevent_completed"
    assert "cleaned_text" not in updates
    # La reformulation, elle, a bien tourné sur le texte de repli.
    assert updates["reformulated_text"] == "reformulé"


def test_etape_porteuse_ratee_rend_la_reunion_partielle_AVEC_son_motif(_puller, monkeypatch):
    updates, status = _run(_puller, monkeypatch, steps={"absentee_summary": None})
    assert status == "kevent_partially_completed"
    assert updates["last_error_kind"] == "llm_chain_partial"
    assert "résumé pour les absents" in updates["last_error_message"]


def test_une_erreur_du_moteur_n_est_pas_annoncee_comme_une_reponse_vide(_puller, monkeypatch):
    """503 « Model is too busy » mesuré en prod le 2026-09-23 : le motif doit
    nommer les tentatives échouées, pas prétendre que le moteur s'est tu."""
    boom = RuntimeError("LiteLLM 5xx: 503: Model is too busy")
    updates, status = _run(_puller, monkeypatch, steps={"meeting_analysis": boom})
    assert status == "kevent_partially_completed"
    msg = updates["last_error_message"]
    assert "compte-rendu structuré" in msg
    assert "tentatives échouées" in msg
    assert "n'a rien renvoyé" not in msg
