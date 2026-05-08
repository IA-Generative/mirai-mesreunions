import importlib.util
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MODULE_PATH = os.path.join(ROOT, "libs", "shared", "app", "device_fingerprint.py")
SPEC = importlib.util.spec_from_file_location("device_fingerprint", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MOD)

compute_fp_hash = MOD.compute_fp_hash


# Realistic Chrome browser vs Chrome PWA on the same Android device. The PWA
# user_agent gains a 'wv' marker and the screen height shrinks because the
# system status bar is gone; platform/language/timezone stay identical.
ANDROID_BROWSER_FP = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) Chrome/121|"
    "Linux armv8l|fr-FR|412|915|Europe/Paris"
)
ANDROID_PWA_FP = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7; wv) Chrome/121|"
    "Linux armv8l|fr-FR|412|892|Europe/Paris"
)


def test_empty_fingerprint_returns_empty_string():
    assert compute_fp_hash("") == ""
    assert compute_fp_hash("   ") == ""


def test_returns_64_char_sha256_hex():
    h = compute_fp_hash("ua|MacIntel|en-US|1920|1080|UTC")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_browser_and_pwa_fingerprints_collapse_to_same_hash():
    """Same device, different launch contexts → fusion candidate."""
    assert compute_fp_hash(ANDROID_BROWSER_FP) == compute_fp_hash(ANDROID_PWA_FP)


def test_user_agent_change_alone_does_not_change_hash():
    """user_agent is intentionally excluded — must be ignored entirely."""
    a = compute_fp_hash("UA1|MacIntel|fr-FR|1|1|Europe/Paris")
    b = compute_fp_hash("UA2|MacIntel|fr-FR|1|1|Europe/Paris")
    assert a == b


def test_screen_size_change_does_not_change_hash():
    """Screen dimensions diverge between PWA standalone and browser tab."""
    a = compute_fp_hash("ua|MacIntel|fr-FR|1440|900|Europe/Paris")
    b = compute_fp_hash("ua|MacIntel|fr-FR|390|844|Europe/Paris")
    assert a == b


def test_different_platform_yields_different_hash():
    """A genuinely different device type must not collide."""
    mac = compute_fp_hash("ua|MacIntel|fr-FR|1|1|Europe/Paris")
    win = compute_fp_hash("ua|Win32|fr-FR|1|1|Europe/Paris")
    assert mac != win


def test_different_timezone_yields_different_hash():
    a = compute_fp_hash("ua|MacIntel|fr-FR|1|1|Europe/Paris")
    b = compute_fp_hash("ua|MacIntel|fr-FR|1|1|America/New_York")
    assert a != b


def test_case_insensitive_for_stable_fields():
    """Avoid spurious mismatches from casing differences across browsers."""
    lower = compute_fp_hash("ua|macintel|fr-fr|1|1|europe/paris")
    upper = compute_fp_hash("ua|MacIntel|FR-FR|1|1|Europe/Paris")
    assert lower == upper


def test_too_short_fingerprint_returns_empty():
    """Defensive: malformed client input must not raise or produce a useful hash."""
    assert compute_fp_hash("|||||") == ""
    assert compute_fp_hash("only-ua") == ""
