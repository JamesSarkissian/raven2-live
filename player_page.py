"""Recordings page for the Raven 2 viewer: list of flights, video player,
flight data and a satellite/street map that follows the drone in time.

Map tiles: Esri World Imagery (satellite) and OpenStreetMap (streets), via
Leaflet. They need internet, so this works best on your normal Wi-Fi; without
it the flight path is drawn on a plain background instead.
"""

PLAYER_PAGE = r"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Raven 2 Recordings</title>
<link rel=stylesheet href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>
:root{--bg:#0b0d10;--panel:#141920;--line:#232b36;--fg:#eef1f5;--dim:#9aa4b2;--acc:#38bdf8;--ok:#3ddc84;--warn:#ffb020;--bad:#ff5c5c}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:13px/1.35 -apple-system,system-ui,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--line)}
header h1{font-size:15px;margin:0;font-weight:650}
header a{color:var(--acc);text-decoration:none;margin-left:auto;font-size:12px}
#app{display:grid;grid-template-columns:250px 1fr;height:calc(100vh - 43px)}
aside{border-right:1px solid var(--line);overflow:auto}
.rec{padding:10px 14px;border-bottom:1px solid var(--line);cursor:pointer}
.rec:hover{background:#111720}.rec.sel{background:#16202c;box-shadow:inset 3px 0 var(--acc)}
.rec .d{font-weight:600}.rec .m{color:var(--dim);font-size:12px;margin-top:2px}
.empty{padding:16px;color:var(--dim)}
main{display:flex;flex-direction:column;min-width:0;overflow:auto}
#stage{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:10px;padding:10px}
#videoBox,#mapBox{position:relative;background:#000;border-radius:8px;overflow:hidden;min-height:220px}
#videoBox{aspect-ratio:16/9;min-height:0}
#frame:not([src]){visibility:hidden}
#frame{width:100%;height:100%;object-fit:contain;display:block}
#boxes{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
#boxbtn:disabled{opacity:.45;cursor:default}
.rec .m .dt{color:#ffb4b4}
#gap{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);background:rgba(0,0,0,.65);color:var(--dim);padding:6px 10px;border-radius:6px;font-size:12px}
#chip{position:absolute;top:8px;left:8px;font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;border-radius:4px;padding:1px 6px;background:#2a3340;color:var(--dim)}
#chip.armed{background:var(--bad);color:#fff}#chip.landing{background:var(--warn);color:#111}
#lmap{position:absolute;inset:0;background:#1a1f27}
#mapctl{position:absolute;right:8px;bottom:8px;z-index:500;display:flex;gap:6px}
#mapnote{position:absolute;left:8px;top:8px;z-index:500;color:var(--dim);font-size:11px;background:rgba(0,0,0,.6);padding:3px 7px;border-radius:5px}
button,select,a.btn{white-space:nowrap;background:#1f2630;color:var(--fg);border:1px solid #2e3744;border-radius:6px;padding:5px 9px;font:inherit;font-size:12px;cursor:pointer;text-decoration:none;display:inline-block}
button:hover,a.btn:hover{background:#2a3340}
button.on{background:#24415a;border-color:var(--acc)}
#stats{display:flex;flex-wrap:wrap;gap:4px 18px;padding:2px 14px 6px}
.st .k{color:var(--dim);font-size:10px;letter-spacing:.06em;text-transform:uppercase}
.st .v{font-variant-numeric:tabular-nums;font-weight:650;font-size:15px}
.st .u{color:var(--dim);font-weight:400;font-size:11px;margin-left:1px}
#timeline{display:flex;align-items:center;gap:10px;padding:8px 14px 14px;flex-wrap:wrap}
#play{width:64px}
#seek{flex:1;min-width:160px;accent-color:var(--acc)}
#time{font-variant-numeric:tabular-nums;color:var(--dim);min-width:92px}
.arrow{width:0;height:0;border-left:8px solid transparent;border-right:8px solid transparent;border-bottom:20px solid #fff;filter:drop-shadow(0 0 2px #000);transform-origin:50% 60%}
.home{background:var(--warn);color:#111;font:700 10px system-ui;border-radius:50%;width:18px!important;height:18px!important;line-height:18px;text-align:center;border:2px solid #111}
.leaflet-container{font:inherit;background:#1a1f27}
@media (max-width:900px){#app{grid-template-columns:1fr;grid-template-rows:auto 1fr;height:auto}
 aside{display:flex;overflow-x:auto;border-right:0;border-bottom:1px solid var(--line)}
 .rec{flex:none;border-bottom:0;border-right:1px solid var(--line)}
 #stage{grid-template-columns:1fr}#videoBox{aspect-ratio:16/9;min-height:0}#mapBox{height:300px}}
</style></head><body>
<header><h1>Raven 2 Recordings</h1><a id=live href="/" hidden>← Live view</a></header>
<div id=app>
  <aside id=list><div class=empty>Loading…</div></aside>
  <main>
    <div id=stage>
      <div id=videoBox><img id=frame alt="Recorded drone camera"><canvas id=boxes></canvas><div id=gap hidden>No video here (signal gap)</div><span id=chip hidden></span></div>
      <div id=mapBox><div id=lmap></div><div id=mapnote hidden></div>
        <div id=mapctl><button id=sat class=on>Satellite</button><button id=street>Streets</button><button id=follow class=on title="Keep the drone in view">Follow</button><a class=btn id=gmaps target=_blank rel=noopener>Google Maps</a></div></div>
    </div>
    <div id=stats></div>
    <div id=timeline>
      <button id=play>▶ Play</button>
      <input id=seek type=range min=0 max=1000 value=0 aria-label="Position in recording">
      <span id=time>0:00 / 0:00</span>
      <select id=speed aria-label="Playback speed"><option value=.5>0.5×</option><option value=1 selected>1×</option><option value=2>2×</option><option value=4>4×</option></select>
      <button id=units>ft</button>
      <button id=boxbtn title="Show the pothole and crack boxes saved while DETECT was on (B)">Boxes</button>
      <a class=btn id=kml>KML</a><a class=btn id=geo>GeoJSON</a>
    </div>
  </main>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
const $=id=>document.getElementById(id);
let imperial=false;try{imperial=localStorage.getItem('units')==='ft'}catch(e){}
const DIRS=['N','NE','E','SE','S','SW','W','NW'];
const fmtT=x=>Math.floor(x/60)+':'+String(Math.floor(x%60)).padStart(2,'0');
const num=(v,d)=>v==null?'—':(+v).toFixed(d);
const len=v=>v==null?'—':imperial?num(v*3.28084,0)+'<span class=u>ft</span>':num(v,1)+'<span class=u>m</span>';
const spd=v=>v==null?'—':imperial?num(v*2.23694,1)+'<span class=u>mph</span>':num(v,1)+'<span class=u>m/s</span>';
let rec=null, D=null, t0=0, T=0, cur=0, playing=false, speed=1, shownFrame=-1, loading=false, wantFrame=-1;
// ---- list -------------------------------------------------------------------
async function loadList(){
  const r=await (await fetch('/recordings',{cache:'no-store'})).json();
  $('live').hidden=!r.live;
  const list=$('list'); list.textContent='';
  if(!r.recordings.length){list.innerHTML='<div class=empty>No recordings yet. Press REC in the live view, or unlock the motors.</div>';return;}
  for(const x of r.recordings){
    const el=document.createElement('div'); el.className='rec'; el.dataset.name=x.name;
    const d=new Date(x.start*1000);
    el.innerHTML='<div class=d></div><div class=m></div>';
    el.children[0].textContent=d.toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'})+', '+d.toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});
    el.children[1].textContent=[x.duration!=null?fmtT(x.duration):'recording…',x.length_m!=null?(imperial?Math.round(x.length_m*3.28084)+' ft':Math.round(x.length_m)+' m'):null,x.mb+' MB'].filter(Boolean).join(' · ');
    const dc=x.detections||{}, dn=[dc.pothole&&dc.pothole+' pothole box'+(dc.pothole>1?'es':''),dc.crack&&dc.crack+' crack box'+(dc.crack>1?'es':'')].filter(Boolean);
    if(dn.length){const s=document.createElement('div');s.className='m dt';s.textContent=dn.join(' · ');el.appendChild(s);}
    el.onclick=()=>open_(x.name);
    list.appendChild(el);
  }
  const want=new URLSearchParams(location.search).get('name');
  open_(want&&r.recordings.some(x=>x.name===want)?want:r.recordings[0].name);
}
// ---- open a recording -------------------------------------------------------
async function open_(name){
  pause();
  document.querySelectorAll('.rec').forEach(e=>e.classList.toggle('sel',e.dataset.name===name));
  history.replaceState(null,'','?name='+name);
  D=await (await fetch('/rec/'+name+'/data',{cache:'no-store'})).json(); rec=name;
  const starts=[D.frames[0],D.rows.length?D.rows[0][0]:undefined].filter(x=>x!=null);
  const ends=[D.frames[D.frames.length-1],D.rows.length?D.rows[D.rows.length-1][0]:undefined].filter(x=>x!=null);
  t0=Math.min(...starts); T=Math.max(...ends)-t0; cur=0; shownFrame=-1;
  D.col={}; D.fields.forEach((f,i)=>D.col[f]=i+1);
  $('kml').href='/rec/'+name+'/track.kml'; $('geo').href='/rec/'+name+'/track.geojson';
  boxRender(); mapLoad(); render();
}
// last index with arr[i] <= x (arr sorted), using key(i)
function idxAt(n,key,x){let lo=0,hi=n-1,a=-1;while(lo<=hi){const m=(lo+hi)>>1;if(key(m)<=x){a=m;lo=m+1}else hi=m-1}return a;}
function row(){if(!D||!D.rows.length)return null;const i=idxAt(D.rows.length,i=>D.rows[i][0],t0+cur);if(i<0)return null;const r=D.rows[i],o={};for(const f in D.col)o[f]=r[D.col[f]];o.t=r[0];return o;}
// ---- video: load one frame at a time so playback never queues up ------------
function showFrame(i){
  wantFrame=i; if(loading||i===shownFrame||i<0)return;
  loading=true; const img=new Image(), n=i;
  img.onload=img.onerror=()=>{loading=false;if(img.complete&&img.naturalWidth){$('frame').src=img.src;shownFrame=n;drawBoxes();}if(wantFrame!==shownFrame)showFrame(wantFrame);};
  img.src='/rec/'+rec+'/frame/'+n+'.jpg';
}
function render(){
  if(!D)return;
  const fi=idxAt(D.frames.length,i=>D.frames[i],t0+cur); showFrame(fi);
  $('gap').hidden=!(fi<0||t0+cur-D.frames[fi]>1.5);
  const r=row()||{};
  const fix=r.lat!=null&&!(r.lat===0&&r.lon===0)&&r.gps_fix!==false;
  const hdg=r.heading_deg;
  const vs=r.vspeed_ms==null?'—':(r.vspeed_ms>0.05?'↑':r.vspeed_ms<-0.05?'↓':'')+spd(Math.abs(r.vspeed_ms));
  const cells=[['Battery',r.battery_pct==null?'—':num(r.battery_pct,0)+'<span class=u>%</span>'],['Alt',len(r.alt_m)],['Dist',len(r.dist_m)],
    ['H.Spd',spd(r.hspeed_ms)],['V.Spd',vs],['Hdg',hdg==null?'—':num(hdg,0)+'° <span class=u>'+DIRS[Math.round(((hdg%360)+360)%360/45)%8]+'</span>'],
    ['Sats',r.sats==null?'—':num(r.sats,0)],['R / P / Y',r.roll_deg==null?'—':num(r.roll_deg,0)+'° '+num(r.pitch_deg,0)+'° '+num(r.yaw_deg,0)+'°'],
    ['Position',fix?Math.abs(r.lat).toFixed(6)+'° '+(r.lat>=0?'N':'S')+', '+Math.abs(r.lon).toFixed(6)+'° '+(r.lon>=0?'E':'W'):'—']];
  $('stats').innerHTML=cells.map(c=>'<div class=st><div class=k>'+c[0]+'</div><div class=v>'+c[1]+'</div></div>').join('');
  $('chip').hidden=r.state==null; $('chip').textContent=r.state||''; $('chip').className=r.state==='Unlocked'?'armed':r.state==='Landing'?'landing':'';
  $('time').textContent=fmtT(cur)+' / '+fmtT(T);
  if(!seeking)$('seek').value=T?Math.round(cur/T*1000):0;
  $('gmaps').href=fix?'https://www.google.com/maps/search/?api=1&query='+r.lat+','+r.lon:'https://www.google.com/maps';
  mapUpdate(fix?[r.lat,r.lon]:null,hdg);
}
// ---- saved detection boxes (DETECT was on while recording) -------------------
let showBoxes=true; try{showBoxes=localStorage.getItem('boxes')!=='off'}catch(e){}
const BOXC={pothole:'#ff4d4d',crack:'#ffd23f'};
function boxRender(){
  const has=!!(D&&D.dets&&D.dets.length), b=$('boxbtn');
  b.disabled=!has; b.classList.toggle('on',has&&showBoxes);
  b.textContent=has?(showBoxes?'Boxes on':'Boxes off'):'No boxes';
  b.title=has?'Pothole and crack boxes saved while DETECT was on · click to '+(showBoxes?'hide':'show')+' (B)'
             :'No boxes saved: DETECT was off while this was recorded';
  drawBoxes();
}
function drawBoxes(){
  const c=$('boxes'), f=$('frame'), r=c.getBoundingClientRect(), k=devicePixelRatio||1;
  c.width=Math.round(r.width*k); c.height=Math.round(r.height*k);
  const g=c.getContext('2d'); g.clearRect(0,0,c.width,c.height);
  if(!showBoxes||!D||!D.dets||!D.dets.length||shownFrame<0||!f.naturalWidth)return;
  // the result for the frame on screen: the model ran on most frames, not all
  const ft=D.frames[shownFrame], i=idxAt(D.dets.length,j=>D.dets[j][0],ft+0.01);
  if(i<0||ft-D.dets[i][0]>0.3)return;
  // where the picture sits inside the box (object-fit: contain)
  const s=Math.min(c.width/f.naturalWidth,c.height/f.naturalHeight), w=f.naturalWidth*s, h=f.naturalHeight*s, ox=(c.width-w)/2, oy=(c.height-h)/2;
  g.lineWidth=2*k; g.font='600 '+11*k+'px system-ui';
  for(const[cls,score,x0,y0,x1,y1]of D.dets[i][1]){
    const col=BOXC[cls]||'#fff', X=ox+x0*w, Y=oy+y0*h;
    g.strokeStyle=col; g.strokeRect(X,Y,(x1-x0)*w,(y1-y0)*h);
    const t=cls+' '+Math.round(score*100)+'%', tw=g.measureText(t).width+6*k;
    g.fillStyle=col; g.fillRect(X,Math.max(0,Y-15*k),tw,15*k); g.fillStyle='#111'; g.fillText(t,X+3*k,Math.max(0,Y-15*k)+11.5*k);
  }
}
$('boxbtn').onclick=()=>{showBoxes=!showBoxes;try{localStorage.setItem('boxes',showBoxes?'on':'off')}catch(e){}boxRender();};
window.addEventListener('resize',drawBoxes);
// ---- playback ---------------------------------------------------------------
let last=0, seeking=false;
function tick(now){if(!playing)return;cur+=(now-last)/1000*speed;last=now;if(cur>=T){cur=T;pause();}render();requestAnimationFrame(tick);}
function play(){if(!D)return;if(cur>=T)cur=0;playing=true;$('play').textContent='❚❚ Pause';last=performance.now();requestAnimationFrame(tick);}
function pause(){playing=false;$('play').textContent='▶ Play';}
$('play').onclick=()=>playing?pause():play();
$('seek').oninput=()=>{seeking=true;cur=$('seek').value/1000*T;render();};
$('seek').onchange=()=>{seeking=false;};
$('speed').onchange=()=>{speed=+$('speed').value;};
$('units').textContent=imperial?'m':'ft';
$('units').onclick=()=>{imperial=!imperial;$('units').textContent=imperial?'m':'ft';try{localStorage.setItem('units',imperial?'ft':'m')}catch(e){}render();};
document.addEventListener('keydown',e=>{if(e.target.tagName==='INPUT'&&e.target.type!=='range')return;
  if(e.key===' '){e.preventDefault();playing?pause():play();}
  if((e.key==='b'||e.key==='B')&&!$('boxbtn').disabled)$('boxbtn').click();
  if(e.key==='ArrowRight'){cur=Math.min(T,cur+5);render();} if(e.key==='ArrowLeft'){cur=Math.max(0,cur-5);render();}});
// ---- map: satellite/streets with internet, plain path without ---------------
let map=null, layers={}, full=null, done=null, home=null, drone=null, follow=true, fallback=!window.L;
if(!fallback){
  map=L.map('lmap',{zoomControl:true,attributionControl:true}).setView([0,0],2);
  layers.sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:21,maxNativeZoom:19,attribution:'Imagery © Esri'});
  layers.street=L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:21,maxNativeZoom:19,attribution:'© OpenStreetMap contributors'});
  layers.sat.addTo(map);
  map.on('dragstart',()=>{follow=false;$('follow').classList.remove('on');});
} else {
  $('mapnote').hidden=false; $('mapnote').textContent='Map tiles need internet: showing the path only';
  $('sat').hidden=$('street').hidden=true;
}
function setLayer(k){if(!map)return;for(const n in layers)map.removeLayer(layers[n]);layers[k].addTo(map);$('sat').classList.toggle('on',k==='sat');$('street').classList.toggle('on',k==='street');}
$('sat').onclick=()=>setLayer('sat'); $('street').onclick=()=>setLayer('street');
$('follow').onclick=()=>{follow=!follow;$('follow').classList.toggle('on',follow);render();};
function mapLoad(){
  if(fallback){drawPlain(null,null);return;}
  [full,done,home,drone].forEach(x=>x&&map.removeLayer(x));
  full=L.polyline(D.track,{color:'#9aa4b2',weight:3,opacity:.7}).addTo(map);
  done=L.polyline([],{color:'#38bdf8',weight:4}).addTo(map);
  home=D.home?L.marker(D.home,{icon:L.divIcon({className:'home',html:'H'})}).addTo(map):null;
  drone=L.marker(D.track[0]||[0,0],{icon:L.divIcon({className:'',html:'<div class=arrow></div>',iconSize:[16,20],iconAnchor:[8,12]})});
  if(D.track.length>1)map.fitBounds(full.getBounds(),{padding:[30,30],maxZoom:19});
  else if(D.track.length)map.setView(D.track[0],19);
}
function mapUpdate(pos,hdg){
  if(fallback){drawPlain(pos,hdg);return;}
  if(!pos){if(map.hasLayer(drone))map.removeLayer(drone);return;}
  if(!map.hasLayer(drone))drone.addTo(map);
  drone.setLatLng(pos); const a=drone.getElement(); if(a)a.firstChild.style.transform='rotate('+(hdg||0)+'deg)';
  // path flown so far = track points up to the current position
  let k=0,best=1e18; D.track.forEach((p,i)=>{const d=(p[0]-pos[0])**2+(p[1]-pos[1])**2;if(d<best){best=d;k=i;}});
  done.setLatLngs(D.track.slice(0,k+1).concat([pos]));
  if(follow&&playing&&!map.getBounds().pad(-0.2).contains(pos))map.panTo(pos);
}
function drawPlain(pos,hdg){
  const box=$('lmap'); let c=box.querySelector('canvas');
  if(!c){c=document.createElement('canvas');box.appendChild(c);}
  const W=box.clientWidth,H=box.clientHeight,dpr=devicePixelRatio||1; c.width=W*dpr;c.height=H*dpr;c.style.width=W+'px';c.style.height=H+'px';
  const g=c.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,W,H);
  if(!D||!D.track.length)return;
  const ref=D.track[0],kx=111320*Math.cos(ref[0]*Math.PI/180),ky=110540,xy=p=>[(p[1]-ref[1])*kx,(p[0]-ref[0])*ky];
  const P=D.track.map(xy),xs=P.map(p=>p[0]),ys=P.map(p=>p[1]);
  const cx=(Math.min(...xs)+Math.max(...xs))/2,cy=(Math.min(...ys)+Math.max(...ys))/2;
  const span=Math.max(30,(Math.max(...xs)-Math.min(...xs))*1.3,(Math.max(...ys)-Math.min(...ys))*1.3*W/H),sc=W/span;
  const px=p=>[W/2+(p[0]-cx)*sc,H/2-(p[1]-cy)*sc];
  g.beginPath();P.forEach((p,i)=>{const q=px(p);i?g.lineTo(q[0],q[1]):g.moveTo(q[0],q[1]);});g.strokeStyle='#38bdf8';g.lineWidth=3;g.stroke();
  if(D.home){const h=px(xy(D.home));g.fillStyle='#ffb020';g.beginPath();g.arc(h[0],h[1],7,0,7);g.fill();}
  if(pos){const p=px(xy(pos));g.save();g.translate(p[0],p[1]);g.rotate((hdg||0)*Math.PI/180);g.beginPath();g.moveTo(0,-10);g.lineTo(7,8);g.lineTo(0,4);g.lineTo(-7,8);g.closePath();g.fillStyle='#fff';g.fill();g.restore();}
  g.fillStyle='#9aa4b2';g.font='bold 11px system-ui';g.fillText('N ↑',10,H-12);
}
addEventListener('resize',()=>{if(map)map.invalidateSize();else render();});
loadList();
</script></body></html>
"""
