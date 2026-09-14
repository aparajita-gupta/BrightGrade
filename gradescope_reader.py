"""
gradescope_reader.py

Reads a Gradescope course's assignment list (name + due date), using
a persisted login session -- same pattern as brightspace_reader.py.

Setup:
    pip install playwright
    playwright install chromium
"""

import re
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.gradescope.com"
SESSION_FILE = Path(__file__).parent / "gradescope_session.json"


class SessionExpiredError(Exception):
    """Raised by ensure_session(interactive=False) instead of blocking on
    input() -- a scheduled run needs to fail fast and let the caller notify
    rather than hang forever waiting for a login no one is there to do."""


def _session_is_valid() -> bool:
    if not SESSION_FILE.exists():
        return False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(SESSION_FILE))
        page = context.new_page()
        page.goto(f"{BASE_URL}/account")
        page.wait_for_load_state("networkidle")
        valid = "/login" not in page.url
        browser.close()
        return valid


def ensure_session(interactive: bool = True):
    if _session_is_valid():
        return

    if not interactive:
        raise SessionExpiredError("Gradescope session expired or missing")

    print("No valid Gradescope session found (missing or expired).")
    print("A browser window will open -- please log in, then return")
    print("here and press Enter once you're on your dashboard.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{BASE_URL}/login")
        input("Press Enter once logged in... ")
        context.storage_state(path=str(SESSION_FILE))
        browser.close()

    print(f"Session saved to {SESSION_FILE}")


def get_context(playwright):
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(storage_state=str(SESSION_FILE))
    return browser, context

def get_enrolled_courses(context) -> list[dict]:
    """
    Returns [{ "course_id": str, "name": str }] for every course
    linked from your Gradescope dashboard.
    """
    page = context.new_page()
    page.goto(f"{BASE_URL}/")
    page.wait_for_load_state("networkidle")

    links = page.query_selector_all('a[href*="/courses/"]')
    seen = {}
    for link in links:
        href = link.get_attribute("href") or ""
        match = re.search(r"/courses/(\d+)", href)
        if not match:
            continue
        course_id = match.group(1)
        text = link.inner_text().strip()
        if course_id not in seen and text:
            seen[course_id] = text

    page.close()
    return [{"course_id": cid, "name": name} for cid, name in seen.items()]

def get_course_assignments(context, course_id: str) -> list[dict]:
    """
    Returns [{ "title": str, "due_date_raw": str }] for a Gradescope
    course, reading the real "Due (EDT)" column confirmed via the
    extension -- and pulling out just the actual date line from the
    cell (which also contains a relative countdown line stacked above it).
    """
    page = context.new_page()
    page.goto(f"{BASE_URL}/courses/{course_id}")
    page.wait_for_load_state("networkidle")

    results = []
    tables = page.query_selector_all("table")

    for table in tables:
        rows = table.query_selector_all("tr")
        if not rows:
            continue

        header_cells = rows[0].query_selector_all("th, td")
        headers = [c.inner_text().strip().lower() for c in header_cells]
        due_idx = next((i for i, h in enumerate(headers) if "due" in h), None)
        if due_idx is None:
            continue  # not the assignments table

        for row in rows[1:]:
            cells = row.query_selector_all("th, td")
            if len(cells) <= due_idx:
                continue
            title = cells[0].inner_text().strip()
            due_cell_text = cells[due_idx].inner_text().strip()
            if not title or not due_cell_text:
                continue
            # Gradescope's own LTI-integration setup row ("Gradescope LTI
            # 1.3") shows up in this table like a real assignment but
            # isn't one -- confirmed real data (ECE 301). Real assignment
            # titles are never literally "Gradescope ...".
            if title.lower().startswith("gradescope"):
                continue

            # The "Released\nDue (EDT)" header stacks two dates per cell:
            # line 1 is Released, line 2 is the actual Due date -- always
            # take the last line, since a naive "first month match" grabs
            # Released instead (both lines match a month pattern).
            #
            # BUT: assignments with a late-submission policy add a THIRD
            # line, "Late Due Date: ...", after the real due date --
            # confirmed via real data (ECE 362's Lab 0/HW 0) where blindly
            # taking the last line grabbed the late date instead of the
            # actual due date. Filter that line out before taking "last".
            due_lines = [l.strip() for l in due_cell_text.split("\n") if l.strip()]
            due_lines = [l for l in due_lines if not re.match(r"(?i)^late due date\b", l)]
            date_line = due_lines[-1] if due_lines else None
            if date_line:
                results.append({"title": title, "due_date_raw": date_line})

    page.close()
    return results


def get_course_name(context, course_id: str) -> str:
    page = context.new_page()
    page.goto(f"{BASE_URL}/courses/{course_id}")
    page.wait_for_load_state("networkidle")
    heading = page.query_selector("h1")
    name = heading.inner_text().strip() if heading else str(course_id)
    page.close()
    return name


if __name__ == "__main__":
    ensure_session()

    COURSE_ID = "1262127"  # ME 270's Gradescope course ID, confirmed working

    with sync_playwright() as p:
        browser, context = get_context(p)

        name = get_course_name(context, COURSE_ID)
        assignments = get_course_assignments(context, COURSE_ID)

        print(f"\n{name}: {len(assignments)} assignment(s) found")
        for a in assignments:
            print(f"  - {a['title']}: {a['due_date_raw']}")

        browser.close()