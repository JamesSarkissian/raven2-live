#!/bin/bash
# Double-click to label drone frames (pothole and crack boxes) for training.
# Run extract_frames.py on new recordings first. Local only, nothing uploaded.
cd "$(dirname "$0")" || exit 1
(sleep 2; open "http://localhost:8768/") &
/usr/bin/python3 label_tool.py
