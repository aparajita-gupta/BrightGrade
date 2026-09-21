#!/bin/bash
# Double-click this in Finder to launch the dashboard. A Terminal window
# will briefly appear (unavoidable for a .command file without building a
# signed macOS app) then your browser opens to the dashboard automatically.
cd "$(dirname "$0")"
python3 webapp.py
