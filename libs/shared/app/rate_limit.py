"""Rate-limiting léger + extraction d'IP client proxy-aware.

Pensé pour le durcissement de la vérification libre du code court (QR) :
- ``client_ip`` extrait l'**IP client réelle** derrière le reverse-proxy, en
  ne faisant confiance qu'au(x) dernier(s) hop(s) (le XFF est posé par
  l'ingress ; un XFF arbitraire fourni par le client ne doit pas leurrer le
  compteur) ;
- ``SlidingWindowLimiter`` est un limiteur en mémoire (par pod) à fenêtre
  glissante, volontairement généreux : il borne l'abus depuis une source
  sans pénaliser une organisation entière derrière une IP NAT (le verrou
  par-code en base reste l'autorité fine).
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from typing import Deque, Dict, Optional


def client_ip(
    *,
    remote_addr: Optional[str],
    forwarded_for: Optional[str],
    trusted_proxy_hops: int = 1,
) -> str:
    """IP client réelle.

    ``forwarded_for`` = en-tête ``X-Forwarded-For`` (liste séparée par des
    virgules). L'ingress **ajoute** l'IP du pair direct à la fin de la liste ;
    le hop fiable est donc le ``trusted_proxy_hops``-ième en partant de la
    droite. Un préfixe XFF forgé par le client se retrouve à gauche et ne peut
    pas déplacer ce hop.
    """
    hops = max(1, int(trusted_proxy_hops or 1))
    if forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if parts:
            idx = len(parts) - hops
            if idx < 0:
                idx = 0
            return parts[idx]
    return (remote_addr or "").strip()


class SlidingWindowLimiter:
    """Limiteur à fenêtre glissante, thread-safe, en mémoire.

    ``max_events`` autorisés par clé sur ``window_seconds``. Non distribué
    (par pod) : suffisant comme borne anti-abus de source, le contrôle fin
    multi-réplicas étant porté par l'état en base.
    """

    def __init__(self, max_events: int, window_seconds: float):
        self.max_events = max(1, int(max_events))
        self.window = float(window_seconds)
        self._events: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float) -> bool:
        """True si l'événement est autorisé ; l'enregistre le cas échéant."""
        if not key:
            return True
        cutoff = now - self.window
        with self._lock:
            dq = self._events[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max_events:
                return False
            dq.append(now)
            return True

    def reset(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._events.clear()
            else:
                self._events.pop(key, None)
