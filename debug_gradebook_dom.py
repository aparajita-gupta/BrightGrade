"""
debug_gradebook_dom.py

One-off diagnostic: dumps the raw row structure of Brightspace's "My
Grades" page for one course, so the real markup that distinguishes a
grade CATEGORY row ("Labs", "Homework", "Final Grade") from a real
gradable ITEM row (an actual assignment/quiz/exam) can be inspected --
needed to fix get_gradebook_items() in brightspace_reader.py properly
instead of guessing at text-based heuristics.

Run:
    python3 debug_gradebook_dom.py <org_unit_id>

Find the org_unit_id from pipeline.log's "Auto-discovered ... Brightspace
course(s)" diagnostic lines, or just run with no argument to use ECE 362's
(from that log).
"""

import sys

from playwright.sync_api import sync_playwright

from brightspace_reader import ensure_session, get_context, BASE_URL

ORG_UNIT_ID = sys.argv[1] if len(sys.argv) > 1 else "1632370"  # ece362, wl.202710.ECE.36200.001 -- from its calendar event URLs


def main():
    ensure_session()

    with sync_playwright() as p:
        browser, context = get_context(p)
        page = context.new_page()
        page.goto(f"{BASE_URL}/d2l/lms/grades/my_grades/main.d2l?ou={ORG_UNIT_ID}")
        page.wait_for_load_state("networkidle")

        rows = page.query_selector_all("table tr")
        print(f"{len(rows)} row(s) found\n")

        for i, row in enumerate(rows):
            cells = row.query_selector_all("th, td")
            if not cells:
                continue
            label = cells[0].inner_text().strip()
            row_class = row.get_attribute("class") or ""
            cell0_class = cells[0].get_attribute("class") or ""
            has_link = cells[0].query_selector("a") is not None
            is_th = cells[0].evaluate("el => el.tagName") == "TH"
            print(f"[{i}] label={label!r}")
            print(f"     row_class={row_class!r}")
            print(f"     cell0_class={cell0_class!r} is_th={is_th} has_link={has_link}")

        browser.close()


if __name__ == "__main__":
    main()
