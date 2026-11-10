#!/usr/bin/env python3
"""Live camera viewer for the Sky Rider Raven 2, shown in your browser.

Run it while the Mac is joined to the drone's SkyRider_... Wi-Fi, then open
http://localhost:8765. Flight stays on the physical remote.

It sends exactly two kinds of traffic, both printed before it starts:
  - video, TCP port 7060: start stream, heartbeat, stop stream
  - telemetry, UDP port 50000: the Sky Rider app's own 18-byte "send flight
    data" request, once per second (see telemetry.py)
It never opens the movement port (9060) or settings port (8060), sends no
stick, arm, calibration or settings command, and changes no setting.
--no-telemetry skips the telemetry request. --demo fills the overlay with
simulated values (clearly marked DEMO) to preview the layout.

Flights are recorded to recordings/ (video + flight data + GPS path, see
recorder.py): press REC, or recording starts by itself when the motors
unlock. --replay recordings/<name> plays a recording back through the
viewer without touching the drone.
"""
import argparse
import json
import math
import os
import re
import signal
import socket
import subprocess
import struct
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

from lw_unscramble import unscramble
from stream_test import (CMD_HEARTBEAT, CMD_RET_STREAM, DRONE_IP, HDR_LEN,
                         HEARTBEAT, HEARTBEAT_EVERY, MAGIC, START, START_SD, STOP,
                         VIDEO_PORT, recv_exact)
import telemetry as tm
from player_page import PLAYER_PAGE
from recorder import (FlightLog, Replay, Track, frame_bytes, list_recordings,
                      past_tracks, recording_data, recording_folder, valid_fix)

HTTP_PORT = 8765
TELEMETRY_FIELDS = ("battery_pct", "battery_v", "alt_m", "dist_m",
                    "hspeed_ms", "vspeed_ms", "heading_deg", "lat", "lon",
                    "sats", "gps_fix", "roll_deg", "pitch_deg", "yaw_deg",
                    "state")
BATTERY_WINDOW = 5   # seconds; battery % shown is the lowest raw value in this window
STALL_RECONNECT = 8.0   # seconds without any video data before reconnecting
MAX_MESSAGE = 4_000_000 # bigger "frames" mean the stream is out of step
HERE = os.path.dirname(os.path.abspath(__file__))
RECORDINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
SHOTS = os.environ.get("RAVEN_SHOTS", os.path.join(HERE, "drone_screenshots"))   # SHOT button saves here
# the detector needs TensorFlow, which lives in its own Python environment
TF_PYTHON = os.environ.get("RAVEN_TF_PYTHON", os.path.expanduser("~/PotholeRecognitionModel/tf_env/bin/python"))
# the model retrained on the drone's own frames when it exists, else the original
_DRONE_MODEL = os.path.join(HERE, "models", "road_damage_drone", "best.keras")
MODEL = os.environ.get("RAVEN_MODEL") or (_DRONE_MODEL if os.path.exists(_DRONE_MODEL)
                                          else os.path.join(HERE, "models", "road_damage", "best.keras"))
# minimum confidence for a box: the drone-trained model gives real damage
# lower scores on drone video, so it gets a lower cutoff
THRESHOLD = float(os.environ.get("RAVEN_THRESHOLD") or (0.15 if "road_damage_drone" in MODEL else 0.25))


class Camera(threading.Thread):
    """Keeps one stream connection to the drone and holds the latest frame."""

    def __init__(self, ip, start_msg=START):
        super().__init__(daemon=True)
        self.ip = ip
        self.start_msg = start_msg
        self.frame_times = deque()  # arrival times over the last 2 s, for fps
        self.last_frame_t = 0.0
        self.rssi = self.noise = self.tx_rate = None   # the Mac's Wi-Fi link
        self.cond = threading.Condition()
        self.frame = None
        self.seq = 0
        self.connected = False
        self.error = ""
        self.fps = 0.0
        self.running = True
        self.telemetry = dict.fromkeys(TELEMETRY_FIELDS)
        self.demo = False
        self.tel_time = 0.0
        self.track = Track()    # this session's flight path for the live map
        self.log = None         # FlightLog when recording is possible
        self.replay = None      # recording name when replaying
        self.library = False    # recordings-only mode: no drone at all
        self.detector = None    # LiveDetector (DETECT toggle)
        self.shots = []         # [lat, lon] of screenshots taken this session
        self._sock = None

    def run(self):
        while self.running:
            try:
                self._stream()
            except (OSError, ConnectionError, ValueError) as e:
                self.error = str(e)
            self.connected = False
            if self.running:
                time.sleep(1)   # drone out of range or rebooting; retry

    def _stream(self):
        """Read the camera stream. Slow or patchy Wi-Fi is expected: partial
        frames are kept across pauses, heartbeats go out on time even when no
        data arrives, and it only reconnects after STALL_RECONNECT of silence."""
        self._sock = sock = socket.create_connection((self.ip, VIDEO_PORT), timeout=5)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            sock.settimeout(1.0)
            sock.sendall(self.start_msg)
            self.connected, self.error = True, ""
            buf = bytearray()
            last_hb = last_data = time.time()
            while self.running:
                self._parse(buf)
                now = time.time()
                if now - last_hb >= HEARTBEAT_EVERY:
                    sock.sendall(HEARTBEAT)
                    last_hb = now
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    chunk = None
                if chunk == b"":
                    raise ConnectionError("camera closed the connection")
                if chunk:
                    buf += chunk
                    last_data = time.time()
                elif time.time() - last_data > STALL_RECONNECT:
                    raise ConnectionError("no video for %d s" % STALL_RECONNECT)
            sock.sendall(STOP)
        finally:
            sock.close()

    def _parse(self, buf):
        """Take every complete message out of buf; resync if out of step."""
        while len(buf) >= HDR_LEN:
            if buf[:len(MAGIC)] != MAGIC:
                i = buf.find(MAGIC, 1)
                del buf[:i if i > 0 else len(buf) - len(MAGIC) + 1]
                continue
            ints = struct.unpack_from("<9I", buf, len(MAGIC))
            if ints[3] > MAX_MESSAGE:
                del buf[:1]
                continue
            end = HDR_LEN + ints[3]
            if len(buf) < end:
                return
            body = bytes(buf[HDR_LEN:end])
            del buf[:end]
            if ints[0] == CMD_RET_STREAM and len(body) >= 32:
                _, size, _, _ = struct.unpack_from("<LLQL", body)
                frame = bytearray(body[32:32 + size])
                if ints[4] == 129:
                    unscramble(frame, ints[5], ints[6])
                self.publish_frame(bytes(frame))

    def stop(self):
        self.running = False
        sock = self._sock
        if sock and self.connected:
            try:
                sock.sendall(STOP)
            except OSError:
                pass

    def publish_frame(self, jpeg):
        now = self.last_frame_t = time.time()
        self.frame_times.append(now)
        with self.cond:
            self.frame = jpeg
            self.seq += 1
            self.cond.notify_all()
        if self.log:
            self.log.frame(jpeg, time.time())

    def set_telemetry(self, **values):
        self.telemetry.update((k, v) for k, v in values.items() if k in TELEMETRY_FIELDS)
        now = self.tel_time = time.time()
        snapshot = dict(self.telemetry)
        self.track.update(snapshot, now)
        if self.log:
            snapshot["rssi_dbm"] = self.rssi
            self.log.telemetry(snapshot, now)

    def link(self):
        """Video rate over the last 2 s, time since the last frame, Mac Wi-Fi."""
        now = time.time()
        while self.frame_times and now - self.frame_times[0] > 2:
            self.frame_times.popleft()
        return {"fps": round(len(self.frame_times) / 2, 1),
                "frame_age": round(now - self.last_frame_t, 1) if self.last_frame_t else None,
                "rssi": self.rssi, "noise": self.noise, "tx_rate": self.tx_rate}

    def map_data(self):
        t = self.telemetry
        log = self.log
        current = log.rec.name if log and log.rec else None
        return {"home": self.track.home, "track": self.track.latlon(),
                "past": past_tracks(RECORDINGS, skip=current) if not self.replay else [],
                "shots": self.shots,
                "drone": {"lat": t["lat"], "lon": t["lon"], "heading": t["heading_deg"]}
                if valid_fix(t) else None}

    def status(self):
        return {"connected": self.connected, "error": self.error,
                "fps": self.link()["fps"] if not self.replay else round(self.fps, 1),
                "link": self.link(), "frames": self.seq,
                "demo": self.demo, "telemetry": self.telemetry, "replay": self.replay,
                "log": self.log.status() if self.log else None,
                "detect": self.detector.status() if self.detector else None,
                "telemetry_age": round(time.time() - self.tel_time, 1) if self.tel_time else None}


class WifiSignal(threading.Thread):
    """Reads the Mac's Wi-Fi signal once a second via the wifi_signal helper
    (CoreWLAN, read-only). Compiled on first use; skipped if that fails."""

    def __init__(self, cam):
        super().__init__(daemon=True)
        self.cam = cam

    def _helper(self):
        src = os.path.join(HERE, "wifi_signal.swift")
        exe = os.path.join(HERE, ".build", "wifi_signal")
        if not os.path.exists(exe) or os.path.getmtime(exe) < os.path.getmtime(src):
            os.makedirs(os.path.dirname(exe), exist_ok=True)
            subprocess.run(["swiftc", "-O", src, "-o", exe], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return exe

    def run(self):
        try:
            proc = subprocess.Popen([self._helper()], stdout=subprocess.PIPE, text=True)
        except (OSError, subprocess.CalledProcessError):
            return      # no Swift compiler: the readout just omits signal strength
        for line in proc.stdout:
            if not self.cam.running:
                break
            parts = line.split()
            if len(parts) == 3:
                self.cam.rssi, self.cam.noise = int(parts[0]), int(parts[1])
                self.cam.tx_rate = float(parts[2])
            else:
                self.cam.rssi = self.cam.noise = self.cam.tx_rate = None
        proc.terminate()


class Telemetry(threading.Thread):
    """Asks the drone for flight data once a second and decodes the replies."""

    def __init__(self, cam, ip):
        super().__init__(daemon=True)
        self.cam, self.addr = cam, (ip, tm.TELEMETRY_PORT)
        self.battery = deque()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", tm.LOCAL_PORT))
        self.sock.settimeout(0.5)

    def run(self):
        last_req = 0.0
        while self.cam.running:
            if time.time() - last_req >= 1.0:
                assert tm.is_request(tm.REQUEST)
                try:
                    self.sock.sendto(tm.REQUEST, self.addr)
                except OSError:
                    pass    # not on the drone network yet
                last_req = time.time()
            try:
                data, src = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                time.sleep(0.5)
                continue
            if src[0] != self.addr[0]:
                continue
            for direction, cmd, payload in tm.frames(data):
                values = tm.decode(cmd, payload) if direction == ">" else None
                if not values:
                    continue
                raw = values.pop("battery_raw", None)
                if raw is not None:
                    now = time.time()
                    self.battery.append((now, raw))
                    while self.battery and now - self.battery[0][0] > BATTERY_WINDOW:
                        self.battery.popleft()
                    values["battery_pct"] = min(b for _, b in self.battery)
                self.cam.set_telemetry(**values)

    def close(self):
        self.sock.close()


class LiveDetector(object):
    """Runs the pothole/crack model on the live video while the DETECT toggle
    is on. The model needs the TensorFlow Python, so it runs in a helper
    process (detect_worker.py) that is started on toggle-on and stopped on
    toggle-off. It always works on the newest frame and skips any it can't
    keep up with. Read-only: it only looks at frames already received."""

    def __init__(self, cam):
        self.cam = cam
        self.on = self.ready = False
        self.error = ""
        self.result = None
        self.gen = 0
        self.times = deque()
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.on:
                return
            self.on, self.ready, self.error, self.result = True, False, "", None
            self.gen += 1
            threading.Thread(target=self._run, args=(self.gen,), daemon=True).start()

    def stop(self):
        with self.lock:
            self.on = self.ready = False
            self.gen += 1
        with self.cam.cond:
            self.cam.cond.notify_all()

    def _alive(self, gen):
        return self.on and self.gen == gen and self.cam.running

    def _run(self, gen):
        cam = self.cam
        if not os.path.exists(TF_PYTHON):
            self.error, self.on = "TensorFlow Python not found at %s" % TF_PYTHON, False
            return
        os.makedirs(os.path.join(HERE, ".build"), exist_ok=True)
        log = open(os.path.join(HERE, ".build", "detect_worker.log"), "ab")
        try:
            proc = subprocess.Popen([TF_PYTHON, os.path.join(HERE, "detect_worker.py"), MODEL, str(THRESHOLD)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
        except OSError as e:
            self.error, self.on = "could not start the detector: %s" % e, False
            return
        try:
            hello = json.loads(proc.stdout.readline() or b'{"error": "detector exited while loading"}')
            if "error" in hello:
                self.error, self.on = hello["error"], False
                return
            self.ready = self._alive(gen)
            seen = cam.seq
            while self._alive(gen):
                with cam.cond:
                    cam.cond.wait_for(lambda: cam.seq != seen or not self._alive(gen), timeout=1)
                    frame, seq, frame_t = cam.frame, cam.seq, cam.last_frame_t
                if not self._alive(gen) or seq == seen or not frame:
                    continue
                seen = seq
                proc.stdin.write(struct.pack("<I", len(frame)) + frame)
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    raise OSError("detector stopped unexpectedly (see .build/detect_worker.log)")
                r = json.loads(line)
                now = time.time()
                r["seq"], r["t"] = seq, now
                self.times.append(now)
                while self.times and now - self.times[0] > 2:
                    self.times.popleft()
                if self.gen == gen:
                    self.result = r
                    if cam.log:     # recording: keep what was found, with the GPS position
                        cam.log.detections(now, frame_t or now, r["dets"], dict(cam.telemetry))
        except (OSError, ValueError) as e:
            if self.gen == gen:
                self.error, self.on, self.ready = str(e), False, False
        finally:
            try:
                proc.stdin.write(struct.pack("<I", 0))
                proc.stdin.close()
                proc.wait(3)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
            log.close()

    def current(self):
        """Latest boxes if they are fresh, else none."""
        r = self.result
        if not (self.on and r) or time.time() - r["t"] > 1.5:
            return []
        return r["dets"]

    def status(self):
        r = self.result
        now = time.time()
        while self.times and now - self.times[0] > 2:
            self.times.popleft()
        return {"on": self.on, "ready": self.ready, "error": self.error,
                "dets": self.current(), "ms": r["ms"] if r and self.on else None,
                "fps": round(len(self.times) / 2, 1) if self.ready else None,
                "model": os.path.basename(os.path.dirname(MODEL)), "threshold": THRESHOLD}


# ---- screenshots ---------------------------------------------------------------

SHOT_NAME = re.compile(r"\d{8}_\d{6}_\d{3}")


def save_screenshot(cam):
    """Save the current video frame to drone_screenshots/ with where and when
    it was taken (and the detections, if DETECT is on). Returns the metadata.
    The page then adds <name>_annotated.jpg: the frame with the location
    caption and any detection boxes drawn on it."""
    frame = cam.frame
    if not frame:
        return None
    os.makedirs(SHOTS, exist_ok=True)
    now = time.time()
    name = time.strftime("%Y%m%d_%H%M%S", time.localtime(now)) + "_%03d" % int(now % 1 * 1000)
    t = dict(cam.telemetry)
    fix = valid_fix(t)
    dets = cam.detector.current() if cam.detector and cam.detector.on else None
    meta = {"name": name, "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "unix": round(now, 3),
            "lat": t["lat"] if fix else None, "lon": t["lon"] if fix else None, "gps_fix": bool(fix),
            "sats": t["sats"], "alt_m": t["alt_m"], "dist_m": t["dist_m"], "heading_deg": t["heading_deg"],
            "state": t["state"], "battery_pct": t["battery_pct"],
            "google_maps": "https://www.google.com/maps/search/?api=1&query=%.7f,%.7f" % (t["lat"], t["lon"]) if fix else None,
            "detection_on": dets is not None,
            "detections": [{"class": d[0], "score": d[1], "box": d[2:]} for d in dets or []],
            "image": name + ".jpg", "annotated": name + "_annotated.jpg",
            "source": "replay of %s" % cam.replay if cam.replay else ("demo" if cam.demo else "live")}
    with open(os.path.join(SHOTS, name + ".jpg"), "wb") as f:
        f.write(frame)
    with open(os.path.join(SHOTS, name + ".json"), "w") as f:
        json.dump(meta, f, indent=1)
    write_screenshot_index()
    if fix:
        cam.shots.append([t["lat"], t["lon"]])
    return meta


def write_screenshot_index():
    """screenshots.geojson and screenshots.kml: a pin for every screenshot
    with a GPS fix, for Google Earth / any map tool."""
    shots = []
    for n in sorted(os.listdir(SHOTS)):
        if n.endswith(".json") and SHOT_NAME.fullmatch(n[:-5]):
            try:
                with open(os.path.join(SHOTS, n)) as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            if m.get("gps_fix"):
                shots.append(m)
    found = lambda m: ", ".join("%s %d%%" % (d["class"], d["score"] * 100) for d in m["detections"]) or (
        "none" if m["detection_on"] else "detection off")
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [m["lon"], m["lat"]]},
         "properties": {"name": m["name"], "time": m["time"], "image": m["image"], "annotated": m["annotated"],
                        "alt_m": m["alt_m"], "detections": found(m)}} for m in shots]}
    with open(os.path.join(SHOTS, "screenshots.geojson"), "w") as f:
        json.dump(gj, f, indent=1)
    pins = "".join(
        "<Placemark><name>%s</name><description>%s · alt %s m · %s · image %s</description>"
        "<Point><coordinates>%.7f,%.7f,0</coordinates></Point></Placemark>\n"
        % (m["name"], m["time"], m["alt_m"], escape(found(m)), m["annotated"], m["lon"], m["lat"]) for m in shots)
    with open(os.path.join(SHOTS, "screenshots.kml"), "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2">'
                "<Document><name>Raven 2 screenshots</name>\n%s</Document></kml>\n" % pins)


def run_demo(cam):
    """Feed simulated telemetry so the overlay layout can be previewed."""
    cam.demo = True
    t0 = time.time()
    while cam.running:
        t = time.time() - t0
        cam.set_telemetry(
            battery_pct=max(5.0, 96 - t / 4), battery_v=12.4 - t / 400,
            alt_m=12 + 8 * math.sin(t / 9), dist_m=40 + 35 * math.sin(t / 15),
            hspeed_ms=abs(4 * math.sin(t / 5)), vspeed_ms=0.9 * math.cos(t / 9),
            heading_deg=(t * 12) % 360, lat=40.712800 + 0.0003 * math.sin(t / 15),
            lon=-74.006000 + 0.0003 * math.cos(t / 15), sats=11,
            roll_deg=6 * math.sin(t / 2), pitch_deg=-4 * math.cos(t / 3),
            yaw_deg=(t * 12) % 360, gps_fix=True, state="Locked")
        time.sleep(0.2)


PAGE = """<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Raven 2 Live</title>
<link rel=stylesheet href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>
:root{--bg:#0b0d10;--panel:rgba(8,10,14,.55);--fg:#eef1f5;--dim:#9aa4b2;--ok:#3ddc84;--warn:#ffb020;--bad:#ff5c5c}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:13px/1.25 -apple-system,system-ui,sans-serif}
#wrap{position:relative;height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden}
#video{max-width:100%;max-height:100%;display:block}
#empty{position:absolute;color:var(--dim)}
.hud{position:absolute;background:var(--panel);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);border-radius:8px;padding:5px 9px}
.k{color:var(--dim);font-size:10px;letter-spacing:.06em;text-transform:uppercase}
.v{font-variant-numeric:tabular-nums;font-weight:650}
.u{color:var(--dim);font-weight:400;font-size:11px;margin-left:1px}
.na .v{color:var(--dim);font-weight:400}
/* top-left: link + battery (most important) */
#tl{top:10px;left:10px;display:flex;align-items:center;gap:10px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--bad);display:inline-block}
.on .dot{background:var(--ok)}.weak .dot{background:var(--warn)}
#fps{color:var(--dim);font-size:11px;font-variant-numeric:tabular-nums}
#fps.frozen{color:var(--warn);font-weight:600}
#bat{display:flex;align-items:center;gap:6px;font-size:15px}
#batbar{width:26px;height:12px;border:1.5px solid var(--dim);border-radius:3px;padding:1px;position:relative}
#batbar:after{content:"";position:absolute;right:-4px;top:3px;width:2px;height:4px;background:var(--dim);border-radius:1px}
#batfill{height:100%;width:0;border-radius:1px;background:var(--ok)}
/* top-right: position */
#tr{top:10px;right:10px;text-align:right;font:11px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace}
/* bottom strip: flight numbers by importance */
#strip{bottom:10px;left:0;right:0;margin:0 auto;width:max-content;max-width:calc(100% - 20px);display:flex;padding:4px 2px;justify-content:center}
.cell{padding:1px 8px;text-align:center;white-space:nowrap}
.cell .v{font-size:16px}
.cell+.cell{border-left:1px solid rgba(255,255,255,.08)}
/* bottom-left: attitude, small */
#att{bottom:10px;left:10px;font-size:11px}
#att .v{font-size:12px;margin-right:6px}
#demo{color:#111;background:var(--warn);font-weight:700;font-size:10px;letter-spacing:.06em;border-radius:4px;padding:1px 5px}
#nodata{top:10px;left:50%;transform:translateX(-50%);color:var(--dim);font-size:11px;white-space:nowrap}
/* preflight checklist: a small pill that expands into the list */
#pf{top:40px;left:10px;padding:0;overflow:hidden;max-width:calc(100% - 20px)}
#pfhead{display:flex;align-items:center;gap:8px;padding:5px 9px;cursor:pointer;user-select:none;font-size:12px}
#pfhead .v{font-size:12px}
#pf.ready #pfcount{color:var(--ok)}
#pflist{list-style:none;margin:0;padding:2px 9px 8px;display:none;width:250px}
#pf.open #pflist{display:block}
#pflist li{display:flex;align-items:center;gap:7px;padding:3px 0;font-size:12px;color:var(--dim)}
#pflist li.done{color:var(--fg)}
#pflist .box{width:14px;height:14px;flex:none;border-radius:4px;border:1.5px solid #4a5463;display:flex;align-items:center;justify-content:center;font-size:10px}
#pflist li.done .box{background:var(--ok);border-color:var(--ok);color:#0b0d10}
#pflist li.warn .box{background:var(--warn);border-color:var(--warn)}
#pflist li.manual{cursor:pointer}
#pflist li.manual:hover{color:var(--fg)}
#pflist .tag{margin-left:auto;font-size:10px;color:var(--dim)}
#pfreset{margin:4px 0 0;font-size:11px;padding:3px 7px}

#state{font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;border-radius:4px;padding:1px 5px;background:#2a3340;color:var(--dim)}
#state.armed{background:var(--bad);color:#fff}#state.landing{background:var(--warn);color:#111}
#wrap.stale .hud .v,#wrap.stale #tr{opacity:.45}
/* recording */
#rec{font:inherit;font-size:10px;font-weight:700;letter-spacing:.06em;border-radius:4px;padding:1px 6px;background:#2a3340;color:var(--dim);border:0;cursor:pointer}
#rec:hover{color:var(--fg)}
#rec.on{background:var(--bad);color:#fff}
#rec.on:before{content:"● ";animation:blink 1s steps(2) infinite}
@keyframes blink{50%{opacity:0}}
/* flight map: small in the corner, click to enlarge */
#map{top:62px;right:10px;padding:0;overflow:hidden;line-height:0}
#mapc{display:block;cursor:pointer}
#lmapLive{background:#1a1f27}
#lmapLive .leaflet-control-attribution{font-size:8px;line-height:1.2}
#mapbtns{position:absolute;top:4px;right:4px;z-index:600;display:flex;gap:3px;line-height:1}
#mapbtns button,#mapbtns a{background:rgba(20,25,32,.85);color:var(--fg);border:1px solid #2e3744;border-radius:5px;padding:2px 6px;font-size:11px;cursor:pointer;text-decoration:none}
.darrow{width:0;height:0;border-left:7px solid transparent;border-right:7px solid transparent;border-bottom:18px solid #fff;filter:drop-shadow(0 0 2px #000);transform-origin:50% 60%}
.dhome{background:var(--warn);color:#111;font:700 9px system-ui;border-radius:50%;line-height:14px;text-align:center;border:2px solid #111}
#recs{font-size:10px;font-weight:700;letter-spacing:.06em;border-radius:4px;padding:1px 6px;background:#2a3340;color:var(--dim);text-decoration:none;text-transform:uppercase}
#recs:hover{color:var(--fg)}
/* screenshot + detection toggle */
.pill{font:inherit;font-size:10px;font-weight:700;letter-spacing:.06em;border-radius:4px;padding:1px 6px;background:#2a3340;color:var(--dim);border:0;cursor:pointer}
.pill:hover{color:var(--fg);background:#334050}
#det.on{background:var(--ok);color:#0b0d10}
#det.loading{background:var(--warn);color:#111}
#det.err{background:#4a2a2a;color:var(--bad)}
#boxes{position:absolute;pointer-events:none}
#flash{position:absolute;inset:0;background:#fff;opacity:0;pointer-events:none;transition:opacity .35s}
#flash.go{opacity:.55;transition:none}
#toast{top:40px;left:50%;transform:translateX(-50%);font-size:12px;white-space:nowrap;opacity:0;transition:opacity .3s;pointer-events:none;max-width:calc(100% - 20px);overflow:hidden;text-overflow:ellipsis}
#toast.show{opacity:1}
#toast.bad{color:var(--bad)}
/* controls appear on mouse move */
#ctl{bottom:10px;right:10px;display:flex;gap:6px;opacity:0;transition:opacity .25s}
#wrap.active #ctl{opacity:1}
button{background:#1f2630;color:var(--fg);border:1px solid #2e3744;border-radius:6px;padding:5px 9px;font:inherit;font-size:12px;cursor:pointer}
button:hover{background:#2a3340}
@media (max-width:560px){#pflist{width:220px}#map{top:40px}#tr{top:auto;bottom:62px;right:10px}#att{bottom:62px}.cell{padding:1px 5px}.cell .v{font-size:13px}.k{font-size:9px}#fps{display:none}}
</style></head><body>
<div id=wrap>
  <img id=video src="/stream.mjpg" alt="Live drone camera">
  <canvas id=boxes></canvas><div id=flash></div>
  <div id=empty>Waiting for the drone camera…</div>
  <div class=hud id=tl>
    <span id=link title="Camera link"><span class=dot></span></span><span id=fps></span>
    <span id=bat class=na title="Battery"><span id=batbar><div id=batfill></div></span><span class=v id=batv>—</span></span>
    <span id=state title="Motor state" hidden></span>
    <button id=rec title="Record video, flight data and GPS path (R)" hidden>REC</button>
    <button id=shot class=pill title="Screenshot to drone_screenshots/ with its GPS location (S)">SHOT</button>
    <button id=det class=pill title="Live pothole and crack detection (D)">DETECT</button>
    <a id=recs href="/player" target=_blank rel=noopener title="Browse and play past recordings">Recordings</a>
    <span id=demo title="Simulated values, not from the drone" hidden>DEMO</span>
  </div>
  <div class=hud id=toast></div>
  <div class=hud id=nodata>No flight data from the drone yet</div>
  <div class=hud id=tr><span class=k>GPS <span id=sats></span></span><br><span id=lat>—</span><br><span id=lon>—</span></div>
  <div class=hud id=map><canvas id=mapc width=170 height=128 title="Flight map: click to enlarge"></canvas><div id=lmapLive hidden></div>
    <div id=mapbtns><button id=mapsize title="Enlarge map">⤢</button><a id=gmapsLive target=_blank rel=noopener title="Open the drone's position in Google Maps" hidden>Google Maps</a></div></div>
  <div class=hud id=strip>
    <div class=cell id=c_alt><div class=k>Alt</div><span class=v id=alt>—</span></div>
    <div class=cell id=c_dist><div class=k>Dist</div><span class=v id=dist>—</span></div>
    <div class=cell id=c_hs><div class=k>H.Spd</div><span class=v id=hs>—</span></div>
    <div class=cell id=c_vs><div class=k>V.Spd</div><span class=v id=vs>—</span></div>
    <div class=cell id=c_hdg><div class=k>Hdg</div><span class=v id=hdg>—</span></div>
  </div>
  <div class=hud id=att><span class=k>R</span> <span class=v id=roll>—</span><span class=k>P</span> <span class=v id=pitch>—</span><span class=k>Y</span> <span class=v id=yaw>—</span></div>
  <div class=hud id=pf>
    <div id=pfhead title="Preflight checklist"><span class=k>Preflight</span><span class=v id=pfcount></span><span id=pfarrow class=k>▾</span></div>
    <ul id=pflist></ul>
  </div>
  <div class=hud id=ctl><button id=units>ft</button><button id=full>Full screen</button></div>
</div>
<script>
const $=id=>document.getElementById(id);
let imperial=false;try{imperial=localStorage.getItem('units')==='ft'}catch(e){}
const DIRS=['N','NE','E','SE','S','SW','W','NW'];
function setv(id,txt,cell){$(id).innerHTML=txt==null?'—':txt;const c=$(cell||id).closest('.cell,#bat');if(c)c.classList.toggle('na',txt==null);}
const num=(v,d)=>v==null?null:(+v).toFixed(d);
const len=v=>v==null?null:(imperial?num(v*3.28084,0)+'<span class=u>ft</span>':num(v,1)+'<span class=u>m</span>');
const spd=v=>v==null?null:(imperial?num(v*2.23694,1)+'<span class=u>mph</span>':num(v,1)+'<span class=u>m/s</span>');
const deg=v=>v==null?null:num(v,0)+'°';
function render(s){
  // link quality: green = smooth video, amber = weak, red = no video
  const L=s.link||{}, age=L.frame_age, frozen=s.connected&&age!=null&&age>1.5;
  const weak=frozen||(L.fps!=null&&L.fps<8)||(L.rssi!=null&&L.rssi<-70);
  $('link').className=!s.connected||(age!=null&&age>5)?'':weak?'weak':'on';
  const sig=L.rssi!=null?' · '+L.rssi+' dBm':'';
  $('fps').textContent=!s.connected?(s.error?'reconnecting…':'offline')+sig
    :frozen?'video frozen '+Math.round(age)+' s'+sig:s.fps+' fps'+sig;
  $('fps').className=frozen?'frozen':'';
  $('link').title=s.connected?'Camera connected'+(L.rssi!=null?' · Mac Wi-Fi '+L.rssi+' dBm (noise '+L.noise+' dBm, '+L.tx_rate+' Mbps)':'')
    +' · above -60 dBm is strong, below -70 dBm video starts dropping':(s.error||'Not connected');
  $('empty').style.display=s.frames?'none':'';
  const t=s.telemetry, have=Object.values(t).some(v=>v!=null);
  const stale=have&&(s.telemetry_age==null||s.telemetry_age>3);
  $('wrap').classList.toggle('stale',stale);
  $('nodata').style.display=have&&!stale?'none':'';
  $('nodata').textContent=stale?'Flight data lost '+(s.telemetry_age==null?'':Math.round(s.telemetry_age)+' s ago'):'No flight data from the drone yet';
  $('state').hidden=t.state==null;$('state').textContent=t.state||'';
  $('state').className=t.state==='Unlocked'?'armed':t.state==='Landing'?'landing':'';
  // battery: percent if known, else volts
  const pct=t.battery_pct;
  setv('batv',pct!=null?num(pct,0)+'%':(t.battery_v!=null?num(t.battery_v,1)+'<span class=u>V</span>':null));
  $('batfill').style.width=(pct!=null?Math.max(0,Math.min(100,pct)):0)+'%';
  $('batfill').style.background=pct==null?'transparent':pct<20?'var(--bad)':pct<40?'var(--warn)':'var(--ok)';
  $('batv').style.color=pct!=null&&pct<20?'var(--bad)':'';
  setv('alt',len(t.alt_m));setv('dist',len(t.dist_m));setv('hs',spd(t.hspeed_ms));
  setv('vs',t.vspeed_ms==null?null:(t.vspeed_ms>0.05?'↑':t.vspeed_ms<-0.05?'↓':'')+spd(Math.abs(t.vspeed_ms)));
  setv('hdg',t.heading_deg==null?null:deg(t.heading_deg)+' <span class=u>'+DIRS[Math.round(((t.heading_deg%360)+360)%360/45)%8]+'</span>');
  const fix=t.lat!=null&&!(t.lat===0&&t.lon===0)&&t.gps_fix!==false;
  $('lat').textContent=!fix?'—':Math.abs(t.lat).toFixed(6)+'° '+(t.lat>=0?'N':'S');
  $('lon').textContent=!fix?'—':Math.abs(t.lon).toFixed(6)+'° '+(t.lon>=0?'E':'W');
  $('sats').textContent=t.sats==null?'':'· '+t.sats+' sats'+(t.gps_fix===false?' · no fix':'');
  setv('roll',deg(t.roll_deg));setv('pitch',deg(t.pitch_deg));setv('yaw',deg(t.yaw_deg));
  pfRender(s);
  recRender(s);
  detRender(s.detect);
  $('demo').hidden=!(s.demo||s.replay);$('demo').textContent=s.replay?'REPLAY':'DEMO';
}
// ---- preflight checklist -------------------------------------------------
// auto: confirmed live from drone data; manual: tick when done (remote has no data link to us)
const PF=[
 {id:'power', label:'Drone powered on', auto:s=>s.connected||(s.telemetry_age!=null&&s.telemetry_age<3)},
 {id:'remote',label:'Remote on (beep)'},
 {id:'wifi',  label:'Wi-Fi connected', auto:s=>s.connected},
 {id:'gyro',  label:'Gyro calibration (hold 2 s)'},
 {id:'compass',label:'Compass calibration (hold 2 s)'},
 {id:'hturns',label:'Rotate horizontally 3×', turns:true},
 {id:'vturns',label:'Rotate vertically 3×'},
 {id:'ground',label:'On open ground, GPS searching', auto:s=>s.telemetry.sats!=null&&s.telemetry_age!=null&&s.telemetry_age<3},
 {id:'gps',   label:'GPS locked', auto:s=>s.telemetry.gps_fix===true&&s.telemetry.sats>=6&&s.telemetry_age<3},
 {id:'bat',   label:'Battery 30% or more', auto:s=>s.telemetry.battery_pct!=null&&s.telemetry.battery_pct>=30, warnOnly:true},
];
const pfDone={}; let pfOpen=true, pfWasReady=false, pfWasUnlocked=false, turns=0, lastYaw=null, last={};
function pfReset(){for(const k in pfDone)delete pfDone[k];turns=0;lastYaw=null;pfOpen=true;pfWasReady=false;pfRender(last);}
function pfTrackTurns(t){
  // count horizontal turns from heading once compass calibration is ticked
  if(!pfDone.compass||t.yaw_deg==null){lastYaw=null;return;}
  if(lastYaw!=null){let d=t.yaw_deg-lastYaw;if(d>180)d-=360;if(d<-180)d+=360;turns+=Math.abs(d)/360;}
  lastYaw=t.yaw_deg;
  if(turns>=3)pfDone.hturns=true;
}
function pfState(item,s){
  if(item.auto)return item.auto(s)?'done':(item.warnOnly&&s.telemetry.battery_pct!=null?'warn':'');
  return pfDone[item.id]?'done':'';
}
function pfRender(s){
  if(!s.telemetry)return;
  last=s; pfTrackTurns(s.telemetry);
  const ul=$('pflist');
  if(!ul.children.length){   // build once; later renders only update, so clicks are never lost
    for(const item of PF){
      const li=document.createElement('li'); li.className=item.auto?'':'manual';
      li.innerHTML='<span class=box></span><span></span><span class=tag></span>';
      li.children[1].textContent=item.label;
      if(!item.auto)li.onclick=()=>{pfDone[item.id]=!pfDone[item.id];if(item.id==='compass'){turns=0;lastYaw=null;}pfRender(last);};
      ul.appendChild(li); item.li=li;
    }
    const b=document.createElement('button');b.id='pfreset';b.textContent='Reset checklist';b.onclick=pfReset;ul.appendChild(b);
  }
  let n=0;
  for(const item of PF){
    const st=pfState(item,s); if(st==='done')n++;
    item.li.className=st+(item.auto?'':' manual');
    item.li.children[0].textContent=st==='done'?'✓':st==='warn'?'!':'';
    item.li.children[2].textContent=item.auto?'auto':(item.turns&&!pfDone.hturns?(pfDone.compass?turns.toFixed(1)+' / 3':'after compass'):'tap');
  }
  const ready=n===PF.length;
  $('pfcount').textContent=ready?'Ready':n+'/'+PF.length;
  $('pf').classList.toggle('ready',ready);
  // fold away once ready or when motors unlock, so it never covers the video in flight
  const unlocked=s.telemetry.state==='Unlocked';
  if((ready&&!pfWasReady)||(unlocked&&!pfWasUnlocked))pfOpen=false;
  pfWasReady=ready;pfWasUnlocked=unlocked;
  $('pf').classList.toggle('open',pfOpen);
  $('pfarrow').textContent=pfOpen?'▴':'▾';
}
$('pfhead').onclick=()=>{pfOpen=!pfOpen;pfRender(last);};
// ---- recording -------------------------------------------------------------
const mmss=x=>Math.floor(x/60)+':'+String(Math.floor(x%60)).padStart(2,'0');
function recRender(s){
  const b=$('rec'); b.hidden=!s.log;
  if(!s.log)return;
  const r=s.log.recording; b.classList.toggle('on',!!r);
  const f=r&&r.detections||{}, nf=(f.pothole||0)+(f.crack||0);
  b.textContent=r?'REC '+mmss(r.elapsed)+(nf?' · '+nf+' found':''):'REC';
  b.title=r?('Recording '+r.name+(r.auto?' (started on unlock)':'')+' · '+r.mb+' MB'
             +(detOn?' · detections saved too':' · turn DETECT on to also save detections')+' · click to stop (R)')
          :'Record video, flight data and GPS path (R)'+(s.log.auto_record?' · starts by itself when motors unlock':'');
}
async function recToggle(){
  const on=$('rec').classList.contains('on');
  try{await fetch(on?'/record/stop':'/record/start',{method:'POST'});}catch(e){}
  poll();
}
$('rec').onclick=recToggle;
document.addEventListener('keydown',e=>{if((e.key==='r'||e.key==='R')&&!$('rec').hidden)recToggle();});
// ---- flight map --------------------------------------------------------------
let mapBig=false, mapData=null;
const NICE=[1,2,5,10,20,50,100,200,500,1000,2000,5000];
function drawMap(){
  const d0=mapData;
  $('gmapsLive').hidden=!(d0&&d0.drone);
  if(d0&&d0.drone)$('gmapsLive').href='https://www.google.com/maps/search/?api=1&query='+d0.drone.lat+','+d0.drone.lon;
  if(lmap)return liveMapUpdate();
  const c=$('mapc'), [W,H]=mapSize();
  const dpr=devicePixelRatio||1;
  if(c.width!==Math.round(W*dpr)){c.width=Math.round(W*dpr);c.height=Math.round(H*dpr);c.style.width=W+'px';c.style.height=H+'px';}
  const g=c.getContext('2d'); g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,W,H);
  g.font='10px -apple-system,system-ui,sans-serif';
  const d=mapData;
  if(!d||(!d.drone&&!d.track.length)){g.fillStyle='#9aa4b2';g.textAlign='center';g.fillText('Waiting for GPS fix',W/2,H/2+3);return;}
  const ref=d.drone?[d.drone.lat,d.drone.lon]:d.track[d.track.length-1];
  const kx=111320*Math.cos(ref[0]*Math.PI/180), ky=110540;
  const xy=p=>[(p[1]-ref[1])*kx,(p[0]-ref[0])*ky];
  // fit this flight and home; at least 50 m across
  const pts=d.track.map(xy); if(d.home)pts.push(xy(d.home)); pts.push([0,0]);
  let x0=Math.min(...pts.map(p=>p[0])),x1=Math.max(...pts.map(p=>p[0]));
  let y0=Math.min(...pts.map(p=>p[1])),y1=Math.max(...pts.map(p=>p[1]));
  const cx=(x0+x1)/2, cy=(y0+y1)/2, span=Math.max(50,(x1-x0)*1.25,(y1-y0)*1.25*W/H);
  const sc=W/span, px=p=>[W/2+(p[0]-cx)*sc,H/2-(p[1]-cy)*sc];
  const line=(arr,color,width)=>{if(arr.length<2)return;g.beginPath();arr.forEach((p,i)=>{const q=px(xy(p));i?g.lineTo(q[0],q[1]):g.moveTo(q[0],q[1]);});g.strokeStyle=color;g.lineWidth=width;g.lineJoin='round';g.stroke();};
  for(const t of d.past)line(t,'rgba(154,164,178,.45)',2);      // earlier flights = covered ground
  line(d.track,'#38bdf8',2.5);                                   // this flight
  for(const p of d.shots||[]){const q=px(xy(p));g.fillStyle='#c084fc';g.strokeStyle='#0b0d10';g.lineWidth=1.5;g.beginPath();g.arc(q[0],q[1],3.5,0,7);g.fill();g.stroke();}
  if(d.home){const h=px(xy(d.home));g.fillStyle='#ffb020';g.beginPath();g.arc(h[0],h[1],6,0,7);g.fill();
    g.fillStyle='#111';g.textAlign='center';g.font='bold 8px system-ui';g.fillText('H',h[0],h[1]+3);}
  if(d.drone){const p=px([0,0]),a=(d.drone.heading||0)*Math.PI/180;
    g.save();g.translate(p[0],p[1]);g.rotate(a);g.beginPath();g.moveTo(0,-8);g.lineTo(5.5,6);g.lineTo(0,3);g.lineTo(-5.5,6);g.closePath();
    g.fillStyle='#fff';g.strokeStyle='#0b0d10';g.lineWidth=1.5;g.fill();g.stroke();g.restore();}
  // north arrow and scale bar
  g.fillStyle='#9aa4b2';g.textAlign='left';g.font='bold 10px system-ui';g.fillText('N ↑',6,13);
  const target=span/4, m=NICE.reduce((b,v)=>v<=target?v:b,1), len=m*sc;
  g.strokeStyle='#eef1f5';g.lineWidth=2;g.beginPath();g.moveTo(6,H-8);g.lineTo(6+len,H-8);g.stroke();
  g.font='10px system-ui';g.fillStyle='#eef1f5';g.fillText(m>=1000?m/1000+' km':m+' m',10+len,H-5);
}
function mapSize(){const small=innerWidth<560;let W=mapBig?Math.min(460,innerWidth-20):(lmap?(small?150:230):(small?120:170));return [W,Math.round(W*.75)];}
async function pollMap(){try{mapData=await (await fetch('/map',{cache:'no-store'})).json();}catch(e){mapData=null;}drawMap();}
function toggleMapSize(){mapBig=!mapBig;$('mapsize').textContent=mapBig?'⤡':'⤢';if(lmap){const [W,H]=mapSize();const el=$('lmapLive');el.style.width=W+'px';el.style.height=H+'px';lmap.invalidateSize();}drawMap();}
$('mapc').onclick=toggleMapSize; $('mapsize').onclick=toggleMapSize;
// Satellite map when the Mac has internet (e.g. iPhone USB tethering); plain map otherwise.
let lmap=null, lml={}, lmFollow=0, lmPast=-1;
function liveMapTry(){
  if(lmap||!window.L)return;
  const el=$('lmapLive'); el.hidden=false; const [W,H]=[mapBig?Math.min(460,innerWidth-20):(innerWidth<560?150:230)].map(w=>[w,Math.round(w*.75)])[0];
  el.style.width=W+'px';el.style.height=H+'px';
  const m=L.map(el,{zoomControl:false,attributionControl:true}).setView([0,0],18);
  const tiles=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:21,maxNativeZoom:19,attribution:'Imagery © Esri'}).addTo(m);
  let ok=false;
  tiles.once('tileload',()=>{ok=true;lmap=m;$('mapc').style.display='none';
    lml.past=L.layerGroup().addTo(m);lml.shots=L.layerGroup().addTo(m);lml.track=L.polyline([],{color:'#38bdf8',weight:3}).addTo(m);
    lml.home=L.marker([0,0],{icon:L.divIcon({className:'dhome',html:'H',iconSize:[14,14]})});
    lml.drone=L.marker([0,0],{icon:L.divIcon({className:'',html:'<div class=darrow></div>',iconSize:[14,18],iconAnchor:[7,11]})});
    m.on('dragstart zoomstart',e=>{if(e.type==='dragstart')lmFollow=Date.now();});
    drawMap();});
  // no tiles within 6 s means no internet: stay on the plain map
  setTimeout(()=>{if(!ok){m.remove();el.hidden=true;}},6000);
}
function liveMapUpdate(){
  const d=mapData; if(!d)return;
  if(d.past.length!==lmPast){lml.past.clearLayers();d.past.forEach(t=>L.polyline(t,{color:'#9aa4b2',weight:2,opacity:.6}).addTo(lml.past));lmPast=d.past.length;}
  lml.track.setLatLngs(d.track);
  if((d.shots||[]).length!==lml.nShots){lml.shots.clearLayers();(d.shots||[]).forEach(p=>L.circleMarker(p,{radius:5,color:'#0b0d10',weight:1.5,fillColor:'#c084fc',fillOpacity:1}).bindTooltip('Screenshot').addTo(lml.shots));lml.nShots=(d.shots||[]).length;}
  if(d.home){lml.home.setLatLng(d.home);if(!lmap.hasLayer(lml.home))lml.home.addTo(lmap);}
  if(d.drone){const p=[d.drone.lat,d.drone.lon];lml.drone.setLatLng(p);if(!lmap.hasLayer(lml.drone))lml.drone.addTo(lmap);
    const a=lml.drone.getElement();if(a)a.firstChild.style.transform='rotate('+(d.drone.heading||0)+'deg)';
    // follow the drone unless the map was dragged in the last 15 s
    if(Date.now()-lmFollow>15000&&!lmap.getBounds().pad(-0.25).contains(p))lmap.setView(p,Math.max(lmap.getZoom(),18));}
}
function loadLeaflet(){if(window.L)return liveMapTry();const sc=document.createElement('script');
  sc.src='https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js';sc.onload=liveMapTry;document.head.appendChild(sc);}
addEventListener('online',()=>{if(!lmap)loadLeaflet();});
loadLeaflet();
setInterval(pollMap,1000);pollMap();
async function poll(){
  try{render(await (await fetch('/status',{cache:'no-store'})).json());}
  catch(e){$('link').className='';$('fps').textContent='viewer stopped';}
}
setInterval(poll,200);poll();
$('units').textContent=imperial?'m':'ft';
$('units').onclick=()=>{imperial=!imperial;$('units').textContent=imperial?'m':'ft';try{localStorage.setItem('units',imperial?'ft':'m')}catch(e){}poll();};
// ---- live detection ------------------------------------------------------------
const BOXC={pothole:'#ff4d4d',crack:'#ffd21a'};
let detOn=false, dets=[], lastTel={};
function drawBoxes(g,list,W,H,k){
  g.lineJoin='round'; g.font='700 '+Math.round(12*k)+'px -apple-system,system-ui,sans-serif'; g.textBaseline='top';
  for(const [cls,sc,x0,y0,x1,y1] of list){
    const c=BOXC[cls]||'#fff', X=x0*W, Y=y0*H, w=(x1-x0)*W, h=(y1-y0)*H;
    g.strokeStyle=c; g.lineWidth=2.5*k; g.strokeRect(X,Y,w,h);
    const lab=cls+' '+Math.round(sc*100)+'%', tw=g.measureText(lab).width+8*k, th=17*k, ty=Y>=th?Y-th:Y;
    g.fillStyle=c; g.fillRect(X-1.25*k,ty,tw,th); g.fillStyle='#000'; g.fillText(lab,X+3*k,ty+2.5*k);
  }
}
function renderBoxes(){
  const c=$('boxes'), v=$('video'), r=v.getBoundingClientRect(), wr=$('wrap').getBoundingClientRect();
  const dpr=devicePixelRatio||1, W=Math.round(r.width), H=Math.round(r.height);
  if(c.width!==W*dpr||c.height!==H*dpr){c.width=W*dpr;c.height=H*dpr;c.style.width=W+'px';c.style.height=H+'px';}
  c.style.left=(r.left-wr.left)+'px'; c.style.top=(r.top-wr.top)+'px';
  const g=c.getContext('2d'); g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,W,H);
  if(detOn&&dets.length)drawBoxes(g,dets,W,H,1);
}
function detRender(d){
  const b=$('det'); if(!d){b.hidden=true;return;}
  detOn=d.on; dets=d.dets||[];
  b.className='pill'+(d.on?(d.ready?' on':' loading'):(d.error?' err':''));
  const n=k=>dets.filter(x=>x[0]===k).length;
  b.textContent=!d.on?'DETECT':!d.ready?'DETECT…':'DETECT'+(n('pothole')?' · '+n('pothole')+' pothole'+(n('pothole')>1?'s':''):'')+(n('crack')?' · '+n('crack')+' crack'+(n('crack')>1?'s':''):'');
  b.title=d.error?'Detection stopped: '+d.error+' · click to retry'
    :!d.on?'Live pothole and crack detection: off (D)'
    :!d.ready?'Loading the model…':'Detection on · model '+d.model+' (cutoff '+d.threshold+') · '+d.fps+' frames/s · '+d.ms+' ms per frame · click to turn off (D)';
  renderBoxes();
}
async function detToggle(){
  try{detRender(await (await fetch(detOn?'/detect/off':'/detect/on',{method:'POST'})).json());}catch(e){}
}
$('det').onclick=detToggle;
// faster updates than /status while detection is on, so boxes keep up with the video
setInterval(async()=>{if(!detOn)return;try{detRender(await (await fetch('/detections',{cache:'no-store'})).json());}catch(e){}},100);
addEventListener('resize',renderBoxes);
// ---- screenshots ---------------------------------------------------------------
let toastT;
function toast(msg,bad){const t=$('toast');t.textContent=msg;t.className='hud show'+(bad?' bad':'');clearTimeout(toastT);toastT=setTimeout(()=>t.className='hud',3500);}
async function takeShot(){
  const f=$('flash'); f.className='go'; requestAnimationFrame(()=>requestAnimationFrame(()=>f.className=''));
  let m;
  try{const r=await fetch('/screenshot',{method:'POST'}); m=await r.json(); if(!r.ok)return toast('Screenshot failed: '+(m.error||r.status),true);}
  catch(e){return toast('Screenshot failed: viewer not reachable',true);}
  // annotated copy: the frame with detection boxes (if on) and a location caption
  try{
    const img=new Image(); img.src='/shots/'+m.name+'.jpg'; await img.decode();
    const W=img.naturalWidth, H=img.naturalHeight, k=W/1280, c=document.createElement('canvas'); c.width=W; c.height=H;
    const g=c.getContext('2d'); g.drawImage(img,0,0);
    const list=m.detections.map(d=>[d.class,d.score,...d.box]);
    if(m.detection_on)drawBoxes(g,list,W,H,k);
    const dir=m.heading_deg==null?'':' '+DIRS[Math.round(((m.heading_deg%360)+360)%360/45)%8];
    const where=m.gps_fix?Math.abs(m.lat).toFixed(6)+'° '+(m.lat>=0?'N':'S')+', '+Math.abs(m.lon).toFixed(6)+'° '+(m.lon>=0?'E':'W'):'no GPS fix';
    const cnt=k2=>list.filter(x=>x[0]===k2).length;
    const found=!m.detection_on?'':' · '+(list.length?[cnt('pothole')&&cnt('pothole')+' pothole'+(cnt('pothole')>1?'s':''),cnt('crack')&&cnt('crack')+' crack'+(cnt('crack')>1?'s':'')].filter(Boolean).join(', '):'no damage found');
    const cap='Raven 2 · '+m.time+' · '+where+(m.alt_m!=null?' · alt '+(+m.alt_m).toFixed(1)+' m':'')+(m.heading_deg!=null?' · hdg '+Math.round(m.heading_deg)+'°'+dir:'')+found;
    const ch=Math.round(26*k); g.fillStyle='rgba(8,10,14,.68)'; g.fillRect(0,H-ch,W,ch);
    g.fillStyle='#eef1f5'; g.font='600 '+Math.round(14*k)+'px -apple-system,system-ui,sans-serif'; g.textBaseline='middle'; g.fillText(cap,10*k,H-ch/2);
    const blob=await new Promise(r=>c.toBlob(r,'image/jpeg',.92));
    await fetch('/screenshot/'+m.name+'/annotated',{method:'POST',body:blob});
  }catch(e){}
  toast('Saved '+m.name+(m.gps_fix?' · '+(+m.lat).toFixed(5)+', '+(+m.lon).toFixed(5):' · no GPS fix')+(m.detection_on?' · '+m.detections.length+' detection'+(m.detections.length===1?'':'s'):''));
  pollMap();
}
$('shot').onclick=takeShot;
document.addEventListener('keydown',e=>{if(e.metaKey||e.ctrlKey||e.altKey)return;
  if(e.key==='s'||e.key==='S')takeShot(); if((e.key==='d'||e.key==='D')&&!$('det').hidden)detToggle();});
$('full').onclick=()=>document.fullscreenElement?document.exitFullscreen():$('wrap').requestFullscreen();
let idle;document.addEventListener('mousemove',()=>{$('wrap').classList.add('active');clearTimeout(idle);idle=setTimeout(()=>$('wrap').classList.remove('active'),2500);});
$('video').onerror=()=>setTimeout(()=>$('video').src='/stream.mjpg?'+Date.now(),1000);
</script></body></html>
"""


def make_handler(cam):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _recording(self, path):
            """Serve /rec/<name>/data, /frame/<i>.jpg, /track.kml, /track.geojson."""
            parts = path.split("/")      # ['', 'rec', name, what, (i.jpg)]
            folder = recording_folder(RECORDINGS, parts[2]) if len(parts) >= 4 else None
            if not folder:
                return self._send(404, "text/plain", b"no such recording")
            if parts[3] == "data":
                return self._send(200, "application/json", json.dumps(recording_data(folder)).encode())
            if parts[3] == "frame" and len(parts) == 5 and parts[4].endswith(".jpg"):
                try:
                    jpeg = frame_bytes(folder, int(parts[4][:-4]))
                except ValueError:
                    jpeg = None
                return self._send(200, "image/jpeg", jpeg) if jpeg else self._send(404, "text/plain", b"no frame")
            if parts[3] in ("track.kml", "track.geojson"):
                f = os.path.join(folder, parts[3])
                if os.path.exists(f):
                    self.send_response(200)
                    ctype = "application/vnd.google-earth.kml+xml" if f.endswith("kml") else "application/geo+json"
                    body = open(f, "rb").read()
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Content-Disposition", 'attachment; filename="raven2_%s_%s"' % (parts[2], parts[3]))
                    self.end_headers()
                    return self.wfile.write(body)
            return self._send(404, "text/plain", b"not found")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/" and cam.library:
                self.send_response(302)
                self.send_header("Location", "/player")
                self.end_headers()
            elif path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            elif path == "/player":
                self._send(200, "text/html; charset=utf-8", PLAYER_PAGE.encode())
            elif path == "/recordings":
                body = {"live": not cam.library and not cam.replay, "recordings": list_recordings(RECORDINGS)}
                self._send(200, "application/json", json.dumps(body).encode())
            elif path.startswith("/rec/"):
                self._recording(path)
            elif path == "/status":
                self._send(200, "application/json", json.dumps(cam.status()).encode())
            elif path == "/map":
                self._send(200, "application/json", json.dumps(cam.map_data()).encode())
            elif path == "/detections":
                body = cam.detector.status() if cam.detector else {"on": False}
                self._send(200, "application/json", json.dumps(body).encode())
            elif path.startswith("/shots/") and path.endswith(".jpg") and SHOT_NAME.fullmatch(path[7:-4]):
                f = os.path.join(SHOTS, path[7:])
                if os.path.exists(f):
                    with open(f, "rb") as fh:
                        self._send(200, "image/jpeg", fh.read())
                else:
                    self._send(404, "text/plain", b"no such screenshot")
            elif path == "/frame.jpg":
                if cam.frame:
                    self._send(200, "image/jpeg", cam.frame)
                else:
                    self._send(404, "text/plain", b"no frame yet")
            elif path == "/stream.mjpg":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                seen = 0
                try:
                    while cam.running:
                        with cam.cond:
                            cam.cond.wait_for(lambda: cam.seq != seen or not cam.running, timeout=5)
                            frame, seen = cam.frame, cam.seq
                        if not frame:
                            continue
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                         % len(frame) + frame + b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            path = self.path.split("?")[0]
            parts = path.split("/")
            if path in ("/detect/on", "/detect/off") and cam.detector:
                cam.detector.start() if path.endswith("on") else cam.detector.stop()
                return self._send(200, "application/json", json.dumps(cam.detector.status()).encode())
            if path == "/screenshot" and not cam.library:
                meta = save_screenshot(cam)
                if not meta:
                    return self._send(409, "application/json", b'{"error": "no video yet"}')
                return self._send(200, "application/json", json.dumps(meta).encode())
            if len(parts) == 4 and parts[1] == "screenshot" and parts[3] == "annotated" \
                    and SHOT_NAME.fullmatch(parts[2]) and os.path.exists(os.path.join(SHOTS, parts[2] + ".json")):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if 0 < n <= 16_000_000 else b""
                if not body.startswith(b"\xff\xd8"):
                    return self._send(400, "text/plain", b"expected a JPEG")
                with open(os.path.join(SHOTS, parts[2] + "_annotated.jpg"), "wb") as f:
                    f.write(body)
                return self._send(200, "application/json", b'{"ok": true}')
            if not cam.log:
                self._send(409, "text/plain", b"recording is off in demo and replay")
            elif path == "/record/start":
                cam.log.start()
                self._send(200, "application/json", json.dumps(cam.log.status()).encode())
            elif path == "/record/stop":
                cam.log.stop()
                self._send(200, "application/json", json.dumps(cam.log.status()).encode())
            else:
                self._send(404, "text/plain", b"not found")

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Raven 2 live viewer")
    ap.add_argument("ip", nargs="?", default=DRONE_IP)
    ap.add_argument("--demo", action="store_true",
                    help="fill the overlay with simulated values (marked DEMO)")
    ap.add_argument("--port", type=int, default=HTTP_PORT, help="local web page port")
    ap.add_argument("--no-telemetry", action="store_true",
                    help="video only; do not request flight data")
    ap.add_argument("--sd", action="store_true",
                    help="ask the camera for standard-definition video (lighter on weak Wi-Fi)")
    ap.add_argument("--no-auto-record", action="store_true",
                    help="do not start recording when the motors unlock")
    ap.add_argument("--replay", metavar="FOLDER",
                    help="play a recording instead of connecting to the drone")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed")
    ap.add_argument("--loop", action="store_true", help="repeat the replay")
    ap.add_argument("--library", action="store_true",
                    help="only browse and play recordings; never contacts the drone")
    args = ap.parse_args()
    ip = args.ip
    if args.replay or args.library:
        return serve_replay(args)
    print("This viewer sends only these messages:")
    print(" video, TCP %s:%d" % (ip, VIDEO_PORT))
    start_msg = START_SD if args.sd else START
    print("  start stream, once:   ", start_msg.hex(" "), "(standard definition)" if args.sd else "")
    print("  heartbeat, every 1 s: ", HEARTBEAT.hex(" "))
    print("  stop stream, at end:  ", STOP.hex(" "))
    use_tel = not (args.no_telemetry or args.demo)
    if use_tel:
        print(" flight data request, UDP %s:%d (same bytes as the Sky Rider app)" % (ip, tm.TELEMETRY_PORT))
        print("  every 1 s:            ", tm.REQUEST.hex(" "))
    if input("Mac on the drone Wi-Fi and remote bound? Start the viewer? [y/N] ").strip().lower() != "y":
        print("Cancelled. Nothing was sent.")
        return 1

    # Ctrl+C, closing the Terminal window and kill all stop the stream cleanly
    def interrupt(*_):
        raise KeyboardInterrupt
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupt)

    cam = Camera(ip, start_msg)
    cam.detector = LiveDetector(cam)
    WifiSignal(cam).start()
    if not args.demo:
        cam.log = FlightLog(RECORDINGS, TELEMETRY_FIELDS + ("rssi_dbm",),
                            auto_record=not args.no_auto_record)
    cam.start()
    if args.demo:
        threading.Thread(target=run_demo, args=(cam,), daemon=True).start()
    tel = None
    if use_tel:
        try:
            tel = Telemetry(cam, ip)
            tel.start()
        except OSError as e:
            print("Flight data unavailable (UDP port %d busy: %s)" % (tm.LOCAL_PORT, e))
    # localhost only: the page is not reachable from other devices
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(cam))
    server.daemon_threads = True
    print("Viewer running: open http://localhost:%d  (Ctrl+C to stop)" % args.port)
    print("Detection model: %s (cutoff %.2f)" % (os.path.relpath(MODEL, HERE), THRESHOLD))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cam.detector.stop()
        cam.stop()
        if tel:
            tel.close()
        if cam.log and cam.log.rec:
            cam.log.stop()
            print("\nSaved recording %s" % cam.log.last["name"])
        with cam.cond:
            cam.cond.notify_all()
        server.server_close()
        print("\nStopped. Sent stop stream to the camera.")
    return 0


def serve_replay(args):
    """Replay or browse recordings. Nothing is ever sent to the drone."""
    cam = Camera(None)
    if args.library:
        cam.library = True
        what = "Recordings"
    else:
        folder = os.path.abspath(args.replay)
        cam.replay = os.path.basename(folder)
        cam.detector = LiveDetector(cam)
        Replay(cam, folder, speed=args.speed, loop=args.loop).start()
        what = "Replaying " + cam.replay
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(cam))
    server.daemon_threads = True
    url = "http://localhost:%d%s" % (args.port, "/player" if args.library else "")
    print("%s: open %s  (Ctrl+C to stop)" % (what, url), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if cam.detector:
            cam.detector.stop()
        cam.running = False
        with cam.cond:
            cam.cond.notify_all()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
