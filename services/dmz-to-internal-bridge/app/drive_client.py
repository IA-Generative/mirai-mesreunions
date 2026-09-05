"""
HTTP client for the suitenumerique/drive service (mesfichiers).

The Drive exposes a Django REST Framework API under ``/api/v1.0/items/...``
behind ``mozilla-django-oidc`` ``OIDCAuthentication``. We reuse the same
trust chain as the MCR push:

  1. exchange refresh_token → access_token at the Keycloak token endpoint
     (assumes Drive and this service share a Keycloak realm — both live in
     the suite numérique ecosystem).
  2. GET /api/v1.0/items/{id}/         → metadata of a single item
  3. GET /api/v1.0/items/{id}/children/ → list folder contents
  4. GET <download_url>                → fetch the binary payload

Côté écriture (versement du brief), la séquence est :

  5. GET  /api/v1.0/items/                    → racines, dont `main_workspace`
  6. POST /api/v1.0/items/{id}/children/      → dossier, ou fichier + `policy`
  7. PUT  <policy>                            → le corps, sur URL présignée
  8. POST /api/v1.0/items/{id}/upload-ended/  → finalisation
  9. DELETE /api/v1.0/items/{id}/             → soft delete (corbeille 30 j)

Deux propriétés du Drive dictent la forme de ces méthodes : un titre déjà
pris chez les frères est **silencieusement renommé** « brief_01.md » avec un
201 quand même (d'où l'écrasement explicite avant chaque dépôt), et la policy
présignée expire en ~60 s (d'où l'enchaînement create → PUT → upload-ended
fichier par fichier, sans pré-création groupée).

The download URL comes either from the metadata response (typical DRF
pattern with S3-backed FileField producing a presigned URL) or from a
conventional ``/items/{id}/download/`` endpoint. The client supports both:
``download_item`` first reads the metadata, looks for a known URL field,
and falls back to the ``/download/`` route if none is present.

Errors are classified into the same three families as ``MCRClient`` so the
caller (mobile-upload-pwa /api/meeting-prep route) can react uniformly:

  - ``DriveAuthError``        : refresh expired/revoked, or 401/403 from
                                Drive. Caller must wipe the refresh token
                                and tell the user to re-login. No retry.
  - ``DriveTransientError``   : 5xx, timeout, connection error. Caller
                                may retry once or fail soft.
  - ``DriveApplicativeError`` : 4xx other than auth (404 item not found,
                                403 not yours, bad payload). No retry.

Timeouts are conservative (10 s metadata, 60 s download) so a stuck Drive
doesn't hold the synchronous prep route indefinitely.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests as req

logger = logging.getLogger(__name__)


# ─── Exceptions ────────────────────────────────────────────────

class DriveError(Exception):
    """Base class — never raised directly."""


class DriveAuthError(DriveError):
    """Refresh expired/revoked, or 401/403 from Drive. No retry.

    ``status_code`` distingue les causes : 401 = la session/access token
    n'est pas valide (re-login utile) ; 403 = le token est valide mais
    l'utilisateur n'a pas accès à cette ressource précise (re-login
    inutile, c'est une question de permission Drive sur le folder/item).
    None pour les erreurs côté Keycloak (refresh expiré, etc.).
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class DriveTransientError(DriveError):
    """5xx or network failure. Caller may retry."""


class DriveApplicativeError(DriveError):
    """4xx other than auth — not found, forbidden, bad payload. No retry."""


# ─── Client ────────────────────────────────────────────────────

# Candidate field names on the item metadata that may carry a download URL.
# Tried in order until one yields a non-empty string.
#
# Ordre crucial : `url_permalink` d'abord car c'est le seul champ qui pointe
# systématiquement sur l'API DRF (`/api/v1.0/items/<id>/download/`) qui
# accepte le bearer token mozilla-django-oidc. Le champ `url` (que mesfichiers
# remplit sur tous les items) pointe sur `/media/item/<id>/<filename>`, route
# servie par un middleware d'auth différent (cookie session ou JWT media-auth)
# qui refuse notre bearer → 403 sur le download alors que le listing passe.
# Cf diagnostic 2026-05-14 (test-drive children_probe = 200 mais POST = 403).
_DOWNLOAD_URL_KEYS = ("url_permalink", "download_url", "presigned_url", "url", "file", "media_url")

# DRF base path. Items are the unified resource (folders are items too).
_API_PREFIX = "/api/v1.0"

# Valeurs de `ItemTypeChoices` côté Drive — en minuscules, le serveur refuse
# "FOLDER".
_TYPE_FOLDER = "folder"
_TYPE_FILE = "file"

# `MAX_PAGE_SIZE` du Drive : au-delà, le serveur retombe sur PAGE_SIZE=20 et
# on perdrait des frères sans le voir.
_MAX_PAGE_SIZE = 200

# Garde-fou de pagination sur une recherche par titre : le filtre serveur est
# déjà un `icontains`, dépasser ce nombre de pages signifie que la question
# posée était mauvaise, pas qu'il faut continuer à dérouler.
_MAX_LOOKUP_PAGES = 5

# Valeur envoyée quand la policy présignée signe `x-amz-acl` : c'est le défaut
# de `AWS_S3_UPLOAD_ACL` côté suitenumerique/drive. L'URL présignée expose le
# NOM des en-têtes signés, jamais leur valeur — une instance qui change ce
# réglage fera échouer le PUT en 403 et il faudra l'aligner ici.
_UPLOAD_ACL = "private"

# Marge avant expiration en deçà de laquelle on ne rejoue plus un PUT : sans
# elle on relancerait un upload dont la signature meurt pendant le transfert,
# et le 403 obtenu se lirait comme un refus de droits.
_POLICY_RETRY_MARGIN_SECONDS = 10.0


def policy_signed_headers(policy_url: str) -> list[str]:
    """En-têtes signés dans une URL présignée S3, en minuscules.

    On n'envoie QUE ceux-là (hors `host`, porté par l'URL) : un en-tête
    `x-amz-*` non signé fait échouer la signature, et un en-tête signé mais
    absent aussi. Lire la liste plutôt que la deviner rend le client
    indifférent au réglage `AWS_S3_UPLOAD_ACL` de l'instance.
    """
    from urllib.parse import parse_qs, urlparse
    try:
        raw = parse_qs(urlparse(policy_url).query).get("X-Amz-SignedHeaders", [""])[0]
    except Exception:
        return []
    return [h.strip().lower() for h in raw.split(";") if h.strip()]


def policy_expires_at(policy_url: str) -> Optional[float]:
    """Instant d'expiration (epoch UTC) d'une URL présignée, ou None.

    None = date illisible ; les appelants la traitent comme « expirée »,
    parce qu'un rejeu à l'aveugle sur une signature morte produit un 403
    qu'on prendrait pour un refus de droits.
    """
    from datetime import datetime as _dt
    from urllib.parse import parse_qs, urlparse
    try:
        query = parse_qs(urlparse(policy_url).query)
        signed_at = _dt.strptime(query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ")
        expires_in = int(query["X-Amz-Expires"][0])
    except Exception:
        return None
    from datetime import timezone as _tz
    return signed_at.replace(tzinfo=_tz.utc).timestamp() + expires_in


class DriveClient:
    """
    Stateless wrapper around the Drive API. One instance per request is
    fine — there's no state worth reusing across users.
    """

    def __init__(
        self,
        base_url: str,
        oidc_token_endpoint: str,
        oidc_client_id: str,
        oidc_client_secret: str = "",
        timeout: int = 10,
        download_timeout: int = 60,
    ):
        if not base_url:
            raise ValueError("DRIVE_BASE_URL is required to build DriveClient")
        if not oidc_token_endpoint:
            raise ValueError("OIDC_TOKEN_ENDPOINT is required to build DriveClient")
        self.base_url = base_url.rstrip("/")
        self.oidc_token_endpoint = oidc_token_endpoint
        self.oidc_client_id = oidc_client_id
        self.oidc_client_secret = oidc_client_secret
        self.timeout = timeout
        self.download_timeout = download_timeout

    # ── Step 1: refresh → access ─────────────────────────────

    def exchange_refresh(self, refresh_token: str) -> str:
        """Exchange a refresh token for a fresh access token. Same flow as MCRClient."""
        if not refresh_token:
            raise DriveAuthError("Empty refresh token")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.oidc_client_id,
        }
        if self.oidc_client_secret:
            data["client_secret"] = self.oidc_client_secret
        try:
            resp = req.post(self.oidc_token_endpoint, data=data, timeout=self.timeout)
        except req.RequestException as exc:
            raise DriveTransientError(f"Keycloak token endpoint unreachable: {exc}") from exc
        if resp.status_code == 400:
            body = (resp.text or "")[:300]
            raise DriveAuthError(f"Refresh exchange failed (400): {body}")
        if resp.status_code >= 500:
            raise DriveTransientError(f"Keycloak 5xx on token exchange: {resp.status_code}")
        if resp.status_code >= 400:
            raise DriveApplicativeError(f"Keycloak {resp.status_code} on token exchange: {(resp.text or '')[:200]}")
        try:
            access_token = resp.json().get("access_token", "")
        except Exception as exc:
            raise DriveTransientError(f"Keycloak response not JSON: {exc}") from exc
        if not access_token:
            raise DriveAuthError("Keycloak returned no access_token")
        return access_token

    # ── Helpers internes ─────────────────────────────────────

    def _request_with_retry(self, method: str, url: str, *,
                            access_token: Optional[str] = None,
                            timeout: Optional[int] = None,
                            max_attempts: int = 3,
                            label: str = "",
                            extra_headers: Optional[dict] = None,
                            **kwargs):
        """Primitive HTTP unique du client : un verbe, un nombre de tentatives.

        On force `Connection: close` pour ne PAS réutiliser le pool de
        connexions de requests entre 2 appels. Constat prod 2026-05-24 :
        le LB devant le Drive ferme silencieusement les sockets idle, et
        urllib3 réutilise la conn morte → ConnectionResetError sur EVERY
        retry. Une nouvelle TCP par requête contourne le bug.

        On NE retry PAS les 4xx/5xx applicatifs : le caller veut savoir
        si c'est auth vs not_found vs transient. Seul le RequestException
        au niveau socket déclenche le retry.

        ``max_attempts`` est un paramètre et non une constante parce que la
        rejouabilité dépend du verbe : un GET est sans effet de bord, un
        POST /children/ rejoué après un échec réseau tardif créerait un
        second item (renommé `brief_01.md`) invisible côté appelant.
        """
        import time as _time
        last_exc = None
        delay = 0.5
        headers = {"Connection": "close"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        if extra_headers:
            headers.update(extra_headers)
        verb = method.lower()
        for attempt in range(1, max_attempts + 1):
            try:
                # Chaque appel a sa propre Session pour éviter qu'urllib3
                # ré-utilise un pool de connexions partagé via le singleton
                # req.get(). Session fermée explicitement après usage.
                with req.Session() as s:
                    return getattr(s, verb)(
                        url, headers=headers, timeout=timeout or self.timeout, **kwargs
                    )
            except req.RequestException as exc:
                last_exc = exc
                if attempt == max_attempts:
                    break
                logger.warning(
                    "drive_client.%s retry %d/%d after %s: %s",
                    label or verb, attempt, max_attempts, type(exc).__name__, exc,
                )
                _time.sleep(delay)
                delay *= 3
        raise DriveTransientError(
            f"Drive {label or verb} unreachable after {max_attempts} attempts: {last_exc}"
        ) from last_exc

    def _get_with_retry(self, url: str, *, access_token: str,
                         timeout: Optional[int] = None,
                         max_attempts: int = 3,
                         label: str = "GET"):
        """GET rejouable — le seul verbe sans effet de bord côté Drive."""
        return self._request_with_retry(
            "GET", url, access_token=access_token, timeout=timeout,
            max_attempts=max_attempts, label=label,
        )

    # ── Step 2: metadata ─────────────────────────────────────

    def get_item(self, access_token: str, item_id: str) -> dict:
        """GET /api/v1.0/items/{id}/ → item metadata dict."""
        url = f"{self.base_url}{_API_PREFIX}/items/{item_id}/"
        resp = self._get_with_retry(url, access_token=access_token, label=f"GET item {item_id}")
        self._raise_for_status(resp, context=f"GET items/{item_id}")
        try:
            data = resp.json()
        except Exception as exc:
            raise DriveTransientError(f"Drive item response not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise DriveApplicativeError(f"Drive item response is not an object: {type(data).__name__}")
        return data

    # ── Step 3: list children ────────────────────────────────

    def list_children(self, access_token: str, parent_id: str) -> list[dict]:
        """
        GET /api/v1.0/items/{id}/children/ → list of items inside a folder.

        Handles the two common DRF list shapes: a plain ``[...]`` array or a
        paginated ``{"results": [...]}`` envelope. Pagination beyond the first
        page is not followed — folder previews stop at the first ~50-100
        entries which is more than enough for a meeting prep UI.
        """
        url = f"{self.base_url}{_API_PREFIX}/items/{parent_id}/children/"
        resp = self._get_with_retry(url, access_token=access_token,
                                     label=f"list children {parent_id}")
        self._raise_for_status(resp, context=f"GET items/{parent_id}/children")
        items, _ = self._parse_item_page(resp, context=f"GET items/{parent_id}/children")
        return items

    def list_children_paginated(self, access_token: str, parent_id: str, *,
                                page_size: int = 200,
                                max_pages: int = 10) -> list[dict]:
        """Contenu COMPLET d'un dossier, pages suivies.

        ``list_children`` s'arrête à la première page. Or la pagination du
        Drive est à 20 éléments par défaut : un dossier de 25 documents en
        perdait 5, silencieusement. Un navigateur de fichiers ne peut pas se
        le permettre — l'utilisateur croirait le dossier plus petit qu'il
        n'est.

        On demande ``page_size`` (plafonné à 200 côté serveur) puis on suit
        l'URL ``next`` renvoyée, plutôt que de fabriquer nous-mêmes le
        paramètre de page : c'est la seule façon compatible avec les trois
        paginateurs de DRF. ``max_pages`` borne le coût sur un dossier
        anormalement gros.
        """
        url = (f"{self.base_url}{_API_PREFIX}/items/{parent_id}/children/"
               f"?page_size={int(page_size)}")
        out: list[dict] = []
        for _ in range(max(1, max_pages)):
            resp = self._get_with_retry(url, access_token=access_token,
                                        label=f"list children {parent_id}")
            self._raise_for_status(resp, context=f"GET items/{parent_id}/children")
            items, next_url = self._parse_item_page(
                resp, context=f"GET items/{parent_id}/children")
            out.extend(items)
            if not next_url:
                break
            # Garde : ne jamais suivre une URL hors du Drive. Sans ce test, un
            # `next` forgé enverrait le jeton porteur de l'utilisateur vers un
            # hôte arbitraire.
            if not str(next_url).startswith(self.base_url):
                logger.warning("drive: next page ignorée (hôte étranger)")
                break
            url = next_url
        return out

    def list_roots(self, access_token: str) -> list[dict]:
        """
        GET /api/v1.0/items/ → les items racine de l'utilisateur.

        Le workspace personnel s'y repère par ``main_workspace: true``. C'est
        le seul parent sous lequel on est sûr de pouvoir écrire : les autres
        racines sont des partages, potentiellement en lecture seule.
        """
        url = f"{self.base_url}{_API_PREFIX}/items/"
        resp = self._get_with_retry(url, access_token=access_token, label="list roots")
        self._raise_for_status(resp, context="GET items/")
        items, _ = self._parse_item_page(resp, context="GET items/")
        return items

    # ── Step 4: download binary ──────────────────────────────

    def download_item(self, access_token: str, item_id: str, max_bytes: Optional[int] = None) -> tuple[bytes, str]:
        """
        Fetch the binary content of an item. Returns ``(content, content_type)``.

        Strategy:
          1. GET the metadata.
          2. If it includes one of the known URL fields, GET that URL.
             (Presigned S3 URLs don't need our Authorization header — and
             passing one can cause AWS to 400 on the signed request — so we
             only re-send the bearer when the URL is on the same host as
             the Drive itself.)
          3. Else fall back to GET /api/v1.0/items/{id}/download/ with bearer.

        ``max_bytes`` caps the in-memory payload — anything larger raises
        ``DriveApplicativeError`` rather than blowing up the worker.
        """
        metadata = self.get_item(access_token, item_id)

        download_url = None
        for key in _DOWNLOAD_URL_KEYS:
            candidate = metadata.get(key)
            if isinstance(candidate, str) and candidate.strip():
                download_url = candidate
                break

        if download_url:
            send_bearer = download_url.startswith(self.base_url + "/") or download_url.startswith(self.base_url + ":")
        else:
            download_url = f"{self.base_url}{_API_PREFIX}/items/{item_id}/download/"
            send_bearer = True

        # DEBUG : log la première URL téléchargée pour diagnostiquer où ça
        # plante. À retirer une fois le bug RST résolu.
        logger.info(
            "drive_client.download_item: item=%s download_url=%s send_bearer=%s",
            item_id, download_url[:200], send_bearer,
        )
        # Retry sur ConnectionReset/timeout — fréquent sur les gros docs.
        # Sur la branche presigned-S3, le bearer est volontairement omis :
        # AWS 400 sur une requête signée qui porte un Authorization étranger.
        resp = self._request_with_retry(
            "GET", download_url,
            access_token=access_token if send_bearer else None,
            timeout=self.download_timeout,
            label=f"download item {item_id}",
            stream=False,
        )
        if resp.status_code in (401, 403):
            raise DriveAuthError(
                f"Drive download {resp.status_code} for item {item_id}",
                status_code=resp.status_code,
            )
        if resp.status_code >= 500:
            raise DriveTransientError(f"Drive download 5xx: {resp.status_code}")
        if resp.status_code >= 400:
            raise DriveApplicativeError(f"Drive download {resp.status_code}: {(resp.text or '')[:200]}")
        body = resp.content
        if max_bytes is not None and len(body) > max_bytes:
            raise DriveApplicativeError(
                f"Drive item {item_id} exceeds max_bytes={max_bytes} (got {len(body)})"
            )
        content_type = resp.headers.get("Content-Type") or metadata.get("mime_type") or "application/octet-stream"
        return body, content_type

    # ── Step 5: écritures (versement du brief) ───────────────

    def find_child_by_title(self, access_token: str, parent_id: str, title: str,
                            *, item_type: Optional[str] = None) -> Optional[dict]:
        """Retourne l'enfant dont le titre est EXACTEMENT ``title``, ou None.

        Le filtre `title` du Drive est un ``unaccent__icontains`` : il sert à
        réduire la page, jamais à décider. L'égalité est refaite ici, sinon
        « brief.md » sélectionnerait aussi « ancien-brief.md ».
        """
        if not title:
            return None
        url = f"{self.base_url}{_API_PREFIX}/items/{parent_id}/children/"
        params: Optional[dict] = {"title": title, "page_size": _MAX_PAGE_SIZE}
        if item_type:
            params["type"] = item_type
        context = f"GET items/{parent_id}/children?title="
        for _ in range(_MAX_LOOKUP_PAGES):
            resp = self._request_with_retry(
                "GET", url, access_token=access_token, params=params,
                label=f"find child in {parent_id}",
            )
            self._raise_for_status(resp, context=context)
            items, next_url = self._parse_item_page(resp, context=context)
            for item in items:
                if title in (item.get("title"), item.get("filename")):
                    return item
            # Garde SSRF : le `next` est du contenu serveur et porte notre
            # bearer, on ne le suit que s'il reste sur le Drive interrogé.
            if not (isinstance(next_url, str) and next_url.startswith(self.base_url)):
                return None
            url, params = next_url, None
        logger.warning(
            "drive_client.find_child_by_title: %d pages parcourues sans égalité "
            "sur '%s' dans %s — recherche abandonnée", _MAX_LOOKUP_PAGES, title, parent_id,
        )
        return None

    def create_folder(self, access_token: str, parent_id: str, title: str) -> dict:
        """POST /items/<parent>/children/ {type: folder} → l'item créé.

        **Jamais rejoué** : un échec réseau après traitement serveur laisserait
        un dossier bien créé, que la seconde tentative dupliquerait en
        « <titre>_01 » — le Drive renomme silencieusement les homonymes et
        rend un 201 quand même.
        """
        url = f"{self.base_url}{_API_PREFIX}/items/{parent_id}/children/"
        resp = self._request_with_retry(
            "POST", url, access_token=access_token, max_attempts=1,
            label=f"create folder in {parent_id}",
            json={"type": _TYPE_FOLDER, "title": title},
        )
        context = f"POST items/{parent_id}/children (folder '{title}')"
        self._raise_for_status(resp, context=context)
        return self._parse_created_item(resp, context=context)

    def upload_file(self, access_token: str, parent_id: str, filename: str,
                    content: bytes,
                    content_type: str = "application/octet-stream") -> dict:
        """Dépose un fichier en trois temps : create → PUT policy → upload-ended.

        Les trois appels s'enchaînent sans pause : la policy présignée expire
        en ~60 s (``AWS_S3_UPLOAD_POLICY_EXPIRATION``), on ne peut donc pas
        pré-créer les items d'un lot pour les téléverser ensuite.

        Si le PUT ou la finalisation échoue, l'item est supprimé : resté en
        PENDING il est exclu des listings (``_exclude_pending_items`` côté
        Drive) tout en occupant le titre — le versement suivant serait renommé
        « brief_01.md » sans que rien d'anormal ne soit visible.
        """
        url = f"{self.base_url}{_API_PREFIX}/items/{parent_id}/children/"
        resp = self._request_with_retry(
            "POST", url, access_token=access_token, max_attempts=1,
            label=f"create file in {parent_id}",
            json={"type": _TYPE_FILE, "filename": filename},
        )
        context = f"POST items/{parent_id}/children (file '{filename}')"
        self._raise_for_status(resp, context=context)
        item = self._parse_created_item(resp, context=context)
        item_id = item.get("id")

        try:
            policy = item.get("policy")
            if not isinstance(policy, str) or not policy.strip():
                raise DriveApplicativeError(
                    f"Drive n'a pas rendu de policy d'upload pour '{filename}' "
                    "(item déjà READY ou serializer inattendu)"
                )
            self._put_to_policy(policy, content, content_type=content_type,
                                filename=filename)
            self._upload_ended(access_token, item_id, filename=filename)
        except DriveError:
            self._delete_item_quietly(access_token, item_id)
            raise
        return item

    def delete_item(self, access_token: str, item_id: str) -> bool:
        """DELETE /items/<id>/ — soft delete (corbeille 30 j côté Drive).

        Un 404 compte comme un succès : l'objectif est que le titre soit
        libre, et il l'est. C'est aussi ce qui rend l'appel rejouable.
        """
        url = f"{self.base_url}{_API_PREFIX}/items/{item_id}/"
        resp = self._request_with_retry(
            "DELETE", url, access_token=access_token, max_attempts=2,
            label=f"delete item {item_id}",
        )
        if resp.status_code == 404:
            return True
        self._raise_for_status(resp, context=f"DELETE items/{item_id}")
        return True

    # ── Internals ───────────────────────────────────────────

    def _put_to_policy(self, policy_url: str, content: bytes, *,
                       content_type: str, filename: str):
        """PUT du corps sur l'URL présignée rendue par la création."""
        headers = {}
        for name in policy_signed_headers(policy_url):
            if name == "host":
                continue  # porté par l'URL, requests le pose lui-même
            if name == "content-type":
                headers["Content-Type"] = content_type
            elif name == "content-length":
                headers["Content-Length"] = str(len(content))
            elif name == "x-amz-acl":
                headers["x-amz-acl"] = _UPLOAD_ACL
            else:
                logger.warning(
                    "drive_client.upload_file: en-tête signé inconnu '%s' pour "
                    "'%s' — non transmis, le PUT va probablement échouer",
                    name, filename,
                )
        label = f"PUT policy '{filename}'"
        try:
            resp = self._request_with_retry(
                "PUT", policy_url, max_attempts=1, label=label,
                timeout=self.download_timeout, extra_headers=headers, data=content,
            )
        except DriveTransientError:
            expires_at = policy_expires_at(policy_url)
            import time as _time
            if expires_at is None or expires_at - _time.time() < _POLICY_RETRY_MARGIN_SECONDS:
                raise
            logger.warning("drive_client.%s: rejeu unique (policy encore valide)", label)
            resp = self._request_with_retry(
                "PUT", policy_url, max_attempts=1, label=label,
                timeout=self.download_timeout, extra_headers=headers, data=content,
            )
        # Pas de `_raise_for_status` ici : un 403 vient de S3 (signature morte
        # ou en-têtes mal alignés), pas de notre bearer — le présenter comme
        # une DriveAuthError enverrait l'utilisateur se reconnecter pour rien.
        if resp.status_code >= 500:
            raise DriveTransientError(f"{label} → {resp.status_code}")
        if resp.status_code >= 400:
            raise DriveApplicativeError(f"{label} → {resp.status_code}: {(resp.text or '')[:200]}")
        return resp

    def _upload_ended(self, access_token: str, item_id: str, *, filename: str) -> None:
        """POST /items/<id>/upload-ended/ — bascule PENDING → analyse."""
        url = f"{self.base_url}{_API_PREFIX}/items/{item_id}/upload-ended/"
        resp = self._request_with_retry(
            "POST", url, access_token=access_token, max_attempts=1,
            label=f"upload-ended '{filename}'", json={},
        )
        if resp.status_code == 400 and self._has_error_code(
            resp, "item_upload_state_not_pending"
        ):
            # Clé d'idempotence offerte par l'API : l'item n'est plus PENDING,
            # donc un premier appel est bien passé (réponse perdue en route).
            logger.info(
                "drive_client.upload_ended: '%s' déjà finalisé (item=%s) — succès",
                filename, item_id,
            )
            return
        self._raise_for_status(resp, context=f"POST items/{item_id}/upload-ended")

    def _delete_item_quietly(self, access_token: str, item_id: Optional[str]) -> None:
        """Suppression de rattrapage — n'écrase jamais l'erreur d'origine."""
        if not item_id:
            return
        try:
            self.delete_item(access_token, item_id)
        except DriveError as exc:
            logger.warning(
                "drive_client: item PENDING %s non supprimé (%s) — il squatte "
                "son titre en restant invisible dans les listings", item_id, exc,
            )

    @staticmethod
    def _parse_item_page(resp, context: str) -> tuple[list[dict], Optional[str]]:
        """Découpe une réponse de listing en (items, url de page suivante)."""
        try:
            data = resp.json()
        except Exception as exc:
            raise DriveTransientError(f"Drive response not JSON ({context}): {exc}") from exc
        next_url = None
        if isinstance(data, dict) and "results" in data:
            next_url = data.get("next")
            data = data["results"]
        if not isinstance(data, list):
            raise DriveApplicativeError(
                f"Drive children response is neither array nor paginated envelope: {type(data).__name__}"
            )
        return [item for item in data if isinstance(item, dict)], next_url

    @staticmethod
    def _parse_created_item(resp, context: str) -> dict:
        try:
            data = resp.json()
        except Exception as exc:
            raise DriveTransientError(f"Drive create response not JSON ({context}): {exc}") from exc
        if not isinstance(data, dict) or not data.get("id"):
            raise DriveApplicativeError(f"Drive create response without id ({context})")
        return data

    @staticmethod
    def _has_error_code(resp, code: str) -> bool:
        """Cherche un code d'erreur dans une réponse drf-standardized-errors."""
        try:
            errors = (resp.json() or {}).get("errors") or []
            if any(isinstance(e, dict) and e.get("code") == code for e in errors):
                return True
        except Exception:
            pass
        return code in (resp.text or "")

    @staticmethod
    def _raise_for_status(resp, context: str) -> None:
        if resp.status_code in (401, 403):
            raise DriveAuthError(
                f"{context} → {resp.status_code} (token rejected by Drive)",
                status_code=resp.status_code,
            )
        if resp.status_code >= 500:
            raise DriveTransientError(f"{context} → {resp.status_code}")
        if resp.status_code >= 400:
            raise DriveApplicativeError(f"{context} → {resp.status_code}: {(resp.text or '')[:200]}")
