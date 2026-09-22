"""Faux services HTTP pour les tests de bout en bout.

Chaque faux est un vrai serveur HTTP (thread) : le code testé parle réseau
comme en production, avec la vraie bibliothèque ``requests``. On ne
remplace ni module ni fonction ; on remplace l'AUTRE bout du câble.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse


class Recorded:
    """Une requête reçue par un faux service."""

    def __init__(self, method, path, query, headers, body):
        self.method, self.path, self.query, self.headers, self.body = (
            method, path, query, headers, body)

    def __repr__(self):
        return f"<{self.method} {self.path} {self.body!r}>"


class FakeService:
    """Serveur HTTP local piloté par une fonction ``handler(req) -> (status, body)``.

    ``body`` peut être un dict (JSON) ou une chaîne (texte brut, utile pour
    reproduire un corps d'erreur au caractère près).
    """

    def __init__(self, handler: Callable[[Recorded], tuple[int, object]]):
        self.requests: list[Recorded] = []
        self._handler = handler
        outer = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *_a):  # silence
                pass

            def _serve(self):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                ctype = self.headers.get("Content-Type", "")
                body = raw
                if "application/json" in ctype:
                    try:
                        body = json.loads(raw or b"{}")
                    except ValueError:
                        body = raw
                rec = Recorded(self.command, u.path,
                               {k: v[0] for k, v in parse_qs(u.query).items()},
                               dict(self.headers), body)
                outer.requests.append(rec)
                status, payload = outer._handler(rec)
                if isinstance(payload, (dict, list)):
                    data = json.dumps(payload).encode()
                    ctype_out = "application/json"
                else:
                    data = (payload or "").encode()
                    ctype_out = "application/json" if data[:1] in (b"{", b"[") else "text/plain"
                self.send_response(status)
                self.send_header("Content-Type", ctype_out)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = _serve

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def start(self) -> "FakeService":
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()

    def calls(self, method=None, path_prefix=""):
        return [r for r in self.requests
                if (method is None or r.method == method) and r.path.startswith(path_prefix)]
