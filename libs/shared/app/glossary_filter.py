"""Heuristiques pour décider si un terme candidat appartient au
glossaire personnel d'un utilisateur ou s'il s'agit de bruit.

Utilisé à la fois :
- À l'ingestion (``device-token-authority`` ``upsert_user_glossary_batch``)
  → rejet silencieux des bruits évidents pour ne pas polluer la liste.
- À la purge rétroactive (endpoint ``/cleanup``) → identifie les termes
  qui n'auraient PAS dû entrer aujourd'hui, candidats à la suppression.

Règles cumulatives (un terme passe si AU MOINS une règle "keep" matche
ET qu'aucune règle "drop" ne matche). Pure function : pas d'IO, pas
de dépendance, testable unitairement.

Termes ``curated_by_user=True`` ne doivent JAMAIS être filtrés par ce
module — la décision de l'utilisateur prime. Le caller est responsable
de cette protection.
"""

from __future__ import annotations

import re
from typing import Tuple

# ── Stopwords FR étendus ──────────────────────────────────────────────
# Liste manuelle des mots les plus susceptibles de polluer le glossaire :
# articles, pronoms, prépositions, conjonctions, auxiliaires, mots
# courants des comptes-rendus (réunion, point, sujet…). Toutes les
# entrées sont en minuscules ; la vérification utilise term.lower().
_STOPWORDS_FR = {
    # Articles, déterminants
    "le", "la", "les", "un", "une", "des", "de", "du", "au", "aux",
    "ce", "cet", "cette", "ces", "mon", "ma", "mes", "ton", "ta", "tes",
    "son", "sa", "ses", "notre", "votre", "leur", "leurs", "nos", "vos",
    "tout", "toute", "tous", "toutes", "même", "mêmes", "autre", "autres",
    "chaque", "plusieurs", "quelques",
    # Pronoms
    "je", "tu", "il", "elle", "on", "nous", "vous", "ils", "elles",
    "me", "te", "se", "lui", "leur", "y", "en", "que", "qui", "quoi",
    "dont", "où", "celui", "celle", "ceux", "celles",
    "qui", "ce", "ça", "cela", "ceci",
    # Prépositions / conjonctions
    "à", "de", "en", "par", "pour", "sur", "sous", "dans", "avec", "sans",
    "vers", "chez", "entre", "contre", "selon", "depuis", "pendant", "avant",
    "après", "jusque", "jusqu", "près", "loin", "hors",
    "et", "ou", "mais", "donc", "or", "ni", "car", "si", "sinon",
    "comme", "ainsi", "alors", "puis", "ensuite", "enfin", "aussi",
    # Auxiliaires + verbes très fréquents (formes conjuguées courantes)
    "être", "avoir", "faire", "aller", "venir", "voir", "savoir", "pouvoir",
    "vouloir", "devoir", "dire", "prendre", "donner", "mettre", "passer",
    "trouver", "rester", "tenir", "porter", "parler", "aimer", "permettre",
    "est", "sont", "était", "étaient", "sera", "seront", "a", "ont", "avait",
    "avaient", "aura", "auront", "fait", "faite", "fais", "faisait",
    "va", "vont", "allait", "ira", "iront",
    "peut", "peuvent", "pouvait", "pourra", "pourront",
    "doit", "doivent", "devait", "devra", "devront",
    "veut", "veulent", "voulait", "voudra",
    "dit", "dite", "dirent", "disait", "dira",
    # Adverbes fréquents
    "plus", "moins", "très", "trop", "bien", "mal", "peu", "beaucoup",
    "encore", "déjà", "toujours", "jamais", "souvent", "parfois", "rarement",
    "ici", "là", "partout", "ailleurs", "dehors", "dedans", "dessus", "dessous",
    "comment", "pourquoi", "quand", "combien",
    # Adjectifs / mots-outils de compte-rendu
    "nouveau", "nouvelle", "ancien", "ancienne", "premier", "première",
    "dernier", "dernière", "même", "autre", "grand", "petit", "long",
    "court", "bon", "mauvais", "important", "principal", "général",
    "possible", "nécessaire",
    # Vocabulaire courant des réunions / comptes-rendus
    "réunion", "réunions", "point", "points", "sujet", "sujets", "objet",
    "ordre", "jour", "agenda", "comité", "groupe", "équipe", "équipes",
    "projet", "projets", "service", "services", "direction", "section",
    "dossier", "dossiers", "document", "documents", "note", "notes",
    "compte", "rendu", "rendus", "synthèse", "résumé", "rapport",
    "décision", "décisions", "action", "actions", "remarque", "remarques",
    "question", "questions", "réponse", "réponses", "discussion",
    "présentation", "introduction", "conclusion", "suite", "suivi",
    "ensemble", "partie", "parties", "membre", "membres", "personne",
    "personnes", "monsieur", "madame", "merci", "bonjour", "bonsoir",
    "année", "années", "mois", "semaine", "jour", "jours", "heure", "heures",
    "matin", "soir", "après-midi",
    "exemple", "exemples", "cas", "type", "types", "manière", "façon",
    "chose", "choses", "fois", "moment", "moments", "place", "temps",
    "niveau", "niveaux", "domaine", "domaines", "secteur", "secteurs",
    "élément", "éléments", "aspect", "aspects",
    # Mots de liaison
    "puisque", "lorsque", "tandis", "afin", "bien", "quoique",
    "néanmoins", "cependant", "toutefois", "pourtant", "donc",
}


# Acronyme : ≥2 majuscules d'affilée, optionnellement suivies de chiffres.
# Match ANSC, RGPD, 2MLFDI, H2024, etc.
_SIGLE_RE = re.compile(r"^\d*[A-Z]{2,}(?:[A-Z0-9\-]*[A-Z0-9])?$")
# Capitalisation interne : iPhone, NextCloud, MesReunions, MIrAI.
_MIXED_CASE_RE = re.compile(r"^[A-Za-z]*[a-z][A-Z][A-Za-z]*$")
# Token avec chiffres ou tirets internes : Mirai-v2, T0, COVID-19, RGPD-2.
_HAS_DIGIT_OR_DASH = re.compile(r"[0-9\-]")


def is_specific_term(term: str) -> Tuple[bool, str]:
    """Décide si ``term`` ressemble à du vocabulaire spécifique.

    Retourne ``(keep, reason)``. ``reason`` est un slug court utile pour
    le tracé / debug et exposé dans la modale "Nettoyer" pour expliquer
    à l'utilisateur pourquoi un terme est suggéré à la purge.

    Règles de keep (au moins une doit matcher) :
        - acronym : MAJUSCULES + optionnellement chiffres (RGPD, 2MLFDI)
        - mixed_case : capitalisation interne (iPhone, NextCloud)
        - has_digit_or_dash : contient chiffre ou tiret (Mirai-v2)
        - multi_word_proper : ≥2 mots capitalisés (Fernand Naudin)
        - long_specific : ≥13 caractères et hors stopwords
                          (interopérabilité, télétravailleurs)
        - capitalized_long : commence par majuscule, ≥4 caractères, non
                             stopword (Mirai, Drupal). Vrai-positif élevé
                             pour noms propres / produits / projets.

    Règles de drop (priorité absolue) :
        - too_short : <3 caractères (sauf si déjà acronyme matché en keep)
        - stopword : terme dans la liste FR (en lowercase)
        - punct_only : caractères non-alphanumériques uniquement
    """
    t = (term or "").strip()
    if not t:
        return (False, "empty")
    if len(t) > 255:
        return (False, "too_long")

    # Drop : punctuation/symboles seulement
    if not any(c.isalnum() for c in t):
        return (False, "punct_only")

    # Drop : stopword (case-insensitive)
    if t.lower() in _STOPWORDS_FR:
        return (False, "stopword")

    # Keep : acronyme
    if _SIGLE_RE.match(t):
        return (True, "acronym")

    # Drop : trop court (après le check acronyme — RGPD aurait été drop sinon)
    if len(t) < 3:
        return (False, "too_short")

    # Keep : capitalisation interne (iPhone, NextCloud, MesReunions)
    if _MIXED_CASE_RE.match(t):
        return (True, "mixed_case")

    # Keep : contient chiffre ou tiret (Mirai-v2, COVID-19, IPv6, T0)
    if _HAS_DIGIT_OR_DASH.search(t):
        return (True, "has_digit_or_dash")

    # Multi-mots : si ≥2 tokens et au moins 2 commencent par majuscule,
    # c'est probablement un nom propre composé (Fernand Naudin, Marie Curie,
    # Plan France Relance).
    tokens = t.split()
    if len(tokens) >= 2:
        cap_count = sum(1 for tok in tokens if tok and tok[0].isupper())
        if cap_count >= 2:
            return (True, "multi_word_proper")
        # Plusieurs mots mais pas de capitales multiples → suspect
        # ("le projet de loi", "il faut faire"). Drop.
        return (False, "common_phrase")

    # Mono-token, ni acronyme ni mixed-case.
    # Keep si commence par majuscule et ≥4 caractères : probable nom
    # propre, marque, projet (Mirai, Drupal, Anthropic, Anthropics).
    # Reject les capitales courtes (Mai, Lyon est borderline — len=4 OK,
    # mais on est sur du best-effort).
    if t[0].isupper() and len(t) >= 4:
        return (True, "capitalized_long")

    # Keep : mot très long en minuscule, probablement du jargon
    # technique ou administratif (interopérabilité, télétravailleurs,
    # déconcentration).
    if len(t) >= 13:
        return (True, "long_specific")

    # Sinon : probable mot courant en minuscule (le, projet, équipe,
    # bonjour, important). Drop.
    return (False, "likely_common")


def classify(terms):
    """Helper batch : retourne ``{term: (keep, reason)}``."""
    return {t: is_specific_term(t) for t in terms}
