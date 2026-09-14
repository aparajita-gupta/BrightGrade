"""
fetch_ics.py

Fetches and parses your Brightspace calendar subscription (.ics) feed.

Setup:
    pip install icalendar requests
"""

import requests
from icalendar import Calendar
from datetime import datetime

from config import BRIGHTSPACE_ICS_URL as ICS_URL


def fetch_events(ics_url: str) -> list[dict]:
    response = requests.get(ics_url, timeout=15)
    response.raise_for_status()

    cal = Calendar.from_ical(response.text)
    events = []

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        summary = str(component.get("summary", "")).strip()
        dtstart = component.get("dtstart")
        dtstart_val = dtstart.dt if dtstart else None
        description = str(component.get("description", "")).strip()
        location = str(component.get("location", "")).strip()

        events.append(
            {
                "title": summary,
                "start": dtstart_val,
                "description": description,
                "location": location,
            }
        )

    def sort_key(e):
        start = e["start"]
        if isinstance(start, datetime):
            return start.replace(tzinfo=None)
        return datetime.min

    events.sort(key=sort_key)
    return events


def main():
    if ICS_URL == "PASTE_YOUR_ICS_URL_HERE":
        print("Edit config.py and paste your real subscription URL first.")
        return

    events = fetch_events(ICS_URL)
    print(f"Found {len(events)} events.\n")

    for e in events:
        start_str = e["start"].strftime("%Y-%m-%d %H:%M") if e["start"] else "no date"
        print(f"[{start_str}] {e['title']}")
        if e["description"]:
            print(f"    {e['description'][:150]}")
        print()


if __name__ == "__main__":
    main()