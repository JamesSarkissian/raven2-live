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

Captures (`capture_*/`, `stream_*/`, `probe_*/`) are git-ignored: they contain
GPS location and camera images.

## Protocol notes
- Camera: Lewei-style module at 192.168.0.1, MJPEG over TCP 7060 (`lewei_cmd` headers).
- Flight controller: MSP-like frames with a `BT` preamble over UDP 50000 → local port 5010.
  See `telemetry.py` for field layout and units. The Sky Rider app shows km/h labelled "mph"
  and latches its battery % at the lowest value seen.
