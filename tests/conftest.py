"""Isolation des modules entre fichiers de test (unit, regression, e2e).

Beaucoup de tests de ce dossier remplacent des modules dans ``sys.modules``
(``requests``, ``libs.shared.app.config``, le paquet ``app`` d'un service…)
et poussent le dossier d'un service en tête de ``sys.path`` pour charger ce
service sans sa base ni son bus. Ces gestes survivaient au fichier qui les
avait faits : le fichier suivant importait un ``requests`` sans ``Session``,
un ``config`` sans ``ALLOWED_AUDIO_EXTENSIONS``, ou le paquet ``app`` d'un
AUTRE service. Résultat : une vingtaine d'échecs « de pollution », acceptés
comme dette (cf. ``tests/run-regression-campaign.sh``), et une collecte qui
s'interrompait avant la fin.

Principe : chaque fichier de test voit exactement l'environnement qu'il
s'est construit, et rien de ce que les autres ont construit.

  1. Autour de la COLLECTE (l'import du fichier) : on relève ce que le
     fichier a ajouté ou remplacé dans ``sys.modules`` et ``sys.path``
     (son « delta »), puis on remet l'état d'avant.
  2. Autour de l'EXÉCUTION de ses tests : on rejoue ce delta — les objets
     mêmes qu'il avait importés, pour que ``mock.patch("services.…")`` et
     les bouchons posés au niveau module restent visibles — puis on remet
     l'état d'avant.

Seuls les modules « gérés » sont touchés : ceux du dépôt (``app``,
``libs``, ``services``, ``tools``), les chargements ``*_under_test`` et
les bouchons sans fichier. Les bibliothèques réelles ne sont jamais
déchargées.
"""
from __future__ import annotations

import sys

import pytest

_PROJECT_TOP_LEVEL = {"app", "libs", "services", "tools"}
_ATTR = "_mesreunions_import_delta"


def _is_stub(mod) -> bool:
    """Un ``types.ModuleType`` posé à la main : ni fichier, ni spec."""
    return getattr(mod, "__file__", None) is None and getattr(mod, "__spec__", None) is None


def _managed(name: str, mod) -> bool:
    if name.split(".", 1)[0] in _PROJECT_TOP_LEVEL:
        return True
    if "_under_test" in name:
        return True
    return _is_stub(mod)


class _Snapshot:
    def __init__(self):
        self.mods = dict(sys.modules)
        self.path = list(sys.path)

    def delta(self) -> dict:
        """Ce qui a changé depuis la prise de vue, restreint aux modules gérés."""
        added, replaced = {}, {}
        for name, current in sys.modules.items():
            before = self.mods.get(name)
            if before is current:
                continue
            if before is None:
                if _managed(name, current):
                    added[name] = current
            elif _managed(name, current) or _managed(name, before):
                replaced[name] = current
        removed = [n for n, m in self.mods.items()
                   if n not in sys.modules and _managed(n, m)]
        path_added = [p for p in sys.path if p not in self.path]
        return {"added": added, "replaced": replaced, "removed": removed,
                "path_added": path_added}

    def restore(self):
        for name in list(sys.modules):
            current = sys.modules[name]
            before = self.mods.get(name)
            if before is current:
                continue
            if before is None:
                if _managed(name, current):
                    del sys.modules[name]
            elif _managed(name, current) or _managed(name, before):
                sys.modules[name] = before
        for name, before in self.mods.items():
            if name not in sys.modules and _managed(name, before):
                sys.modules[name] = before
        sys.path[:] = self.path


def _replay(delta: dict) -> None:
    for name in delta["removed"]:
        sys.modules.pop(name, None)
    sys.modules.update(delta["added"])
    sys.modules.update(delta["replaced"])
    for p in reversed(delta["path_added"]):
        if p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    """Autour de l'import d'un fichier de test : relever son delta, puis
    rendre l'état d'avant au fichier suivant."""
    if isinstance(collector, pytest.Module):
        snap = _Snapshot()
        yield
        setattr(collector, _ATTR, snap.delta())
        snap.restore()
    else:
        yield


@pytest.fixture(autouse=True, scope="module")
def _isolate_sys_modules(request):
    """Autour de l'exécution des tests d'un fichier : rejouer son delta
    d'import, puis rendre l'état d'avant au fichier suivant."""
    snap = _Snapshot()
    delta = getattr(request.node, _ATTR, None)
    if delta:
        _replay(delta)
    yield
    snap.restore()
