"""
pipeline.py

Orchestrates the full flow:
  1. Fetch the Brightspace ICS feed (dated items -- the reliable source)
  2. For courses you configure below, check Brightspace's Gradebook +
     Assignments tool + Quizzes tool for items STILL missing a date
  3. Cross-reference those undated items against Gradescope's due dates
  4. Save everything to a local JSON store (assignments.json)
  5. Log what happened to pipeline.log
  6. Fire a desktop notification if anything new was found or resolved

Run manually:
    python pipeline.py

Run unattended (e.g. from launchd -- see run_pipeline.sh):
    python pipeline.py --unattended
    Skips the interactive re-login prompt if a session has expired --
    logs it, fires a desktop notification asking for a manual re-login,
    and still saves whatever the ICS step produced, rather than hanging
    forever waiting for input no one is there to give.
"""

import argparse
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plyer import notification  # pip install plyer
from playwright.sync_api import sync_playwright

from matching import normalize_course_name, best_match
from brightspace_reader import (
    ensure_session as ensure_brightspace_session,
    get_context as get_brightspace_context,
    get_undated_items,
    get_enrolled_courses as get_brightspace_enrolled_courses,
    SessionExpiredError as BrightspaceSessionExpiredError,
)
from gradescope_reader import (
    ensure_session as ensure_gradescope_session,
    get_context as get_gradescope_context,
    get_course_assignments,
    get_enrolled_courses as get_gradescope_enrolled_courses,
    SessionExpiredError as GradescopeSessionExpiredError,
)

from fetch_ics import fetch_events
from config import BRIGHTSPACE_ICS_URL as ICS_URL

STORE_FILE = Path(__file__).parent / "assignments.json"
LOG_FILE = Path(__file__).parent / "pipeline.log"

# "Nudge me" reminder thresholds for permanently undated items.
NUDGE_AFTER_DAYS = 14
RENUDGE_EVERY_DAYS = 10

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
)


def load_store() -> dict:
    if STORE_FILE.exists():
        return json.loads(STORE_FILE.read_text())
    return {"assignments": {}}


def save_store(store: dict):
    STORE_FILE.write_text(json.dumps(store, indent=2))


def notify(title: str, message: str, *, sound: bool = False):
    import subprocess
    try:
        script = f'display notification "{message}" with title "{title}"'
        if sound:
            # Session-expired alerts default to silent-banner easy to miss
            # in a pile of notifications -- a sound makes the ones that
            # need you to actually go re-login (unlike routine "N new
            # assignments found" banners) harder to miss.
            script += ' sound name "Basso"'
        subprocess.run(["osascript", "-e", script], check=True)
    except Exception as e:
        logging.info(f"Notification failed (non-fatal): {e}")


def extract_course_from_location(location: str) -> str | None:
    """Brightspace's ICS feed pools every course into one calendar, but each
    VEVENT's LOCATION carries the real course, e.g. 'Fall 2026 ECE
    33700-010 LEC - Merge' -- confirmed via the real live feed. Generic
    campus events (advising, guest lectures) have a non-course LOCATION
    ('Advising with Jaden', a building name) and correctly yield None here
    via normalize_course_name's own no-match fallback check."""
    if not location or not re.search(r"[A-Z]{2,4}\s?-?\s?\d{3,5}", location):
        return None
    return normalize_course_name(location)


def run_ics_step(store: dict) -> int:
    new_count = 0
    events = fetch_events(ICS_URL)
    for event in events:
        key = f"ics::{event['title']}::{event['start']}"
        course = extract_course_from_location(event.get("location", ""))
        if key not in store["assignments"]:
            store["assignments"][key] = {
                "title": event["title"],
                "due_date": str(event["start"]) if event["start"] else None,
                "source": "ics",
                "course": course,
            }
            new_count += 1
        elif "course" not in store["assignments"][key]:
            # Backfill items pushed before LOCATION-based course
            # detection existed, without touching anything else about them.
            store["assignments"][key]["course"] = course
    return new_count


def run_cross_check_step(store: dict, *, interactive: bool = True) -> int:
    new_or_resolved = 0

    # Check BOTH sessions independently rather than stopping at the first
    # expired one -- otherwise if Brightspace expires first, you'd never
    # find out Gradescope also needs a re-login until Brightspace alone
    # got fixed, potentially days later.
    sessions_ok = True
    try:
        ensure_brightspace_session(interactive=interactive)
    except BrightspaceSessionExpiredError as exc:
        logging.error(f"Cross-check step: {exc}")
        notify(
            "Brightspace login needed",
            f"{exc} -- run pipeline.py manually to re-authenticate.",
            sound=True,
        )
        sessions_ok = False

    try:
        ensure_gradescope_session(interactive=interactive)
    except GradescopeSessionExpiredError as exc:
        logging.error(f"Cross-check step: {exc}")
        notify(
            "Gradescope login needed",
            f"{exc} -- run pipeline.py manually to re-authenticate.",
            sound=True,
        )
        sessions_ok = False

    if not sessions_ok:
        return 0

    with sync_playwright() as p:
        bs_browser, bs_context = get_brightspace_context(p)
        gs_browser, gs_context = get_gradescope_context(p)

        # Auto-discover enrolled courses on both sides instead of a
        # hardcoded list -- this is what makes new semesters "just work."
        bs_courses = get_brightspace_enrolled_courses(bs_context)
        gs_courses = get_gradescope_enrolled_courses(gs_context)

        # Pair them up by normalized course key.
        gs_by_key = {normalize_course_name(c["name"]): c for c in gs_courses}

        paired_courses = []
        for bs_course in bs_courses:
            key = normalize_course_name(bs_course["name"])
            gs_match = gs_by_key.get(key)
            if gs_match:
                paired_courses.append(
                    {
                        "brightspace_org_unit_id": bs_course["org_unit_id"],
                        "brightspace_course_label": bs_course["name"],
                        "gradescope_course_id": gs_match["course_id"],
                    }
                )

        logging.info(
            f"Auto-discovered {len(bs_courses)} Brightspace course(s), "
            f"{len(gs_courses)} Gradescope course(s), "
            f"{len(paired_courses)} matched pair(s)"
        )
        # Full name -> normalized-key breakdown, so a run where pairing looks
        # too low is self-diagnosing from pipeline.log alone instead of
        # needing a live debugging session to see why two course lists
        # didn't line up.
        for c in bs_courses:
            logging.info(f"  Brightspace course: {c['name']!r} -> {normalize_course_name(c['name'])!r}")
        for c in gs_courses:
            logging.info(f"  Gradescope course: {c['name']!r} -> {normalize_course_name(c['name'])!r}")

        for course in paired_courses:
            course_key = normalize_course_name(course["brightspace_course_label"])

            undated = get_undated_items(
                bs_context, course["brightspace_org_unit_id"], course_key
            )
            logging.info(f"{course_key}: {len(undated)} undated item(s) from Brightspace")

            gs_items = get_course_assignments(gs_context, course["gradescope_course_id"])
            logging.info(
                f"{course_key}: {len(gs_items)} Gradescope assignment row(s) scraped: "
                f"{[g['title'] for g in gs_items]}"
            )

            for item in undated:
                key = f"{course_key}::{item['title'].lower()}"
                existing = store["assignments"].get(key)

                match = best_match(item["title"], gs_items, key="title")
                due_date = match["due_date_raw"] if match else None

                if existing is None:
                    store["assignments"][key] = {
                        "title": item["title"],
                        "due_date": due_date,
                        "source": "brightspace_gradebook+gradescope" if match else "brightspace_gradebook",
                    }
                    new_or_resolved += 1
                elif existing.get("due_date") is None and due_date is not None:
                    existing["due_date"] = due_date
                    existing["source"] = "brightspace_gradebook+gradescope"
                    new_or_resolved += 1

            # Independent Gradescope discovery: the loop above only ever
            # looks at Gradescope for items Brightspace's gradebook
            # ALREADY lists as undated -- if Brightspace hasn't added the
            # gradebook column for something yet (confirmed real gap:
            # ECE 362's "Lab 1" was live on Gradescope, due in 5 days,
            # but didn't exist in Brightspace's gradebook/Assignments/
            # Quizzes tools at all, so the pipeline had no way to even
            # know it existed), it's silently missed entirely. Cross off
            # anything already known for this course from ANY source
            # (cross-check keys use "{course_key}::...", ICS-sourced ones
            # carry a "course" field -- see run_ics_step), then add
            # whatever's left directly from Gradescope's own list, with
            # Gradescope's own due date, no Brightspace listing required.
            known_for_course = {
                k: v
                for k, v in store["assignments"].items()
                if k.startswith(f"{course_key}::") or v.get("course") == course_key
            }
            for gs_item in gs_items:
                match_key = best_match(
                    gs_item["title"],
                    [{"key": k, "title": v["title"]} for k, v in known_for_course.items()],
                    key="title",
                )
                if match_key:
                    # Already known -- if it's still undated (e.g. found
                    # earlier via Brightspace with no Gradescope match at
                    # the time, or added here before Gradescope had
                    # posted a real date), fill it in now. Consistent
                    # with every other merge path in this codebase: an
                    # ALREADY-set date is never overwritten, even a
                    # stale/wrong one -- clear it manually if Gradescope
                    # corrects a date that was already (wrongly) filled.
                    existing = store["assignments"][match_key["key"]]
                    if existing.get("due_date") is None:
                        existing["due_date"] = gs_item["due_date_raw"]
                        existing["source"] = f"{existing['source']}+gradescope_direct"
                        new_or_resolved += 1
                    continue
                key = f"{course_key}::{gs_item['title'].lower()}"
                if key in store["assignments"]:
                    continue
                store["assignments"][key] = {
                    "title": gs_item["title"],
                    "due_date": gs_item["due_date_raw"],
                    "source": "gradescope_direct",
                }
                new_or_resolved += 1
                logging.info(f"{course_key}: discovered '{gs_item['title']}' directly from Gradescope (not yet on Brightspace)")

        bs_browser.close()
        gs_browser.close()

    return new_or_resolved


def _missing_fields(item: dict) -> list[str]:
    missing = []
    if item.get("due_date") is None:
        missing.append("date")
    # location_expected is set by syllabus_parser.py only for exam-like
    # items (exam/midterm/final) that genuinely should have one -- regular
    # assignments never carry this flag, so they're never nudged for it.
    if item.get("location_expected") and not item.get("location"):
        missing.append("location")
    return missing


def stamp_and_check_undated(store: dict) -> list[dict]:
    """
    Single pass over the whole store rather than threading this into
    every creation point across pipeline.py and syllabus_parser.py.
    Tracks missing dates AND missing locations (for exam-like items) on
    the SAME per-item timer, so if an item is missing either or both,
    they get nudged together in one notification rather than on separate
    schedules:
    - stamps first_seen_undated the first time an item is seen missing
      something (this backfills pre-existing items the first time this
      runs, and picks up items created by a standalone syllabus_parser.py
      run too, regardless of which script created the entry)
    - clears first_seen_undated/last_nudged_at once everything resolves
    - returns the list of items due for a nudge (never nudged and
      missing something >= NUDGE_AFTER_DAYS, or nudged before but
      >= RENUDGE_EVERY_DAYS since the last one), stamping
      last_nudged_at and missing_fields on each as it's included
    """
    now = datetime.now(timezone.utc)
    to_nudge = []

    for item in store["assignments"].values():
        missing = _missing_fields(item)
        if not missing:
            item.pop("first_seen_undated", None)
            item.pop("last_nudged_at", None)
            item.pop("missing_fields", None)
            continue

        item["missing_fields"] = missing

        if "first_seen_undated" not in item:
            item["first_seen_undated"] = now.isoformat()
            continue  # just seen for the first time -- too early to nudge

        first_seen = datetime.fromisoformat(item["first_seen_undated"])
        if now - first_seen < timedelta(days=NUDGE_AFTER_DAYS):
            continue

        last_nudged_raw = item.get("last_nudged_at")
        if last_nudged_raw:
            last_nudged = datetime.fromisoformat(last_nudged_raw)
            if now - last_nudged < timedelta(days=RENUDGE_EVERY_DAYS):
                continue

        item["last_nudged_at"] = now.isoformat()
        to_nudge.append(item)

    return to_nudge


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync assignments from Brightspace/Gradescope")
    parser.add_argument(
        "--unattended",
        action="store_true",
        help="Skip the interactive re-login prompt if a session has expired "
        "(for scheduled/unattended runs) -- notifies instead of hanging",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    logging.info("Pipeline run started" + (" (unattended)" if args.unattended else ""))
    store = load_store()

    # Anything other than an expired session (network timeout, a changed
    # page/DOM, etc.) used to crash silently here -- an unattended launchd
    # run has no one watching stdout, so the only trace was launchd.err,
    # confirmed via real crashes sitting there unnoticed (a Playwright
    # "Timeout 30000ms exceeded"). Catch it, save whatever progress was
    # made before the crash, and notify -- same as the session-expiry path
    # -- then re-raise so it's still a nonzero exit / still in the log.
    try:
        ics_new = run_ics_step(store)

        # Session-expiry notifications (per-service, with sound) happen
        # inside run_cross_check_step itself -- it returns 0 rather than
        # raising when either session is invalid, so a real exception here
        # is something else.
        cross_check_new = run_cross_check_step(store, interactive=not args.unattended)

        nudge_items = stamp_and_check_undated(store)
    except Exception as exc:
        logging.exception("Pipeline run crashed")
        save_store(store)
        notify(
            "Pipeline run failed",
            f"{type(exc).__name__}: {exc} -- check pipeline.log.",
            sound=True,
        )
        raise

    save_store(store)

    total_new = ics_new + cross_check_new
    logging.info(
        f"Run complete: {ics_new} new ICS item(s), "
        f"{cross_check_new} new/resolved cross-check item(s)"
    )

    if total_new > 0:
        notify(
            "Assignment sync complete",
            f"{total_new} new or updated assignment(s) found.",
        )
    else:
        logging.info("No changes this run.")

    if nudge_items:
        labels = []
        for item in nudge_items:
            missing = "/".join(item.get("missing_fields", []))
            tag = " (syllabus says TBD)" if item.get("tbd") else ""
            labels.append(f"{item['title']} [missing {missing}]{tag}")
        logging.info(f"Nudge: {len(labels)} item(s) still missing info after {NUDGE_AFTER_DAYS}+ days: {labels}")
        preview = ", ".join(labels[:5]) + ("..." if len(labels) > 5 else "")
        notify(
            f"{len(labels)} assignment(s) still missing date/location",
            f"{preview} -- check for an updated syllabus.",
        )


if __name__ == "__main__":
    main()