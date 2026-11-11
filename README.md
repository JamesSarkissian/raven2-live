# Raven 2 Live

A macOS app for the **Sky Rider Raven 2 (model DRWG530B)**, a consumer GPS camera drone.
It shows the drone's camera live in your browser with a flight-data overlay, records
flights with their GPS path, plays them back on a satellite map, and finds **potholes
and cracks** in the video with a road-damage detector we trained ourselves.

The drone normally only works with the Sky Rider phone app. This project talks to the
drone's camera and flight controller directly over its own Wi-Fi, using protocols worked
out by watching that traffic. **It only reads.** You always fly with the physical remote.

> Unofficial hobby/research project, not affiliated with Sky Rider. Use at your own risk
> and follow your local drone rules (in the US, the FAA's recreational flyer rules).

## What it does

- **Live view:** the camera at about 15–20 fps in the browser, with battery, altitude,
  distance, horizontal/vertical speed, heading, GPS position and satellites,
  roll/pitch/yaw and motor state on top.
- **Preflight checklist:** follows the drone's takeoff routine (calibration, GPS lock,
  battery) and ticks off what the drone reports.
- **Flight map:** the path, home point and heading, with satellite imagery when the Mac
  has internet.
- **Recording:** video, flight data and GPS path, started with REC or automatically when
  the motors unlock.
- **Recordings page:** playback with the drone moving along its path on a satellite or
  street map, plus KML/GeoJSON export for Google Earth.
- **Pothole and crack detection (DETECT):** runs the model on the live video and draws
  boxes. While recording, every detection is saved with its GPS position. Detections are
  shown again on playback, with an on/off toggle.
- **Screenshots (SHOT):** saves the current frame with where it was taken, plus a KML of
  all screenshots.
- **Data tools:** export frames from recordings, label them in a built-in labelling page,
  and retrain the detector on your own footage.
- **Link diagnostics:** frame rate and Wi-Fi signal strength live, and a report of where
  the video link weakens with distance.

## Safety: what it will never do

These limits are built into the code, not just settings:

- **Video:** connects to TCP port 7060 and sends only *start stream*, *heartbeat* and
  *stop stream*.
- **Flight data:** UDP port 50000. It sends only the phone app's own 18-byte "read
  telemetry" request. It never sends stick, arm, takeoff, calibration or settings commands.
- **Never touches:** the movement port (9060) or settings port (8060). It doesn't modify
  firmware, flight limits, geofencing, no-fly zones or sensors.
- **You confirm first:** every message it will send is printed, and you confirm it before
  the viewer starts.
- **Detection only reads:** it works on frames already received and sends nothing to the
  drone.

## Requirements

- A Mac (developed on macOS 26 with an Apple Silicon M4).
- **The viewer, recorder, labeller and data tools:** only the Python 3 that comes with
  macOS. Run `xcode-select --install` once if `python3` asks for the command line tools.
  - The Wi-Fi signal helper (`wifi_signal.swift`) compiles itself on first run with those
    same tools.
- **Detection and training:** Python 3.9 or newer with TensorFlow 2.20, OpenCV and NumPy
  (see Setup).
- **The drone:** a Sky Rider Raven 2 (DRWG530B) with its remote.

## Setup

```bash
git clone https://github.com/JamesSarkissian/raven2-live.git
cd raven2-live
```

The viewer works straight away; nothing to install. For live detection, training and the
`detect.py` tools, create a Python environment in the project folder:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The viewer finds `.venv/` automatically. To use a different environment, set
`RAVEN_TF_PYTHON=/path/to/python`.

**Trained models are not included in the repo** (`models/` is git-ignored). To get one,
train the detector (see below) or copy a `best.keras` into `models/road_damage/`. Without
a model, DETECT shows an error and everything else works as normal.

## Flying with the live viewer

1. Take the propellers off for your first tests.
2. Power on the drone and bind the remote. Close the Sky Rider app on your phone, since
   only one app can use the camera at a time.
3. On the Mac, join the drone's `SkyRider_...` Wi-Fi network.
4. Double-click **`Raven 2 Live.command`** (or run `./live_viewer.py`) and type `y` to
   confirm.
5. The viewer opens at http://localhost:8765. Fly with the remote as usual.
6. To finish, press Ctrl+C or close the Terminal window. This sends *stop stream*.

**Keys in the viewer:** `R` record · `S` screenshot · `D` detection on/off.

**Other ways to run it:**
- `./live_viewer.py --demo`: preview the overlay with simulated values, no drone needed.
- `./live_viewer.py --no-telemetry`: video only.
- `--sd`: standard-definition video, which can hold up better at range.
- `--no-auto-record`: don't start recording when the motors unlock.

**Satellite map in the field:** the drone's Wi-Fi has no internet. If the Mac has another
connection at the same time (for example iPhone USB tethering), the live map switches to
satellite imagery.

## Recordings

- **What's saved:** each recording goes in `recordings/<date_time>/` and holds:
  - `video.mjpg` and `frames.csv`: the frames and their timestamps
  - `telemetry.csv`: all flight data
  - `track.geojson` and `track.kml`: the GPS path
  - `meta.json`: a summary
  - `detections.csv`: only if DETECT was on
  Video and flight data share timestamps, so every frame has a position.
- **Recordings page:** open it from the live viewer, or double-click
  **`Raven 2 Recordings.command`** (http://localhost:8767/player); it never contacts the
  drone. You can play, seek and change speed, and the map follows the drone.
  - **Boxes on/off** (`B`) shows the saved detections.
- **Replay through the live viewer:** `./live_viewer.py --replay recordings/<name> [--speed 2] [--loop]`.
- **Link report:** `./link_report.py recordings/<name>` shows frame rate, gaps, distance,
  altitude and Wi-Fi signal every 5 s.
  - Tip: keep the laptop raised with a clear line of sight. The video link weakens quickly
    when the drone flies only 1–2 m up.
- **Testing without the drone:** `./fake_drone.py FRAMES_DIR` imitates the drone on this Mac.
  Then run `./live_viewer.py 127.0.0.1 --port 8766`.

## Pothole and crack detector

**The model** (`damage_model.py`):
- A small CenterNet-style detector with about 1.5 M parameters and 256×256 input.
- Trained from random weights, with no outside pre-trained weights.
- It predicts a heatmap that peaks at the centre of each pothole or crack, plus the box size.
- Two classes: `pothole` and `crack`.

**Training data:**
- `./fetch_rdd2022.py China_Drone United_States` downloads only those parts of the
  **RDD2022** road-damage dataset
  ([paper](https://arxiv.org/abs/2209.08538), [Figshare](https://figshare.com/articles/dataset/21431547),
  CC BY 4.0 / CC BY-SA 4.0). It uses ranged downloads instead of fetching the full 13 GB.
- `./build_dataset.py` merges RDD2022 with a pothole dataset exported from Roboflow
  (TensorFlow CSV format, placed in `~/Downloads/Pothole`) into `~/Downloads/RoadDamage`.
  - It uses hard links, so it takes no extra disk space.
  - RDD2022's lengthwise, crosswise and alligator cracks become `crack`; its potholes
    become `pothole`.

**Train:** about 6.5 min per epoch on an M4. Ctrl+C stops it, and running the same command
again resumes.

```bash
.venv/bin/python train_detector.py            # 60 epochs → models/road_damage/best.keras
```

**Try it on images or a recording:**

```bash
.venv/bin/python detect.py ~/Downloads/RoadDamage/test --out results
.venv/bin/python detect.py recordings/<name> --out results --drone
```

`--drone` adds the processing the live viewer uses for the drone's camera:
- a haze and glare correction
- square left/middle/right sections, so the 16:9 frame isn't squashed

### Training on your own drone footage

Public datasets look very different from this drone's hazy, compressed camera, so the
biggest gains come from labelling its own footage:

1. **Record** potholes and cracks with REC. You can fly, or hold the drone with the motors
   locked.
2. **Export frames:** `python3 extract_frames.py recordings/<name> [...] --no-guesses`
   saves one frame per second to `dataset/drone/<name>/`.
3. **Label:** double-click **`Raven 2 Labeller.command`** (http://localhost:8768).
   - Drag to draw a box. `1` = pothole, `2` = crack.
   - Click a box to move or resize it; Delete removes it.
   - Enter = done and next · X = skip a bad frame · U = next unlabelled · B = brighter ·
     ⌘Z = undo.
   - Every change saves immediately, in the training format.
4. **Build the mixed dataset:** `.venv/bin/python build_drone_dataset.py`. It:
   - adds your labelled frames to the public data
   - splits by place, so the same pothole is never in both training and test
   - prepares the frames the way the live viewer sees them
5. **Fine-tune the existing model:**
   ```bash
   .venv/bin/python train_detector.py --data ~/Downloads/RoadDamageDrone \
       --init models/road_damage/best.keras --out models/road_damage_drone --epochs 20 --lr 5e-4
   ```
6. **Compare before and after** on the held-out drone clips:
   `.venv/bin/python evaluate_model.py --model <model>.keras`.

The viewer uses `models/road_damage_drone/best.keras` when it exists, with a confidence
cutoff of 0.15; otherwise it uses `models/road_damage/best.keras` with a cutoff of 0.25.
Override with `RAVEN_MODEL=...` and `RAVEN_THRESHOLD=...`.

### Results so far

The score is average precision (AP), from 0 to 1: it combines how much damage the model
finds with how often its boxes are right. A box counts if it overlaps the labelled box by
≥ 50%.

| Test images | Public data only | + 539 labelled drone frames |
|---|---|---|
| Public test set (817 photos) | 0.460 | 0.465 |
| Held-out drone clips (99 frames) | 0.011 | 0.114 |

- The drone test set is small: in resampling checks the new model scored 0.08–0.17.
- The main limit is the number of different potholes in the drone data. More footage
  should help most.

## Privacy

Recordings, screenshots, labelled frames and network captures contain **GPS positions
and camera images**, so they are git-ignored and never committed:
`recordings/`, `drone_screenshots/`, `dataset/`, `screenshots/`, `capture_*/`,
`stream_*/`, `probe_*/` and `*.pcap`.

Keep them out of anything you share. If you fork this project, check `git status` before
committing.

## Files

| File | Purpose |
|---|---|
| `live_viewer.py` | Live viewer: web server, overlay page, recording, detection toggle |
| `telemetry.py` | Flight-data decoder and the read-only request |
| `stream_test.py` | 20-second video capture test; shared video protocol constants |
| `lw_unscramble.py` | Undoes the camera's per-frame byte scrambling (from pylwdrone, Apache-2.0) |
| `recorder.py` | Flight recorder, replay, GPS track, recordings library |
| `player_page.py` | Recordings page (player and map) |
| `link_report.py` | Link quality versus distance for a recording |
| `wifi_signal.swift` | Reads the Mac's Wi-Fi signal strength (read-only) |
| `fake_drone.py` | Imitates the drone locally for testing |
| `decode_stream.py` | Turns a raw capture into JPEG frames and a playback page |
| `drone_probe.sh` | Listen-only network probe used to work out the protocols |
| `udp_parse.py` | Reads `tcpdump -x` text for analysis |
| `damage_model.py` | Detector model, training targets, loss, decoding, scoring |
| `train_detector.py` | Trains or fine-tunes the detector |
| `detect.py` | Runs the detector on images or recordings |
| `detect_worker.py` | Detector helper process for the viewer's DETECT toggle |
| `evaluate_model.py` | Scores a model on one part of a dataset |
| `fetch_rdd2022.py` | Downloads chosen RDD2022 folders |
| `build_dataset.py` | Merges the pothole dataset and RDD2022 into one training set |
| `extract_frames.py` | Recordings → images for labelling |
| `label_tool.py` | Local labelling page |
| `build_drone_dataset.py` | Mixes labelled drone frames into the training data, split by place |
| `*.command` | Double-click launchers for the viewer, recordings page and labeller |

## Protocol notes

- **Camera:** a Lewei-style module at 192.168.0.1. It streams MJPEG over TCP 7060 with
  `lewei_cmd` headers; some frames are byte-scrambled (`lw_unscramble.py`).
- **Flight controller:** MSP-like frames with a `BT` preamble over UDP 50000, replying to
  local port 5010. `telemetry.py` documents the field layout and units.
- **Phone app quirks:** the Sky Rider app shows km/h labelled as "mph", and its battery %
  stays at the lowest value it has seen.

## Credits

- [pylwdrone](https://github.com/meekworth/pylwdrone) by meekworth (Apache-2.0): the
  Lewei camera unscrambling tables and algorithm.
- [RDD2022](https://arxiv.org/abs/2209.08538) (Arya et al.): road-damage dataset.
- Pothole images: a dataset exported from [Roboflow](https://roboflow.com).
- Maps: [Leaflet](https://leafletjs.com), Esri World Imagery,
  © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.
