#!/usr/bin/env python3
"""Label drone frames yourself: draw pothole and crack boxes in the browser.

    python3 label_tool.py [dataset/drone] [--port 8768]     then open http://localhost:8768
    (or double-click "Raven 2 Labeller.command")

Works on the folders extract_frames.py makes (dataset/drone/<recording>/).
Every change is saved straight away, per folder:
  _labels.json       everything you did: status (todo / done / skip) and boxes
  _annotations.csv   the finished images only, in the training format
                     (filename,width,height,class,xmin,ymin,xmax,ymax);
                     "done" with no boxes = road with no damage (still useful),
                     "skip" = bad frame, left out of training. The images
                     themselves are never changed or deleted.

Keys: drag = new box · 1 pothole / 2 crack · click a box to select, drag to move,
drag a corner to resize · Delete removes it · Enter/Space = done, next ·
X = skip frame · ←/→ = previous/next · U = next unlabelled · B = brighter ·
Cmd+Z = undo.

Plain Python, local only: nothing is uploaded anywhere.
"""
import argparse
import csv
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from extract_frames import jpeg_size  # noqa: E402

CLASSES = ("pothole", "crack")
SAFE = re.compile(r"[A-Za-z0-9_.\-]+")
LOCK = threading.Lock()


def images(folder):
    return sorted(n for n in os.listdir(folder) if n.lower().endswith((".jpg", ".jpeg", ".png")))


def load(folder):
    """{filename: {"status", "boxes": [[class, x0, y0, x1, y1] in pixels], "w", "h"}}.
    Starts from _labels.json, else from extract_frames' _annotations.csv
    (model guesses, if any, become editable boxes on unlabelled images)."""
    path = os.path.join(folder, "_labels.json")
    labels = {}
    if os.path.exists(path):
        with open(path) as f:
            labels = json.load(f)
    else:
        ann = os.path.join(folder, "_annotations.csv")
        if os.path.exists(ann):
            with open(ann) as f:
                for r in csv.DictReader(f):
                    item = labels.setdefault(r["filename"], {"status": "todo", "boxes": []})
                    if r["class"] in CLASSES:
                        item["boxes"].append([r["class"]] + [int(float(r[k])) for k in ("xmin", "ymin", "xmax", "ymax")])
    for n in images(folder):
        item = labels.setdefault(n, {"status": "todo", "boxes": []})
        if not item.get("w"):
            with open(os.path.join(folder, n), "rb") as f:
                item["w"], item["h"] = jpeg_size(f.read())
    return {n: labels[n] for n in images(folder) if n in labels}


def save(folder, labels):
    def atomic(name, write):
        tmp = os.path.join(folder, name + ".tmp")
        with open(tmp, "w", newline="") as f:
            write(f)
        os.replace(tmp, os.path.join(folder, name))

    atomic("_labels.json", lambda f: json.dump(labels, f, indent=0))

    def rows(f):
        w = csv.writer(f)
        w.writerow(["filename", "width", "height", "class", "xmin", "ymin", "xmax", "ymax"])
        for n, it in labels.items():
            if it["status"] != "done":
                continue
            for c, x0, y0, x1, y1 in it["boxes"]:
                w.writerow([n, it["w"], it["h"], c, x0, y0, x1, y1])
            if not it["boxes"]:
                w.writerow([n, it["w"], it["h"], "", "", "", "", ""])
    atomic("_annotations.csv", rows)


def folders(root):
    return sorted(d for d in os.listdir(root) if SAFE.fullmatch(d) and os.path.isdir(os.path.join(root, d))
                  and images(os.path.join(root, d)))


def clean_boxes(boxes, w, h):
    out = []
    for b in boxes:
        c = b[0]
        x0, x1 = sorted(max(0, min(w, int(v))) for v in (b[1], b[3]))
        y0, y1 = sorted(max(0, min(h, int(v))) for v in (b[2], b[4]))
        if c in CLASSES and x1 - x0 >= 3 and y1 - y0 >= 3:
            out.append([c, x0, y0, x1, y1])
    return out


def make_handler(root):
    cache = {}

    def get(d):
        if d not in cache:
            cache[d] = load(os.path.join(root, d))
        return cache[d]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                return self._send(200, "text/html; charset=utf-8", PAGE.encode())
            if path == "/api/list":
                with LOCK:
                    out = [{"folder": d, "file": n, "status": it["status"], "boxes": it["boxes"],
                            "w": it["w"], "h": it["h"]}
                           for d in folders(root) for n, it in get(d).items()]
                return self._send(200, "application/json", json.dumps(out).encode())
            parts = path.split("/")
            if len(parts) == 4 and parts[1] == "img" and SAFE.fullmatch(parts[2]) and SAFE.fullmatch(parts[3]):
                f = os.path.join(root, parts[2], parts[3])
                if os.path.isfile(f):
                    with open(f, "rb") as fh:
                        return self._send(200, "image/jpeg", fh.read())
            self._send(404, "text/plain", b"not found")

        def do_POST(self):
            if self.path != "/api/save":
                return self._send(404, "text/plain", b"not found")
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(min(n, 1 << 20)))
                d, name = req["folder"], req["file"]
                if not (SAFE.fullmatch(d) and SAFE.fullmatch(name)) or req["status"] not in ("todo", "done", "skip"):
                    raise ValueError("bad request")
                with LOCK:
                    labels = get(d)
                    it = labels[name]
                    it["status"] = req["status"]
                    it["boxes"] = clean_boxes(req["boxes"], it["w"], it["h"])
                    save(os.path.join(root, d), labels)
                return self._send(200, "application/json", json.dumps({"ok": True, "boxes": it["boxes"]}).encode())
            except (KeyError, ValueError, TypeError, IndexError) as e:
                return self._send(400, "application/json", json.dumps({"error": str(e)}).encode())

    return Handler


PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Raven 2 Labeller</title>
<style>
:root{--bg:#0b0d10;--panel:#151a21;--line:#2a3340;--fg:#e6edf3;--dim:#8b98a8;--pot:#ff4d4d;--crk:#ffd23f;--ok:#4ade80;--skip:#64748b}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:13px system-ui,-apple-system,sans-serif}
body{display:flex;flex-direction:column}
#top{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line)}
#top b{font-weight:600}
button{background:#1f2630;color:var(--fg);border:1px solid #2e3744;border-radius:6px;padding:5px 10px;font:inherit;cursor:pointer}
button:hover{background:#2a3340}
button.cls{font-weight:700}
#c1.on{background:var(--pot);color:#111;border-color:var(--pot)}#c2.on{background:var(--crk);color:#111;border-color:var(--crk)}
#done{background:#1d3b2a;border-color:#2f6b47}#skip{color:var(--dim)}
.sp{flex:1}.nav{display:inline-flex;gap:8px;align-items:center;white-space:nowrap}
#name{color:var(--dim);font-variant-numeric:tabular-nums}
#st{font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;border-radius:4px;padding:2px 6px;background:#2a3340;color:var(--dim)}
#st.done{background:var(--ok);color:#111}#st.skip{background:var(--skip);color:#fff}
#stage{flex:1;position:relative;min-height:0;display:flex;align-items:center;justify-content:center;padding:10px}
canvas{max-width:100%;max-height:100%;cursor:crosshair;border-radius:4px}
#strip{display:flex;flex-wrap:wrap;gap:2px;padding:8px 12px;background:var(--panel);border-top:1px solid var(--line);max-height:76px;overflow:auto}
#strip i{width:9px;height:9px;border-radius:2px;background:#2a3340;cursor:pointer;display:block}
#strip i.done{background:var(--ok)}#strip i.skip{background:var(--skip)}#strip i.cur{outline:2px solid #fff;outline-offset:1px}
#strip i.gap{margin-left:8px}
#help{color:var(--dim);font-size:11px;padding:0 12px 8px;background:var(--panel)}
#msg{color:var(--dim);font-size:12px}
</style></head><body>
<div id=top>
  <b>Labeller</b><span id=prog></span>
  <span class=sp></span>
  <button id=c1 class=cls title="Pothole (1)">1 Pothole</button>
  <button id=c2 class=cls title="Crack (2)">2 Crack</button>
  <span class=sp></span>
  <span class=nav><button id=prev title="Previous (←)">◀</button>
  <span id=name></span><span id=st></span>
  <button id=next title="Next (→)">▶</button></span>
  <button id=done title="Finished with this image, go to the next (Enter or Space). With no boxes it counts as road with no damage.">✓ Done</button>
  <button id=skip title="Bad frame (blurry, not road): leave it out of training (X)">Skip frame</button>
  <button id=todo title="Next image not labelled yet (U)">Next unlabelled</button>
</div>
<div id=stage><canvas id=cv></canvas></div>
<div id=help>Drag to draw a box · click a box to select it, drag to move, drag a corner to resize · Delete removes it · 1/2 sets the type · Enter/Space done · X skip · ←/→ move · U next unlabelled · B brighter · ⌘Z undo <span id=msg></span></div>
<div id=strip></div>
<script>
const $=id=>document.getElementById(id), cv=$('cv'), g=cv.getContext('2d');
const COL={pothole:'#ff4d4d',crack:'#ffd23f'};
let items=[], cur=0, cls='pothole', sel=-1, img=new Image(), bright=false, drag=null, hist=[];
function it(){return items[cur];}
async function init(){
  items=await (await fetch('/api/list')).json();
  if(!items.length){$('name').textContent='No images: run extract_frames.py first';return;}
  let saved=0; try{saved=+localStorage.getItem('lab_cur')||0;}catch(e){}
  const first=items.findIndex(x=>x.status==='todo');
  go(saved<items.length&&saved>0?saved:(first<0?0:first));
  strip();
}
function strip(){
  const s=$('strip'); s.innerHTML='';
  items.forEach((x,i)=>{const e=document.createElement('i');e.className=x.status+(i===cur?' cur':'')+(i&&items[i-1].folder!==x.folder?' gap':'');
    e.title=x.folder+' / '+x.file+' · '+x.status+(x.boxes.length?' · '+x.boxes.length+' box'+(x.boxes.length>1?'es':''):'');e.onclick=()=>go(i);s.appendChild(e);});
  const n=k=>items.filter(x=>x.status===k).length, b=c=>items.filter(x=>x.status==='done').reduce((a,x)=>a+x.boxes.filter(y=>y[0]===c).length,0);
  $('prog').textContent=n('done')+' / '+items.length+' done · '+b('pothole')+' potholes · '+b('crack')+' cracks · '+items.filter(x=>x.status==='done'&&!x.boxes.length).length+' no damage · '+n('skip')+' skipped';
}
function go(i){
  if(i<0||i>=items.length)return;
  cur=i; sel=-1; hist=[]; try{localStorage.setItem('lab_cur',i);}catch(e){}
  const x=it(); img=new Image(); img.onload=draw; img.src='/img/'+x.folder+'/'+x.file;
  cv.width=x.w||1280; cv.height=x.h||720;
  $('name').textContent=x.folder+' / '+x.file+'  ('+(i+1)+'/'+items.length+')';
  strip(); draw();
}
function draw(){
  const x=it(); if(!x)return;
  g.filter=bright?'brightness(1.25) contrast(1.5)':'none';
  g.clearRect(0,0,cv.width,cv.height); if(img.complete)g.drawImage(img,0,0,cv.width,cv.height); g.filter='none';
  const lw=Math.max(2,cv.width/500);
  x.boxes.forEach((b,i)=>{const c=COL[b[0]];g.lineWidth=i===sel?lw*1.8:lw;g.strokeStyle=c;g.strokeRect(b[1],b[2],b[3]-b[1],b[4]-b[2]);
    if(i===sel){g.fillStyle=c+'33';g.fillRect(b[1],b[2],b[3]-b[1],b[4]-b[2]);g.fillStyle=c;for(const[px,py]of[[b[1],b[2]],[b[3],b[2]],[b[1],b[4]],[b[3],b[4]]])g.fillRect(px-lw*3,py-lw*3,lw*6,lw*6);}
    g.font='bold '+Math.round(cv.width/80)+'px system-ui';g.fillStyle=c;g.fillText(b[0],b[1]+3,Math.max(b[2]-5,14));});
  if(drag&&drag.mode==='new'){const b=drag.box;g.setLineDash([8,6]);g.lineWidth=lw;g.strokeStyle=COL[cls];g.strokeRect(b[1],b[2],b[3]-b[1],b[4]-b[2]);g.setLineDash([]);}
  $('st').textContent=x.status==='todo'?'not labelled':x.status==='done'?(x.boxes.length?'done':'done · no damage'):'skipped';
  $('st').className=x.status;
  $('c1').classList.toggle('on',cls==='pothole');$('c2').classList.toggle('on',cls==='crack');
}
let saveT=null;
function save(x){
  x=x||it(); const body=JSON.stringify({folder:x.folder,file:x.file,status:x.status,boxes:x.boxes});
  clearTimeout(saveT); saveT=null;
  return fetch('/api/save',{method:'POST',body}).then(r=>r.json()).then(r=>{if(r.error)throw r.error;$('msg').textContent='· saved';})
    .catch(e=>{$('msg').textContent='· NOT SAVED: '+e;});
}
function changed(){hist.length>100&&hist.shift(); clearTimeout(saveT); saveT=setTimeout(save,300); strip(); draw();}
function remember(){hist.push(JSON.stringify(it().boxes));}
// the image is captured before moving on, so quick key presses never land on the wrong one
function finish(s){const x=it(); x.status=s; save(x); if(cur<items.length-1)go(cur+1); else{strip();draw();}}
function move(d){if(saveT)save(it()); go(cur+d);}
function pos(e){const r=cv.getBoundingClientRect();return[(e.clientX-r.left)*cv.width/r.width,(e.clientY-r.top)*cv.height/r.height];}
function hit(p){
  const x=it(), k=cv.width/cv.getBoundingClientRect().width*10;
  if(sel>=0){const b=x.boxes[sel];
    for(const[cx,cy,ix,iy]of[[1,2,1,2],[3,2,3,2],[1,4,1,4],[3,4,3,4]])if(Math.abs(p[0]-b[cx])<k&&Math.abs(p[1]-b[cy])<k)return{i:sel,mode:'corner',ix,iy};}
  for(let i=x.boxes.length-1;i>=0;i--){const b=x.boxes[i];if(p[0]>=b[1]&&p[0]<=b[3]&&p[1]>=b[2]&&p[1]<=b[4])return{i,mode:'move'};}
  return null;
}
cv.onmousedown=e=>{
  const p=pos(e), h=hit(p), x=it();
  if(h){sel=h.i; remember(); drag={mode:h.mode,i:h.i,ix:h.ix,iy:h.iy,start:p,orig:x.boxes[h.i].slice(),moved:false};}
  else{sel=-1; drag={mode:'new',start:p,box:[cls,p[0],p[1],p[0],p[1]]};}
  draw();
};
window.onmousemove=e=>{
  if(!drag)return; const p=pos(e), x=it();
  if(drag.mode==='new'){drag.box=[cls,Math.min(drag.start[0],p[0]),Math.min(drag.start[1],p[1]),Math.max(drag.start[0],p[0]),Math.max(drag.start[1],p[1])];}
  else{const b=x.boxes[drag.i], o=drag.orig, dx=p[0]-drag.start[0], dy=p[1]-drag.start[1]; drag.moved=true;
    if(drag.mode==='move'){b[1]=o[1]+dx;b[2]=o[2]+dy;b[3]=o[3]+dx;b[4]=o[4]+dy;}
    else{b[drag.ix]=o[drag.ix]+dx;b[drag.iy]=o[drag.iy]+dy;}}
  draw();
};
window.onmouseup=()=>{
  if(!drag)return; const x=it();
  if(drag.mode==='new'){const b=drag.box.map((v,i)=>i?Math.round(v):v);
    if(b[3]-b[1]>=6&&b[4]-b[2]>=6){remember();x.boxes.push(b);sel=x.boxes.length-1;drag=null;return changed();}}
  else if(drag.moved){const b=x.boxes[drag.i];x.boxes[drag.i]=[b[0],Math.round(Math.min(b[1],b[3])),Math.round(Math.min(b[2],b[4])),Math.round(Math.max(b[1],b[3])),Math.round(Math.max(b[2],b[4]))];drag=null;return changed();}
  else hist.pop();
  drag=null; draw();
};
function setCls(c){cls=c; const x=it(); if(sel>=0&&x.boxes[sel][0]!==c){remember();x.boxes[sel][0]=c;return changed();} draw();}
document.onkeydown=e=>{
  const x=it(); if(!x)return;
  if((e.metaKey||e.ctrlKey)&&e.key==='z'){e.preventDefault();if(hist.length){x.boxes=JSON.parse(hist.pop());sel=-1;changed();}return;}
  if(e.metaKey||e.ctrlKey||e.altKey)return;
  const k=e.key;
  if(k==='1')setCls('pothole'); else if(k==='2')setCls('crack');
  else if((k==='Delete'||k==='Backspace')&&sel>=0){e.preventDefault();remember();x.boxes.splice(sel,1);sel=-1;changed();}
  else if(k==='Enter'||k===' '){e.preventDefault();finish('done');}
  else if(k==='x'||k==='X')finish('skip');
  else if(k==='ArrowRight')move(1); else if(k==='ArrowLeft')move(-1);
  else if(k==='u'||k==='U')nextTodo();
  else if(k==='b'||k==='B'){bright=!bright;draw();}
  else if(k==='Escape'){sel=-1;draw();}
};
function nextTodo(){for(let j=1;j<=items.length;j++){const i=(cur+j)%items.length;if(items[i].status==='todo')return move(i-cur);} $('msg').textContent='· everything is labelled';}
$('c1').onclick=()=>setCls('pothole');$('c2').onclick=()=>setCls('crack');
$('prev').onclick=()=>move(-1);$('next').onclick=()=>move(1);
$('done').onclick=()=>finish('done');$('skip').onclick=()=>finish('skip');$('todo').onclick=nextTodo;
window.onbeforeunload=()=>{if(saveT){save();}};
init();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", nargs="?", default=os.path.join(HERE, "dataset", "drone"))
    ap.add_argument("--port", type=int, default=8768)
    args = ap.parse_args()
    if not os.path.isdir(args.root):
        sys.exit("No %s yet: run extract_frames.py on your recordings first." % args.root)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(os.path.abspath(args.root)))
    print("Labelling %s\nOpen http://localhost:%d  (Ctrl+C to stop; every change is already saved)"
          % (args.root, args.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
