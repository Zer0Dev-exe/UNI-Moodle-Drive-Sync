"""Dashboard web: logs en directo (SSE), ficheros sincronizados y control del intervalo."""
import collections
import json
import logging
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HISTORY = 500
_history = collections.deque(maxlen=HISTORY)
_clients: set[queue.Queue] = set()
_lock = threading.Lock()
ctl = None  # objeto Control de moodle_sync (intervalo, wake, estado, state.json)


class WebLogHandler(logging.Handler):
    """Guarda cada línea de log y la reparte a los navegadores conectados."""

    def emit(self, record):
        ev = {"t": time.strftime("%H:%M:%S", time.localtime(record.created)),
              "level": record.levelname, "msg": self.format(record)}
        with _lock:
            _history.append(ev)
            for q in _clients:
                q.put(ev)


PAGE = r"""<!doctype html>
<html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Moodle Sync</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
:root{--bg:#f4f5f8;--panel:#fff;--fg:#14161a;--mut:#6a7280;--line:#e3e6ec;--soft:#eef0f5;--acc:#4f46e5;--acc2:#7c3aed;--accfg:#fff;
 --ok:#16a34a;--warn:#d97706;--err:#dc2626;--chg:#16a34a;--shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.04);--term:#0f1115;--termfg:#d7dae0}
@media (prefers-color-scheme:dark){:root{--bg:#0e0f13;--panel:#17181d;--fg:#ececf1;--mut:#8b92a0;--line:#2a2c33;--soft:#20222a;--acc:#818cf8;--acc2:#a78bfa;--accfg:#0e0f13;
 --ok:#4ade80;--warn:#fbbf24;--err:#f87171;--chg:#4ade80;--shadow:none;--term:#0a0b0e;--termfg:#cfd3db}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:20px 20px 60px}
.top{display:flex;align-items:center;gap:12px;margin-bottom:18px}
.logo{width:36px;height:36px;border-radius:10px;flex:none}
h1{font-size:18px;margin:0;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:12px}
.sp{flex:1}
.pill{display:inline-flex;align-items:center;gap:7px;padding:4px 11px;border-radius:99px;background:var(--panel);border:1px solid var(--line);font-size:12px;color:var(--mut);box-shadow:var(--shadow)}
.pill i{width:8px;height:8px;border-radius:50%;background:var(--err)}
.pill.on i{background:var(--ok);box-shadow:0 0 0 0 var(--ok);animation:pulse 2s infinite}
@keyframes pulse{70%{box-shadow:0 0 0 6px transparent}100%{box-shadow:0 0 0 0 transparent}}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
@media(max-width:820px){.stats{grid-template-columns:repeat(2,1fr)}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;box-shadow:var(--shadow)}
.card .l{color:var(--mut);font-size:12px;display:flex;align-items:center;gap:6px}
.card .v{font-size:26px;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums;margin-top:2px}
.card .n{color:var(--mut);font-size:12px;margin-top:2px;min-height:18px}
.next{display:flex;align-items:center;gap:14px}
.ring{width:52px;height:52px;flex:none;transform:rotate(-90deg)}
.ring circle{fill:none;stroke-width:5}.ring .bg{stroke:var(--soft)}
.ring .fg{stroke:url(#g);stroke-linecap:round;transition:stroke-dashoffset .9s linear}
.ring.busy .fg{animation:spin 1.1s linear infinite;stroke-dasharray:40 200;stroke-dashoffset:0}
@keyframes spin{to{transform:rotate(360deg);transform-origin:center}}
.bar{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;margin-bottom:12px}
.seg{display:inline-flex;background:var(--soft);border-radius:10px;padding:3px;gap:2px}
.seg button{border:0;background:none;color:var(--mut);padding:5px 13px;border-radius:7px;font:inherit;font-weight:500;cursor:pointer}
.seg button:hover{color:var(--fg)}
.seg button.on{background:var(--panel);color:var(--fg);box-shadow:var(--shadow)}
.ctl{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.ctl label{color:var(--mut);font-size:12px}
input[type=number],input[type=search]{background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font:inherit;outline:none}
input:focus{border-color:var(--acc);box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 22%,transparent)}
input[type=number]{width:70px}
.btn{background:linear-gradient(135deg,var(--acc),var(--acc2));color:var(--accfg);border:0;border-radius:8px;padding:7px 14px;font:inherit;font-weight:600;cursor:pointer}
.btn:hover{filter:brightness(1.08)}.btn:disabled{opacity:.5;cursor:default}
.btn.sec{background:var(--panel);color:var(--fg);border:1px solid var(--line);font-weight:500}
.chips{display:flex;gap:4px}
.chips button{border:1px solid var(--line);background:var(--panel);color:var(--mut);padding:3px 10px;border-radius:99px;font:inherit;font-size:12px;cursor:pointer}
.chips button.on{border-color:var(--acc);color:var(--acc)}
#toast{font-size:12px;color:var(--ok);min-width:60px}
section{display:none}section.on{display:block}
.term{background:var(--term);color:var(--termfg);border-radius:14px;border:1px solid var(--line);padding:10px 4px;height:calc(100vh - 360px);min-height:320px;overflow:auto;font:12.5px/1.65 ui-monospace,"JetBrains Mono",Menlo,monospace}
.row{display:flex;gap:12px;padding:1px 14px}
.row[hidden]{display:none}
.row:hover{background:rgba(127,127,127,.1)}
.t{color:#6b7280;flex:none}
.m{white-space:pre-wrap;word-break:break-word;min-width:0}
.row.change .m{color:#4ade80;font-weight:600}
.row.same .m{color:#6b7280}
.row.WARNING .m{color:#fbbf24}.row.ERROR .m{color:#f87171}
.empty{color:var(--mut);text-align:center;padding:40px 0}
.tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.tools .cnt{color:var(--mut);font-size:12px;margin-left:auto}
details.dir{margin:1px 0}
details.dir>summary{list-style:none;display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;cursor:pointer;font-weight:600}
details.dir>summary::-webkit-details-marker{display:none}
details.dir>summary:hover{background:var(--soft)}
details.dir>summary::before{content:"";width:6px;height:6px;border-right:2px solid var(--mut);border-bottom:2px solid var(--mut);transform:rotate(-45deg);transition:transform .15s;margin:0 4px 0 2px;flex:none}
details.dir[open]>summary::before{transform:rotate(45deg)}
summary .ic{width:18px;height:18px;flex:none;color:var(--acc)}
summary .n{margin-left:auto;background:var(--soft);color:var(--mut);font-weight:500;font-size:11px;padding:1px 8px;border-radius:99px}
.kids{margin-left:15px;padding-left:12px;border-left:1px solid var(--line)}
#tree>details.dir{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:6px;margin-bottom:12px}
#tree>details.dir>summary{font-size:15px;padding:8px 10px}
.file{display:flex;align-items:center;gap:10px;padding:5px 8px;border-radius:8px;color:inherit;text-decoration:none}
.file:hover{background:var(--soft)}
.ext{flex:none;width:42px;text-align:center;font:700 9.5px/1 ui-monospace,monospace;letter-spacing:.03em;padding:5px 0;border-radius:5px;color:#fff;background:#64748b}
.fn{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file .when{margin-left:auto;color:var(--mut);font-size:12px;white-space:nowrap;flex:none}
.tag{font-size:10.5px;font-weight:700;padding:1px 7px;border-radius:99px;flex:none}
.tag.new{background:color-mix(in srgb,var(--ok) 18%,transparent);color:var(--ok)}
.tag.del{background:color-mix(in srgb,var(--err) 16%,transparent);color:var(--err)}
</style>
<div class="wrap">
 <div class="top">
  <svg class="logo" viewBox="0 0 64 64"><defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4f46e5"/><stop offset="1" stop-color="#a855f7"/></linearGradient></defs>
   <rect width="64" height="64" rx="15" fill="url(#lg)"/><g fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"><path d="M45 24a15 15 0 0 0-26 3"/><path d="M19 40a15 15 0 0 0 26-3"/></g><path d="M48 14v11H37z" fill="#fff"/><path d="M16 50V39h11z" fill="#fff"/></svg>
  <div><h1>Moodle Sync</h1><div class="sub">Moodle → Google Drive</div></div>
  <span class="sp"></span>
  <span class="pill" id="live"><i></i><span>conectando…</span></span>
 </div>

 <div class="stats">
  <div class="card"><div class="l">Ficheros</div><div class="v" id="s-files">–</div><div class="n" id="s-files-n"></div></div>
  <div class="card"><div class="l">Asignaturas</div><div class="v" id="s-courses">–</div><div class="n" id="s-courses-n"></div></div>
  <div class="card"><div class="l">Cambios hoy</div><div class="v" id="s-today">–</div><div class="n" id="s-last"></div></div>
  <div class="card next">
   <svg class="ring" id="ring" viewBox="0 0 60 60"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4f46e5"/><stop offset="1" stop-color="#a855f7"/></linearGradient></defs>
    <circle class="bg" cx="30" cy="30" r="25"/><circle class="fg" id="ringfg" cx="30" cy="30" r="25" stroke-dasharray="157.08" stroke-dashoffset="157.08"/></svg>
   <div><div class="l">Próxima sincronización</div><div class="v" id="s-next">–</div><div class="n" id="s-iv"></div></div>
  </div>
 </div>

 <div class="bar">
  <div class="seg" id="tabs"><button data-tab="logs" class="on">Logs</button><button data-tab="files">Archivos</button></div>
  <span class="sp"></span>
  <div class="ctl">
   <label>Intervalo</label>
   <div class="seg" id="presets"></div>
   <input type="number" id="iv" min="1" max="1440" title="minutos">
   <button class="btn" id="save">Guardar</button>
   <button class="btn sec" id="now">Sincronizar ya</button>
   <span id="toast"></span>
  </div>
 </div>

 <section id="logs" class="on">
  <div class="tools"><div class="chips" id="filters"><button data-f="act" class="on">Actividad</button><button data-f="all">Todo</button><button data-f="err">Errores</button></div></div>
  <div class="term" id="term"><div class="empty" id="logempty">Esperando eventos…</div></div>
 </section>

 <section id="files">
  <div class="tools">
   <input type="search" id="q" placeholder="Buscar fichero o carpeta…" size="32">
   <button class="btn sec" id="exp">Desplegar todo</button><button class="btn sec" id="col">Plegar todo</button>
   <span class="cnt" id="count"></span>
  </div>
  <div id="tree"></div>
 </section>
</div>

<script>
const $=s=>document.querySelector(s);
const mk=(tag,cls,txt)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e};
const toast=t=>{$('#toast').textContent=t;setTimeout(()=>$('#toast').textContent='',2500)};
const SVGNS='http://www.w3.org/2000/svg';
// ---- pestañas
$('#tabs').onclick=e=>{const b=e.target.closest('button');if(!b)return;
  document.querySelectorAll('#tabs button,section').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');$('#'+b.dataset.tab).classList.add('on');
  if(b.dataset.tab==='files')render();else $('#term').scrollTop=$('#term').scrollHeight};
// ---- logs
let filter='act',lastEvent=null;
const CHG=/^(NUEVO|RE-SUBIDO|ACTUALIZADO|BORRADO|MOVIDO)|=====/;
function visible(r){const k=r.dataset.k;return filter==='all'||(filter==='act'&&k!=='same')||(filter==='err'&&(k==='ERROR'||k==='WARNING'))}
function add(e){
  $('#logempty')?.remove();
  const k=e.level==='ERROR'||e.level==='WARNING'?e.level:CHG.test(e.msg)?'change':e.msg.startsWith('Sin cambios')?'same':'info';
  const r=mk('div','row '+(k==='info'?'':k));r.dataset.k=k;
  r.append(mk('span','t',e.t),mk('span','m',e.msg));
  r.hidden=!visible(r);
  const t=$('#term'),bottom=t.scrollTop+t.clientHeight>=t.scrollHeight-40;
  t.append(r);if(bottom)t.scrollTop=t.scrollHeight;
  lastEvent=e;
  if(k==='change')refreshSoon();
}
$('#filters').onclick=e=>{const b=e.target.closest('button');if(!b)return;
  document.querySelectorAll('#filters button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
  filter=b.dataset.f;document.querySelectorAll('.row').forEach(r=>r.hidden=!visible(r));$('#term').scrollTop=$('#term').scrollHeight};
const es=new EventSource('/events');
es.onopen=()=>{$('#live').className='pill on';$('#live').lastChild.textContent='en directo'};
es.onerror=()=>{$('#live').className='pill';$('#live').lastChild.textContent='desconectado'};
es.onmessage=m=>add(JSON.parse(m.data));
// ---- ficheros
let files=[],timer;
const collator=new Intl.Collator(undefined,{numeric:true,sensitivity:'base'});
const EXT={pdf:'#dc2626',doc:'#2563eb',docx:'#2563eb',odt:'#2563eb',xls:'#16a34a',xlsx:'#16a34a',csv:'#16a34a',ppt:'#ea580c',pptx:'#ea580c',
 zip:'#a16207',rar:'#a16207','7z':'#a16207',html:'#0891b2',htm:'#0891b2',ipynb:'#f59e0b',py:'#3776ab',md:'#475569',txt:'#64748b',json:'#64748b',yaml:'#64748b',yml:'#64748b',png:'#db2777',jpg:'#db2777',jpeg:'#db2777',mp4:'#7c3aed'};
function ext(n){const m=/\.([A-Za-z0-9]+)$/.exec(n);return m?m[1].toLowerCase():''}
function ago(s){if(!s)return'';const d=(Date.now()-new Date(s).getTime())/1000;
  if(d<60)return'ahora';if(d<3600)return'hace '+Math.floor(d/60)+' min';if(d<86400)return'hace '+Math.floor(d/3600)+' h';
  if(d<86400*7)return'hace '+Math.floor(d/86400)+' d';return s.slice(0,10)}
function folderIcon(){const s=document.createElementNS(SVGNS,'svg');s.setAttribute('viewBox','0 0 24 24');s.setAttribute('class','ic');
  const p=document.createElementNS(SVGNS,'path');p.setAttribute('d','M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z');p.setAttribute('fill','currentColor');p.setAttribute('opacity','.85');s.append(p);return s}
const count=n=>n.files.length+Object.values(n.dirs).reduce((t,d)=>t+count(d),0);
function build(node,open){
  const box=document.createDocumentFragment();
  for(const name of Object.keys(node.dirs).sort(collator.compare)){
    const d=mk('details','dir');d.open=open;
    const s=mk('summary');s.append(folderIcon(),mk('span','',name),mk('span','n',count(node.dirs[name])));d.append(s);
    const kids=mk('div','kids');kids.append(build(node.dirs[name],open));d.append(kids);box.append(d);
  }
  for(const f of node.files.sort((a,b)=>collator.compare(a.name,b.name))){
    const a=mk('a','file');a.href='https://drive.google.com/file/d/'+f.drive_id+'/view';a.target='_blank';a.rel='noopener';a.title=f.name;
    const e=ext(f.name);const x=mk('span','ext',(e||'file').slice(0,5).toUpperCase());x.style.background=EXT[e]||'#64748b';
    a.append(x,mk('span','fn',f.name));
    if(f.deleted)a.append(mk('span','tag del','borrado en Moodle'));
    else if(Date.now()-new Date(f.updated).getTime()<864e5)a.append(mk('span','tag new','reciente'));
    a.append(mk('span','when',ago(f.updated)));
    box.append(a);
  }
  return box;
}
function render(){
  const q=$('#q').value.toLowerCase().trim(),root={dirs:{},files:[]};
  const rows=files.filter(f=>!q||(f.name+' '+f.path).toLowerCase().includes(q));
  $('#count').textContent=rows.length+' de '+files.length+' ficheros';
  for(const f of rows){let n=root;for(const p of f.path.split('/').filter(Boolean))n=n.dirs[p]??={dirs:{},files:[]};n.files.push(f)}
  const out=$('#tree');out.textContent='';
  if(!rows.length){out.append(mk('div','empty',files.length?'Sin resultados':'Todavía no hay ficheros'));return}
  out.append(build(root,!!q));
}
$('#q').oninput=render;
$('#exp').onclick=()=>document.querySelectorAll('#tree details').forEach(d=>d.open=true);
$('#col').onclick=()=>document.querySelectorAll('#tree details').forEach(d=>d.open=false);
function stats(){
  const local=new Date(Date.now()-new Date().getTimezoneOffset()*6e4).toISOString().slice(0,10);
  const courses=new Set(files.map(f=>f.path.split('/')[0]));
  const del=files.filter(f=>f.deleted).length;
  $('#s-files').textContent=files.length;$('#s-files-n').textContent=del?del+' borrados en Moodle':'todos en Drive';
  $('#s-courses').textContent=courses.size;$('#s-courses-n').textContent=[...courses].sort(collator.compare).slice(0,2).join(' · ');
  const t=files.filter(f=>f.updated.slice(0,10)===local);
  $('#s-today').textContent=t.length;
  const last=files.reduce((m,f)=>f.updated>m?f.updated:m,'');
  $('#s-last').textContent=last?'último '+ago(last):'';
}
async function loadFiles(){files=await (await fetch('/api/files')).json();stats();if($('#files').classList.contains('on'))render()}
function refreshSoon(){clearTimeout(timer);timer=setTimeout(loadFiles,1500)}
// ---- control e intervalo
const PRESETS=[1,2,5,10,15,30,60];
for(const m of PRESETS){const b=mk('button','',m<60?m+'m':'1h');b.dataset.m=m;$('#presets').append(b)}
$('#presets').onclick=e=>{const b=e.target.closest('button');if(b){$('#iv').value=b.dataset.m;save()}};
let st={interval:0,running:false,next_in:0},got=0;
function markPreset(){document.querySelectorAll('#presets button').forEach(b=>b.classList.toggle('on',+b.dataset.m===st.interval))}
async function status(){
  try{st=await (await fetch('/api/status')).json();got=Date.now();
    if(document.activeElement!==$('#iv'))$('#iv').value=st.interval;markPreset();tick()}catch(e){}
}
const C=2*Math.PI*25;
function tick(){
  const left=Math.max(0,st.next_in-(Date.now()-got)/1000),ring=$('#ring');
  ring.classList.toggle('busy',st.running);
  if(st.running){$('#s-next').textContent='En curso';$('#s-iv').textContent='sincronizando…';return}
  const m=Math.floor(left/60),s=Math.floor(left%60);
  $('#s-next').textContent=m+':'+String(s).padStart(2,'0');
  $('#s-iv').textContent='cada '+st.interval+' min';
  $('#ringfg').style.strokeDashoffset=C*(left/(st.interval*60||1));
}
async function post(url,body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j;
}
async function save(){try{await post('/api/interval',{minutes:+$('#iv').value});toast('Guardado');status()}catch(e){toast(e.message)}}
$('#save').onclick=save;
$('#now').onclick=async()=>{try{await post('/api/sync');toast('Lanzada');setTimeout(status,300)}catch(e){toast(e.message)}};
$('#iv').onkeydown=e=>{if(e.key==='Enter')save()};
if(location.hash==='#archivos')document.querySelector('[data-tab=files]').click();
loadFiles();status();setInterval(status,3000);setInterval(tick,1000);
</script></html>
"""
FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4f46e5"/><stop offset="1" stop-color="#a855f7"/></linearGradient></defs>
<g id="i"><rect width="64" height="64" rx="15" fill="url(#g)"/>
<g fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round">
<path d="M45 24a15 15 0 0 0-26 3"/><path d="M19 40a15 15 0 0 0 26-3"/></g>
<path d="M48 14v11H37z" fill="#fff"/><path d="M16 50V39h11z" fill="#fff"/></g></svg>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype != "text/event-stream" else ""))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        # Evita DNS rebinding: solo se atiende como localhost
        return self.headers.get("Host", "").split(":")[0] in ("localhost", "127.0.0.1")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "host no permitido"})
        if self.path == "/favicon.svg":
            return self._send(200, FAVICON, "image/svg+xml")
        if self.path == "/events":
            return self._events()
        if self.path == "/api/status":
            return self._send(200, ctl.status())
        if self.path == "/api/files":
            return self._send(200, ctl.files())
        self._send(200, PAGE, "text/html")

    def do_POST(self):
        if not self._host_ok() or "application/json" not in self.headers.get("Content-Type", ""):
            return self._send(403, {"error": "no permitido"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/api/interval":
                return self._send(200, {"interval": ctl.set_interval(body.get("minutes"))})
            if self.path == "/api/sync":
                ctl.sync_now()
                return self._send(200, {"ok": True})
            self._send(404, {"error": "no existe"})
        except ValueError as e:
            self._send(400, {"error": str(e)})

    def _events(self):
        q = queue.Queue()
        with _lock:
            backlog = list(_history)
            _clients.add(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for ev in backlog:
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _lock:
                _clients.discard(q)


def start(port: int, control):
    """Arranca el servidor en un hilo y engancha el log."""
    global ctl
    ctl = control
    handler = WebLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logging.getLogger("moodle_sync").info("Web en directo en el puerto %d", port)
