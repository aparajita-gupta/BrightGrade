"""
ai_titles.py

Generates Todoist task titles for new assignments by mimicking the
style (capitalization, punctuation, structure, abbreviations) of the
user's existing real Todoist tasks, via the Gemini API. Used by
todoist_sync.py when run with --ai-titles.

Titles for a course's batch of assignments are generated together in
a single call rather than one call per assignment -- independent
per-item calls have no shared state, so nothing forces them to agree
on a format; a single call has to commit to one consistent style
across the whole batch.

Setup:
    pip install google-genai
    Get a key at aistudio.google.com and set GEMINI_API_KEY in config.py
"""

from __future__ import annotations

import json
import logging

from google import genai
from google.genai import types

from config import GEMINI_API_KEY

MODEL = "gemini-flash-lite-latest"

# The google-genai SDK's own logging (raw HTTP request lines at INFO, plus
# a boilerplate "use Chat.send_message instead" notice logged at WARNING)
# otherwise leaks into pipeline.log via the shared root logger.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)


def build_batch_prompt(
    recent_titles: list[str],
    course_number: str,
    assignment_titles: list[str],
    recurring_flags: list[bool] | None = None,
) -> str:
    recurring_flags = recurring_flags or [False] * len(assignment_titles)
    examples = "\n".join(f"- {t}" for t in recent_titles[:20])
    items = "\n".join(
        f"{i + 1}. {t}"
        + (
            " (this repeats on a regular schedule -- title it as the general "
            "recurring item, with NO instance/week number, e.g. 'Weekly "
            "Feedback' not 'Weekly Feedback 3')"
            if is_recurring
            else ""
        )
        for i, (t, is_recurring) in enumerate(zip(assignment_titles, recurring_flags))
    )
    return (
        "Here are examples of my recent real Todoist task titles:\n"
        f"{examples}\n\n"
        "Study those examples and infer my actual naming convention -- "
        "capitalization, spacing, abbreviations, etc. Note: any \"@word\" "
        "tag in the examples above (e.g. \"@ece 270\", \"@study\") is a "
        "Todoist label reference -- typing \"@word\" auto-creates/attaches "
        "a label with that exact text, completely separate from any label "
        "applied through other means. Labeling for these new tasks is "
        "already handled separately and correctly elsewhere, so the titles "
        "you write must NOT include an \"@\" anywhere, or it will silently "
        "create a duplicate, wrongly-named label. Also note: any course "
        "number/abbreviation in the examples above (e.g. \"270\", \"ece\", "
        "\"ma 266\") names a DIFFERENT, unrelated course from the one "
        "you're writing titles for now -- copy the STYLE only, never the "
        "specific department letters or number from an example. Then "
        "write Todoist titles for the batch of new assignments below, all "
        "in course number "
        f"{course_number} (refer to the course by NUMBER ONLY, with no "
        "letter prefix -- never a subject abbreviation like \"ME\" or a "
        "department abbreviation copied from an example like \"ece\"). "
        "Apply the SAME exact convention consistently to every item -- do "
        "not vary the format from one item to the next.\n\n"
        "Always place the course number at the very START of the title, "
        "immediately before the assignment name -- never at the end and "
        "never in the middle. For example 'HW 1' in course 362 becomes "
        "'362 HW 1' or '362 - HW 1', NOT 'HW 1 362'.\n\n"
        "Never include a due date or due time in any title -- Todoist "
        "already tracks that separately.\n\n"
        f"Assignments:\n{items}\n\n"
        f"Reply with ONLY a JSON array of exactly {len(assignment_titles)} "
        "strings, in the same order as the list above. No other text."
    )


def generate_ai_titles_batch(
    recent_titles: list[str],
    course_number: str,
    assignment_titles: list[str],
    recurring_flags: list[bool] | None = None,
) -> list[str] | None:
    if not GEMINI_API_KEY or not assignment_titles:
        return None

    prompt = build_batch_prompt(recent_titles, course_number, assignment_titles, recurring_flags)

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )
        titles = json.loads(response.text)
        if isinstance(titles, list) and len(titles) == len(assignment_titles):
            return [str(t).strip() for t in titles]
        logging.warning(f"AI batch title response shape mismatch: {response.text!r}")
        return None
    except Exception as exc:
        logging.warning(f"Gemini batch title generation failed: {exc}")
        return None


def generate_course_label(existing_labels: list[str], course_display: str) -> str | None:
    """Given the real labels already in the account, propose a name for a
    new course's label in the same naming convention -- rather than
    hardcoding an assumed format, since the account's real convention
    (lowercase, "subject number" with a space, e.g. "ece 270", "ma 266")
    isn't something code should guess at."""
    if not GEMINI_API_KEY or not existing_labels:
        return None

    examples = "\n".join(f"- {l}" for l in existing_labels)
    prompt = (
        "Here are my existing Todoist labels:\n"
        f"{examples}\n\n"
        "Most of these are per-course labels. I need a NEW label for a "
        "course that doesn't have one yet, matching the exact same naming "
        "convention (capitalization, spacing, abbreviation style) as the "
        "existing per-course labels above.\n\n"
        f"New course: {course_display}\n\n"
        "Reply with ONLY the new label name, nothing else -- no quotes, no "
        "explanation."
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.1),
        )
        text = (response.text or "").strip()
        return text or None
    except Exception as exc:
        logging.warning(f"Gemini label generation failed for {course_display!r}: {exc}")
        return None
