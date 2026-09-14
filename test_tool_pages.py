"""
test_tool_pages.py

Standalone test for the untested Assignments tool and Quizzes tool
readers in brightspace_reader.py. Prints raw output so we can verify
they actually find real data, and tune selectors if not.

Usage:
    python test_tool_pages.py
"""

from playwright.sync_api import sync_playwright
from brightspace_reader import (
    ensure_session,
    get_context,
    get_assignments_tool_items,
    get_quizzes_tool_items,
)

# ORG_UNIT_ID = "1565975"  # ME 270
ORG_UNIT_ID = "412690" # civics 


def dump_raw_page(context, url: str, label: str):
    page = context.new_page()
    page.goto(url)
    page.wait_for_load_state("networkidle")

    print(f"\n=== RAW DUMP: {label} ===")
    print(f"URL: {url}")
    print(f"Page title: {page.title()}")

    lines = page.evaluate(
        """
        () => {
            function collectTextDeep(root, out) {
                const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                    const text = node.textContent.trim();
                    if (text) out.push(text);
                }
                const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of all) {
                    if (el.shadowRoot) collectTextDeep(el.shadowRoot, out);
                }
            }
            const lines = [];
            collectTextDeep(document.body, lines);
            return [...new Set(lines)];
        }
        """
    )

    # Filter out Brightspace's internal minified JS config, which
    # otherwise renders as huge junk text blobs.
    def looks_like_code(line):
        return (
            "D2L.OR[" in line
            or "D2L.O(" in line
            or line.count("{") > 1
            or "_type" in line
        )

    clean_lines = [l for l in lines if not looks_like_code(l)]

    print(f"Found {len(clean_lines)} clean text line(s) (filtered out {len(lines) - len(clean_lines)} junk/code lines).")
    for line in clean_lines:
        print(f"  {line}")

    page.close()


def main():
    ensure_session()

    with sync_playwright() as p:
        browser, context = get_context(p)

        assignments_url = f"https://purdue.brightspace.com/d2l/lms/dropbox/user/folders_list.d2l?ou={ORG_UNIT_ID}"
        quizzes_url = f"https://purdue.brightspace.com/d2l/lms/quizzing/user/quizzes_list.d2l?ou={ORG_UNIT_ID}"

        dump_raw_page(context, assignments_url, "Assignments tool")
        dump_raw_page(context, quizzes_url, "Quizzes tool")

        print("\n\n=== PARSED RESULTS (using current reader logic) ===")
        assignments = get_assignments_tool_items(context, ORG_UNIT_ID)
        print(f"\nAssignments tool parsed: {len(assignments)} item(s)")
        for a in assignments:
            print(f"  - {a['title']}  ->  {a['due_date']}")

        quizzes = get_quizzes_tool_items(context, ORG_UNIT_ID)
        print(f"\nQuizzes tool parsed: {len(quizzes)} item(s)")
        for q in quizzes:
            print(f"  - {q['title']}  ->  {q['due_date']}")

        browser.close()


if __name__ == "__main__":
    main()