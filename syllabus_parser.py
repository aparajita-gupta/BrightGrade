"""
syllabus_parser.py

Extracts quiz/midterm/exam dates and locations from a syllabus PDF via
Gemini, and merges them into assignments.json. Standalone for now --
prompt.md's item #3 (a "nudge me" reminder for permanently undated
items) doesn't exist yet, so TBD-flagged items just sit in
assignments.json with due_date: null and tbd: true until that gets
built.

Run:
    python3 syllabus_parser.py

A native macOS file-picker window opens for you to choose the PDF --
no path typing needed. Existing assignments with an already-resolved
due date (e.g. from Gradescope) are never overwritten; the syllabus is
treated as a lower-confidence source, only used to fill in genuinely
missing dates.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

from config import GEMINI_API_KEY
from matching import is_exam_like, normalize_course_name
from pipeline import load_store, save_store, LOG_FILE
from todoist_sync import normalize_label_name

MODEL = "gemini-flash-lite-latest"
EASTERN = ZoneInfo("America/New_York")

# Real exams you've added to Calendar.app by hand run about an hour;
# finals run longer -- confirmed by inspecting your own past exam events
# (as opposed to Purdue's separately auto-imported all-day exam-schedule
# entries, which this deliberately does NOT try to match the style of).
DEFAULT_EXAM_DURATION_MIN = 60
DEFAULT_FINAL_DURATION_MIN = 120

CAMPUS_CONTEXT = "Purdue University, West Lafayette, IN"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
)
# The google-genai SDK's own logging (raw HTTP request lines at INFO, plus
# a boilerplate "use Chat.send_message instead" notice logged at WARNING)
# otherwise leaks into pipeline.log via the shared root logger.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "course": {
            "type": "string",
            "description": "The course name/code as it appears on the syllabus, e.g. 'ME 270'",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "e.g. 'Midterm 1', 'Quiz 3', 'Final Exam'"},
                    "date": {
                        "type": "string",
                        "nullable": True,
                        "description": "ISO YYYY-MM-DD if a specific calendar date is determinable "
                        "(infer the year from the syllabus's stated term/semester), else null",
                    },
                    "time": {
                        "type": "string",
                        "nullable": True,
                        "description": "24-hour HH:MM local START time only if explicitly stated, "
                        "else null. If the syllabus gives a range like '6:30-7:30pm', use ONLY the "
                        "start, e.g. '18:30' -- never include the range or the end time.",
                    },
                    "location": {"type": "string", "nullable": True},
                    "tbd": {
                        "type": "boolean",
                        "description": "true if the syllabus itself says TBD/'to be announced' or "
                        "otherwise doesn't give a real date -- never guess a date instead",
                    },
                    "section": {
                        "type": "string",
                        "nullable": True,
                        "description": "If the syllabus gives DIFFERENT dates/times/locations for "
                        "this item depending on lecture/lab/recitation section (e.g. a table of "
                        "exam times per section, or per-section lab due dates), the section "
                        "identifier this particular date applies to, exactly as written (e.g. "
                        "'Section 1', 'Lab A', '001', 'Tuesday section'). If the syllabus does NOT "
                        "distinguish by section for this item (one date applies to everyone), null.",
                    },
                },
                "required": ["title", "date", "time", "location", "tbd", "section"],
            },
        },
    },
    "required": ["course", "items"],
}


def pick_pdf_file() -> Path | None:
    script = 'POSIX path of (choose file with prompt "Select syllabus PDF" of type {"pdf"})'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def extract_syllabus_data(pdf_path: Path) -> dict | None:
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY is not set in config.py.")
        return None

    pdf_bytes = pdf_path.read_bytes()
    today = date.today().isoformat()
    prompt = (
        f"Today's real-world date is {today} -- use this to ground any date "
        "inference below, do not rely on your own internal sense of "
        "\"current\" date.\n\n"
        "This is a course syllabus PDF. Extract every quiz, midterm, exam, "
        "and final exam date it lists (skip regular weekly homework unless "
        "individually dated). For each one, give its date, time, and "
        "location if the syllabus states them. If the syllabus says "
        "'TBD', 'to be announced', or otherwise doesn't give a real date "
        "for something, mark it tbd=true rather than guessing a date.\n\n"
        "If a date is given without an explicit year (e.g. 'Tuesday, "
        "September 8' with no year stated), do NOT search for a "
        "historical or future year where that weekday happens to match -- "
        "that produces wrong, sometimes years-off results. Instead use "
        "the syllabus's stated term/semester (e.g. 'Fall 2026') if given, "
        "and if even that's absent, use the semester that contains or "
        "immediately follows today's date above. A syllabus describes a "
        "single upcoming/current academic term, never an arbitrary past "
        "year.\n\n"
        "If the syllabus gives DIFFERENT dates/times/locations for the "
        "same item depending on which lecture/lab/recitation section a "
        "student is in (e.g. a table of exam times by section, or "
        "per-section lab due dates), emit ONE item per section with that "
        "item's 'section' field set to the section identifier -- do not "
        "collapse them into one guessed date."
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model=MODEL,
            contents=[
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                prompt,
            ],
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
            ),
        )
        return json.loads(response.text)
    except Exception as exc:
        logging.error(f"Syllabus extraction failed for {pdf_path.name}: {exc}")
        print(f"Extraction failed: {exc}")
        return None


def get_calendar_names() -> list[str]:
    result = subprocess.run(
        ["osascript", "-e", 'tell application "Calendar" to name of calendars'],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [n.strip() for n in result.stdout.strip().split(", ") if n.strip()]


def find_matching_calendar(course_key: str, calendar_names: list[str]) -> str | None:
    for name in calendar_names:
        if normalize_label_name(name) == course_key:
            return name
    return None


def _applescript_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def parse_start_time(time_str: str | None) -> str | None:
    """Normalizes to 'HH:MM', defensively taking just the leading start
    time if the model returned a range like '18:30-19:30' despite being
    asked for a single start time -- confirmed happens in practice (real
    syllabus, ECE 301). Returns None if unparseable."""
    if not time_str:
        return None
    match = re.match(r"\s*(\d{1,2}:\d{2})", time_str)
    return match.group(1) if match else None


def create_calendar_event(
    calendar_name: str,
    title: str,
    date_str: str,
    time_str: str | None,
    location: str | None,
) -> bool:
    """Creates a real timed (or, if no time was given, all-day) event in
    the given Calendar.app calendar -- matching the style of exams you've
    added by hand in the past (short title, real start/end time, ~1hr for
    a midterm / ~2hr for a final), not Purdue's separately auto-imported
    all-day exam-schedule entries."""
    date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    title_esc = _applescript_escape(title)

    geocodable_location = None
    if location:
        # A bare building code/room like "ELLT 116" or "PHYS 112" won't
        # resolve to a map pin in Calendar.app's event view on its own;
        # appending campus context is what actually gets a pin to show,
        # matching the real map-pinned "Elliott Hall of Music" events
        # you've added by hand in the past. Skip if it already looks like
        # it carries that context (e.g. a full street address).
        if "purdue" in location.lower() or "west lafayette" in location.lower():
            geocodable_location = location
        else:
            geocodable_location = f"{location}, {CAMPUS_CONTEXT}"
    loc_esc = _applescript_escape(geocodable_location) if geocodable_location else None

    start_time = parse_start_time(time_str)
    if start_time:
        start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
        duration = (
            DEFAULT_FINAL_DURATION_MIN
            if "final" in title.lower()
            else DEFAULT_EXAM_DURATION_MIN
        )
        end_dt = start_dt + timedelta(minutes=duration)
        start_str = start_dt.strftime("%m/%d/%Y %I:%M:%S %p")
        end_str = end_dt.strftime("%m/%d/%Y %I:%M:%S %p")
        date_props = f'start date:date "{start_str}", end date:date "{end_str}"'
    else:
        # Calendar.app's AppleScript API rejects an all-day event with no
        # end date at all ("No end date has been set.", -10025) -- it
        # doesn't default one the way the app's own UI does. Confirmed
        # real failure (HIST 371's "First Exam"/"Second Exam", no time
        # given): create_calendar_event silently failed every time an
        # exam had no explicit time. A single-day all-day event's end
        # date is the same calendar day.
        start_str = date_obj.strftime("%m/%d/%Y")
        date_props = f'start date:date "{start_str}", end date:date "{start_str}", allday event:true'

    props = f'summary:"{title_esc}", {date_props}'
    script = (
        f'tell application "Calendar"\n'
        f'  tell calendar "{_applescript_escape(calendar_name)}"\n'
        f"    set newEvent to make new event with properties {{{props}}}\n"
    )
    if loc_esc:
        script += f'    set location of newEvent to "{loc_esc}"\n'
    script += "  end tell\nend tell\n"

    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        logging.error(f"Calendar event creation failed for '{title}': {result.stderr.strip()}")
        return False
    return True


def resolve_due_date(item: dict) -> str | None:
    if item.get("tbd") or not item.get("date"):
        return None
    if not item.get("time"):
        return item["date"]

    start_time = parse_start_time(item["time"])
    if not start_time:
        logging.warning(f"Unparseable time {item['time']!r} for {item['title']!r}; using date only")
        return item["date"]

    naive = datetime.strptime(f"{item['date']} {start_time}", "%Y-%m-%d %H:%M")
    eastern = naive.replace(tzinfo=EASTERN)
    return eastern.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def merge_into_store(
    store: dict,
    course_key: str,
    items: list[dict],
    *,
    calendar_titles: set[str] | None = None,
    calendar_name: str | None = None,
) -> dict[str, int]:
    stats = {"added": 0, "filled_in": 0, "skipped_has_date": 0, "tbd": 0, "calendar_events": 0}
    calendar_titles = calendar_titles or set()

    for item in items:
        key = f"{course_key}::{item['title'].lower()}"
        due_date = resolve_due_date(item)
        existing = store["assignments"].get(key)
        to_calendar = item["title"] in calendar_titles and not item.get("tbd") and item.get("date")

        entry_extra = {}
        if item.get("location"):
            entry_extra["location"] = item["location"]
        elif is_exam_like(item["title"]):
            # No location on the syllabus for something that should have
            # one (an exam/midterm/final) -- flag it so pipeline.py's
            # nudge check knows to remind about it too, same as a missing
            # date, rather than silently treating "no location" as final.
            entry_extra["location_expected"] = True
        if item.get("tbd"):
            entry_extra["tbd"] = True
            stats["tbd"] += 1

        is_new = existing is None
        is_fill = existing is not None and existing.get("due_date") is None and due_date is not None

        # Only mark an item "delivery: calendar" (which makes
        # todoist_sync.py skip it entirely) if a real Calendar.app event
        # actually got created. Confirmed real bug: this used to get set
        # unconditionally whenever to_calendar was true, so a failed
        # AppleScript save (or no matching calendar found) left the item
        # excluded from Todoist too -- nowhere, not Calendar, not
        # Todoist. Now a failure/no-match falls back to a normal Todoist
        # task instead of disappearing.
        if to_calendar and (is_new or is_fill) and calendar_name:
            created = create_calendar_event(
                calendar_name, item["title"], item["date"], item.get("time"), item.get("location")
            )
            if created:
                stats["calendar_events"] += 1
                entry_extra["delivery"] = "calendar"

        if is_new:
            store["assignments"][key] = {
                "title": item["title"],
                "due_date": due_date,
                "source": "syllabus_pdf",
                **entry_extra,
            }
            stats["added"] += 1
        elif is_fill:
            existing["due_date"] = due_date
            existing["source"] = f"{existing['source']}+syllabus_pdf"
            existing.update(entry_extra)
            if "location" in entry_extra:
                existing.pop("location_expected", None)
            stats["filled_in"] += 1
        elif existing.get("due_date") is not None:
            stats["skipped_has_date"] += 1
        else:
            existing.update(entry_extra)
            if "location" in entry_extra:
                existing.pop("location_expected", None)

    return stats


def select_section(items: list[dict]) -> list[dict]:
    """If the syllabus gave different dates for different sections (e.g.
    a per-section exam-time table), ask which section the user is in and
    keep only that section's items -- plus any item with no section
    distinction at all (section=None applies to everyone)."""
    sections = sorted({i["section"] for i in items if i.get("section")})
    if len(sections) <= 1:
        return items

    print(f"\nThis syllabus lists different dates for {len(sections)} sections:")
    for i, s in enumerate(sections, 1):
        print(f"  {i}. {s}")
    answer = input("Which section are you in? [number]: ").strip()

    try:
        chosen = sections[int(answer) - 1]
    except (ValueError, IndexError):
        matches = [s for s in sections if s.lower() == answer.lower()]
        if not matches:
            print("Not recognized -- keeping all sections' items (review carefully before merging).")
            return items
        chosen = matches[0]

    print(f"Using section: {chosen}")
    return [i for i in items if not i.get("section") or i["section"] == chosen]


def main():
    pdf_path = pick_pdf_file()
    if pdf_path is None:
        print("No file selected.")
        sys.exit(0)

    print(f"Reading {pdf_path.name}...")
    data = extract_syllabus_data(pdf_path)
    if data is None:
        sys.exit(1)

    course_key = normalize_course_name(data["course"])
    items = select_section(data["items"])

    print(f"\nDetected course: {data['course']} -> {course_key}")
    print(f"Found {len(items)} item(s):")
    for item in items:
        due = resolve_due_date(item)
        flag = " [TBD]" if item.get("tbd") else ""
        loc = f" @ {item['location']}" if item.get("location") else ""
        print(f"  - {item['title']}: {due or '(no date)'}{loc}{flag}")

    exam_items = [i for i in items if is_exam_like(i["title"]) and not i.get("tbd") and i.get("date")]
    calendar_titles: set[str] = set()
    calendar_name = None
    if exam_items:
        names = ", ".join(i["title"] for i in exam_items)
        print(f"\n{len(exam_items)} exam-type item(s) detected: {names}")
        choice = input(
            "Add these as [C]alendar events (matches how you've added exams in "
            "the past) or [T]odoist tasks like everything else? [C/t]: "
        ).strip().lower()
        if choice in ("", "c", "calendar"):
            calendar_names = get_calendar_names()
            calendar_name = find_matching_calendar(course_key, calendar_names)
            if calendar_name is None:
                print(
                    f"  No Calendar.app calendar matching '{course_key}' found -- "
                    "falling back to Todoist tasks for these instead."
                )
            else:
                calendar_titles = {i["title"] for i in exam_items}
                print(f"  Will add to your '{calendar_name}' calendar.")

    answer = input("\nMerge these into assignments.json? [Y/n]: ").strip().lower()
    if answer not in ("", "y", "yes"):
        print("Cancelled, nothing written.")
        return

    store = load_store()
    stats = merge_into_store(
        store, course_key, items, calendar_titles=calendar_titles, calendar_name=calendar_name
    )
    save_store(store)

    logging.info(
        f"Syllabus parsed ({pdf_path.name}, course={course_key}): "
        f"{stats['added']} added, {stats['filled_in']} filled in, "
        f"{stats['skipped_has_date']} already had a real date, "
        f"{stats['tbd']} flagged TBD, {stats['calendar_events']} added as calendar events"
    )
    print(
        f"\nDone: {stats['added']} added, {stats['filled_in']} filled in, "
        f"{stats['skipped_has_date']} skipped (already had a real date), "
        f"{stats['tbd']} flagged TBD, {stats['calendar_events']} added as calendar events."
    )


if __name__ == "__main__":
    main()
