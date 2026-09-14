#!/bin/bash
# Run by launchd (see com.aparajita.assignment-pipeline.plist) once daily.
# Deliberately uses ";" not "&&" between the two steps: todoist_sync.py
# should still attempt to push whatever is currently unsynced in
# assignments.json even if today's pipeline.py run degraded (e.g. an
# expired Brightspace/Gradescope session) -- it doesn't depend on today's
# scrape having produced anything new.

cd /Users/aparajita/Documents/projects/python_backend

PYTHON=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3

"$PYTHON" pipeline.py --unattended
"$PYTHON" todoist_sync.py --ai-titles
