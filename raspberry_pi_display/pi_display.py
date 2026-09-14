"""
pi_display.py

Ambient deadline display for a Raspberry Pi 3 + 0.96" SSD1306 OLED
(I2C, 128x64). Polls Todoist's own REST API directly over WiFi (not
this project's Mac/Python backend) and shows the single next-due task
with a live countdown -- same concept as esp32_display/, just running
as a real Python process on Linux instead of Arduino firmware.

Setup: see raspberry_pi_display/SETUP.md

"Next due" is computed across ALL active Todoist tasks with a due
date, not filtered to course-labeled ones -- see prompt.md's item #7
notes if you want to add a label filter later.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from pi_config import TODOIST_API_TOKEN

API_URL = "https://api.todoist.com/api/v1/tasks"
FETCH_INTERVAL_SECONDS = 10 * 60
RENDER_INTERVAL_SECONDS = 30

# Fallback timezone for naive due.date strings (no "Z", no offset --
# confirmed via live API testing that this happens for any due_string
# task with a specific time, e.g. "tomorrow at 5pm", even though the
# due object's own "timezone" field comes back null in that case).
# Prefer the due object's own "timezone" field when present; this is
# only the fallback when it's absent. This account's real observed
# timezone -- update if you ever change your Todoist account timezone.
ACCOUNT_TIMEZONE = ZoneInfo("America/Los_Angeles")

LOG_FILE = Path(__file__).parent / "pi_display.log"
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
)


# ---------------------------------------------------------------------------
# Pure logic -- no hardware/network I/O, so this can be (and is) unit
# tested directly in plain Python, unlike the ESP32 firmware.
# ---------------------------------------------------------------------------

def parse_due(due: dict) -> datetime:
    """Todoist's due.date is either a bare 'YYYY-MM-DD', a full ISO
    datetime with a 'Z' suffix (tasks created with an explicit
    due_datetime, e.g. by todoist_sync.py), OR a NAIVE datetime with no
    'Z'/offset at all (tasks created via due_string with a specific
    time, e.g. quick-adding "tomorrow at 5pm" in the app) -- confirmed
    against the real live API, all three shapes. A naive string means
    account-local time, not UTC: localize using the due object's own
    "timezone" field when present, else ACCOUNT_TIMEZONE as a fallback
    (confirmed empirically that due_string-created naive due dates
    come back with timezone: null despite representing local time)."""
    raw = due["date"]
    text = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        tz_name = due.get("timezone")
        tz = ZoneInfo(tz_name) if tz_name else ACCOUNT_TIMEZONE
        dt = dt.replace(tzinfo=tz)
    return dt


def pick_next_due(tasks: list[dict]) -> dict | None:
    """Returns the task (raw Todoist dict) with the earliest due date
    among tasks that have one, or None if none do."""
    best = None
    best_due = None
    for task in tasks:
        due = task.get("due")
        if not due or not due.get("date"):
            continue
        try:
            due_dt = parse_due(due)
        except ValueError:
            continue
        if best_due is None or due_dt < best_due:
            best = task
            best_due = due_dt
    return best


def format_countdown(remaining: timedelta) -> str:
    total_seconds = int(remaining.total_seconds())
    if total_seconds < 0:
        return "OVERDUE"

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# I/O -- network fetch and hardware render, kept separate from the pure
# logic above so the logic could be verified without real hardware.
# ---------------------------------------------------------------------------

def fetch_tasks() -> list[dict]:
    response = requests.get(
        API_URL,
        headers={"Authorization": f"Bearer {TODOIST_API_TOKEN}"},
        params={"limit": 200},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def build_display():
    # Imported here (not at module level) so the pure logic above can be
    # unit tested on a machine without the Pi-specific I2C libraries
    # installed at all (e.g. during development on the Mac).
    from luma.core.interface.serial import i2c
    from luma.oled.device import ssd1306

    serial = i2c(port=1, address=0x3C)
    return ssd1306(serial, width=128, height=64)


def render(display, title: str | None, countdown: str | None):
    from PIL import ImageFont
    from luma.core.render import canvas

    font_small = ImageFont.load_default()
    with canvas(display) as draw:
        draw.text((0, 0), "NEXT DUE", font=font_small, fill="white")
        draw.line((0, 12, 128, 12), fill="white")

        if title is None:
            draw.text((0, 24), "Nothing dated", font=font_small, fill="white")
            draw.text((0, 36), "upcoming.", font=font_small, fill="white")
        else:
            draw.text((0, 20), title[:21], font=font_small, fill="white")
            draw.text((0, 40), countdown, font=font_small, fill="white")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    display = build_display()
    cached_task = None
    cached_due = None
    last_fetch = 0.0

    while True:
        now = time.monotonic()

        if now - last_fetch >= FETCH_INTERVAL_SECONDS or last_fetch == 0.0:
            try:
                tasks = fetch_tasks()
                cached_task = pick_next_due(tasks)
                cached_due = parse_due(cached_task["due"]) if cached_task else None
                logging.info(
                    f"Next due: {cached_task['content'] if cached_task else '(none)'}"
                )
            except Exception as exc:
                logging.error(f"Fetch failed: {exc}")
            last_fetch = now

        if cached_task is None:
            render(display, None, None)
        else:
            remaining = cached_due - datetime.now(timezone.utc)
            render(display, cached_task["content"], format_countdown(remaining))

        time.sleep(RENDER_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
