#!/usr/bin/env python3
"""Banc hors ligne de la fiche d'une réunion épurée (2026-09-22, E7).

Même harnais que `banc-ecran-epure.py` (vrai gabarit, vrai bundle, vrai menu
commun, routes bouchonnées). On ouvre la fiche de « Réunion CESEDA » et on
vérifie :

  F1  en-tête : retour « Mes réunions », titre, « ⋯ », une ligne de méta
      (date · durée · source · importée le) ; ✓ ↺ invisibles tant que le titre
      n'a pas changé ; l'ancienne ligne technique et le (i) ont disparu
  F2  deux boutons : « Télécharger le compte-rendu ▾ » et « Modifier le
      compte-rendu »
  F3  Télécharger : Compte-rendu, Pour les absents, Transcription nettoyée,
      Synthèse narrative — chacun DOCX · ODT · MD ; « Pour les absents » vise
      /api/file/transcript/absentee/… ; les fichiers intermédiaires repliés
  F4  le compte-rendu se LIT à l'écran (En bref + décisions), et une balise
      venue du texte du modèle n'est pas exécutée (échappée)
  F5  onglets : « Pour les absents » charge et affiche son texte ; «
      Transcription » montre le correcteur, qui est caché sur les autres
  F6  « ⋯ » : Poser une question, Régénérer, Refaire toute la transcription,
      Détails techniques, Corbeille ; « Régénérer » déclenche le bouton de
      régénération historique (caché) ; les boutons de régénération ne sont
      plus visibles en pied
  F7  « modifier » (date) ouvre l'éditeur de date, fermé par défaut
  F8  l'onglet choisi survit au rafraîchissement de la fiche
  F10 une réunion SANS résumé pour les absents garde l'onglet, qui le dit et
      propose « Rédiger le résumé pour les absents » (→ régénération)
  F9  aucune erreur JavaScript

    python3 tests/e2e/banc-fiche-reunion.py
"""
import importlib.util
import json
import pathlib
import sys

ICI = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("banc_epure", ICI / "banc-ecran-epure.py")
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)

from playwright.sync_api import sync_playwright  # noqa: E402

ECHECS = []


def verifie(cond, quoi):
    print(("  ✅ " if cond else "  ❌ ") + quoi)
    if not cond:
        ECHECS.append(quoi)


CR = {
    "themes": [{"title": "Refonte des fiches CESEDA", "summary": "Publier la collection Légifrance."}],
    "decisions": [{"decision": "Publier la collection à tout le monde <script>window.__xss=1</script>", "owner": "M. Delorme"}],
    "gaps": [{"question": "Qui porte la mise à jour trimestrielle ?"}],
}
STATUT_F4 = {
    "available": True, "transcription_status": "kevent_completed", "transcription_engine": "whisper",
    "suggested_filename": "", "key_points_summary": "Refonte des fiches CESEDA : publication décidée.",
    "meeting_analysis_json": json.dumps(CR),
    "outputs": {"transcript": True, "transcript-tagged": True, "transcript-corrected": True,
                "transcript-cleaned": True, "transcript-reformulated": True, "meeting-cr": True, "absentee": True},
}


def main():
    html = B.rendre_gabarit()
    with sync_playwright() as p:
        nav = p.chromium.launch()
        ctx = nav.new_context(locale="fr-FR", viewport={"width": 1280, "height": 900})
        B.monter(ctx, html, devices=[])

        def statut(route):
            fid = route.request.url.split("?")[0].rsplit("/", 1)[1]
            if fid == "f4":
                return route.fulfill(body=json.dumps(STATUT_F4), content_type="application/json")
            if fid == "f2":
                sans = dict(STATUT_F4, outputs={k: v for k, v in STATUT_F4["outputs"].items() if k != "absentee"})
                return route.fulfill(body=json.dumps(sans), content_type="application/json")
            return route.fallback()
        ctx.route("**/api/file/transcript-status/*", statut)
        ctx.route("**/api/file/transcript-text/f4/absentee",
                  lambda r: r.fulfill(body=json.dumps({"available": True, "text": "## Pour ceux qui n'étaient pas là\n\n- La collection sera publique."}),
                                      content_type="application/json"))
        page = ctx.new_page()
        page.add_init_script("localStorage.setItem('mesreunions.visite.vue','1');")
        erreurs = []
        page.on("pageerror", lambda e: erreurs.append(str(e)))
        page.goto("https://mesreunions.numerique-interieur.com/")
        page.wait_for_function("window.__MESREUNIONS_SHELL_READY__ === true", timeout=15_000)
        page.wait_for_selector(".meetings-tab-header", timeout=10_000)
        page.evaluate("window.showFileDetail('f4')")
        page.wait_for_selector('.file-detail[data-detail-file-id="f4"] .fiche-onglets', timeout=10_000)
        fiche = page.locator('.file-detail[data-detail-file-id="f4"]')

        # F1
        verifie("Mes réunions" in fiche.locator(".file-detail-back").inner_text(), "F1 retour « Mes réunions »")
        verifie(fiche.locator("[data-fiche-pts]").count() == 1, "F1 bouton « ⋯ »")
        meta = fiche.locator(".fiche-meta").inner_text()
        verifie("importée le" in meta and "54 min" in meta and ("Fichier importé" in meta or "Téléphone" in meta),
                "F1 une ligne de méta : date · durée · source · importée le")
        verifie(not fiche.locator(".file-detail-rename-btn").is_visible(), "F1 ✓ invisible tant que le titre n'a pas changé")
        verifie(fiche.locator(".file-detail-techline, .file-detail-info-btn--inline").count() == 0, "F1 plus de ligne technique ni de (i)")

        # F2
        verifie(fiche.locator("[data-fiche-dl]").count() == 1 and "Télécharger le compte-rendu" in fiche.locator("[data-fiche-dl]").inner_text(),
                "F2 « Télécharger le compte-rendu ▾ »")
        verifie(fiche.locator('.fiche-actions [data-cr-edit="f4"]').count() == 1, "F2 « Modifier le compte-rendu »")

        # F3
        fiche.locator("[data-fiche-dl]").click()
        menu = fiche.locator(".fiche-dl-menu")
        verifie(menu.is_visible(), "F3 le menu s'ouvre")
        texte = menu.evaluate("m => m.textContent")
        for lib in ("Compte-rendu", "Pour les absents", "Transcription nettoyée", "Synthèse narrative"):
            verifie(lib in texte, f"F3 ligne « {lib} »")
        hrefs = menu.evaluate("m => Array.from(m.querySelectorAll('a')).map(a => a.getAttribute('href'))")
        verifie("/api/file/meeting-cr/odt/f4" in hrefs and "/api/file/meeting-cr/docx/f4" in hrefs, "F3 compte-rendu en DOCX et ODT")
        verifie(all(f"/api/file/transcript/absentee/{e}/f4" in hrefs for e in ("docx", "odt", "md")), "F3 « Pour les absents » en DOCX · ODT · MD")
        verifie(menu.locator("details.downloads-inter").count() == 1 and not menu.locator("details.downloads-inter").evaluate("d => d.open"),
                "F3 fichiers intermédiaires repliés")
        page.keyboard.press("Escape")
        verifie(not menu.is_visible(), "F3 Échap ferme le menu")

        # F4
        cr = fiche.locator('[data-fiche-panneau="cr"]')
        crt = cr.inner_text()
        verifie("Refonte des fiches CESEDA : publication décidée." in crt, "F4 « En bref » affiché")
        verifie("Publier la collection à tout le monde" in crt, "F4 les décisions se lisent à l'écran")
        verifie(page.evaluate("window.__xss") is None and cr.locator("script").count() == 0, "F4 une balise du texte du modèle n'est pas exécutée")

        # F5
        corr = fiche.locator(".file-detail-corrector-block")
        verifie(not corr.is_visible(), "F5 correcteur caché sur l'onglet Compte-rendu")
        fiche.locator('[data-fiche-onglet="absents"]').click()
        page.wait_for_function("document.querySelector('[data-fiche-panneau=\"absents\"]').textContent.includes('publique')", timeout=5_000)
        verifie(fiche.locator('[data-fiche-panneau="absents"]').is_visible() and not cr.is_visible(), "F5 « Pour les absents » charge et s'affiche")
        fiche.locator('[data-fiche-onglet="tr"]').click()
        verifie(fiche.get_attribute("data-onglet") == "tr", "F5 onglet Transcription actif")
        verifie(page.evaluate("getComputedStyle(document.querySelector('.file-detail-corrector-block')).display") != "none",
                "F5 le correcteur s'affiche sur « Transcription »")

        # F8 — l'onglet survit au rafraîchissement
        page.evaluate("window.refreshDownloadsBlocks ? window.refreshDownloadsBlocks() : null")
        page.evaluate("""() => { const c = document.querySelector('.file-detail[data-detail-file-id="f4"] .file-detail-fullinfo');
                               return window.loadTranscriptStatus && window.loadTranscriptStatus('f4', c); }""")
        page.wait_for_timeout(600)
        verifie(page.get_attribute('.file-detail[data-detail-file-id="f4"]', "data-onglet") == "tr"
                and page.get_attribute('[data-fiche-onglet="tr"]', "aria-selected") == "true",
                "F8 l'onglet choisi survit au rafraîchissement")
        fiche.locator('[data-fiche-onglet="cr"]').click()

        # F6
        fiche.locator("[data-fiche-pts]").click()
        pts = fiche.locator(".fiche-pts-menu")
        ptst = pts.inner_text()
        for lib in ("Poser une question", "Régénérer le compte-rendu", "Refaire toute la transcription", "Détails techniques", "Mettre à la corbeille"):
            verifie(lib in ptst, f"F6 « ⋯ › {lib} »")
        page.wait_for_selector('[data-feedback-regen="llm-only"][data-feedback-file="f4"]', state="attached", timeout=5_000)
        verifie(not page.is_visible('[data-feedback-regen="llm-only"][data-feedback-file="f4"]'), "F6 les boutons de régénération ne sont plus en pied")
        page.evaluate("""() => { window.__regen = 0; document.querySelector('[data-feedback-regen="llm-only"][data-feedback-file="f4"]')
                                .addEventListener('click', (e) => { window.__regen++; e.stopImmediatePropagation(); e.preventDefault(); }, true); }""")
        pts.locator('[data-fiche-action="regen-llm"]').click()
        verifie(page.evaluate("window.__regen") == 1, "F6 « Régénérer » déclenche la régénération historique")
        verifie(not pts.is_visible(), "F6 le menu se ferme après un choix")

        # F7
        ed = fiche.locator("[data-fiche-date-editeur]")
        verifie(not ed.is_visible(), "F7 éditeur de date fermé par défaut")
        fiche.locator("[data-fiche-date]").click()
        verifie(ed.is_visible(), "F7 « modifier » ouvre l'éditeur de date")

        # F10 — sans résumé pour les absents
        page.evaluate("window.showFileDetail('f2')")
        page.wait_for_selector('.file-detail[data-detail-file-id="f2"] .fiche-onglets', timeout=10_000)
        f2 = page.locator('.file-detail[data-detail-file-id="f2"]')
        verifie(f2.locator('[data-fiche-onglet="absents"]').count() == 1, "F10 l'onglet « Pour les absents » est là, même sans résumé")
        f2.locator('[data-fiche-onglet="absents"]').click()
        pa = f2.locator('[data-fiche-panneau="absents"]')
        verifie("pas encore été rédigé" in pa.inner_text() and pa.locator('[data-fiche-action="regen-llm"]').count() == 1,
                "F10 il le dit et propose « Rédiger le résumé pour les absents »")
        page.wait_for_selector('[data-feedback-regen="llm-only"][data-feedback-file="f2"]', state="attached", timeout=5_000)
        page.evaluate("""() => { window.__regen2 = 0; document.querySelector('[data-feedback-regen="llm-only"][data-feedback-file="f2"]')
                                .addEventListener('click', (e) => { window.__regen2++; e.stopImmediatePropagation(); e.preventDefault(); }, true); }""")
        pa.locator('[data-fiche-action="regen-llm"]').click()
        verifie(page.evaluate("window.__regen2") == 1, "F10 le bouton lance la régénération")

        verifie(not erreurs, "F9 aucune erreur JavaScript" + (f" — {erreurs[:2]}" if erreurs else ""))
        page.screenshot(path=str(ICI / "fiche-epure.png"), full_page=False)
        ctx.close()
        nav.close()

    print()
    if ECHECS:
        print(f"❌ {len(ECHECS)} contrôle(s) en échec :")
        for e in ECHECS:
            print("   - " + e)
        sys.exit(1)
    print("✅ banc fiche : tout passe")


if __name__ == "__main__":
    main()
