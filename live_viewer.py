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
"""
import argparse
import json
import math
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

HTTP_PORT = 8765
TELEMETRY_FIELDS = ("battery_pct", "battery_v", "alt_m", "dist_m",
                    "hspeed_ms", "vspeed_ms", "heading_deg", "lat", "lon",
                    "sats", "gps_fix", "roll_deg", "pitch_deg", "yaw_deg",
                    "state")
BATTERY_WINDOW = 5   # seconds; battery % shown is the lowest raw value in this window


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
                    with self.cond:
                        self.frame = bytes(frame)
                        self.seq += 1
                        self.cond.notify_all()
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

    def set_telemetry(self, **values):
        self.telemetry.update((k, v) for k, v in values.items() if k in TELEMETRY_FIELDS)

    def status(self):
        return {"connected": self.connected, "error": self.error,
                "fps": round(self.fps, 1), "frames": self.seq,
                "demo": self.demo, "telemetry": self.telemetry,
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
                self.cam.tel_time = time.time()

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
        cam.tel_time = time.time()
        time.sleep(0.2)


PAGE = """<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Raven 2 Live</title>
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
#nodata{top:44px;left:10px;color:var(--dim);font-size:11px}
#state{font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;border-radius:4px;padding:1px 5px;background:#2a3340;color:var(--dim)}
#state.armed{background:var(--bad);color:#fff}#state.landing{background:var(--warn);color:#111}
#wrap.stale .hud .v,#wrap.stale #tr{opacity:.45}
/* controls appear on mouse move */
#ctl{bottom:10px;right:10px;display:flex;gap:6px;opacity:0;transition:opacity .25s}
#wrap.active #ctl{opacity:1}
button{background:#1f2630;color:var(--fg);border:1px solid #2e3744;border-radius:6px;padding:5px 9px;font:inherit;font-size:12px;cursor:pointer}
button:hover{background:#2a3340}
@media (max-width:560px){#tr{top:auto;bottom:62px;right:10px}#att{bottom:62px}.cell{padding:1px 5px}.cell .v{font-size:13px}.k{font-size:9px}#fps{display:none}}
</style></head><body>
<div id=wrap>
  <img id=video src="/stream.mjpg" alt="Live drone camera">
  <div id=empty>Waiting for the drone camera…</div>
  <div class=hud id=tl>
    <span id=link title="Camera link"><span class=dot></span></span><span id=fps></span>
    <span id=bat class=na title="Battery"><span id=batbar><div id=batfill></div></span><span class=v id=batv>—</span></span>
    <span id=state title="Motor state" hidden></span>
    <span id=demo title="Simulated values, not from the drone" hidden>DEMO</span>
  </div>
  <div class=hud id=nodata>No flight data from the drone yet</div>
  <div class=hud id=tr><span class=k>GPS <span id=sats></span></span><br><span id=lat>—</span><br><span id=lon>—</span></div>
  <div class=hud id=strip>
    <div class=cell id=c_alt><div class=k>Alt</div><span class=v id=alt>—</span></div>
    <div class=cell id=c_dist><div class=k>Dist</div><span class=v id=dist>—</span></div>
    <div class=cell id=c_hs><div class=k>H.Spd</div><span class=v id=hs>—</span></div>
    <div class=cell id=c_vs><div class=k>V.Spd</div><span class=v id=vs>—</span></div>
    <div class=cell id=c_hdg><div class=k>Hdg</div><span class=v id=hdg>—</span></div>
  </div>
  <div class=hud id=att><span class=k>R</span> <span class=v id=roll>—</span><span class=k>P</span> <span class=v id=pitch>—</span><span class=k>Y</span> <span class=v id=yaw>—</span></div>
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
  $('demo').hidden=!s.demo;
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
  $('lat').textContent=t.lat==null?'—':Math.abs(t.lat).toFixed(6)+'° '+(t.lat>=0?'N':'S');
  $('lon').textContent=t.lon==null?'—':Math.abs(t.lon).toFixed(6)+'° '+(t.lon>=0?'E':'W');
  $('sats').textContent=t.sats==null?'':'· '+t.sats+' sats'+(t.gps_fix===false?' · no fix':'');
  setv('roll',deg(t.roll_deg));setv('pitch',deg(t.pitch_deg));setv('yaw',deg(t.yaw_deg));
}
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

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            elif path == "/status":
                self._send(200, "application/json", json.dumps(cam.status()).encode())
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

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Raven 2 live viewer")
    ap.add_argument("ip", nargs="?", default=DRONE_IP)
    ap.add_argument("--demo", action="store_true",
                    help="fill the overlay with simulated values (marked DEMO)")
    ap.add_argument("--port", type=int, default=HTTP_PORT, help="local web page port")
    ap.add_argument("--no-telemetry", action="store_true",
                    help="video only; do not request flight data")
    args = ap.parse_args()
    ip = args.ip
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
        with cam.cond:
            cam.cond.notify_all()
        server.server_close()
        print("\nStopped. Sent stop stream to the camera.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
