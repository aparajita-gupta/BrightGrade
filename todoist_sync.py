"""
todoist_sync.py

Push dated assignments from assignments.json to Todoist.

Uses Todoist API v1 (REST v2 was shut down 2026-02-10):
    https://developer.todoist.com/api/v1/

First run (default):
    Marks every assignment already in assignments.json as synced without
    creating Todoist tasks, so only genuinely new discoveries get pushed.

Run:
    python todoist_sync.py              # push new dated assignments
    python todoist_sync.py --init       # mark existing items synced (no push)
    python todoist_sync.py --backfill   # push all unsynced future-dated items
    python todoist_sync.py --dry-run    # preview without API writes
    python todoist_sync.py --reset-format  # re-prompt for title template
    python todoist_sync.py --ai-titles  # generate titles with Gemini instead of the template
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from ai_titles import generate_ai_titles_batch, generate_course_label
from config import TODOIST_API_TOKEN
from matching import normalize_course_name
from pipeline import notify

# Gradescope's "Due (EDT)" column is always US Eastern local time
# (the label doesn't change with DST); zoneinfo handles EDT/EST for us.
GRADESCOPE_TZ = ZoneInfo("America/New_York")
BARE_DATE_FORMATS = ("%b %d at %I:%M%p", "%b %d at %I:%M %p")

# This account's real observed Todoist timezone (confirmed empirically,
# same constant used by raspberry_pi_display/pi_display.py) -- due_string
# text has no way to carry an explicit UTC offset, so a recurring task's
# "at HH:MMam/pm" is parsed by Todoist in THIS timezone, not the source
# data's Eastern time. We convert explicitly before building the string
# so the resulting UTC instant is still correct.
ACCOUNT_TIMEZONE = ZoneInfo("America/Los_Angeles")

BASE_DIR = Path(__file__).parent
STORE_FILE = BASE_DIR / "assignments.json"
SYNCED_FILE = BASE_DIR / "todoist_synced.json"
FORMAT_FILE = BASE_DIR / "todoist_format_preference.json"
LOG_FILE = BASE_DIR / "pipeline.log"

API_BASE = "https://api.todoist.com/api/v1"
DEFAULT_TEMPLATE = "{COURSE} - {title}"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
)


class TodoistError(Exception):
    pass


class TodoistClient:
    def __init__(self, token: str):
        self.token = token
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        max_retries: int = 5,
    ) -> Any:
        url = f"{API_BASE}{path}"
        for attempt in range(max_retries):
            response = self.session.request(
                method, url, params=params, json=json_body, timeout=30
            )
            if response.status_code == 429 or (
                response.status_code >= 500 and attempt < max_retries - 1
            ):
                retry_after = int(response.headers.get("Retry-After", 0))
                if retry_after <= 0:
                    try:
                        payload = response.json()
                        retry_after = int(payload.get("error_extra", {}).get("retry_after", 0))
                    except Exception:
                        retry_after = 0
                wait = retry_after or min(2**attempt, 30)
                logging.warning(
                    f"Todoist {response.status_code} on {path}; retrying in {wait}s"
                )
                time.sleep(wait)
                continue

            if response.status_code >= 400:
                detail = response.text[:500]
                raise TodoistError(f"{method} {path} failed ({response.status_code}): {detail}")

            if response.status_code == 204 or not response.content:
                return None
            return response.json()

        raise TodoistError(f"{method} {path} failed after {max_retries} retries")

    def paginate(self, path: str, *, params: dict | None = None, limit: int = 200) -> list[dict]:
        items: list[dict] = []
        cursor = None
        base_params = dict(params or {})
        base_params["limit"] = min(limit, 200)

        while True:
            page_params = dict(base_params)
            if cursor:
                page_params["cursor"] = cursor
            payload = self._request("GET", path, params=page_params)
            items.extend(payload.get("results", []))
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return items

    def get_labels(self) -> list[dict]:
        return self.paginate("/labels")

    def get_active_tasks(self, *, limit: int = 200) -> list[dict]:
        return self.paginate("/tasks", limit=limit)

    def get_completed_tasks(self, *, limit: int = 200) -> list[dict]:
        # /tasks/completed returns {"items": [...]} rather than {"results": [...]},
        # so it doesn't fit the generic paginate() helper. A single call already
        # covers this account's full completed history (94 items, no cursor
        # returned), so no pagination loop for now.
        payload = self._request("GET", "/tasks/completed", params={"limit": limit})
        return payload.get("items", [])

    def ensure_label(self, name: str, existing: set[str]) -> None:
        if name in existing:
            return
        self._request("POST", "/labels", json_body={"name": name})
        existing.add(name)

    def create_task(
        self,
        *,
        content: str,
        due_string: str | None = None,
        due_date: str | None = None,
        due_datetime: str | None = None,
        labels: list[str] | None = None,
    ) -> dict:
        body: dict[str, Any] = {"content": content}
        if due_string:
            body["due_string"] = due_string
        if due_date:
            body["due_date"] = due_date
        if due_datetime:
            body["due_datetime"] = due_datetime
        if labels:
            body["labels"] = labels
        return self._request("POST", "/tasks", json_body=body)


def load_json(path: Path, default: dict) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2))


def load_store() -> dict:
    if not STORE_FILE.exists():
        raise FileNotFoundError(f"{STORE_FILE} not found. Run pipeline.py first.")
    return json.loads(STORE_FILE.read_text())


def load_synced() -> dict:
    return load_json(SYNCED_FILE, {"synced_keys": {}, "initialized": False})


def save_synced(data: dict):
    save_json(SYNCED_FILE, data)


def load_format_preference() -> dict | None:
    if FORMAT_FILE.exists():
        return json.loads(FORMAT_FILE.read_text())
    return None


def save_format_preference(template: str):
    save_json(
        FORMAT_FILE,
        {
            "template": template,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def course_key_to_display(course_key: str) -> str:
    match = re.fullmatch(r"([a-z]{2,4})(\d{3})", course_key.lower())
    if match:
        return f"{match.group(1).upper()} {match.group(2)}"
    return course_key.upper()


def course_key_to_label(course_key: str) -> str:
    # No space, since a space in a Todoist label breaks @label typing/autocomplete
    # in the app even though the API itself would accept one.
    match = re.fullmatch(r"([a-z]{2,4})(\d{3})", course_key.lower())
    if match:
        return f"{match.group(1).upper()}{match.group(2)}"
    return course_key.upper()


def course_key_to_number(course_key: str) -> str:
    match = re.fullmatch(r"([a-z]{2,4})(\d{3})", course_key.lower())
    if match:
        return match.group(2)
    return course_key.upper()


def extract_course_from_key(key: str, item: dict | None = None) -> str | None:
    if key.startswith("ics::"):
        # ics:: items come from Brightspace's combined calendar feed, which
        # pools all courses together -- the course (if any) is resolved
        # separately in pipeline.py's run_ics_step from the ICS event's
        # LOCATION field and stored on the assignment as "course".
        return item.get("course") if item else None
    if "::" in key:
        return key.split("::", 1)[0]
    return None


def parse_bare_month_day(text: str) -> datetime | None:
    """Parse Gradescope-style 'Aug 18 at 8:00AM' (no year) and assign
    the current calendar year -- these dates are always within the
    active academic term, and many are already-past (graded) items,
    so rolling a past date forward to next year would wrongly mark
    finished assignments as upcoming."""
    for fmt in BARE_DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(year=date.today().year)
    return None


def parse_due_date(raw: str) -> datetime | date | None:
    if not raw:
        return None

    text = str(raw).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)

    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass

    bare = parse_bare_month_day(text)
    if bare is not None:
        return bare.date()

    return None


def is_future_or_today(due_value: str) -> bool:
    parsed = parse_due_date(due_value)
    if parsed is None:
        return True
    if isinstance(parsed, datetime):
        parsed = parsed.astimezone(timezone.utc).date() if parsed.tzinfo else parsed.date()
    return parsed >= date.today()


def build_due_fields(due_value: str) -> dict[str, str]:
    text = str(due_value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return {"due_date": text}

    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            return {"due_datetime": dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")}
        return {"due_datetime": dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    except ValueError:
        pass

    bare = parse_bare_month_day(text)
    if bare is not None:
        eastern = bare.replace(tzinfo=GRADESCOPE_TZ)
        return {"due_datetime": eastern.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}

    return {"due_string": text}


def infer_title_template(tasks: list[dict], labels: list[dict]) -> tuple[str, str]:
    """
    Guess a title template from recent Todoist tasks.
    Returns (template, explanation).
    """
    label_names = {label["name"] for label in labels}
    course_like_labels = {
        name
        for name in label_names
        if re.search(r"[A-Za-z]{2,4}\s?\d{3}", name) or re.fullmatch(r"[A-Za-z]{2,4}\d{3}", name)
    }

    samples: list[tuple[str, str]] = []
    for task in tasks[:100]:
        content = task.get("content", "")
        for sep in (" - ", ": ", " | "):
            if sep not in content:
                continue
            prefix, suffix = content.split(sep, 1)
            prefix = prefix.strip()
            suffix = suffix.strip()
            if not prefix or not suffix:
                continue
            if re.search(r"[A-Za-z]{2,4}\s?\d{3}", prefix) or prefix in course_like_labels:
                samples.append((prefix, suffix))
                break

    if samples:
        sep_counts = {" - ": 0, ": ": 0, " | ": 0}
        for task in tasks[:100]:
            content = task.get("content", "")
            for sep in sep_counts:
                if sep in content:
                    sep_counts[sep] += 1
        best_sep = max(sep_counts, key=sep_counts.get)
        template = f"{{COURSE}}{best_sep}{{title}}"
        example_prefix, example_suffix = samples[0]
        explanation = (
            f"Found {len(samples)} recent task(s) that look course-prefixed, "
            f"e.g. '{example_prefix}{best_sep}{example_suffix}'."
        )
        return template, explanation

    if course_like_labels:
        example = sorted(course_like_labels)[0]
        explanation = (
            f"Found course-like labels ({', '.join(sorted(course_like_labels)[:3])}); "
            f"defaulting to '{DEFAULT_TEMPLATE}'."
        )
        return DEFAULT_TEMPLATE, explanation

    return DEFAULT_TEMPLATE, "No strong pattern found in recent tasks; using a simple default."


def format_task_title(template: str, course_key: str | None, title: str) -> str:
    if not course_key:
        # No identifiable course (e.g. a non-course ICS event like an
        # advising deadline) -- just use the title, rather than inventing
        # a fake "School" course prefix.
        return title
    course = course_key_to_display(course_key)
    return (
        template.replace("{COURSE}", course)
        .replace("{course}", course)
        .replace("{title}", title)
        .replace("{TITLE}", title)
    )


def normalize_label_name(name: str) -> str:
    """Like matching.normalize_course_name, but for real Todoist label
    names, which are already short & clean (e.g. 'ece 362') rather than a
    full sentence like 'Summer 2026 ME 27000-Y01 DIS' -- so unlike that
    function, matching case-insensitively here is safe (no risk of
    grabbing a word like "Summer" instead of the course code) and is in
    fact required, since real labels are conventionally lowercase."""
    match = re.search(r"([A-Za-z]{2,4})\s?-?\s?(\d{3,5})", name)
    if not match:
        return name.strip().lower().replace(" ", "")
    prefix = match.group(1).lower()
    number = match.group(2)
    if len(number) == 5 and number.endswith("00"):
        number = number[:3]
    return f"{prefix}{number}"


def choose_school_label(labels: list[dict]) -> str | None:
    names = {label["name"].lower(): label["name"] for label in labels}
    for candidate in ("school", "academics", "classes", "coursework"):
        if candidate in names:
            return names[candidate]
    return None


def prompt_for_template(client: TodoistClient) -> str:
    print("Analyzing your existing Todoist tasks and labels...")
    labels = client.get_labels()
    tasks = client.get_active_tasks(limit=100)
    recent_titles = [task.get("content", "") for task in tasks[:8] if task.get("content")]

    template, explanation = infer_title_template(tasks, labels)
    print()
    print(explanation)
    if recent_titles:
        print("\nRecent task titles:")
        for title in recent_titles:
            print(f"  - {title}")

    example_course = "ME 270"
    example_title = "HW 08"
    preview = format_task_title(template, "me270", example_title)
    print(f"\nProposed template: {template}")
    print(f"Example output:    {preview}")
    print()
    answer = input("Use this format for all future assignments? [Y/n/custom]: ").strip()

    if answer.lower() in ("", "y", "yes"):
        chosen = template
    elif answer.lower() in ("n", "no"):
        chosen = DEFAULT_TEMPLATE
        print(f"Using default: {chosen}")
    else:
        chosen = answer
        print(f"Using custom template: {chosen}")

    save_format_preference(chosen)
    print(f"Saved to {FORMAT_FILE.name}")
    return chosen


def get_template(client: TodoistClient | None, *, reset: bool = False, dry_run: bool = False) -> str:
    if reset and FORMAT_FILE.exists():
        FORMAT_FILE.unlink()

    pref = load_format_preference()
    if pref and pref.get("template"):
        return pref["template"]

    if client is None:
        if dry_run:
            return DEFAULT_TEMPLATE
        raise TodoistError(
            "No saved title template and no API token available to infer one. "
            "Run without --dry-run once, or create todoist_format_preference.json manually."
        )

    return prompt_for_template(client)


def mark_all_synced(store: dict, synced: dict) -> int:
    count = 0
    for key in store.get("assignments", {}):
        if key not in synced["synced_keys"]:
            synced["synced_keys"][key] = {
                "marked_at": datetime.now(timezone.utc).isoformat(),
                "reason": "init",
            }
            count += 1
    synced["initialized"] = True
    return count


def iter_push_candidates(
    store: dict,
    synced: dict,
    *,
    backfill: bool = False,
    include_past: bool = False,
) -> list[tuple[str, dict]]:
    candidates: list[tuple[str, dict]] = []
    for key, item in store.get("assignments", {}).items():
        if item.get("delivery") == "calendar":
            # Already handled directly as a Calendar.app event by
            # syllabus_parser.py -- never also push it as a Todoist task.
            continue

        synced_entry = synced.get("synced_keys", {}).get(key)
        if synced_entry and not backfill:
            continue
        if synced_entry and backfill and synced_entry.get("reason") != "init":
            continue

        due_date = item.get("due_date")
        if not due_date:
            continue
        if not include_past and not is_future_or_today(str(due_date)):
            continue
        candidates.append((key, item))
    return candidates


def strip_instance_number(title: str) -> str:
    return re.sub(r"\s*#?\d+\s*$", "", title).strip()


def detect_recurring_groups(
    candidates: list[tuple[str, dict]]
) -> tuple[list[dict], set[str]]:
    """Groups candidate assignments that look like the same recurring item
    (same course, same title with only a trailing instance number
    differing, due at evenly-spaced intervals) so they become ONE Todoist
    recurring task instead of one one-off task per instance -- e.g.
    'Weekly Feedback 1'..'Weekly Feedback 8' becomes a single task titled
    'Weekly Feedback' that repeats every week, matching how a person
    would actually track it rather than 8 near-identical entries.

    Requires >=3 instances with a consistent gap (all consecutive gaps
    within 1 day of each other, 1-21 days) before calling something
    recurring -- two items that happen to land a week apart by
    coincidence shouldn't get collapsed."""
    by_group: dict[tuple[str | None, str], list[tuple[str, dict, date | datetime]]] = {}
    for key, item in candidates:
        course_key = extract_course_from_key(key, item)
        base = strip_instance_number(item.get("title", ""))
        if not base:
            continue
        parsed = parse_due_date(str(item["due_date"]))
        if parsed is None:
            continue
        by_group.setdefault((course_key, base), []).append((key, item, parsed))

    groups: list[dict] = []
    grouped_keys: set[str] = set()
    for (course_key, base), members in by_group.items():
        if len(members) < 3:
            continue
        members.sort(key=lambda m: m[2])
        as_dates = [m[2].date() if isinstance(m[2], datetime) else m[2] for m in members]
        deltas = [(b - a).days for a, b in zip(as_dates, as_dates[1:])]
        if any(d <= 0 for d in deltas) or max(deltas) - min(deltas) > 1:
            continue
        interval = round(sum(deltas) / len(deltas))
        if not (1 <= interval <= 21):
            continue

        times = {m[2].strftime("%H:%M") for m in members if isinstance(m[2], datetime)}
        common_time = times.pop() if len(times) == 1 else None

        groups.append(
            {
                "course_key": course_key,
                "base_title": base,
                "members": members,
                "interval_days": interval,
                "anchor": as_dates[0],
                "time": common_time,
            }
        )
        grouped_keys.update(m[0] for m in members)

    return groups, grouped_keys


def build_recurring_due_string(interval_days: int, anchor: date, time_hm: str | None) -> str:
    if interval_days == 7:
        freq = "every week"
    elif interval_days % 7 == 0:
        freq = f"every {interval_days // 7} weeks"
    elif interval_days == 1:
        freq = "every day"
    else:
        freq = f"every {interval_days} days"

    due_string = f"{freq} starting {anchor.strftime('%b %-d %Y')}"
    if time_hm:
        eastern_dt = datetime.strptime(time_hm, "%H:%M").replace(
            year=anchor.year, month=anchor.month, day=anchor.day, tzinfo=GRADESCOPE_TZ
        )
        local_dt = eastern_dt.astimezone(ACCOUNT_TIMEZONE)
        due_string += " at " + local_dt.strftime("%I:%M%p").lstrip("0").lower()
    return due_string


def sync_assignments(
    *,
    client: TodoistClient | None,
    dry_run: bool = False,
    init_only: bool = False,
    backfill: bool = False,
    reset_format: bool = False,
    ai_titles: bool = False,
) -> dict[str, int]:
    store = load_store()
    synced = load_synced()
    stats = {"marked": 0, "created": 0, "skipped": 0, "errors": 0, "created_titles": []}

    first_run = not synced.get("initialized") and SYNCED_FILE.exists() is False
    if first_run and not init_only and not backfill:
        init_only = True

    if init_only:
        stats["marked"] = mark_all_synced(store, synced)
        if not dry_run:
            save_synced(synced)
            logging.info(f"Todoist init: marked {stats['marked']} existing assignment(s) as synced")
        print(
            f"{'[dry-run] Would mark' if dry_run else 'Initialized sync state: marked'} "
            f"{stats['marked']} existing assignment(s) as synced "
            f"without creating Todoist tasks."
        )
        print("Only newly discovered dated assignments will be pushed on future runs.")
        print("Use --backfill if you want to push current unsynced items later.")
        return stats

    template = get_template(client, reset=reset_format, dry_run=dry_run)
    candidates = iter_push_candidates(store, synced, backfill=backfill)

    if not candidates:
        print("No new dated assignments to push.")
        if not dry_run:
            save_synced(synced)
        return stats

    recurring_groups, grouped_keys = detect_recurring_groups(candidates)
    single_candidates = [(k, i) for k, i in candidates if k not in grouped_keys]

    label_name = None
    existing_labels: set[str] = set()
    labels: list[dict] = []
    if client:
        labels = client.get_labels()
        existing_labels = {label["name"] for label in labels}
        label_name = choose_school_label(labels)

    recent_titles: list[str] = []
    if ai_titles and client:
        # Active tasks alone aren't enough real style history to learn from
        # (this account currently has none) -- completed tasks are the real
        # naming-convention history, so pull those too, most recent first.
        active = [t.get("content", "") for t in client.get_active_tasks(limit=100) if t.get("content")]
        completed = sorted(
            client.get_completed_tasks(limit=200),
            key=lambda t: t.get("completed_at", ""),
            reverse=True,
        )
        recent_titles = active + [t.get("content", "") for t in completed if t.get("content")]

    # Generate AI titles once per course, in a single batched call per course,
    # rather than one call per assignment -- independent per-item calls have
    # no shared state, so nothing forces them to agree on a format.
    #
    # Also resolve each course's label here: reuse a real existing label if
    # one already matches this course (matched via normalize_course_name,
    # same logic pipeline.py uses to pair Brightspace/Gradescope courses),
    # otherwise ask the AI to propose a new name in the same convention as
    # the account's real labels (e.g. "ece 270", "ma 266") rather than
    # guessing a format in code.
    ai_title_map: dict[str, str] = {}
    group_title_map: dict[int, str] = {}
    course_label_map: dict[str | None, str] = {}
    if ai_titles:
        # Units to title: standalone assignments (keyed by their store key)
        # plus one unit per recurring group (keyed by its index), grouped
        # by course so each course still gets one consistent-style batch
        # call -- recurring groups get their base title (no instance
        # number) and are flagged so the AI knows not to invent one.
        by_course: dict[str | None, list[tuple[str, bool]]] = {}
        for key, item in single_candidates:
            course_key = extract_course_from_key(key, item)
            by_course.setdefault(course_key, []).append((f"single:{key}", False))
        for idx, group in enumerate(recurring_groups):
            by_course.setdefault(group["course_key"], []).append((f"group:{idx}", True))

        existing_label_names = [l["name"] for l in labels]
        label_by_course_key = {normalize_label_name(name): name for name in existing_label_names}

        for course_key, units in by_course.items():
            course_number = course_key_to_number(course_key) if course_key else "School"
            titles = []
            for unit_id, _ in units:
                kind, raw_id = unit_id.split(":", 1)
                if kind == "single":
                    titles.append(store["assignments"][raw_id].get("title", raw_id))
                else:
                    titles.append(recurring_groups[int(raw_id)]["base_title"])
            recurring_flags = [is_rec for _, is_rec in units]
            generated = generate_ai_titles_batch(recent_titles, course_number, titles, recurring_flags)
            if generated is None:
                logging.warning(
                    f"AI title generation failed for course {course_number}; "
                    "falling back to template for its items"
                )
            else:
                for (unit_id, _), ai_title in zip(units, generated):
                    kind, raw_id = unit_id.split(":", 1)
                    if kind == "single":
                        ai_title_map[raw_id] = ai_title
                    else:
                        group_title_map[int(raw_id)] = ai_title

            if not course_key:
                continue
            existing_label = label_by_course_key.get(course_key)
            if existing_label:
                course_label_map[course_key] = existing_label
                continue
            proposed = generate_course_label(existing_label_names, course_key_to_display(course_key))
            course_label_map[course_key] = proposed or course_key_to_label(course_key)

    def resolve_label(course_key: str | None) -> str | None:
        if not course_key:
            return None
        if ai_titles:
            return course_label_map.get(course_key)
        return course_key_to_label(course_key)

    print(
        f"{'Would push' if dry_run else 'Pushing'} {len(single_candidates)} one-off "
        f"assignment(s) and {len(recurring_groups)} recurring item(s) "
        f"(covering {sum(len(g['members']) for g in recurring_groups)} instances)..."
    )

    for key, item in single_candidates:
        course_key = extract_course_from_key(key, item)
        title = item.get("title", key)
        due_fields = build_due_fields(str(item["due_date"]))

        content = ai_title_map.get(key) if ai_titles else None
        if content is None:
            content = format_task_title(template, course_key, title)
        course_label = resolve_label(course_key)
        labels = [l for l in (label_name, course_label) if l]

        if dry_run:
            print(f"  [dry-run] {content} | {due_fields} | labels={labels}")
            stats["created"] += 1
            continue

        try:
            assert client is not None
            if label_name:
                client.ensure_label(label_name, existing_labels)
            if course_label:
                client.ensure_label(course_label, existing_labels)
            task = client.create_task(
                content=content,
                labels=labels or None,
                **due_fields,
            )
            synced["synced_keys"][key] = {
                "marked_at": datetime.now(timezone.utc).isoformat(),
                "reason": "backfill" if backfill else "created",
                "todoist_task_id": task.get("id"),
            }
            stats["created"] += 1
            stats["created_titles"].append(content)
            print(f"  + {content}")
            logging.info(f"Created Todoist task for {key}: {content}")
            time.sleep(0.15)
        except TodoistError as exc:
            stats["errors"] += 1
            logging.error(f"Failed to create Todoist task for {key}: {exc}")
            print(f"  ERROR {key}: {exc}")

    for idx, group in enumerate(recurring_groups):
        course_key = group["course_key"]
        content = group_title_map.get(idx) if ai_titles else None
        if content is None:
            content = format_task_title(template, course_key, group["base_title"])
        course_label = resolve_label(course_key)
        labels = [l for l in (label_name, course_label) if l]
        due_string = build_recurring_due_string(group["interval_days"], group["anchor"], group["time"])
        member_keys = [m[0] for m in group["members"]]

        if dry_run:
            print(
                f"  [dry-run][recurring] {content} | due_string={due_string!r} | "
                f"labels={labels} | covers {len(member_keys)} instances: {member_keys}"
            )
            stats["created"] += 1
            continue

        try:
            assert client is not None
            if label_name:
                client.ensure_label(label_name, existing_labels)
            if course_label:
                client.ensure_label(course_label, existing_labels)
            task = client.create_task(content=content, due_string=due_string, labels=labels or None)
            for member_key in member_keys:
                synced["synced_keys"][member_key] = {
                    "marked_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "recurring",
                    "todoist_task_id": task.get("id"),
                }
            stats["created"] += 1
            stats["created_titles"].append(f"{content} (recurring)")
            print(f"  + {content} (recurring, {len(member_keys)}x)")
            logging.info(
                f"Created recurring Todoist task '{content}' ({due_string}) "
                f"covering {len(member_keys)} instance(s): {member_keys}"
            )
            time.sleep(0.15)
        except TodoistError as exc:
            stats["errors"] += 1
            logging.error(f"Failed to create recurring Todoist task '{content}': {exc}")
            print(f"  ERROR recurring '{content}': {exc}")

    if not dry_run:
        save_synced(synced)
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync assignments.json to Todoist")
    parser.add_argument(
        "--init",
        action="store_true",
        help="Mark all existing assignments as synced without pushing",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Push unsynced future-dated assignments already in assignments.json (init-marked items)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview actions without writing to Todoist",
    )
    parser.add_argument(
        "--reset-format",
        action="store_true",
        help="Forget saved title template and re-prompt",
    )
    parser.add_argument(
        "--ai-titles",
        action="store_true",
        help="Generate task titles with Gemini (mimicking your existing Todoist "
        "style) instead of the fixed template; falls back to the template "
        "per-item if the API call fails",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if not TODOIST_API_TOKEN and not args.dry_run:
        print(
            "Set TODOIST_API_TOKEN in config.py first "
            "(Todoist -> Settings -> Integrations -> Developer)."
        )
        sys.exit(1)

    # Normally no client is created for --dry-run (no writes needed), but
    # --ai-titles still needs a client for read-only calls (recent task
    # titles for style, label lookups) so the preview reflects real output.
    client = None
    if TODOIST_API_TOKEN and (not args.dry_run or args.ai_titles):
        client = TodoistClient(TODOIST_API_TOKEN)

    stats = sync_assignments(
        client=client,
        dry_run=args.dry_run,
        init_only=args.init,
        backfill=args.backfill,
        reset_format=args.reset_format,
        ai_titles=args.ai_titles,
    )

    if not args.init:
        print(
            f"Done: created={stats['created']} "
            f"errors={stats['errors']} marked={stats['marked']}"
        )

        # Mirror pipeline.py's discovery notification with a push-side one --
        # "N found" and "N pushed" can legitimately differ (already-synced
        # items, dry-run, push errors), so both are worth seeing separately,
        # especially on the unattended launchd run nobody is watching.
        if not args.dry_run:
            if stats["created"] > 0:
                # A bare count told you SOMETHING happened but not what --
                # list the actual titles (same preview-then-"+N more"
                # pattern as pipeline.py's nudge notification) so you can
                # tell at a glance whether it's what you expected.
                titles = stats["created_titles"]
                preview = ", ".join(titles[:5]) + (f" +{len(titles) - 5} more" if len(titles) > 5 else "")
                notify(
                    f"{stats['created']} task(s) pushed to Todoist",
                    preview
                    + (f" ({stats['errors']} error(s))" if stats["errors"] else ""),
                )
            elif stats["errors"] > 0:
                notify(
                    "Todoist sync failed",
                    f"{stats['errors']} task(s) failed to push -- check the log.",
                    sound=True,
                )


if __name__ == "__main__":
    main()
