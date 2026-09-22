#!/usr/bin/env python3
"""Banc hors ligne de l'écran épuré de Mes réunions (2026-09-22).

Le VRAI gabarit (`index.html` rendu par Jinja), le VRAI bundle Vite
(`app/static/dist/shell.js`, à reconstruire avant : `npm run build`) et le VRAI
menu commun de la bêta (`src/menu.js` du dépôt frère mirai-apps-menu, ≥ 1.13.0),
servis par Playwright sous le vrai nom d'hôte ; les routes `/api/*` et
`/_beta/*` sont bouchonnées. Rien n'est remplacé dans le code testé.

Ce qu'il prouve :
  N1  la barre a deux entrées (Mes réunions, Préparer une réunion), le second
      clic change d'écran et pose aria-current ; aucun onglet Téléphones,
      Données utiles, Corbeille, Admin
  N2  la page Mes réunions a DEUX boutons d'en-tête : « Importer une réunion ▾ »
      et « Relancer (n) » — ni « Consulter », ni « Interroger », ni « Mode avancé »
  N3  « Relancer » compte les réunions en échec (2 dans le jeu d'essai)
  N4  le menu « Importer » liste les six sources, dont « Enregistrer directement
      avec mon téléphone »
  N5  le menu commun porte la section « Mes réunions · avancé » : Mes téléphones (0),
      Mes données utiles, Corbeille, Revoir la visite guidée — et « Corbeille »
      ouvre l'écran corbeille, qui porte un retour « Mes réunions » qui marche
  N6  la visite guidée démarre seule à la première arrivée, a 5 pas, se termine,
      ne rejoue pas au rechargement, et se rejoue depuis le menu
  N7  sans téléphone associé : la carte « Enregistrez vos réunions depuis votre
      téléphone » est sous la liste ; l'assistant s'ouvre avec ses 3 étapes, sans
      case « ne plus montrer », sans lien réel vers un magasin
  N8  le bouton flottant « Interroger mes réunions » reste monté quand le service
      n'est pas configuré, et le dit
  N10 supprimer une réunion : le bandeau « Annuler » paraît, et « Annuler »
      appelle la restauration (fichier → /api/file/<id>/restore)
  N9  ancien réflexe : `?tab=devices` (rechargement sur l'ancien onglet) ouvre
      l'écran Mes téléphones, pas une page vide

    python3 tests/e2e/banc-ecran-epure.py
"""
import json
import os
import pathlib
import sys

RACINE = pathlib.Path(__file__).resolve().parent.parent.parent
WEB = RACINE / "services" / "mesreunions-web"
MENU_JS = pathlib.Path(os.environ.get(
    "MENU_JS", str(RACINE.parent / "mirai-apps-menu" / "src" / "menu.js")))

sys.path.insert(0, str(RACINE))

from playwright.sync_api import sync_playwright  # noqa: E402

ECHECS = []


def verifie(cond, quoi):
    print(("  ✅ " if cond else "  ❌ ") + quoi)
    if not cond:
        ECHECS.append(quoi)


def rendre_gabarit():
    import flask
    app = flask.Flask(__name__, template_folder=str(WEB / "app" / "templates"),
                      static_folder=str(WEB / "app" / "static"))
    with app.test_request_context("/"):
        return flask.render_template(
            "index.html",
            user={"name": "Testeuse Cohorte", "email": "t@example.gouv.fr", "sub": "abc", "roles": []},
            short_ttl_enabled=False, device_retention_days=15,
            allowed_audio_extensions="m4a,mp3,wav", asset_version="banc",
        )


SESSIONS = [{
    "id": "s1", "simple_code": "ABC-123", "status": "active",
    "uploads": [
        {"id": "f1", "original_filename": "COPIL migration SI-RH.m4a", "status": "transferred",
         "created_at": "2026-09-22T09:00:00Z", "audio_duration_seconds": 4320},
        {"id": "f2", "original_filename": "Point hebdo.m4a", "status": "transferred",
         "created_at": "2026-09-22T08:00:00Z", "audio_duration_seconds": 2880},
        {"id": "f3", "original_filename": "Comité sécurité.m4a", "status": "transferred",
         "created_at": "2026-09-21T08:00:00Z", "audio_duration_seconds": 6000},
        {"id": "f4", "original_filename": "Réunion CESEDA.m4a", "status": "transferred",
         "created_at": "2026-09-19T08:00:00Z", "audio_duration_seconds": 3240},
    ],
}]
STATUTS = {"f1": "kevent_failed", "f2": "completed", "f3": "kevent_failed", "f4": "completed"}


APPELS = []


def monter(ctx, html, devices):
    def route(r):
        url = r.request.url
        reste = url.split("://", 1)[1]
        chemin = "/" + (reste.split("/", 1)[1] if "/" in reste else "")
        chemin = chemin.split("?")[0]
        if chemin == "/" or chemin == "/index.html":
            return r.fulfill(body=html, content_type="text/html; charset=utf-8")
        if chemin.startswith("/static/"):
            f = WEB / "app" / chemin.lstrip("/")
            if f.is_file():
                ct = "application/javascript" if f.suffix == ".js" else ("text/css" if f.suffix == ".css" else "application/octet-stream")
                return r.fulfill(body=f.read_bytes(), content_type=ct)
            return r.fulfill(status=404, body="")
        if chemin == "/_beta/menu.js":
            return r.fulfill(body=MENU_JS.read_text(encoding="utf-8"), content_type="application/javascript")
        if chemin == "/_beta/capacites.json":
            return r.fulfill(body=json.dumps({"avis": True, "messages": True, "telemetrie": False}), content_type="application/json")
        if chemin.startswith("/_beta/"):
            return r.fulfill(body="{}", content_type="application/json")
        if chemin == "/api/my-sessions":
            return r.fulfill(body=json.dumps(SESSIONS), content_type="application/json")
        if chemin == "/api/my-devices":
            return r.fulfill(body=json.dumps(devices), content_type="application/json")
        if chemin.startswith("/api/file/transcript-status/"):
            fid = chemin.rsplit("/", 1)[1]
            return r.fulfill(body=json.dumps({"available": True, "transcription_status": STATUTS.get(fid, "completed"),
                                              "suggested_filename": "", "outputs": {}}),
                             content_type="application/json")
        if chemin == "/api/rag/status":
            return r.fulfill(body=json.dumps({"configured": False}), content_type="application/json")
        if chemin == "/api/youtube/my-imports":
            return r.fulfill(body=json.dumps({"imports": []}), content_type="application/json")
        if chemin == "/api/my-trash":
            return r.fulfill(body=json.dumps({"files": [], "sessions": [], "briefs": [], "retention_days": 30}), content_type="application/json")
        if chemin.startswith("/api/preparations"):
            return r.fulfill(body=json.dumps({"preparations": [], "briefs": []}), content_type="application/json")
        if r.request.method in ("DELETE", "POST") and chemin.startswith("/api/"):
            APPELS.append((r.request.method, chemin))
            return r.fulfill(body=json.dumps({"ok": True}), content_type="application/json")
        if chemin.startswith("/api/"):
            return r.fulfill(body="{}", content_type="application/json")
        return r.fulfill(status=204, body="")
    ctx.route("**/*", route)


def main():
    html = rendre_gabarit()
    verifie(MENU_JS.is_file(), f"menu commun trouvé ({MENU_JS})")
    with sync_playwright() as p:
        nav = p.chromium.launch()
        ctx = nav.new_context(locale="fr-FR", viewport={"width": 1280, "height": 800})
        erreurs_js = []
        monter(ctx, html, devices=[])
        page = ctx.new_page()
        page.on("pageerror", lambda e: erreurs_js.append(str(e)))
        page.goto("https://mesreunions.numerique-interieur.com/")
        page.wait_for_function("window.__MESREUNIONS_SHELL_READY__ === true", timeout=15_000)
        page.wait_for_selector("#mm-barre", timeout=10_000)
        try:
            page.click("#mm-tuto-ok", timeout=3_000)
        except Exception:
            pass
        page.wait_for_selector(".meetings-tab-header", timeout=10_000)

        # N6 — la visite démarre seule
        page.wait_for_selector("#mr-visite:not([hidden])", timeout=8_000)
        etape = lambda pg: pg.evaluate("document.querySelector('#mr-visite [data-etape]').textContent")
        verifie(etape(page) == "Étape 1 sur 5", "N6 la visite guidée démarre seule, 5 pas")
        verifie("Vos réunions" in page.inner_text("#mr-visite"), "N6 pas 1 : « Vos réunions »")
        verifie(not page.is_visible("#mr-visite [data-prec]"), "N6 pas 1 : pas de « Précédent » (le DSFR ignore [hidden])")
        for _ in range(4):
            page.click("#mr-visite [data-suiv]")
            page.wait_for_timeout(150)
        verifie(etape(page) == "Étape 5 sur 5" and "Terminer" in page.inner_text("#mr-visite [data-suiv]"),
                "N6 pas 5 : « Terminer »")
        page.click("#mr-visite [data-suiv]")
        page.wait_for_timeout(200)
        verifie(page.evaluate("document.getElementById('mr-visite').hidden") is True, "N6 la visite se termine")
        verifie(page.evaluate("localStorage.getItem('mesreunions.visite.vue')") == "1", "N6 le souvenir est posé")

        # N1 — la barre
        verifie(page.locator("#nav-principale .tab-btn").count() == 2, "N1 deux entrées dans la barre")
        verifie(page.locator("#tab-btn-devices, #tab-btn-trash, #tab-btn-useful-data, #tab-btn-admin").count() == 0,
                "N1 plus d'onglet téléphones / corbeille / données utiles / admin")
        page.click("#tab-btn-brief")
        page.wait_for_timeout(300)
        verifie(page.get_attribute("#tab-btn-brief", "aria-current") == "page"
                and page.get_attribute("#tab-btn-transfers", "aria-current") is None,
                "N1 « Préparer une réunion » devient l'entrée courante")
        verifie(page.is_visible("#panel-brief") and not page.is_visible("#panel-transfers"), "N1 l'écran change")
        page.click("#tab-btn-transfers")
        page.wait_for_selector(".meetings-tab-header", timeout=5_000)

        # N2 / N3 — l'en-tête de la page
        tete = page.inner_text(".meetings-tab-header")
        verifie("Importer une réunion" in tete and "Relancer" in tete, "N2 « Importer une réunion » et « Relancer »")
        verifie("Consulter" not in tete and "Interroger" not in tete and "Mode avancé" not in tete and "Plus d'actions" not in tete,
                "N2 ni Consulter, ni Interroger, ni Mode avancé, ni Plus d'actions")
        verifie(page.locator(".meetings-tab-header .fr-btn").count() == 2, "N2 exactement deux boutons")
        page.wait_for_function("document.querySelector('.mt-relancer-nb') && document.querySelector('.mt-relancer-nb').textContent === '2'", timeout=8_000)
        verifie(page.inner_text(".mt-relancer-nb") == "2", "N3 « Relancer » compte 2 réunions en échec")
        verifie(page.locator("#advanced-toggle, #purge-btn").count() == 0, "N2 plus de bouton Mode avancé ni de purge")

        # N4 — le menu Importer
        page.click('.meetings-tab-header [data-menu="add"]')
        page.wait_for_selector("#mt-menu-add:not([hidden])")
        menu = page.inner_text("#mt-menu-add")
        for lib in ["Importer un fichier audio", "Importer un dossier audio", "Depuis YouTube",
                    "Depuis compte-rendu.mirai", "Depuis La Suite numérique", "Enregistrer directement avec mon téléphone"]:
            verifie(lib in menu, f"N4 source « {lib} »")
        page.keyboard.press("Escape")

        # N7 — sans téléphone : la carte, puis l'assistant
        page.wait_for_selector("[data-mr-carte-tel]", timeout=5_000)
        verifie("Enregistrez vos réunions depuis votre téléphone" in page.inner_text("[data-mr-carte-tel]"), "N7 la carte téléphone sous la liste")
        page.click('[data-mr-carte-tel] [data-action="meetings-new:record-with-phone"]')
        page.wait_for_selector(".enroll-modal-wrap", timeout=5_000)
        modale = page.inner_text(".enroll-modal-wrap")
        verifie("Associer en sécurité votre téléphone" in modale, "N7 l'assistant s'ouvre")
        verifie(page.is_visible("#assistant-etapes") and "Retrouver dans Mes réunions" in modale, "N7 les 3 étapes à la première ouverture")
        verifie("Utiliser l'application Transcript" in modale
                and "Pas encore publiée" in page.evaluate("document.querySelector('.enroll-modal-wrap').textContent"),
                "N7 la voie Transcript, en simulation assumée")
        verifie(page.locator(".enroll-modal-wrap a[href*='apple.com'], .enroll-modal-wrap a[href*='google.com']").count() == 0,
                "N7 aucun lien réel vers un magasin")
        verifie("Ne plus montrer" not in modale, "N7 pas de case « ne plus montrer »")
        verifie("Associer ce téléphone" in modale, "N7 la voie du code : « Associer ce téléphone »")
        verifie(not page.is_visible("#enrollment-collapsed"), "N7 pas de bandeau « déjà associé » sans téléphone")
        page.click(".enroll-modal-close")
        page.wait_for_timeout(200)
        verifie(page.evaluate("localStorage.getItem('mesreunions.assistant-telephone.vu')") == "1", "N7 l'explication est marquée vue")

        # N8 — le bouton flottant
        verifie(page.is_visible("#rag-fab") and "indisponible" in page.inner_text("#rag-fab"), "N8 la bulle reste montée et dit « indisponible »")

        # N5 — le menu commun
        page.click("#mm-b-cpt")
        page.wait_for_selector("#mm-hote:not([hidden])", timeout=5_000)
        hote = page.evaluate("document.getElementById('mm-hote').textContent")
        verifie("Mes réunions · avancé" in hote, "N5 section « Mes réunions · avancé » dans le menu commun")
        for lib in ["Mes téléphones", "Mes données utiles", "Corbeille", "Revoir la visite guidée"]:
            verifie(lib in hote, f"N5 entrée « {lib} »")
        verifie("Administration" not in hote, "N5 pas d'entrée Administration pour un compte non admin")
        verifie(page.inner_text("#mm-hote [data-mm-entree='0'] .mm-compte") == "0", "N5 « Mes téléphones » compte 0")
        page.click("#mm-hote >> text=Corbeille")
        page.wait_for_timeout(400)
        verifie(page.is_visible("#panel-trash") and not page.is_visible("#panel-transfers"), "N5 « Corbeille » ouvre l'écran corbeille")
        verifie(page.get_attribute("#tab-btn-transfers", "aria-current") is None, "N5 aucune entrée courante sur un écran sans onglet")
        page.click('#panel-trash [data-action="mr:retour"]')
        page.wait_for_selector(".meetings-tab-header", timeout=5_000)
        verifie(page.is_visible("#panel-transfers") and page.get_attribute("#tab-btn-transfers", "aria-current") == "page",
                "N5 « ← Mes réunions » ramène à la liste")

        # N6 bis — rejouer depuis le menu, et pas de rejeu seul au rechargement
        page.click("#mm-b-cpt")
        page.click("#mm-hote >> text=Revoir la visite guidée")
        page.wait_for_selector("#mr-visite:not([hidden])", timeout=5_000)
        verifie(etape(page) == "Étape 1 sur 5", "N6 la visite se rejoue depuis le menu")
        page.click("#mr-visite [data-passer]")
        page.reload()
        page.wait_for_function("window.__MESREUNIONS_SHELL_READY__ === true", timeout=15_000)
        page.wait_for_selector(".meetings-tab-header", timeout=10_000)
        page.wait_for_timeout(2_500)
        verifie(page.locator("#mr-visite:not([hidden])").count() == 0, "N6 pas de rejeu seul au rechargement")

        # N10 — le filet de la corbeille
        page.once("dialog", lambda d: d.accept())
        page.evaluate("window.deleteFile('f4', 'Réunion CESEDA.m4a')")
        page.wait_for_selector("#mr-annuler:not([hidden])", timeout=5_000)
        verifie("mise à la corbeille" in page.inner_text("#mr-annuler") and "Annuler" in page.inner_text("#mr-annuler"),
                "N10 bandeau « … mise à la corbeille · Annuler · Voir la corbeille »")
        verifie(("DELETE", "/api/file/f4") in APPELS, "N10 la suppression part (DELETE /api/file/f4)")
        page.click("#mr-annuler [data-annuler]")
        page.wait_for_timeout(500)
        verifie(("POST", "/api/file/f4/restore") in APPELS, "N10 « Annuler » restaure (POST /api/file/f4/restore)")
        verifie(page.evaluate("document.getElementById('mr-annuler').hidden") is True, "N10 le bandeau se ferme")

        # N9 — l'ancien réflexe ?tab=devices
        page.goto("https://mesreunions.numerique-interieur.com/?tab=devices")
        page.wait_for_function("window.__MESREUNIONS_SHELL_READY__ === true", timeout=15_000)
        page.wait_for_timeout(800)
        verifie(page.is_visible("#panel-devices") and "Mes téléphones" in page.inner_text("#panel-devices"),
                "N9 ?tab=devices ouvre l'écran Mes téléphones")

        verifie(not erreurs_js, "aucune erreur JavaScript non rattrapée" + (f" — {erreurs_js[:2]}" if erreurs_js else ""))
        ctx.close()

        # N7 bis — un téléphone déjà associé : le bandeau, la seconde voie
        ctx2 = nav.new_context(locale="fr-FR", viewport={"width": 1280, "height": 800})
        monter(ctx2, html, devices=[{"device_id": "d1", "device_name": "iPhone de Testeuse", "status": "active",
                                     "qr_token": "q1", "retention_expires_at": "2026-10-07T10:00:00Z",
                                     "last_seen_at": "2026-09-22T09:12:00Z", "recent_uploads_24h": 2}])
        page2 = ctx2.new_page()
        page2.add_init_script("localStorage.setItem('mesreunions.visite.vue','1');localStorage.setItem('mesreunions.assistant-telephone.vu','1');")
        page2.goto("https://mesreunions.numerique-interieur.com/")
        page2.wait_for_function("window.__MESREUNIONS_SHELL_READY__ === true", timeout=15_000)
        page2.wait_for_selector(".meetings-tab-header", timeout=10_000)
        page2.wait_for_timeout(800)
        verifie(page2.locator("[data-mr-carte-tel]").count() == 0, "N7 téléphone associé : plus de carte sous la liste")
        page2.click('.meetings-tab-header [data-menu="add"]')
        page2.click('#mt-menu-add [data-action="meetings-new:record-with-phone"]')
        page2.wait_for_selector(".enroll-modal-wrap", timeout=5_000)
        m2 = page2.inner_text(".enroll-modal-wrap")
        verifie(not page2.is_visible("#assistant-etapes"), "N7 explication déjà vue : pas les 3 étapes")
        verifie(page2.is_visible("#enrollment-collapsed") and "iPhone de Testeuse" in m2, "N7 bandeau « déjà associé » avec le nom")
        verifie("Associer un autre téléphone" in m2 and "Associer cet autre téléphone" in m2, "N7 la seconde voie devient « un autre téléphone »")
        page2.click(".enroll-modal-close")
        page2.click("#mm-b-cpt")
        page2.wait_for_selector("#mm-hote:not([hidden])", timeout=5_000)
        verifie(page2.inner_text("#mm-hote [data-mm-entree='0'] .mm-compte") == "1", "N5 « Mes téléphones » compte 1")
        ctx2.close()
        nav.close()

    print()
    if ECHECS:
        print(f"❌ {len(ECHECS)} contrôle(s) en échec :")
        for e in ECHECS:
            print("   - " + e)
        sys.exit(1)
    print("✅ banc écran épuré : tout passe")


if __name__ == "__main__":
    main()
