"""Tests du rate-limiting et de l'extraction d'IP proxy-aware (durcissement QR)."""

from libs.shared.app.rate_limit import client_ip, SlidingWindowLimiter


# ─── client_ip : proxy-aware ───────────────────────────────────────────────

def test_client_ip_uses_remote_addr_without_xff():
    assert client_ip(remote_addr="203.0.113.5", forwarded_for=None) == "203.0.113.5"


def test_client_ip_trusts_last_hop_with_one_proxy():
    # nginx ajoute l'IP réelle en fin de liste : "<client>" suivi du vrai pair.
    xff = "198.51.100.9"  # un seul hop ajouté par l'ingress
    assert client_ip(remote_addr="10.0.0.1", forwarded_for=xff, trusted_proxy_hops=1) == "198.51.100.9"


def test_client_ip_ignores_forged_xff_prefix():
    # L'attaquant force un XFF "1.2.3.4" ; l'ingress y ajoute l'IP réelle.
    xff = "1.2.3.4, 198.51.100.9"
    real = client_ip(remote_addr="10.0.0.1", forwarded_for=xff, trusted_proxy_hops=1)
    assert real == "198.51.100.9"  # le préfixe forgé n'est pas retenu


def test_client_ip_two_trusted_hops():
    # Deux proxys de confiance (ex. LB + ingress) : on remonte de 2.
    xff = "1.2.3.4, 203.0.113.7, 10.0.0.2"
    assert client_ip(remote_addr="10.0.0.9", forwarded_for=xff, trusted_proxy_hops=2) == "203.0.113.7"


# ─── SlidingWindowLimiter ──────────────────────────────────────────────────

def test_limiter_allows_up_to_max_then_blocks():
    lim = SlidingWindowLimiter(max_events=3, window_seconds=60)
    assert lim.allow("ip1", now=1000.0) is True
    assert lim.allow("ip1", now=1000.1) is True
    assert lim.allow("ip1", now=1000.2) is True
    assert lim.allow("ip1", now=1000.3) is False  # 4e bloqué


def test_limiter_window_slides():
    lim = SlidingWindowLimiter(max_events=2, window_seconds=10)
    assert lim.allow("ip1", now=0.0) is True
    assert lim.allow("ip1", now=1.0) is True
    assert lim.allow("ip1", now=2.0) is False
    # Au-delà de la fenêtre, les anciens événements expirent.
    assert lim.allow("ip1", now=12.0) is True


def test_limiter_isolated_per_key():
    lim = SlidingWindowLimiter(max_events=1, window_seconds=60)
    assert lim.allow("a", now=1.0) is True
    assert lim.allow("b", now=1.0) is True  # clé différente, non impactée
    assert lim.allow("a", now=1.1) is False


def test_limiter_many_users_same_ip_not_starved_by_generous_cap():
    # Borne généreuse : N requêtes légitimes d'utilisateurs derrière une NAT.
    lim = SlidingWindowLimiter(max_events=50, window_seconds=60)
    allowed = sum(1 for i in range(40) if lim.allow("nat-ip", now=1000.0 + i * 0.01))
    assert allowed == 40
