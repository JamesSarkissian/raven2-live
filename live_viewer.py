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
import signal
import socket
import struct
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lw_unscramble import unscramble
from stream_test import (CMD_HEARTBEAT, CMD_RET_STREAM, DRONE_IP, HDR_LEN,
                         HEARTBEAT, HEARTBEAT_EVERY, MAGIC, START, STOP,
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
RECORDINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")


class Camera(threading.Thread):
    """Keeps one stream connection to the drone and holds the latest frame."""

    def __init__(self, ip):
        super().__init__(daemon=True)
        self.ip = ip
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
        self._sock = None

    def run(self):
        while self.running:
            try:
                self._stream()
            except (OSError, ConnectionError, ValueError) as e:
                self.error = str(e)
            self.connected = False
            if self.running:
                time.sleep(2)   # drone out of range or rebooting; retry

    def _stream(self):
        self._sock = sock = socket.create_connection((self.ip, VIDEO_PORT), timeout=5)
        try:
            sock.sendall(START)
            self.connected, self.error = True, ""
            last_hb = t0 = time.time()
            count = 0
            while self.running:
                hdr = recv_exact(sock, HDR_LEN)
                if hdr[:len(MAGIC)] != MAGIC:
                    raise ValueError("lost sync with camera stream")
                ints = struct.unpack_from("<9I", hdr, len(MAGIC))
                body = recv_exact(sock, ints[3])
                if ints[0] == CMD_RET_STREAM and len(body) >= 32:
                    _, size, _, _ = struct.unpack_from("<LLQL", body)
                    frame = bytearray(body[32:32 + size])
                    if ints[4] == 129:
                        unscramble(frame, ints[5], ints[6])
                    self.publish_frame(bytes(frame))
                    count += 1
                    if time.time() - t0 >= 1:
                        self.fps, count, t0 = count / (time.time() - t0), 0, time.time()
                elif ints[0] != CMD_HEARTBEAT:
                    pass    # unknown message types are ignored
                if time.time() - last_hb >= HEARTBEAT_EVERY:
                    sock.sendall(HEARTBEAT)
                    last_hb = time.time()
            sock.sendall(STOP)
        finally:
            sock.close()

    def stop(self):
        self.running = False
        sock = self._sock
        if sock and self.connected:
            try:
                sock.sendall(STOP)
            except OSError:
                pass

    def publish_frame(self, jpeg):
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
            self.log.telemetry(snapshot, now)

    def map_data(self):
        t = self.telemetry
        log = self.log
        current = log.rec.name if log and log.rec else None
        return {"home": self.track.home, "track": self.track.latlon(),
                "past": past_tracks(RECORDINGS, skip=current) if not self.replay else [],
                "drone": {"lat": t["lat"], "lon": t["lon"], "heading": t["heading_deg"]}
                if valid_fix(t) else None}

    def status(self):
        return {"connected": self.connected, "error": self.error,
                "fps": round(self.fps, 1), "frames": self.seq,
                "demo": self.demo, "telemetry": self.telemetry, "replay": self.replay,
                "log": self.log.status() if self.log else None,
                "telemetry_age": round(time.time() - self.tel_time, 1) if self.tel_time else None}


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
.on .dot{background:var(--ok)}
#fps{color:var(--dim);font-size:11px}
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
/* controls appear on mouse move */
#ctl{bottom:10px;right:10px;display:flex;gap:6px;opacity:0;transition:opacity .25s}
#wrap.active #ctl{opacity:1}
button{background:#1f2630;color:var(--fg);border:1px solid #2e3744;border-radius:6px;padding:5px 9px;font:inherit;font-size:12px;cursor:pointer}
button:hover{background:#2a3340}
@media (max-width:560px){#pflist{width:220px}#map{top:40px}#tr{top:auto;bottom:62px;right:10px}#att{bottom:62px}.cell{padding:1px 5px}.cell .v{font-size:13px}.k{font-size:9px}#fps{display:none}}
</style></head><body>
<div id=wrap>
  <img id=video src="/stream.mjpg" alt="Live drone camera">
  <div id=empty>Waiting for the drone camera…</div>
  <div class=hud id=tl>
    <span id=link title="Camera link"><span class=dot></span></span><span id=fps></span>
    <span id=bat class=na title="Battery"><span id=batbar><div id=batfill></div></span><span class=v id=batv>—</span></span>
    <span id=state title="Motor state" hidden></span>
    <button id=rec title="Record video, flight data and GPS path (R)" hidden>REC</button>
    <a id=recs href="/player" target=_blank rel=noopener title="Browse and play past recordings">Recordings</a>
    <span id=demo title="Simulated values, not from the drone" hidden>DEMO</span>
  </div>
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
  <div class=hud id=ctl><button id=units>ft</button><button id=snap>Snapshot</button><button id=full>Full screen</button></div>
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
  $('link').className=s.connected?'on':'';
  $('link').title=s.connected?'Camera connected':(s.error||'Not connected');
  $('fps').textContent=s.connected?s.fps+' fps':(s.error?'reconnecting…':'offline');
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
  b.textContent=r?'REC '+mmss(r.elapsed):'REC';
  b.title=r?('Recording '+r.name+(r.auto?' (started on unlock)':'')+' · '+r.mb+' MB · click to stop')
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
    lml.past=L.layerGroup().addTo(m);lml.track=L.polyline([],{color:'#38bdf8',weight:3}).addTo(m);
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
$('snap').onclick=()=>{const a=document.createElement('a');a.href='/frame.jpg?'+Date.now();
  a.download='raven2_'+new Date().toISOString().replace(/[:.]/g,'-')+'.jpg';a.click();};
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
    print("  start stream, once:   ", START.hex(" "))
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

    cam = Camera(ip)
    if not args.demo:
        cam.log = FlightLog(RECORDINGS, TELEMETRY_FIELDS,
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
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
        cam.running = False
        with cam.cond:
            cam.cond.notify_all()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
