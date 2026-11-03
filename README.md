# Raven 2 Live

A Mac viewer for the Sky Rider Raven 2 (DRWG530B) drone: live camera video in the
browser with a flight-data overlay (battery, altitude, distance, speeds, heading,
GPS, satellites, roll/pitch/yaw, motor state).

Flight always stays on the physical remote.

## Safety rules (enforced in code)
- Video: TCP 7060 only, sending only *start stream*, *heartbeat*, *stop stream*.
- Flight data: UDP 50000 only, sending only the Sky Rider app's own 18-byte
  read request (`telemetry.REQUEST`). No stick, arm, calibration or settings commands.
- Never touches the movement port (9060) or settings port (8060), firmware,
  flight limits, geofencing or sensors.
- Every message is printed and confirmed before the viewer starts.

## Use
1. Props off for testing. Power on the drone, bind the remote, close the Sky Rider app on the phone.
2. Join the drone's `SkyRider_...` Wi-Fi on the Mac.
3. Double-click `Raven 2 Live.command` (or run `./live_viewer.py`) and type `y`.
4. The overlay opens at http://localhost:8765. Ctrl+C or closing Terminal sends *stop stream*.

A **Preflight** checklist (top left) follows the takeoff routine: power, remote, Wi-Fi,
gyro and compass calibration, 3 horizontal and 3 vertical turns, GPS search and lock,
battery. Steps the drone reports are ticked automatically; horizontal turns are counted
from the heading after the compass step is ticked; the rest are tapped by hand. It folds
away when everything is ready or the motors unlock.

## Recording, replay and flight map
- **REC** (top left, or press `R`) records the camera video, all flight data and the GPS
  path to `recordings/<date_time>/`. Recording also **starts by itself when the motors
  unlock** and stops 5 s after they lock again (`--no-auto-record` turns that off).
- Each recording holds `video.mjpg` + `frames.csv` (frame timestamps), `telemetry.csv`,
  `track.geojson` and `track.kml` (open in geojson.io or Google Earth), and `meta.json`.
  Video and flight data share Unix timestamps, so every frame has a position.
- The **flight map** (top right, click to enlarge) draws this flight's path, home (H),
  the drone and its heading, and earlier flights in grey to show ground already covered.
- **Recordings** (top left in the live view) opens the recordings page in a new tab: a list
  of flights by date and time, and a player with video, flight data and a **satellite or
  street map** where the drone moves along its path in time with the video. It has
  play/pause, a timeline, 0.5–4× speed, Space/←/→ keys, a Google Maps button for the
  current position, and KML/GeoJSON downloads. Without the drone, double-click
  `Raven 2 Recordings.command` (http://localhost:8767/player); it never contacts the drone.
- When the Mac has internet (for example iPhone USB tethering while on the drone's Wi-Fi),
  the live map switches to **satellite imagery** that follows the drone (⤢ enlarges it,
  plus a Google Maps button). Without internet it falls back to the plain map.
  Imagery: Esri World Imagery; streets: OpenStreetMap.
- `./live_viewer.py --replay recordings/<name> [--speed 2] [--loop]` plays a recording
  through the viewer without connecting to the drone.
- `./fake_drone.py FRAMES_DIR [UDP_TEXT]` imitates the drone on this Mac for testing:
  run it, then `./live_viewer.py 127.0.0.1 --port 8766`.

`./live_viewer.py --demo` previews the overlay with simulated values; `--no-telemetry` is video only.

## Files
| File | Purpose |
|---|---|
| `live_viewer.py` | Live viewer server and overlay page |
| `telemetry.py` | Flight-data decoder and the read request |
| `stream_test.py` | 20-second video capture test; shared video protocol constants |
| `lw_unscramble.py` | Undoes the camera's per-frame byte scrambling (from pylwdrone, Apache-2.0) |
| `decode_stream.py` | Turns a capture into JPEG frames and a playback page |
| `drone_probe.sh` | Listen-only network probe |
| `udp_parse.py` | Reads `tcpdump -x` text for analysis |
| `recorder.py` | Flight recorder, replay and GPS track |
| `fake_drone.py` | Imitates the drone locally for testing |
| `player_page.py` | Recordings page (player and satellite map) |

Captures and recordings (`capture_*/`, `stream_*/`, `probe_*/`, `recordings/`) are git-ignored: they contain
GPS location and camera images.

## Protocol notes
- Camera: Lewei-style module at 192.168.0.1, MJPEG over TCP 7060 (`lewei_cmd` headers).
- Flight controller: MSP-like frames with a `BT` preamble over UDP 50000 → local port 5010.
  See `telemetry.py` for field layout and units. The Sky Rider app shows km/h labelled "mph"
  and latches its battery % at the lowest value seen.
