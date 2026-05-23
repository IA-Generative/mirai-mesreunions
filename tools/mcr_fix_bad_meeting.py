#!/usr/bin/env python3
"""
Localise la row 'pourrie' qui fait planter GET /api/meetings sur MCR
(validator pydantic VISIO + meeting_platform_id), puis propose de la
PATCHer (meilleur) ou DELETEr.

Usage (3 manières de fournir le token, par ordre de préférence) :

    A) Variable d'env (recommandé pour les tokens longs >2000 chars) :
         export MCR_TOKEN='eyJ...'
         python3 tools/mcr_fix_bad_meeting.py

    B) Fichier :
         pbpaste > /tmp/tk.txt        # macOS, ou : echo 'eyJ...' > /tmp/tk.txt
         python3 tools/mcr_fix_bad_meeting.py /tmp/tk.txt

    C) Prompt interactif (peut buguer si le token est très long) :
         python3 tools/mcr_fix_bad_meeting.py

Pour récupérer le token : sur https://compte-rendu.mirai.fake-domain.name/,
DevTools (F12) → Network → filtre "token" → recharge → clique sur la requête
POST .../openid-connect/token → Response → copie la valeur de access_token.

Aucune dépendance externe (stdlib only).
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://compte-rendu.mirai.fake-domain.name/api"


def load_token() -> str:
    """3 sources d'input par ordre de priorité : argv (fichier), env, prompt."""
    if len(sys.argv) > 1:
        path = sys.argv[1]
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as exc:
            sys.exit(f"Impossible de lire {path} : {exc}")
    env_token = os.getenv("MCR_TOKEN", "").strip()
    if env_token:
        return env_token
    print("Colle l'access_token puis Entrée (sans guillemets, sans 'Bearer ')")
    print("Si le token est très long et que le terminal coupe, relance avec :")
    print("  export MCR_TOKEN='eyJ...' && python3 tools/mcr_fix_bad_meeting.py")
    print()
    try:
        return sys.stdin.readline().strip()
    except KeyboardInterrupt:
        sys.exit("\nAbandon.")


def http(method, path, token, body=None):
    url = BASE + path
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read() or b"").decode("utf-8", errors="replace")
    except Exception as exc:
        return -1, f"{type(exc).__name__}: {exc}"


def get_id_at_page(token, page):
    code, body = http("GET", f"/meetings?page={page}&page_size=1", token)
    if code == 500:
        return "BROKEN", body
    if code != 200:
        return f"ERR_{code}", body
    try:
        data = json.loads(body)
        items = data.get("data") or []
        if not items:
            return "EMPTY", body
        return items[0].get("id"), items[0]
    except Exception as exc:
        return f"PARSE_{exc}", body


def bisect_to_find_broken_page(token, max_page=200):
    """Avance page par page jusqu'à trouver la première qui crashe."""
    print(f"\n→ Bisection page=1..{max_page} (page_size=1)…\n")
    last_ok_page = None
    last_ok_id = None
    for p in range(1, max_page + 1):
        val, raw = get_id_at_page(token, p)
        if val == "BROKEN":
            print(f"  page {p:3d} → 500 (row pourrie !)")
            return p, last_ok_page, last_ok_id
        if val == "EMPTY":
            print(f"  page {p:3d} → liste épuisée sans crash. Bizarre.")
            return None, last_ok_page, last_ok_id
        if isinstance(val, int):
            print(f"  page {p:3d} → id={val}  (\"{raw.get('name','')[:60]}\")")
            last_ok_page, last_ok_id = p, val
        else:
            print(f"  page {p:3d} → {val} : {raw[:200]}")
            return None, last_ok_page, last_ok_id
    print(f"  Atteint max_page={max_page} sans crash.")
    return None, last_ok_page, last_ok_id


def find_neighbor_ids_after(token, broken_page, n=3):
    """Récupère les IDs des `n` pages SUIVANTES (passé la row pourrie)."""
    ids = []
    for p in range(broken_page + 1, broken_page + 1 + n):
        val, raw = get_id_at_page(token, p)
        if isinstance(val, int):
            ids.append((p, val, raw.get("name", "")))
    return ids


def try_patch(token, meeting_id):
    """Tente un PATCH neutralisant les champs interdits."""
    print(f"\n→ PATCH /meetings/{meeting_id}  body={{meeting_platform_id:null, meeting_password:null}}")
    code, body = http(
        "PATCH",
        f"/meetings/{meeting_id}",
        token,
        body={"meeting_platform_id": None, "meeting_password": None},
    )
    print(f"  HTTP {code}")
    if body:
        print(f"  body : {body[:400]}")
    return code


def main():
    print(__doc__)
    print("=" * 70)
    token = load_token()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    # Nettoyage : retire d'éventuels guillemets/virgules de copier-coller JSON.
    token = token.strip().strip('"').strip(",").strip().strip('"').strip()
    if not token or "." not in token or token.count(".") < 2:
        sys.exit(f"Token vide ou format inattendu (len={len(token)}, dots={token.count('.')}). Abandon.")
    print(f"Token reçu : {len(token)} caractères, {token.count('.')} segments (attendu : 2).")

    # 0) Ping rapide /me pour valider le token
    print("\n→ Validation token via GET /me…")
    code, body = http("GET", "/me", token)
    if code != 200:
        sys.exit(f"  /me a renvoyé {code} — token invalide ?\n  body : {body[:300]}")
    try:
        me = json.loads(body)
        print(f"  OK user={me.get('email')} sub={me.get('keycloak_uuid')}")
    except Exception:
        print(f"  OK (réponse non-JSON: {body[:100]})")

    # 1) Localise la page qui crashe
    broken_page, last_ok_page, last_ok_id = bisect_to_find_broken_page(token)
    if broken_page is None:
        print("\nAucune page n'a crashé jusqu'à max_page. La row pourrie est plus loin ; relance avec max_page plus grand.")
        return

    print(f"\n📍 Row pourrie à la position {broken_page}.")
    if last_ok_id is not None:
        print(f"   Dernière row OK avant : page={last_ok_page} id={last_ok_id}")

    # 2) Cherche les IDs des rows suivantes pour deviner l'ID du milieu
    print(f"\n→ Récup des IDs des 5 pages suivantes (pour cerner l'ID)…")
    next_ids = find_neighbor_ids_after(token, broken_page, n=5)
    for p, mid, name in next_ids:
        print(f"  page {p} → id={mid}  \"{name[:60]}\"")

    if not next_ids:
        print("  Pas d'IDs suivants — la row pourrie est la dernière. Tente des IDs autour de", last_ok_id)
        return

    next_id = next_ids[0][1]
    print(f"\n💡 IDs candidats : entre {last_ok_id} (avant) et {next_id} (après).")
    if last_ok_id is None:
        candidates = list(range(max(1, next_id - 5), next_id))
    else:
        candidates = list(range(last_ok_id + 1, next_id))
        if not candidates:
            candidates = [last_ok_id + 1, next_id - 1]

    if len(candidates) > 1:
        print(f"   {len(candidates)} candidat(s) entre les deux. On va tester un par un.")
    print(f"   Liste : {candidates}")

    proceed = input("\nLancer les PATCH automatiquement ? [y/N] : ").strip().lower()
    if proceed != "y":
        print("Aucun PATCH lancé. Commandes à essayer manuellement :")
        for cid in candidates:
            print(f"  curl -X PATCH '{BASE}/meetings/{cid}' -H 'Authorization: Bearer …' -H 'Content-Type: application/json' -d '{{\"meeting_platform_id\":null,\"meeting_password\":null}}'")
        return

    for cid in candidates:
        code = try_patch(token, cid)
        if code == 200:
            print(f"\n✅ PATCH id={cid} OK. Re-teste GET /meetings : doit refonctionner.")
            code_test, body_test = http("GET", "/meetings?page=1&page_size=5", token)
            print(f"   GET /meetings page=1 → HTTP {code_test}")
            if code_test == 200:
                print("   🎉 Plus de crash. Tu peux retourner sur mesreunions cliquer 📥 Depuis MCR.")
            return
        if code == 404:
            continue  # mauvais ID, on passe au suivant
        # Tout autre code (500 VISIO inclus) : on continue mais signale
        print(f"   id={cid} : code {code} — pas un PATCH valide, on continue.")

    print("\n⚠️ Aucun candidat n'a abouti à un 200. Essaie d'élargir manuellement la plage d'IDs.")


if __name__ == "__main__":
    main()
