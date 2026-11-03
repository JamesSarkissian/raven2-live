"""Flight recorder, replay and GPS flight path for the Raven 2 viewer.

A recording is a folder recordings/<YYYYmmdd_HHMMSS>/ holding:
  video.mjpg      camera frames (JPEG) back to back
  frames.csv      t, offset, size for each frame in video.mjpg
  telemetry.csv   t plus every flight-data field, one row per update
  track.geojson   flight path (written when the recording stops)
  track.kml       the same path for Google Earth
  meta.json       start/stop time and summary
t is Unix time in seconds, so video and flight data line up for training
and for placing detections on the map.
"""
import csv
import datetime
import json
import math
import os
import re
import shutil
import threading
import time

EARTH_R = 6371000.0
MIN_STEP_M = 1.0        # add a track point once the drone has moved this far
AUTO_STOP_AFTER = 5.0   # seconds locked before an auto-started recording stops
TAKEOFF_CONFIRM = 2.0   # seconds the motors must stay unlocked to count as a takeoff;
                        # the drone sometimes reports "Unlocked" for a single packet
                        # while the motors lock after landing
NEW_FLIGHT_GAP = 5.0    # seconds locked before a later takeoff counts as a new flight


def valid_fix(t):
    """True when the telemetry holds a usable GPS position."""
    lat, lon = t.get("lat"), t.get("lon")
    return (t.get("gps_fix") is not False and lat is not None and lon is not None
            and not (lat == 0 and lon == 0))


def distance_m(a, b):
    """Great-circle distance in metres between (lat, lon) pairs."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * EARTH_R * math.asin(math.sqrt(h))


class Track(object):
    """GPS flight path: a point every MIN_STEP_M metres, plus the home point.

    Home is where the motors were unlocked for takeoff (where the drone measures
    distance from). Until a takeoff is confirmed it is the first GPS position.
    keep_home=True (recordings) keeps the first takeoff; otherwise a takeoff
    after the motors were locked for NEW_FLIGHT_GAP starts a new home.
    """

    def __init__(self, keep_home=False):
        self.points = []        # (t, lat, lon, alt_m)
        self.home = None
        self.keep_home = keep_home
        self._takeoff_home = False
        self._unlock_t = None   # when the current unlock began
        self._unlock_pos = None
        self._unlock_new = False
        self._locked_t = None   # when the current locked period began
        self.lock = threading.Lock()

    def update(self, tel, t=None):
        if not valid_fix(tel):
            return
        t = time.time() if t is None else t
        pos = (tel["lat"], tel["lon"])
        with self.lock:
            if tel.get("state") == "Unlocked":
                if self._unlock_t is None:
                    # motors just unlocked: remember where, and confirm it later
                    self._unlock_t, self._unlock_pos = t, pos
                    self._unlock_new = (self._locked_t is None
                                        or t - self._locked_t >= NEW_FLIGHT_GAP)
                elif self._unlock_pos and t - self._unlock_t >= TAKEOFF_CONFIRM:
                    if self._unlock_new and not (self.keep_home and self._takeoff_home):
                        self.home, self._takeoff_home = self._unlock_pos, True
                    self._unlock_pos = None
            else:
                if self._unlock_t is not None or self._locked_t is None:
                    self._locked_t = t
                self._unlock_t = None
            if self.home is None:
                self.home = pos
            if not self.points or distance_m(self.points[-1][1:3], pos) >= MIN_STEP_M:
                self.points.append((t, pos[0], pos[1], tel.get("alt_m")))

    def length_m(self):
        with self.lock:
            pts = [p[1:3] for p in self.points]
        return sum(distance_m(a, b) for a, b in zip(pts, pts[1:]))

    def latlon(self, max_points=2000):
        with self.lock:
            pts = self.points[:]
        step = max(1, len(pts) // max_points)
        out = [[p[1], p[2]] for p in pts[::step]]
        if pts and (len(pts) - 1) % step:
            out.append([pts[-1][1], pts[-1][2]])
        return out

    def write(self, folder, name):
        with self.lock:
            pts = self.points[:]
            home = self.home
        if not pts:
            return
        coords = [[p[2], p[1]] + ([p[3]] if p[3] is not None else []) for p in pts]
        start = datetime.datetime.fromtimestamp(pts[0][0]).isoformat(timespec="seconds")
        end = datetime.datetime.fromtimestamp(pts[-1][0]).isoformat(timespec="seconds")
        feats = [{"type": "Feature",
                  "properties": {"name": name, "start": start, "end": end,
                                 "length_m": round(self.length_m(), 1)},
                  "geometry": {"type": "LineString" if len(coords) > 1 else "Point",
                               "coordinates": coords if len(coords) > 1 else coords[0]}}]
        if home:
            feats.append({"type": "Feature", "properties": {"name": "Home"},
                          "geometry": {"type": "Point", "coordinates": [home[1], home[0]]}})
        with open(os.path.join(folder, "track.geojson"), "w") as f:
            json.dump({"type": "FeatureCollection", "features": feats}, f)
        line = " ".join("%.7f,%.7f,%.1f" % (p[2], p[1], p[3] or 0) for p in pts)
        with open(os.path.join(folder, "track.kml"), "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
                    '<name>%s</name><Placemark><name>Flight path</name>'
                    '<Style><LineStyle><color>ff00d0ff</color><width>3</width></LineStyle></Style>'
                    '<LineString><altitudeMode>clampToGround</altitudeMode>'
                    '<coordinates>%s</coordinates></LineString></Placemark>'
                    '</Document></kml>\n' % (name, line))


class Recorder(object):
    """Writes one recording folder. frame() and telemetry() are thread-safe."""

    def __init__(self, root, fields, auto=False):
        self.name = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.folder = os.path.join(root, self.name)
        os.makedirs(self.folder)
        self.fields = fields
        self.auto = auto
        self.start = time.time()
        self.track = Track(keep_home=True)
        self.unlocked_s = 0.0   # time the motors were unlocked during this recording
        self.frames = 0
        self.bytes = 0
        self.lock = threading.Lock()
        self._video = open(os.path.join(self.folder, "video.mjpg"), "wb")
        self._fidx = open(os.path.join(self.folder, "frames.csv"), "w", newline="")
        self._fcsv = csv.writer(self._fidx)
        self._fcsv.writerow(["t", "offset", "size"])
        self._tel = open(os.path.join(self.folder, "telemetry.csv"), "w", newline="")
        self._tcsv = csv.writer(self._tel)
        self._tcsv.writerow(["t"] + list(fields))
        self._last_flush = time.time()
        self.closed = False

    def frame(self, jpeg, t):
        with self.lock:
            if self.closed:
                return
            self._fcsv.writerow(["%.3f" % t, self.bytes, len(jpeg)])
            self._video.write(jpeg)
            self.bytes += len(jpeg)
            self.frames += 1
            self._maybe_flush()

    def telemetry(self, snapshot, t):
        with self.lock:
            if self.closed:
                return
            self._tcsv.writerow(["%.3f" % t] + ["" if snapshot.get(k) is None else snapshot[k]
                                                for k in self.fields])
            self.track.update(snapshot, t)
            self._maybe_flush()

    def _maybe_flush(self):
        # flush every 2 s so a crash or dead battery loses little
        if time.time() - self._last_flush > 2:
            for f in (self._video, self._fidx, self._tel):
                f.flush()
            self._last_flush = time.time()

    def stop(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for f in (self._video, self._fidx, self._tel):
                f.close()
        self.track.write(self.folder, self.name)
        with open(os.path.join(self.folder, "meta.json"), "w") as f:
            json.dump({"name": self.name, "start": self.start, "stop": time.time(),
                       "frames": self.frames, "video_bytes": self.bytes,
                       "track_points": len(self.track.points),
                       "track_length_m": round(self.track.length_m(), 1),
                       "auto_started": self.auto}, f, indent=1)

    def status(self):
        return {"active": not self.closed, "name": self.name, "auto": self.auto,
                "elapsed": round(time.time() - self.start, 1), "frames": self.frames,
                "mb": round(self.bytes / 1e6, 1)}


class FlightLog(object):
    """Starts and stops recordings, by button or automatically on unlock."""

    def __init__(self, root, fields, auto_record=True):
        self.root, self.fields, self.auto_record = root, fields, auto_record
        self.rec = None
        self.last = None        # status of the most recent finished recording
        self._locked_since = None
        self._state = None
        self._last_t = None
        self.lock = threading.Lock()

    def start(self, auto=False):
        with self.lock:
            if not self.rec:
                self.rec = Recorder(self.root, self.fields, auto)
            return self.rec.status()

    def stop(self):
        with self.lock:
            rec, self.rec = self.rec, None
        if rec:
            rec.stop()
            if rec.auto and rec.unlocked_s < TAKEOFF_CONFIRM:
                # started by a one-packet "Unlocked" blip, not a takeoff
                shutil.rmtree(rec.folder, ignore_errors=True)
            else:
                self.last = rec.status()
        return self.last

    def frame(self, jpeg, t):
        rec = self.rec
        if rec:
            rec.frame(jpeg, t)

    def telemetry(self, snapshot, t):
        rec = self.rec
        state = snapshot.get("state")
        if rec:
            rec.telemetry(snapshot, t)
            if state == "Unlocked" and self._state == "Unlocked" and self._last_t is not None:
                rec.unlocked_s += t - self._last_t
        self._last_t = t
        if self.auto_record and state == "Unlocked" and self._state != "Unlocked" and not rec:
            self.start(auto=True)
        # auto-started recordings stop a few seconds after the motors lock again
        if state == "Unlocked":
            self._locked_since = None
        elif self._locked_since is None:
            self._locked_since = t
        elif rec and rec.auto and t - self._locked_since > AUTO_STOP_AFTER:
            self.stop()
        self._state = state

    def status(self):
        rec = self.rec
        return {"recording": rec.status() if rec else None, "last": self.last,
                "auto_record": self.auto_record}


def past_tracks(root, skip=None, max_points=400):
    """Flight paths from earlier recordings, for the coverage layer on the map."""
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name, "track.geojson")
        if name == skip or not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                geo = json.load(f)
        except (OSError, ValueError):
            continue
        for feat in geo.get("features", []):
            if feat["geometry"]["type"] == "LineString":
                c = feat["geometry"]["coordinates"]
                step = max(1, len(c) // max_points)
                out.append([[p[1], p[0]] for p in c[::step]])
    return out


def _parse(v):
    if v == "":
        return None
    if v in ("True", "False"):
        return v == "True"
    try:
        return float(v)
    except ValueError:
        return v


class Replay(threading.Thread):
    """Plays a recording into the viewer as if it were live. Sends nothing."""

    def __init__(self, cam, folder, speed=1.0, loop=False):
        super().__init__(daemon=True)
        self.cam, self.folder, self.speed, self.loop = cam, folder, speed, loop

    def _events(self):
        ev = []
        with open(os.path.join(self.folder, "frames.csv")) as f:
            for row in csv.DictReader(f):
                ev.append((float(row["t"]), 0, (int(row["offset"]), int(row["size"]))))
        with open(os.path.join(self.folder, "telemetry.csv")) as f:
            for row in csv.DictReader(f):
                t = float(row.pop("t"))
                ev.append((t, 1, {k: _parse(v) for k, v in row.items()}))
        ev.sort(key=lambda e: (e[0], e[1]))
        return ev

    def run(self):
        events = self._events()
        if not events:
            self.cam.error = "recording is empty"
            return
        video = open(os.path.join(self.folder, "video.mjpg"), "rb")
        self.cam.connected, self.cam.error = True, ""
        while self.cam.running:
            t0, w0 = events[0][0], time.time()
            count, fps_t = 0, time.time()
            for t, kind, data in events:
                if not self.cam.running:
                    break
                delay = (t - t0) / self.speed - (time.time() - w0)
                if delay > 0:
                    time.sleep(delay)
                if kind == 0:
                    video.seek(data[0])
                    self.cam.publish_frame(video.read(data[1]))
                    count += 1
                    if time.time() - fps_t >= 1:
                        self.cam.fps, count, fps_t = count / (time.time() - fps_t), 0, time.time()
                else:
                    self.cam.set_telemetry(**data)
            if not self.loop:
                break
        video.close()
        self.cam.connected = False
        self.cam.error = "replay finished"


# ---- recordings library (browsing and playback in the viewer) ----------------

def recording_folder(root, name):
    """Folder for a recording name, or None. Names are checked so a request
    can never reach outside the recordings folder."""
    if not re.fullmatch(r"\d{8}_\d{6}", name or ""):
        return None
    folder = os.path.join(root, name)
    return folder if os.path.isfile(os.path.join(folder, "frames.csv")) else None


def list_recordings(root):
    """Newest first, with what the list in the viewer shows."""
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root), reverse=True):
        folder = recording_folder(root, name)
        if not folder:
            continue
        meta = {}
        try:
            with open(os.path.join(folder, "meta.json")) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            pass    # still being recorded, or the viewer was killed
        start = meta.get("start") or time.mktime(time.strptime(name, "%Y%m%d_%H%M%S"))
        out.append({"name": name, "start": start,
                    "duration": round(meta["stop"] - start, 1) if meta.get("stop") else None,
                    "frames": meta.get("frames"), "length_m": meta.get("track_length_m"),
                    "mb": round(os.path.getsize(os.path.join(folder, "video.mjpg")) / 1e6, 1),
                    "complete": bool(meta)})
    return out


def recording_data(folder, max_rate=10.0):
    """Frame times, flight data (at most max_rate rows per second) and the path."""
    with open(os.path.join(folder, "frames.csv")) as f:
        frames = [float(r["t"]) for r in csv.DictReader(f)]
    rows, fields, last = [], [], -1.0
    with open(os.path.join(folder, "telemetry.csv")) as f:
        reader = csv.reader(f)
        header = next(reader, ["t"])
        fields = header[1:]
        for r in reader:
            t = float(r[0])
            if t - last >= 1.0 / max_rate:
                rows.append([t] + [_parse(v) for v in r[1:]])
                last = t
    track, home = [], None
    path = os.path.join(folder, "track.geojson")
    if os.path.exists(path):
        with open(path) as f:
            for feat in json.load(f).get("features", []):
                g = feat["geometry"]
                if g["type"] == "LineString":
                    track = [[c[1], c[0]] for c in g["coordinates"]]
                elif feat["properties"].get("name") == "Home":
                    home = [g["coordinates"][1], g["coordinates"][0]]
    return {"name": os.path.basename(folder), "frames": frames, "fields": fields,
            "rows": rows, "track": track, "home": home}


_index_cache = {}


def frame_bytes(folder, i):
    """JPEG bytes of frame i, read straight out of video.mjpg."""
    idx = _index_cache.get(folder)
    if idx is None:
        with open(os.path.join(folder, "frames.csv")) as f:
            idx = [(int(r["offset"]), int(r["size"])) for r in csv.DictReader(f)]
        if os.path.exists(os.path.join(folder, "meta.json")):
            _index_cache[folder] = idx     # only cache finished recordings
    if not 0 <= i < len(idx):
        return None
    with open(os.path.join(folder, "video.mjpg"), "rb") as f:
        f.seek(idx[i][0])
        return f.read(idx[i][1])


def rebuild_track(folder):
    """Recompute track.geojson/kml and meta from telemetry.csv (after a fix to
    the home or path logic). The video and flight data are not touched."""
    track = Track(keep_home=True)
    with open(os.path.join(folder, "telemetry.csv")) as f:
        for r in csv.DictReader(f):
            t = float(r.pop("t"))
            track.update({k: _parse(v) for k, v in r.items()}, t)
    name = os.path.basename(os.path.normpath(folder))
    track.write(folder, name)
    meta_path = os.path.join(folder, "meta.json")
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
        meta.update(track_points=len(track.points), track_length_m=round(track.length_m(), 1))
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=1)
    return track
