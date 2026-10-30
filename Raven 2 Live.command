#!/bin/bash
# Double-click to start the Raven 2 live viewer. Join the drone's Wi-Fi first.
cd "$(dirname "$0")" || exit 1
(sleep 3; open "http://localhost:8765") &
/usr/bin/python3 live_viewer.py "$@"
