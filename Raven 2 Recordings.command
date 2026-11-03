#!/bin/bash
# Double-click to browse and play recorded flights. Never contacts the drone,
# so use it on your normal Wi-Fi (the satellite map needs internet).
cd "$(dirname "$0")" || exit 1
(sleep 2; open "http://localhost:8767/player") &
/usr/bin/python3 live_viewer.py --library --port 8767
