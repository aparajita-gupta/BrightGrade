# Project Context & Continuation Prompt

Paste this entire file as your first message in a new Claude
conversation to resume work on this project with full context.

---

## What this project is

A personal automation system that aggregates academic deadlines from
Brightspace (Purdue's LMS) and Gradescope, cross-references them to
fill in missing due dates, and pushes them to Todoist. Built because
Brightspace's own calendar/reminder system is unreliable — some
courses never get assignment due dates pushed to the calendar at all,
even though those assignments exist (verified) in the Gradebook with
real deadlines posted separately on Gradescope.

Two parallel implementations were built and tested against real data,
deliberately, to compare architectures before committing to one.

---

## Background research already done (don't re-litigate this)

- **Brightspace's official Valence REST API** requires an
  admin-registered OAuth client from Purdue IT. Individual students
  cannot self-register hobby apps against it — ruled out.
- **Brightspace's ICS calendar subscription feed** is the best data
  source: a private, token-bearing URL any student can generate
  themselves from Brightspace's Calendar settings, zero admin
  approval needed, zero login/session handling (plain HTTP GET).
  Confirmed working: returns full history back to 2020, includes all
  enrolled courses automatically.
- **Gradescope has no official API at all.** Only viable approach is
  browser automation with a logged-in session (no institutional
  gatekeeping issue here, just requires handling your own login).
- **Key structural finding**: the ICS feed only contains items with
  an actual due date. Assignments that exist in a course's Gradebook
  but were never given a calendar date are invisible to the ICS feed
  entirely — this motivated the whole cross-referencing system below.
- **Purdue-specific quirk**: Brightspace's internal course codes pad
  the real course number with two trailing zeros (e.g. "ME 27000" =
  real course "ME 270"). Any course-code matching logic MUST strip
  this padding or matching silently fails.

---

## Implementation A: Python backend (the one currently being built out)

Location: local folder, run via `python pipeline.py`. Files:

- **`config.py`** — `BRIGHTSPACE_ICS_URL`, `TODOIST_API_TOKEN` (and
  will need an AI provider key once that's added — see "Open decision"
  below)
- **`matching.py`** — `normalize_course_name()` (handles the zero-
  padding quirk above), `normalize_title()`, `title_similarity()`
  (token-overlap fuzzy matching), `best_match()`
- **`fetch_ics.py`** — fetches/parses the ICS feed via `icalendar` +
  `requests`
- **`brightspace_reader.py`** — Playwright-based, persisted login
  session that self-checks validity every run (re-prompts for manual
  login only when actually expired, not blindly every time):
  - `get_gradebook_items`, `get_assignments_tool_items`,
    `get_quizzes_tool_items` — three separate Brightspace pages per
    course; Assignments/Quizzes tool pages CAN have real due dates
    that never reached the calendar, Gradebook usually has none
  - `get_undated_items` — merges all three, returns only items still
    missing a date after checking everything
  - `get_enrolled_courses` — auto-discovers every enrolled course
    from the Brightspace homepage; works for any number of courses,
    nothing hardcoded, so new semesters "just work" without code changes
  - Known quirk already handled: Brightspace renders some pages
    (Calendar widget, Assignments/Quizzes tool list views) via web
    components, meaning plain `<table><tr>` queries can return an
    empty shell even when real content exists elsewhere in the DOM.
    A shadow-DOM-piercing text walker was used during debugging to
    find real structure; current readers use real `<table>` markup
    where it exists and are known-correct against real tested data.
- **`gradescope_reader.py`** — same session-persistence pattern;
  `get_course_assignments` reads the real "Due (EDT)" table column
  (had to specifically target this header text, since the cell
  contains a relative countdown line stacked above the actual date
  line — naive text extraction concatenated both into garbage);
  `get_enrolled_courses` auto-discovers all Gradescope courses
- **`pipeline.py`** — orchestrates: fetch ICS → auto-discover +
  auto-pair Brightspace/Gradescope courses by normalized course code
  → run undated-item cross-check per course → save to
  `assignments.json` → log to `pipeline.log` → macOS desktop
  notification via `osascript` summarizing what changed
- **`todoist_sync.py`** — **tested and working against the real
  Todoist account.** Pulls existing Todoist labels/recent task titles
  to infer a naming template, asks once whether to use it for all
  future assignments (saved to `todoist_format_preference.json` so
  never asked again), pushes only new dated assignments (tracked in
  `todoist_synced.json` to prevent duplicates on re-runs). Per-course
  labels auto-created to match the account's real naming convention.
  `--ai-titles` flag switches title generation to Gemini (see
  `ai_titles.py`) instead of the template, opt-in for now.
- **`ai_titles.py`** — generates Todoist titles via Gemini by
  mimicking the account's real completed-task history, one batched
  call per course (not one call per assignment, so the whole batch
  commits to one consistent style). Also proposes new per-course
  label names matching the account's existing label convention when
  one doesn't exist yet. See item #4 below for what this took to get
  right.

**Verified working via real output inspection**: a full run pulled 61
historical ICS items and resolved 33+ previously-undated ME 270
assignments via the Gradescope cross-check — confirmed by inspecting
`assignments.json` directly, where resolved items are tagged
`"source": "brightspace_gradebook+gradescope"`. Two genuinely
unresolved items (Final Exam, Course Eval — no Gradescope match
exists) correctly stayed `due_date: null` rather than being guessed.

**Current limitation**: only runs when manually invoked. No
scheduling set up yet.

## Implementation B: Chrome extension (built in parallel, for comparison)

Manifest V3, content scripts auto-trigger on every matching page load
— no button click needed. Calls Brightspace's `whoami` endpoint
inheriting the live browser session (no separate login at all). Same
shadow-DOM-piercing + course-matching logic, independently ported to
JavaScript — landed on identical results to the Python version when
tested against the same real data, which cross-validated the matching
approach itself. Stores in `chrome.storage.local`, fires real Chrome
notifications (deduplicated so repeat visits don't spam).

**Currently behind the Python backend on**: Assignments/Quizzes tool
scraping (only reads Gradebook), multi-course auto-discovery (only
reads whichever single page you're on). No decision has been made yet
on whether to bring the extension to parity, retire it, or use both
for different purposes.

---

## What's NOT built yet (in rough priority order)

1. **Todoist integration** — `todoist_sync.py` is tested and working
   against the real Todoist account (auth, task creation, dry-run,
   init/backfill flow, labels). Along the way, fixed real bugs
   surfaced by real data:
   - `gradescope_reader.py` was reading the "Released" date instead
     of the actual "Due" date -- the due-date cell is two stacked
     lines (Released, then Due (EDT)); the old code grabbed the first
     line that matched a month pattern (always Released) instead of
     the last line.
   - `matching.py`'s `normalize_course_name()` used a case-insensitive
     regex, so on strings like "Summer 2026 ME 27000..." it matched
     "mmer"+"2026" out of "Summer" before ever reaching the real
     course code. Fixed to uppercase-only letters (real course
     prefixes are always fully capitalized; surrounding sentence text
     isn't).
   - `todoist_sync.py`'s date parsing now resolves Gradescope's
     year-less dates to the *current* calendar year (not "next future
     occurrence") and correctly converts from Eastern (Gradescope's
     actual timezone) to UTC instead of handing ambiguous text to
     Todoist's own NLP date parser.
   - Tasks get a per-course Todoist label auto-created to match
     whatever naming convention already exists in the account (see
     #4 -- this detection is AI-driven, not hardcoded).
   - **Follow-up real-data bugs found and fixed in a later session**
     (all confirmed against the live account, not just code review):
     - `fetch_ics.py`/`pipeline.py`'s `run_ics_step` were discarding
       the ICS event's `LOCATION` field entirely, so calendar-feed
       (`ics::`) items had no way to resolve a course -- they all fell
       back to a literal `"School"` label/title placeholder. Fixed by
       parsing `LOCATION` (e.g. `"Fall 2026 ECE 33700-010 LEC -
       Merge"`) through `normalize_course_name()`; non-course
       locations (advising, guest lectures) correctly yield no label.
       Existing store entries backfilled; 39 already-pushed live
       Todoist tasks deleted and re-pushed with correct labels.
     - `normalize_course_name()` requires uppercase course letters (by
       design, to avoid false-matching "Summer" in a sentence -- see
       above) but real existing Todoist labels are already lowercase
       (`"ece 362"`), so it never recognized them as matches and let
       the AI mint duplicate no-space labels (`"ECE362"`) instead of
       reusing the real one. Fixed via a separate
       `normalize_label_name()` in `todoist_sync.py` that's safely
       case-insensitive (real label names are short and clean, no
       "Summer"-style ambiguity risk).
     - Course number placement in AI-generated titles was
       inconsistent/trailing (e.g. `"hw 1 362"`); now the AI prompt
       explicitly requires it at the very start (`"362 hw 1"`), and
       all previously-pushed live tasks were bulk-renamed to match.
     - **Recurring tasks**: `todoist_sync.py` now detects groups of
       ≥3 same-course assignments whose titles differ only by a
       trailing instance number and whose due dates are evenly spaced
       (all gaps within 1 day of each other, 1-21 day interval), and
       collapses them into ONE Todoist recurring task (via
       `due_string`, e.g. `"every week starting Sep 1 2026 at
       6:30pm"`) instead of N one-off tasks -- e.g. "Weekly Feedback
       1"-"8" becomes a single recurring "Weekly Feedback". The AI
       prompt is told which items are recurring so it omits the
       instance number. Real gotcha: `due_string` has no way to carry
       an explicit UTC offset, so its time is parsed in the Todoist
       *account's* timezone (Pacific), not the source data's Eastern
       time -- `ACCOUNT_TIMEZONE` explicit conversion avoids a silent
       3-hour shift. Verified with synthetic data (no real recurring
       groups exist yet at time of writing).
     - **Gradescope "Late Due Date" bug**: `gradescope_reader.py`'s
       "always take the last line of the due-date cell" fix (see
       above) broke again for assignments with a late-submission
       policy, which stack a THIRD line ("Late Due Date: ...") after
       the real due date -- the code was grabbing that instead. Fixed
       by filtering out any line matching `^late due date\b` before
       taking "last". Confirmed via real data (`ECE 362` Lab 0/HW 0).
     - **Brightspace gradebook scraping was returning grade-category
       and summary rows as if they were assignments** (`"Labs"`,
       `"Homework"`, `"Final Grade"`, `"Total"`, even a raw points
       fraction like `"0 / 3,933"`). Real DOM inspection (via a
       one-off `debug_gradebook_dom.py` diagnostic) showed category
       and real-item rows share identical CSS classes at the cell
       level -- no clean universal signal. Fixed with two real signals
       that do work: a category row with expandable children gets row
       class `"d_ggl1"`; bonus/penalty roll-up rows always contain
       "bonus"/"penalt" in the label. A few bare category words
       (`"Midterm"`, `"Final"`, `"Component"`) can't be safely
       filtered without risking a real course's actual item names --
       harmless residual, since they never match a real Gradescope
       title and so never get a date or reach Todoist.
     - Only 2 of your current-semester courses (`ece362`, `ece301`)
       actually use Gradescope at all -- the other 5 auto-paired-zero
       result was correct behavior, not a bug; confirmed via the
       diagnostic course-name logging added to `pipeline.log` (see #2).
     - **Architectural gap fixed: independent Gradescope discovery.**
       `run_cross_check_step`'s per-course loop only ever checked
       Gradescope for items Brightspace's gradebook ALREADY listed as
       undated -- if Brightspace hadn't added the gradebook column for
       something yet (professors often add these later than the
       Gradescope posting itself), the pipeline had no way to even
       know it existed. Confirmed via a real screenshot: `ECE 362`'s
       `Lab 1`-`Lab 10` were all live and due-dated on Gradescope but
       completely absent from `assignments.json` under any source.
       Fixed by adding a second pass per course that scans Gradescope's
       own assignment list directly, skips anything already known for
       that course from ANY source (cross-check keys
       `"{course_key}::..."`, ICS-sourced entries via their `course`
       field), and adds whatever's left straight from Gradescope with
       Gradescope's own due date -- no Brightspace listing required.
       Runs automatically every time (manual or the 8 PM scheduled
       job), covering both Brightspace-listed and Gradescope-only
       items going forward, in either order either side posts first.
       Verified via `best_match` dry-run against the real captured
       title list before applying live -- `Lab 0`/`HW 0` correctly
       matched as already-known (no duplicate), `Lab 1`-`Lab 10`
       correctly identified as new.
     - **Follow-up: the independent-discovery pass also refreshes an
       already-known-but-still-undated item**, not just brand new
       ones. Initially it only ever created new entries, so an item
       matched-but-undated (e.g. found via Brightspace with no
       Gradescope match yet at the time) would never get its date
       filled in on a later run even after Gradescope posted one --
       real gap, caught by asking "will Lab 2-10 auto-update once
       Gradescope's dates aren't stale anymore?" and realizing the
       honest answer was no. Fixed to fill in `due_date` when the
       matched existing entry has none yet, same "never overwrite an
       ALREADY-set date, even a stale one" policy as every other merge
       path in this codebase -- so a genuinely stale date (confirmed
       real case: `ECE 362` Labs 2-10 showing due dates back in
       Jan-Apr, before the semester even started -- almost certainly
       leftover from a reused/duplicated prior-semester Gradescope
       course template) needs a manual `due_date: null` reset once to
       start being tracked as pending again; from then on a real
       correction flows through automatically.
     - **Follow-up: filtered out Gradescope's own LTI-integration setup
       row** (`"Gradescope LTI 1.3"`), which the independent-discovery
       pass swept up like a real assignment and pushed to Todoist
       (confirmed real case, `ECE 301`) -- real assignment titles are
       never literally `"Gradescope ..."`, so `get_course_assignments`
       now skips any row whose title starts with that.
2. **Scheduled automation** — implemented and verified end-to-end.
   `com.aparajita.assignment-pipeline` launchd agent (installed in
   `~/Library/LaunchAgents/`) runs `run_pipeline.sh` daily at 8:00 PM
   (plus once at every login/reboot via `RunAtLoad`, as a catch-up for
   a missed 8 PM while asleep). That script runs `pipeline.py
   --unattended` then `todoist_sync.py --ai-titles` -- full hands-off
   automation, not just a data refresh.
   - `--unattended` (new flag on `pipeline.py`): skips the interactive
     Duo login prompt. `brightspace_reader.py` / `gradescope_reader.py`
     now take `ensure_session(interactive=...)` and raise
     `SessionExpiredError` instead of blocking on `input()` when
     `interactive=False`. Verified against a genuinely expired
     Brightspace session: logs it, fires a desktop notification asking
     for a manual re-login, still saves the ICS step's progress, exits
     0 -- doesn't hang, doesn't crash.
   - `run_pipeline.sh` deliberately chains the two scripts with `;`,
     not `&&` -- `todoist_sync.py` should still attempt to push
     whatever's already unsynced even if today's scrape degraded.
   - Real gotcha: the launchd job initially failed with `/bin/bash:
     ...: Operation not permitted` -- macOS's TCC privacy protection
     on `~/Documents` blocks a raw launchd-spawned process even though
     the identical command works fine from an interactive Terminal.
     Fixed by manually granting Full Disk Access to `/bin/bash` in
     System Settings -> Privacy & Security. If this job ever mysteriously
     stops working after a macOS update, check that grant first.
   - Sessions persist across runs via the saved cookie files
     (`brightspace_session.json` / `gradescope_session.json`) --
     this only ever needs a fresh manual login when Brightspace's
     server-side session actually times out (observed lasting several
     weeks), not on every run.
   - **Follow-up fix**: `run_cross_check_step` originally checked
     Brightspace's session then Gradescope's, stopping at the first
     expired one -- so if Brightspace expired first (the real observed
     case, for 12+ days straight), Gradescope's session was never even
     checked, and you'd never find out it also needed a re-login until
     Brightspace alone got fixed. Now both are checked independently,
     each firing its own notification (`"Brightspace login needed"` /
     `"Gradescope login needed"`) naming which service specifically.
     Session-expiry notifications also now play a sound (`Basso`) so
     they're harder to miss in a pile of silent routine banners.
   - Added per-course diagnostic logging to `pipeline.log`
     (`run_cross_check_step` now logs every discovered
     Brightspace/Gradescope course name plus its normalized key, and
     every Gradescope assignment row scraped per course) -- a run
     where auto-pairing looks too low, or a cross-check finds nothing,
     is now self-diagnosing from the log alone instead of needing a
     live debugging session.
3. **"Nudge me" reminder for permanently undated items** — implemented
   and unit-tested. `pipeline.py`'s new `stamp_and_check_undated()`,
   called once per run (fits naturally into the existing 8 PM
   scheduled job from #2, no new infrastructure needed):
   - First nudge after **14 days** undated, re-nudge every **10 days**
     after that if still unresolved (not every single day).
   - Delivered as a desktop notification (`notify()`), same pattern as
     the existing "new assignments found" one, kept separate/distinct
     from it.
   - Implemented as a single pass over the whole store rather than
     threading stamping logic into every place an undated item can get
     created (`pipeline.py`'s cross-check step, `syllabus_parser.py`'s
     merge) -- this one pass both backfills `first_seen_undated` onto
     any undated item that doesn't have it yet (handles pre-existing
     undated items and items created by a standalone
     `syllabus_parser.py` run alike) and clears
     `first_seen_undated`/`last_nudged_at` once an item resolves, with
     zero changes needed to `syllabus_parser.py`.
   - `tbd: true` items (from #5's syllabus parsing) get the same
     treatment as any other undated item, with the notification text
     calling out "(syllabus says TBD)" specifically for those.
   - Verified against a synthetic store covering every case (brand
     new, under threshold, past threshold, cooldown respected,
     re-nudge after cooldown, resolved-and-cleared, TBD), then applied
     for real to backfill `first_seen_undated` onto the 2 real
     currently-undated items (`me270::course eval`,
     `ece20002::final exam`).
   - **Follow-up: extended to missing locations too**, not just
     missing dates (needed for #5's Calendar-event delivery, since a
     Calendar event without a location is much less useful than one
     without a precise time). `syllabus_parser.py` flags exam-like
     items (exam/midterm/final -- see `matching.is_exam_like()`,
     shared between both files) with `location_expected: true` when
     the syllabus gave no location; `stamp_and_check_undated()`
     (renamed conceptually, function name unchanged) now checks BOTH
     missing-date and missing-location on the SAME per-item timer, so
     an item missing either or both gets nudged together in one
     notification rather than on separate schedules. Regular
     assignments never carry `location_expected`, so they're never
     nudged for location. Verified with a synthetic store covering all
     four combinations (date only, location only, both, neither).
4. **AI-based smarter Todoist titles** — implemented and tested
   (Gemini, via `ai_titles.py`). Opt-in behind `--ai-titles` on
   `todoist_sync.py`; the plain template stays the default. I intend
   to switch this to always-on once I'm satisfied with output quality
   over more real runs -- not done yet, ask before flipping the
   default.
   - Real style source turned out to be **completed tasks**, not
     active ones -- this account had zero active tasks (only ever
     what this pipeline itself pushed), so the real naming history
     lives in `/tasks/completed` (94 items going back to March 2026).
     Active tasks alone are not enough signal to imitate a style from.
   - Titles for a whole course are generated in **one batched Gemini
     call**, not one call per assignment -- independent per-item
     calls have no shared state, so nothing forces them to agree on a
     format (confirmed: same prompt, same examples, wildly
     inconsistent per-call output). One call has to commit to a
     single consistent style across the batch.
   - My real established style is lowercase and terse (e.g. "270 hw
     02", "pcq", "266 pearson hw"), not the "ME 270 - HW 08" template
     format the pipeline had been assuming.
   - Course must be referred to by number only, no subject letters
     (e.g. "270", not "ME 270" or "ME"), and titles must never repeat
     the due date/time (Todoist already tracks that).
   - Real gotcha: Todoist auto-parses any `"@word"` inside a task's
     `content` text into an attached label, completely independent of
     whatever is explicitly passed via the API's `labels` field. My
     own historical tasks use `"@ece 270"`-style tags in the content
     text for exactly this reason -- it's really just their normal
     labeling mechanism. The AI-generated titles must NOT include an
     "@" anywhere, since labeling is already handled correctly and
     separately via the API's `labels` field -- letting both happen
     at once silently creates a second, differently-named, redundant
     label.
   - Per-course Todoist label name is also AI-detected, not
     hardcoded: given the account's real existing labels (e.g.
     "ece 270", "ma 266" -- lowercase, space-separated), the AI
     proposes a matching name for a course that doesn't have a label
     yet (e.g. "me 270"), which then gets created via `ensure_label`.
   - No `description` field on pushed tasks.
5. **Syllabus PDF parsing** — implemented and tested against a real
   syllabus (ECE 20002: 10 items added, 1 correctly flagged TBD rather
   than guessed). `syllabus_parser.py`:
   - Built standalone rather than wired into #3 -- #3 doesn't exist
     yet, so TBD-flagged items (`"tbd": true`, `due_date: null`) just
     sit in `assignments.json` until that gets built later.
   - Opens a native macOS file-picker (`osascript`/AppleScript
     `choose file`) instead of taking a CLI path argument -- asked for
     explicitly, since typing a path wasn't the interaction wanted.
   - Hands the PDF directly to Gemini as bytes
     (`types.Part.from_bytes(..., mime_type="application/pdf")`)
     rather than pre-extracting text -- avoids mangling a syllabus's
     date table, which plain text extraction tends to do.
   - Existing assignments with an already-resolved due date (e.g. from
     Gradescope) are never overwritten -- syllabus dates only fill in
     genuinely missing ones. A syllabus is a lower-confidence, static
     source next to Gradescope's live per-student due dates.
   - Real gotcha: Gemini's structured-output schema is NOT standard
     JSON Schema -- `"type": ["string", "null"]` fails validation.
     Needs OpenAPI-style `"type": "string", "nullable": true` instead.
   - Real gotcha: the `google-genai` SDK leaks its own logging into
     `pipeline.log` via the shared root logger -- an INFO-level line
     per HTTP request, plus a one-time WARNING-level "use
     Chat.send_message instead" notice. Fixed by setting
     `logging.getLogger("httpx")` to WARNING and
     `logging.getLogger("google_genai.models")` specifically (not just
     the `"google_genai"` parent) to ERROR, since that notice is
     logged at WARNING, above a plain INFO-suppression threshold. This
     already affected `ai_titles.py`'s `--ai-titles` runs too --
     fixed there in the same pass.
   - Interactive confirmation before writing (prints what it found,
     asks Y/n) -- same pattern as `todoist_sync.py`'s title-template
     prompt, since this is real data merging into `assignments.json`.
   - **Follow-up: exams can now go to Calendar.app instead of Todoist,
     as a per-run user choice.** Confirmed via real historical data
     that exams have always been tracked as actual Calendar events
     (real start/end time, ~1hr for a midterm / ~2hr for a final,
     short title, real location), not Todoist tasks -- distinct from
     Purdue's own separately auto-imported all-day exam-schedule
     calendar entries, which this deliberately does not try to match
     the style of. When a syllabus has exam-like items, it now asks
     `[C]alendar events or [T]odoist tasks?` (default Calendar) --
     "some people may want to just add it as a task" was the explicit
     reason this stayed a choice rather than a forced default.
     Calendar events are created via AppleScript/`osascript`
     (`create_calendar_event`), matched to the right per-course
     Calendar.app calendar by name (`find_matching_calendar`, reusing
     `normalize_label_name` from `todoist_sync.py` -- you already have
     one real Calendar.app calendar per course). Items delivered this
     way get `"delivery": "calendar"` on their store entry, which
     `todoist_sync.py`'s `iter_push_candidates` explicitly skips, so
     they never also get pushed as a duplicate Todoist task.
   - Location is appended with campus context
     (`"<location>, Purdue University, West Lafayette, IN"`) before
     being set on a Calendar event, since a bare room code like `"ELLT
     116"` won't resolve to a map pin in Calendar.app's event view on
     its own -- confirmed live that this gets a real pin to show,
     matching your own historical map-pinned events.
   - **Multi-section syllabus support**: if a syllabus gives different
     dates/times/locations depending on lecture/lab/recitation section
     (e.g. a per-section exam-time table), each item now carries a
     `section` field from Gemini, and `select_section()` prompts once
     with a numbered list of detected sections before anything else
     happens, keeping only that section's items (plus any item with no
     section distinction, which applies to everyone). Verified against
     a real 3-section syllabus (ECE 301).
   - Real gotcha: Gemini sometimes returns a time RANGE (e.g.
     `"18:30-19:30"`) despite the schema asking for a single start
     time only, which crashed `datetime.strptime` (real syllabus, ECE
     301). Fixed with `parse_start_time()`, used defensively in both
     `resolve_due_date` and `create_calendar_event`, that takes just
     the leading `HH:MM` and falls back to date-only (logged, not
     crashed) if genuinely unparseable.
6. **Feature parity between the two implementations**, or a decision
   not to bother.
7. **ESP32 hardware display** — purely conceptual, no work started.
   Ambient physical display of upcoming deadlines.
8. **Mobile notifications** — currently desktop-only on both
   implementations; would come "for free" once Todoist integration
   (#1) works, via Todoist's own mobile app notifications.

---

## AI provider: Gemini (chosen, used for #4)

`ai_titles.py` uses the Gemini API (`google-genai` SDK,
`from google import genai`, model `gemini-flash-lite-latest`).
`GEMINI_API_KEY` lives in `config.py`. Still open for #5
(syllabus PDF parsing) when that gets picked up -- default to Gemini
too unless there's a reason not to.

Notes for future model-name lookups: the `gemini-2.5-flash` id that
seemed like the obvious default turned out to be deprecated for this
account ("no longer available to new users") even though it still
showed up in `client.models.list()` -- don't trust a model showing up
in the list as proof it's actually usable; a real `generate_content`
call is the only reliable check. `gemini-flash-lite-latest` worked.

Below is the original provider research, kept for context:

- **Anthropic (Claude) API**: pay-as-you-go, no subscription, sign up
  at console.anthropic.com, `pip install anthropic`. No Cloud/Firebase
  project setup needed at all — just an API key used directly in the
  local Python script.
- **Google Gemini API via AI Studio**: also pay-as-you-go with a free
  tier, sign up at aistudio.google.com, `pip install google-genai`.
  Also no Cloud/Firebase project setup needed — AI Studio silently
  creates a backing Google Cloud project for you, but you never have
  to touch the Cloud console yourself. Functionally just as simple to
  set up as the Anthropic option.
- **Ruled out for this project**: Firebase AI Logic (Google's
  client-side Gemini SDK with App Check security) — this exists and
  is genuinely well-suited for browser apps/extensions that need to
  call an LLM WITHOUT exposing an API key client-side, but it's
  irrelevant here since the AI features are planned to live in the
  local Python backend, not the browser extension. If the extension
  ever needs AI features later, revisit this specifically then.
- **Since everything AI-related is currently planned to run locally
  in the Python backend (not the extension), there is no security
  reason to prefer one provider over the other** — both are equally
  simple, equally secure (key stays local, never exposed), and
  equally free of Cloud/Firebase project setup. The choice is really
  just "which model's output quality/style do I prefer," or "I'm
  already comfortable in Google's ecosystem" as a tiebreaker.

Provider question is resolved for #4 (Gemini). Default to Gemini for
#5 too when it's picked up, unless there's a specific reason raised
at the time to reconsider.

---

## How I want you to work with me

- Don't assume file contents — ask me to paste the current version of
  a specific file before editing it, since you don't have live repo
  access in this chat (unlike Cursor, which I also use for this
  project and which reads files directly).
- Implement one numbered item at a time. Tell me clearly when each is
  done and how to test it, rather than trying to do everything at once.
- If something needs verification against current, real API behavior
  (Todoist's actual REST v2 quirks, current Anthropic/Gemini SDK
  syntax, etc.), search for current documentation rather than relying
  on possibly-outdated training knowledge.
- Ask before any architectural change I haven't explicitly requested
  (no database, no web UI, no refactor of working modules without a
  concrete reason).
- Keep using established patterns already in this codebase (session
  persistence + revalidation, JSON file storage, `logging` to
  `pipeline.log`) rather than introducing new infrastructure without
  a specific reason to.
- Give full file contents when providing new or edited code (not
  diffs/snippets alone), since I'll be copy-pasting these directly
  into my project files myself.
