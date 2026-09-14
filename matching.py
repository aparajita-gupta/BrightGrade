"""
matching.py

Course-key normalization and fuzzy title matching -- a direct Python
port of the logic proven working in the Chrome extension (shared.js).
"""

import re

# Shared between syllabus_parser.py (deciding which items get a location
# nudge / go to Calendar) and pipeline.py (nothing currently, kept here so
# both can import the same definition instead of drifting out of sync).
EXAM_KEYWORD_RE = re.compile(r"exam|midterm|final", re.IGNORECASE)


def is_exam_like(title: str) -> bool:
    return bool(EXAM_KEYWORD_RE.search(title))


def normalize_course_name(raw: str) -> str:
    """
    'Summer 2026 ME 27000-Y01 DIS' -> 'me270'
    'ME 270 - Y01'                 -> 'me270'
    Purdue-style internal codes pad the real course number with two
    trailing zeros (27000 = course 270), so we strip that when present.

    Letters must be uppercase-only: real course prefixes (ME, ECE, TDM...)
    are always fully capitalized, while surrounding sentence text (e.g.
    "Summer 2026", "Spring 2026") is mixed-case. A case-insensitive match
    here would let re.search's leftmost-match behavior grab "mmer"+"2026"
    out of "Summer 2026 ME 27000..." before ever reaching the real code.
    """
    match = re.search(r"([A-Z]{2,4})\s?-?\s?(\d{3,5})", raw)
    if not match:
        return raw.strip().lower()
    prefix = match.group(1).lower()
    number = match.group(2)
    if len(number) == 5 and number.endswith("00"):
        number = number[:3]
    return f"{prefix}{number}"


def normalize_title(raw: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 ]", "", raw.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def title_similarity(a: str, b: str) -> float:
    """
    Simple token-overlap similarity, 0 to 1. Good enough for matching
    'Homework 3' vs 'HW 3 - Vectors' without needing a real NLP library.
    """
    ta = set(normalize_title(a).split())
    tb = set(normalize_title(b).split())
    if not ta or not tb:
        return 0.0
    overlap = len(ta & tb)
    return overlap / max(len(ta), len(tb))


def best_match(target_title: str, candidates: list[dict], key="title", threshold=0.5):
    """
    Finds the best-matching candidate dict for target_title, by
    title_similarity, above the given threshold. Returns None if no
    candidate clears the threshold.
    """
    best = None
    best_score = 0.0
    for candidate in candidates:
        score = title_similarity(target_title, candidate[key])
        if score > threshold and score > best_score:
            best = candidate
            best_score = score
    return best