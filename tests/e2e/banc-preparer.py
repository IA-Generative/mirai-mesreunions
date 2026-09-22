#!/usr/bin/env python3
"""Banc hors ligne de « Préparer une réunion » épurée (2026-09-22, E8).

Même harnais que `banc-ecran-epure.py`. On vérifie :

  B1  la liste : pas de titre répété, une phrase, « Préparer une réunion »
  B2  l'assistant a TROIS étapes (La réunion · Vous et les autres · Ce que
      l'IA doit lire), plus de récapitulatif ; « Insister sur » et les
      sources sont à l'étape 3 ; plus de question « Qu'attendez-vous du
      brief ? » ; plus d'avertissement « SMTP »
  B3  parcours : sujet + durée (pastille) → rôle (pastille) + ce qui doit être
      acquis → « Rédiger le brief » : le POST porte sujet, durée 60, rôle,
      focus, et un `expectation` NON VIDE (déduit — le serveur l'exige)
  B4  un brouillon d'avant (5 étapes, sans `v`) resté sur l'étape 4 (Focus)
      rouvre sur l'étape 3, pas sur un écran vide
  B5  la fiche du brief : « Préparer une réunion » en retour, « Télécharger
      le brief ▾ » et « Modifier le brief » nommés, « ⋯ » avec Glossaire /
      Lier un enregistrement / Préparer la réunion suivante ; les réglages
      (participants, récurrence, envoi) sont SOUS le brief, repliés
  B6  aucune erreur JavaScript

    python3 tests/e2e/banc-preparer.py
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
POSTS = []


def verifie(cond, quoi):
    print(("  ✅ " if cond else "  ❌ ") + quoi)
    if not cond:
        ECHECS.append(quoi)


BRIEF = {"preparation": {
    "id": "p1", "title": "COPIL migration SI-RH", "subject": "COPIL migration SI-RH", "role": "J'anime la réunion",
    "duration_minutes": 60, "created_at": "2026-09-22T10:00:00", "participants": [],
    "content": {"objective_reformulated": "Arbitrer la date de bascule du SI-RH.",
                "agenda": [{"title": "Scénarios de bascule", "duration_minutes": 25}]},
}}


def main():
    html = B.rendre_gabarit()
    with sync_playwright() as p:
        nav = p.chromium.launch()
        ctx = nav.new_context(locale="fr-FR", viewport={"width": 1280, "height": 900})
        B.monter(ctx, html, devices=[])

        def preparations(route):
            req = route.request
            chemin = req.url.split("://", 1)[1].split("/", 1)[1].split("?")[0]
            if req.method == "POST" and chemin == "api/preparations":
                POSTS.append(json.loads(req.post_data or "{}"))
                return route.fulfill(body=json.dumps({"id": "p1", "preparation": BRIEF["preparation"]}), content_type="application/json")
            if chemin == "api/preparations/p1":
                return route.fulfill(body=json.dumps(BRIEF), content_type="application/json")
            if chemin == "api/preparations":
                return route.fulfill(body=json.dumps({"preparations": [BRIEF["preparation"]]}), content_type="application/json")
            return route.fulfill(body=json.dumps({}), content_type="application/json")
        ctx.route("**/api/preparations**", preparations)

        page = ctx.new_page()
        page.add_init_script("localStorage.setItem('mesreunions.visite.vue','1');")
        erreurs = []
        page.on("pageerror", lambda e: erreurs.append(str(e)))
        page.goto("https://mesreunions.numerique-interieur.com/")
        page.wait_for_function("window.__MESREUNIONS_SHELL_READY__ === true", timeout=15_000)
        page.click("#tab-btn-brief")
        page.wait_for_timeout(500)

        # B1
        liste = page.locator("#brief-list-view")
        verifie(liste.locator("h1").count() == 0, "B1 pas de titre répété")
        verifie(liste.locator('[data-action="open-wizard"]').inner_text().strip() == "Préparer une réunion", "B1 bouton « Préparer une réunion »")

        # B2
        page.evaluate("try{Object.keys(localStorage).filter(k=>k.includes('wizard')||k.includes('brouillon')||k.includes('draft')).forEach(k=>localStorage.removeItem(k))}catch(e){}")
        liste.locator('[data-action="open-wizard"]').click()
        page.wait_for_selector("#wizard-modal-backdrop.is-open", timeout=5_000)
        etapes = page.locator("#wizard-stepper").inner_text()
        verifie("La réunion" in etapes and "Vous et les autres" in etapes and "Ce que l'IA doit lire" in etapes and "Récap" not in etapes,
                f"B2 trois étapes ({etapes.strip()!r})")
        verifie(page.locator("#wizard-step-recap, #wizard-step-focus").count() == 0, "B2 plus d'étape Récap ni Focus")
        verifie(page.locator('#wizard-step-documents input[name="wizard-focus"]').count() == 7, "B2 « Insister sur » est à l'étape 3")
        verifie(not page.locator("#wizard-expectation").is_visible(), "B2 plus de question « Qu'attendez-vous du brief ? »")
        verifie("SMTP" not in page.evaluate("document.getElementById('wizard-modal-backdrop').textContent"), "B2 plus d'avertissement SMTP")

        # B3
        page.fill("#wizard-subject", "COPIL migration SI-RH : arbitrer la date de bascule")
        page.click('.wizard-chips[data-target="wizard-duration"] .wizard-chip[data-minutes="60"]')
        page.click("#wizard-next-btn")
        verifie(page.locator("#wizard-step-contexte.is-active").count() == 1, "B3 étape 2 atteinte")
        page.click('.wizard-chips[data-target="wizard-role"] .wizard-chip >> nth=0')
        page.fill("#wizard-success-criteria", "La date de bascule est arbitrée.")
        page.click("#wizard-next-btn")
        verifie(page.locator("#wizard-step-documents.is-active").count() == 1 and page.is_visible("#wizard-submit-btn")
                and "Rédiger le brief" in page.inner_text("#wizard-submit-btn"), "B3 étape 3 : « Rédiger le brief »")
        page.check('input[name="wizard-focus"][value="Décisions à prendre"]')
        page.click("#wizard-submit-btn")
        page.wait_for_timeout(1200)
        corps = POSTS[-1] if POSTS else {}
        verifie(bool(POSTS), "B3 le brief est demandé (POST /api/preparations)")
        verifie(corps.get("subject", "").startswith("COPIL") and corps.get("duration_minutes") == 60, "B3 sujet et durée transmis")
        verifie(bool((corps.get("role") or "").strip()), "B3 rôle transmis")
        verifie((corps.get("expectation") or "").strip() == "La date de bascule est arbitrée.", "B3 expectation déduit (non vide)")
        verifie("Décisions à prendre" in (corps.get("focus") or []), "B3 « Insister sur » transmis")

        # B5
        page.evaluate("window.showBriefDetail && window.showBriefDetail('p1')")
        page.wait_for_selector("#brief-detail-view", state="visible", timeout=5_000)
        page.wait_for_timeout(500)
        vue = page.locator("#brief-detail-view")
        verifie("Préparer une réunion" in vue.locator('[data-action="show-list"]').inner_text(), "B5 retour « Préparer une réunion »")
        verifie("Télécharger le brief" in vue.locator("#brief-detail-export-btn").inner_text(), "B5 « Télécharger le brief ▾ » nommé")
        verifie(vue.locator('.brief-actions [data-action="toggle-amend"]').count() == 1, "B5 « Modifier le brief » visible")
        vue.locator("#brief-detail-more-btn").click()
        plus = page.locator("#brief-detail-more-menu")
        verifie(plus.is_visible() and "Glossaire" in plus.inner_text() and "Lier un enregistrement" in plus.inner_text()
                and "réunion suivante" in plus.inner_text(), "B5 « ⋯ » : glossaire, lier, réunion suivante")
        vue.locator("#brief-detail-more-btn").click()
        pos = page.evaluate("""() => { const b = document.getElementById('brief-detail-body').getBoundingClientRect().top;
                                       const r = document.getElementById('brief-reglages').getBoundingClientRect().top; return r > b; }""")
        verifie(pos and not page.evaluate("document.getElementById('brief-reglages').open"), "B5 réglages sous le brief, repliés")

        # B4 — un brouillon d'avant (5 étapes, sans `v`) arrêté sur « Focus » (4e, index 3)
        page.evaluate("""() => {
            const k = 'mesreunions.prep-wizard.drafts';
            const tous = JSON.parse(localStorage.getItem(k) || '{}');
            tous['ancien'] = { step: 3, updated_at: Date.now(), fields: { 'wizard-subject': 'Brouillon ancien' } };
            localStorage.setItem(k, JSON.stringify(tous));
            window.reopenPrepDraft('ancien');
        }""")
        page.wait_for_selector("#wizard-modal-backdrop.is-open", timeout=5_000)
        verifie(page.locator("#wizard-step-documents.is-active").count() == 1
                and page.input_value("#wizard-subject") == "Brouillon ancien",
                "B4 un brouillon d'avant (étape Focus) rouvre sur l'étape 3, contenu intact")
        page.evaluate("window.closeWizard && window.closeWizard()")
        verifie(not erreurs, "B6 aucune erreur JavaScript" + (f" — {erreurs[:2]}" if erreurs else ""))
        page.screenshot(path=str(ICI / "preparer-epure.png"))
        ctx.close()
        nav.close()

    print()
    if ECHECS:
        print(f"❌ {len(ECHECS)} contrôle(s) en échec :")
        for e in ECHECS:
            print("   - " + e)
        sys.exit(1)
    print("✅ banc préparer : tout passe")


if __name__ == "__main__":
    main()
