"""
brightspace_reader.py

Reads three Brightspace sources for a given course:
  1. Gradebook            - confirms an item exists, rarely has a date
  2. Assignments tool     - often DOES have a real due date
  3. Quizzes tool         - often DOES have a real due date/availability window

Then reports which items are STILL missing a date after checking all
three, so the pipeline knows what to look up on Gradescope instead.

Setup:
    pip install playwright
    playwright install chromium

Session handling:
    Brightspace login goes through Purdue SSO + Duo, which a script
    can't complete unattended. So the FIRST run opens a real (visible)
    browser window for you to log in and clear Duo manually, once.
    After that, the session is saved to SESSION_FILE and reused on
    every future run -- no repeated logins, until Brightspace expires
    the session on its own (at which point this script will tell you
    to re-run the login step, rather than failing silently).
"""

import json
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_URL = "https://purdue.brightspace.com"
SESSION_FILE = Path(__file__).parent / "brightspace_session.json"


class SessionExpiredError(Exception):
    """Raised by ensure_session(interactive=False) instead of blocking on
    input() -- Duo can't be cleared unattended, so a scheduled run needs to
    fail fast and let the caller notify rather than hang forever."""


# ---------------------------------------------------------------------------
# Session handling
# ---------------------------------------------------------------------------

def _session_is_valid() -> bool:
    """Headlessly checks whether the saved session still works."""
    if not SESSION_FILE.exists():
        return False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(SESSION_FILE))
        page = context.new_page()
        page.goto(f"{BASE_URL}/d2l/home")
        page.wait_for_load_state("networkidle")
        valid = "login" not in page.url and "sso.purdue.edu" not in page.url
        browser.close()
        return valid


def ensure_session(interactive: bool = True):
    if _session_is_valid():
        return

    if not interactive:
        raise SessionExpiredError("Brightspace session expired or missing")

    print("No valid Brightspace session found (missing or expired).")
    print("A browser window will open -- please log in (including Duo),")
    print("then return here and press Enter once you're on your homepage.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{BASE_URL}/d2l/login")
        input("Press Enter once logged in... ")
        context.storage_state(path=str(SESSION_FILE))
        browser.close()

    print(f"Session saved to {SESSION_FILE}")


def get_context(playwright):
    """
    Returns a browser context loaded with the saved session. Call
    ensure_session() before this on first-ever run.
    """
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(storage_state=str(SESSION_FILE))
    return browser, context

def get_enrolled_courses(context) -> list[dict]:
    """
    Returns [{ "org_unit_id": str, "name": str }] for every course
    linked from your Brightspace homepage -- auto-discovers your
    current course list instead of needing it hardcoded.
    """
    page = context.new_page()
    page.goto(f"{BASE_URL}/d2l/home")
    page.wait_for_load_state("networkidle")

    collect_script = """
        () => {
            function collectLinksDeep(root, out) {
                const anchors = root.querySelectorAll ? root.querySelectorAll('a[href*="/d2l/home/"]') : [];
                for (const a of anchors) {
                    out.push({ href: a.href, text: a.textContent.trim() });
                }
                const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of all) {
                    if (el.shadowRoot) collectLinksDeep(el.shadowRoot, out);
                }
            }
            const out = [];
            collectLinksDeep(document.body, out);
            return out;
        }
    """

    # The course-card widget (d2l-my-courses, a lit-based web component) is
    # populated by its own async fetch AFTER "networkidle" already fires --
    # confirmed live: a run right after networkidle saw only the 3 static
    # campus-switcher links ("Purdue University West Lafayette/Fort Wayne/
    # Northwest"), while re-running the identical scrape moments later saw
    # the full real course list. Poll instead of trusting one snapshot: keep
    # re-collecting until the link count stops growing (course cards have
    # rendered) or we hit the timeout.
    links = page.evaluate(collect_script)
    for _ in range(20):  # up to ~10s
        page.wait_for_timeout(500)
        next_links = page.evaluate(collect_script)
        if len(next_links) <= len(links):
            links = next_links
            break
        links = next_links

    seen = {}
    for link in links:
        match = re.search(r"/d2l/home/(\d+)", link["href"])
        if not match or not link["text"]:
            continue
        org_unit_id = match.group(1)
        if org_unit_id not in seen and org_unit_id != "":
            seen[org_unit_id] = link["text"]

    page.close()
    return [{"org_unit_id": oid, "name": name} for oid, name in seen.items()]


# ---------------------------------------------------------------------------
# Course code helpers (same normalization logic proven in the extension)
# ---------------------------------------------------------------------------

def normalize_course_name(raw: str) -> str:
    match = re.search(r"([A-Za-z]{2,4})\s?-?\s?(\d{3,5})", raw)
    if not match:
        return raw.strip().lower()
    prefix = match.group(1).lower()
    number = match.group(2)
    if len(number) == 5 and number.endswith("00"):
        number = number[:3]
    return f"{prefix}{number}"


# ---------------------------------------------------------------------------
# Page readers
# ---------------------------------------------------------------------------
# NOTE: selectors below are best-guess based on standard D2L page
# structure. Brightspace instances vary; if a reader below returns an
# empty list on a page you know has content, inspect that real page
# and tell me what the row/date elements actually look like so I can
# tune the selector -- same process we used for the extension.

def get_gradebook_items(context, org_unit_id: str) -> list[dict]:
    """
    Returns [{ "title": str, "due_date": None }] -- Gradebook rarely
    has real due dates, so this mainly confirms an item's existence.
    """
    page = context.new_page()
    page.goto(f"{BASE_URL}/d2l/lms/grades/my_grades/main.d2l?ou={org_unit_id}")
    page.wait_for_load_state("networkidle")

    items = []
    rows = page.query_selector_all("table tr")
    for row in rows:
        cells = row.query_selector_all("th, td")
        if not cells:
            continue
        label = cells[0].inner_text().strip()
        # Skip obvious non-assignment rows (percentages, points totals,
        # nav text, and known table-header/summary labels). Confirmed via
        # real data (ECE 362) this page mixes actual gradable items in
        # with grade CATEGORY rows ("Labs", "Homework") and summary rows
        # ("Final Grade", "Total") -- categories and real items share the
        # exact same CSS classes at the cell level (both d_gt d_ich,
        # is_th=True), so there's no clean structural signal there. Two
        # things DO reliably distinguish some of them though:
        # 1. A category row that has expandable children gets row class
        #    "d_ggl1" (confirmed on "Labs"/"Lab Penalties"/"Homework"/
        #    "Course Project"); a real leaf item never does.
        # 2. Bonus/penalty roll-up rows always have "bonus" or "penalt"
        #    in the label ("Participation Bonus (Bonus)", "Lab
        #    Penalties", "PCB Bonus Credit") -- real gradable deliverables
        #    are never named this way.
        # This still leaves a few bare category words (e.g. "Midterm",
        # "Final", "Component") that look identical to a real item at
        # this level and can't be safely filtered without risking a real
        # course's actual item names -- they'll sit with due_date=None
        # and, since they never match a real Gradescope title, simply
        # never get a real date or reach Todoist. Harmless besides an
        # occasional odd nudge notification.
        row_class = row.get_attribute("class") or ""
        JUNK_LABELS = {
            "grade item", "points", "grade", "comments and assessments",
            "final grade", "total", "adjusted final grade",
        }
        # e.g. "0 / 3,933" -- the original digit-only pattern didn't
        # account for a comma thousands-separator and let this slip
        # through as a real title (confirmed via real data, ECE 362).
        if (
            not label
            or re.match(r"^[\d.,/%\s-]+$", label)
            or label.lower() in JUNK_LABELS
            or "d_ggl1" in row_class
            or re.search(r"bonus|penalt", label, re.IGNORECASE)
        ):
            continue
        items.append({"title": label, "due_date": None, "source": "gradebook"})

    page.close()
    return items


def get_assignments_tool_items(context, org_unit_id: str) -> list[dict]:
    """
    Returns [{ "title": str, "due_date": str|None, "status": str }]
    from the Assignments (dropbox) tool, which often has real dates.
    """
    page = context.new_page()
    page.goto(f"{BASE_URL}/d2l/lms/dropbox/user/folders_list.d2l?ou={org_unit_id}")
    page.wait_for_load_state("networkidle")

    items = []
    rows = page.query_selector_all("table tr")
    for row in rows:
        cells = row.query_selector_all("th, td")
        if len(cells) < 2:
            continue
        title = cells[0].inner_text().strip()
        if not title:
            continue
        JUNK_LABELS = {
            "assignment", "completion status", "score", "evaluation status",
            "no category", "without category", "attempts", "feedback:",
            "on attempt", "results per page"
        }
        if title.lower() in JUNK_LABELS or "per page" in title.lower():
            continue
        row_text = row.inner_text()
        date_match = re.search(
            r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}.*?(AM|PM)",
            row_text,
            re.IGNORECASE,
        )
        items.append(
            {
                "title": title,
                "due_date": date_match.group(0) if date_match else None,
                "source": "assignments_tool",
            }
        )

    page.close()
    return items


def get_quizzes_tool_items(context, org_unit_id: str) -> list[dict]:
    """
    Returns [{ "title": str, "due_date": str|None }] from the Quizzes
    tool, which often has real availability/due dates.
    """
    page = context.new_page()
    page.goto(f"{BASE_URL}/d2l/lms/quizzing/user/quizzes_list.d2l?ou={org_unit_id}")
    page.wait_for_load_state("networkidle")

    items = []
    rows = page.query_selector_all("table tr")
    for row in rows:
        cells = row.query_selector_all("th, td")
        if len(cells) < 2:
            continue
        title = cells[0].inner_text().strip()
        if not title:
            continue
        JUNK_LABELS = {
            "assignment", "completion status", "score", "evaluation status",
            "no category", "without category", "attempts", "feedback:",
            "on attempt", "results per page"
        }
        if title.lower() in JUNK_LABELS or "per page" in title.lower():
            continue
        row_text = row.inner_text()
        date_match = re.search(
            r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}.*?(AM|PM)",
            row_text,
            re.IGNORECASE,
        )
        items.append(
            {
                "title": title,
                "due_date": date_match.group(0) if date_match else None,
                "source": "quizzes_tool",
            }
        )

    page.close()
    return items


# ---------------------------------------------------------------------------
# Merge + resolve
# ---------------------------------------------------------------------------

def get_undated_items(context, org_unit_id: str, course_key: str) -> list[dict]:
    """
    Checks Gradebook + Assignments tool + Quizzes tool, in that
    priority order, and returns only items that are STILL missing a
    due date after all three -- i.e. what needs a Gradescope lookup.
    """
    gradebook = get_gradebook_items(context, org_unit_id)
    assignments = get_assignments_tool_items(context, org_unit_id)
    quizzes = get_quizzes_tool_items(context, org_unit_id)

    # Build a lookup of titles that DO have a resolved date from the
    # tool pages, so we can cross off matching Gradebook entries.
    dated_titles = set()
    for item in assignments + quizzes:
        if item["due_date"]:
            dated_titles.add(item["title"].strip().lower())

    undated = []
    seen = set()
    for item in gradebook:
        key = item["title"].strip().lower()
        if key in dated_titles or key in seen:
            continue
        seen.add(key)
        undated.append({**item, "course": course_key})

    return undated


# ---------------------------------------------------------------------------
# Example run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ensure_session()

    # ME 270's org unit ID, from the URLs we already confirmed working.
    ORG_UNIT_ID = "1565975"
    COURSE_KEY = normalize_course_name("ME 27000")

    with sync_playwright() as p:
        browser, context = get_context(p)

        undated = get_undated_items(context, ORG_UNIT_ID, COURSE_KEY)

        print(f"\n{len(undated)} item(s) still missing a due date for {COURSE_KEY}:")
        for item in undated:
            print(f"  - {item['title']}  (source: {item['source']})")

        browser.close()