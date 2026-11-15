"""Identité Passage2 : confiance dans le proxy, en-têtes, groupes, fail-closed."""

import pytest

from libs.shared.app.passage2 import (
    Passage2Config,
    Passage2Error,
    Passage2StartupError,
    read_identity,
)

SECRET = "s3cr3t-proxy-" + "x" * 32
HEADERS = {
    "Mellon-NameID": "Jeanne.Martin@interieur.gouv.fr",
    "Mellon-Groups": "agents,dnum",
    "X-Passage2-Proxy-Secret": SECRET,
}


def cfg(**env):
    base = {"ENVIRONMENT": "production", "PASSAGE2_PROXY_SECRET": SECRET}
    base.update(env)
    return Passage2Config.from_env({k: v for k, v in base.items() if v is not None})


def test_reads_identity_from_trusted_proxy():
    identity = read_identity(cfg(), HEADERS, "10.0.0.5")
    assert identity.email == "jeanne.martin@interieur.gouv.fr"
    assert identity.name_id == "Jeanne.Martin@interieur.gouv.fr"
    assert identity.groups == ("agents", "dnum")


@pytest.mark.parametrize("secret", ["", "wrong", SECRET[:-1]])
def test_rejects_requests_that_bypass_the_proxy(secret):
    with pytest.raises(Passage2Error) as err:
        read_identity(cfg(), {**HEADERS, "X-Passage2-Proxy-Secret": secret}, "10.0.0.5")
    assert err.value.code == "untrusted_proxy"


def test_trusted_proxy_cidr():
    c = cfg(PASSAGE2_PROXY_SECRET=None, PASSAGE2_TRUSTED_PROXIES="10.42.0.0/16")
    assert read_identity(c, HEADERS, "10.42.3.4").email
    with pytest.raises(Passage2Error):
        read_identity(c, HEADERS, "192.168.1.1")
    with pytest.raises(Passage2Error):
        read_identity(c, HEADERS, None)


def test_missing_name_id_is_not_authenticated():
    headers = {k: v for k, v in HEADERS.items() if k != "Mellon-NameID"}
    with pytest.raises(Passage2Error) as err:
        read_identity(cfg(), headers, "10.0.0.5")
    assert err.value.code == "not_authenticated"


def test_requires_an_email():
    c = cfg(PASSAGE2_EMAIL_HEADER="Mellon-mail")
    with pytest.raises(Passage2Error) as err:
        read_identity(c, HEADERS, "10.0.0.5")
    assert err.value.code == "email_missing"
    ok = read_identity(c, {**HEADERS, "Mellon-mail": "a.b@x.fr"}, "10.0.0.5")
    assert ok.email == "a.b@x.fr"


def test_group_restriction():
    assert read_identity(cfg(PASSAGE2_REQUIRED_GROUPS="dnum"), HEADERS, "1.1.1.1")
    with pytest.raises(Passage2Error) as err:
        read_identity(cfg(PASSAGE2_REQUIRED_GROUPS="admins"), HEADERS, "1.1.1.1")
    assert err.value.code == "forbidden_group"


@pytest.mark.parametrize("env", [
    {"PASSAGE2_PROXY_SECRET": None},
    {"PASSAGE2_PROXY_SECRET": "short"},
    {"PASSAGE2_DEV_IDENTITY": "dev@x.fr"},
    {"PASSAGE2_PROXY_SECRET": None, "PASSAGE2_TRUSTED_PROXIES": "not-a-cidr"},
])
def test_refuses_to_boot_open(env):
    with pytest.raises(Passage2StartupError):
        cfg(**env)


def test_dev_identity_only_in_development():
    c = cfg(ENVIRONMENT="development", PASSAGE2_PROXY_SECRET=None,
            PASSAGE2_DEV_IDENTITY="dev@x.fr")
    assert read_identity(c, {}, "127.0.0.1").email == "dev@x.fr"
