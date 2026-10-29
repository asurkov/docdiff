#!/usr/bin/env python3
"""Render a repo's uncommitted Markdown changes against a base revision as one inline-diff page.

Usage: docdiff.py [--repo PATH] [--base REV] [--out DIR] [--serve] [paths...]

paths are pathspecs relative to the repo root (default: every *.md).

Old = the file at REV (default HEAD); new = the working tree, staged or not, untracked included.
With --serve, serves --out on a free 127.0.0.1 port, or reuses a server already serving it, and
prints "URL: ...". The server also stores what the page sends: accepted changes in accepted.json,
revisit requests in revisit.jsonl, both in --out.
"""
import argparse, base64, difflib, hashlib, html, http.server, json, mimetypes, os, pathlib, re
import shutil, subprocess, sys, tempfile, threading, time, urllib.request

# Tags, entities, words and single punctuation marks, each with its trailing space: "nothing," and
# "nothing" share the word.
TOKEN = re.compile(r"<[^>]+>|&#?\w+;\s*|\w+(?:['’]\w+)*\s*|[^\w\s<]\s*|\s+")
BLOCK = re.compile(r'</?(?:p|li|ul|ol|h\d|tr|td|th|table|pre|blockquote|div)\b')
# Edits this few words apart in one block are one change to step to.
MERGE_WORDS = 3
# Unchanged words on each side that go into a change's key, so an accepted change comes back when
# the text right around it is edited, and not when the doc changes elsewhere.
CONTEXT = 4


def git(repo, *args, check=True):
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if check and r.returncode:
        sys.exit(f'git {" ".join(args)}: {r.stderr.decode().strip()}')
    return r


def changed_files(repo, base, paths):
    spec = ['--', *(paths or ['*.md'])]
    out = git(repo, 'diff', '--name-status', '--find-renames', base, *spec).stdout.decode()
    files = []
    for line in out.splitlines():
        status, *names = line.split('\t')
        old, new = (names[0], names[-1])
        if not new.endswith('.md'):
            continue
        files.append((None if status == 'A' else old, None if status == 'D' else new))
    untracked = git(repo, 'ls-files', '--others', '--exclude-standard', *spec).stdout.decode()
    files += [(None, f) for f in untracked.splitlines() if f.endswith('.md')]
    return files


def render(markdown):
    r = subprocess.run(['pandoc', '-f', 'gfm', '-t', 'html'], input=markdown, capture_output=True)
    if r.returncode:
        sys.exit(f'pandoc: {r.stderr.decode().strip()}')
    return r.stdout.decode()


def inline_images(page, repo, rel_dir, base, from_tree):
    """Embed relative images as data URIs: the page is served from outside the repo."""
    def repl(m):
        src = m.group(2)
        if re.match(r'[a-z]+:|/|#', src):
            return m.group(0)
        path = os.path.normpath(os.path.join(rel_dir, src))
        data = None
        if from_tree and (repo / path).is_file():
            data = (repo / path).read_bytes()
        else:
            r = git(repo, 'show', f'{base}:{path}', check=False)
            data = r.stdout if r.returncode == 0 else None
        if data is None:
            return m.group(0)
        mime = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        return f'{m.group(1)}data:{mime};base64,{base64.b64encode(data).decode()}{m.group(3)}'
    return re.sub(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', repl, page)


def is_tag(t):
    return t.startswith('<')


def words(tokens):
    return [t.strip() for t in tokens if not is_tag(t) and t.strip()]


def diff(old, new, gid):
    a, b = TOKEN.findall(old), TOKEN.findall(new)
    key = lambda t: t if is_tag(t) else t.strip()
    sm = difflib.SequenceMatcher(None, [key(t) for t in a], [key(t) for t in b], autojunk=False)
    out, n, split = [], 0, True
    # Per change: CONTEXT words before it, what it removes and adds, the unchanged words inside it,
    # and CONTEXT words after it. Hashed, that is the key an accept is stored under.
    sig, last = {}, []

    def wrap(tokens, tag, keep_tags):
        run = []

        def flush():
            if ''.join(run).strip():
                out.append(f'<{tag} class="chg" data-g="{gid}-{n}">{"".join(run)}</{tag}>')
            else:
                out.extend(run)
            run.clear()
        for t in tokens:
            if is_tag(t):
                flush()
                if keep_tags:
                    out.append(t)
            else:
                run.append(t)
        flush()

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == 'equal':
            seg = b[j1:j2]
            text = words(seg)
            count = sum(1 for t in seg if not is_tag(t) and re.match(r'\w', t))
            split = split or count > MERGE_WORDS or any(BLOCK.match(t) for t in seg)
            if n:
                sig[n] += ['|', *text[:CONTEXT]] if split else ['=', *text]
            last = text
            out.extend(seg)
            continue
        if split:
            n, split = n + 1, False
            sig[n] = [*last[-CONTEXT:], '|']
        sig[n] += ['-', *words(a[i1:i2]), '+', *words(b[j1:j2])]
        if i2 > i1:
            wrap(a[i1:i2], 'del', False)
        if j2 > j1:
            wrap(b[j1:j2], 'ins', True)
    keys = {g: hashlib.sha1(' '.join(s).encode()).hexdigest()[:12] for g, s in sig.items()}
    page = re.sub(rf'data-g="{gid}-(\d+)"', lambda m: f'{m.group(0)} data-k="{keys[int(m.group(1))]}"',
                  ''.join(out))
    return page, n


STYLE = '''
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--ins:#dafbe1;--insb:#1a7f37;--del:#ffebe9;--delb:#cf222e;--code:#f6f8fa;--link:#0969da}
@media (prefers-color-scheme: dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--line:#3d444d;--ins:#033a16;--insb:#3fb950;--del:#4b1113;--delb:#f85149;--code:#161b22;--link:#4493f8}}
body{background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0}
main{max-width:920px;margin:0 auto;padding:16px 16px 40vh}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;z-index:1;display:flex;flex-wrap:wrap;gap:6px 18px;align-items:center;font-size:14px}
header a{color:var(--fg);font-weight:600} .n{color:var(--muted)}
button:disabled{opacity:.4;cursor:default}
button{font:inherit;padding:4px 10px;border:1px solid var(--line);background:var(--code);color:var(--fg);border-radius:6px;cursor:pointer}
.file{margin:48px 0 8px;padding:6px 10px;background:var(--code);border:1px solid var(--line);border-radius:6px;font:600 14px ui-monospace,Menlo,monospace}
.state{color:var(--muted);font-weight:400}
.accf{float:right;font:12px -apple-system,BlinkMacSystemFont,sans-serif;padding:1px 8px}
ins{background:var(--ins);text-decoration:none;box-shadow:inset 0 -2px var(--insb)}
del{background:var(--del);color:var(--delb);text-decoration:line-through}
.chg{cursor:pointer} .show-review .ok{cursor:auto} .show-review del.ok{display:none} .show-review ins.ok{background:none;box-shadow:none} .show-diff .ok{opacity:.45}
code{background:var(--code);padding:1px 4px;border-radius:4px;font-size:90%}
pre{background:var(--code);padding:12px;overflow-x:auto}
img{max-width:100%}
table{border-collapse:collapse;display:block;overflow-x:auto;font-size:14px} td,th{border:1px solid var(--line);padding:4px 8px;vertical-align:top}
a{color:var(--link)} .cur{outline:2px solid #bf8700;outline-offset:2px}
body:not(.show-review):not(.show-diff) .v-diff,body:not(.show-old) .v-old,body:not(.show-new) .v-new{display:none}
body:not(.show-review):not(.show-diff) .step{display:none}
.seg{display:inline-flex} .seg button{border-radius:0} .seg button:first-child{border-radius:6px 0 0 6px}
.seg button:last-child{border-radius:0 6px 6px 0} .seg button[aria-pressed=true]{background:var(--fg);color:var(--bg)}
#rp{position:fixed;left:0;right:0;bottom:0;background:var(--bg);border-top:1px solid var(--line);padding:10px 16px;z-index:2;font-size:14px}
#pop{position:absolute;z-index:3;display:flex;gap:2px;padding:2px;background:var(--bg);border:1px solid var(--line);border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.18)}
#pop[hidden]{display:none} #pop button{padding:1px 9px;font-size:15px;line-height:1.4}
#rp blockquote{margin:0 0 8px;padding:2px 10px;border-left:3px solid var(--line);color:var(--muted);max-height:6em;overflow:auto;white-space:pre-wrap}
#rp textarea{display:block;width:100%;box-sizing:border-box;min-height:4.5em;margin-bottom:8px;font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px}
'''

SCRIPT = '''
const $=id=>document.getElementById(id),body=document.body,repo=body.dataset.repo;
const pos=$('pos'),next=$('next'),prev=$('prev'),acc=$('acc'),rev=$('rev'),rp=$('rp'),rq=$('rq'),rt=$('rt'),pop=$('pop'),pa=$('pa'),pr=$('pr');
const groups=[],byG=new Map();
for(const e of document.querySelectorAll('.v-diff .chg')){let x=byG.get(e.dataset.g);
if(!x){const s=e.closest('section');x={k:e.dataset.k,file:s.dataset.file,sec:s,els:[]};byG.set(e.dataset.g,x);groups.push(x);}x.els.push(e);}
let ok={},cur=body.dataset.view,c=null,anchor=null,req=null;
const isOk=x=>!!(ok[x.file]||{})[x.k],gi=x=>groups.indexOf(x);
const L=()=>cur==='review'?groups.filter(x=>!isOk(x)):cur==='diff'?groups:[];
const txt=(x,t)=>x.els.filter(e=>e.tagName===t).map(e=>e.textContent).join(' ').replace(/\\s+/g,' ').trim();
function summary(x){const d=txt(x,'DEL'),i=txt(x,'INS');return [d&&'−'+d,i&&'+'+i].filter(Boolean).join(' ').slice(0,300);}
function step(d){const l=L(),m=c||anchor,at=m?gi(m):d>0?-1:groups.length;return (d>0?l.find(x=>gi(x)>at):l.findLast(x=>gi(x)<at))||null;}
function status(){const l=L(),i=l.indexOf(c);prev.disabled=!step(-1);next.disabled=!step(1);
acc.disabled=!c;acc.textContent=c&&isOk(c)?'↺ unaccept':'✓ accept';
pos.textContent=cur!=='review'&&cur!=='diff'?'select text, r to revisit · v view':i>=0?'change '+(i+1)+' of '+l.length+' · '+c.file:
(l.length||'nothing')+(cur==='review'?' to review':' changes')+' · n / p step · a accept · r revisit · v view';}
function focus(x,scroll=true){hidePop();if(c)c.els.forEach(e=>e.classList.remove('cur'));c=x;if(x)anchor=x;
if(c){c.els.forEach(e=>e.classList.add('cur'));if(scroll)c.els.find(e=>e.offsetParent)?.scrollIntoView({block:'center',behavior:'smooth'});}status();}
function go(d){const x=step(d);if(x)focus(x);}
function paint(){for(const x of groups)x.els.forEach(e=>e.classList.toggle('ok',isOk(x)));
for(const s of document.querySelectorAll('section')){const gs=groups.filter(x=>x.sec===s),todo=gs.filter(x=>!isOk(x)).length,b=s.querySelector('.accf');
$('t-'+s.id).textContent=gs.length?todo?' · '+todo+' to review':' · all accepted':'';b.hidden=!gs.length;b.textContent=todo?'accept file':'unaccept file';}
if(c&&!L().includes(c))focus(null);else status();}
async function post(url,data){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
if(!r.ok)throw new Error(url+' '+r.status);return r.json();}
async function mark(file,gs,on){try{ok=await post('accept',on?{file,add:Object.fromEntries(gs.map(x=>[x.k,summary(x)]))}:{file,remove:gs.map(x=>x.k)});}
catch(e){pos.textContent='not saved: '+e.message+' (is the docdiff server running?)';return false;}paint();return true;}
async function accept(){if(!c||acc.disabled)return;const x=c,on=!isOk(x),nx=cur==='review'&&on?step(1)||step(-1):x;
if(await mark(x.file,[x],on))focus(nx,nx!==x);}
document.querySelectorAll('.accf').forEach(b=>b.onclick=()=>{const s=b.closest('section'),gs=groups.filter(x=>x.sec===s),on=gs.some(x=>!isOk(x));
mark(s.dataset.file,on?gs.filter(x=>!isOk(x)):gs,on);});
function hidePop(){pop.hidden=true;}
// Under a clicked change: accept and revisit. Under the end of a text selection: revisit only.
function showPop(r,withAccept){pa.hidden=!withAccept;pa.textContent=c&&isOk(c)?'↺':'✓';pa.title=c&&isOk(c)?'Unaccept (a)':'Accept (a)';pop.hidden=false;
pop.style.left=Math.max(8,Math.min(r.left,innerWidth-pop.offsetWidth-8))+scrollX+'px';pop.style.top=r.bottom+scrollY+4+'px';}
document.addEventListener('click',e=>{if(e.target.closest('header,#pop,#rp')||!getSelection().isCollapsed)return;
const t=cur==='review'?e.target.closest('.v-diff .chg:not(.ok)'):e.target.closest('.v-diff .chg');
if(t){focus(groups.find(x=>x.els.includes(t)),false);showPop(t.getBoundingClientRect(),true);}else focus(null,false);});
document.addEventListener('mouseup',e=>{if(e.target.closest('header,#pop,#rp'))return;setTimeout(()=>{const s=getSelection();
if(s.isCollapsed||!s.rangeCount||!s.anchorNode.parentElement?.closest('main .v'))return;
const rs=s.getRangeAt(0).getClientRects();if(rs.length)showPop(rs[rs.length-1],false);});});
[pa,pr].forEach(b=>b.onmousedown=e=>e.preventDefault());
pa.onclick=()=>{hidePop();accept();};pr.onclick=()=>{hidePop();openRev(grab());};
const vb=[...document.querySelectorAll('.seg button')],views=vb.map(b=>b.dataset.v);
function view(v){if(v===cur)return;hidePop();
const secs=[...document.querySelectorAll('section')];let s=secs[0],r=0;
for(const x of secs){const b=x.getBoundingClientRect();if(b.bottom>80){s=x;r=Math.max(0,80-b.top)/Math.max(1,b.height);break;}}
body.classList.replace('show-'+cur,'show-'+v);cur=v;vb.forEach(b=>b.setAttribute('aria-pressed',b.dataset.v===v));
if(c&&!L().includes(c))focus(null,false);else status();
if(s){const b=s.getBoundingClientRect();scrollTo(0,scrollY+b.top+r*b.height-80);}}
function grab(){const s=getSelection();
if(!s.isCollapsed&&s.rangeCount&&!rp.contains(s.anchorNode))return{quote:s.toString().trim(),el:s.anchorNode.parentElement};
return c?{quote:summary(c),el:c.els[0]}:null;}
function heading(el){const v=el.closest('.v');if(!v)return '';
const h=[...v.querySelectorAll('h1,h2,h3,h4')].filter(h=>h.compareDocumentPosition(el)&Node.DOCUMENT_POSITION_FOLLOWING).pop();
if(!h)return '';const k=h.cloneNode(true);k.querySelectorAll('del').forEach(d=>d.remove());return k.textContent.trim();}
function openRev(g){const s=g&&g.el.closest('section');if(!s){pos.textContent='select text, or step to a change, then press r';return;}
req={file:s.dataset.file,section:heading(g.el),quote:g.quote,view:cur};rq.textContent=g.quote;rp.hidden=false;rt.focus();}
function closeRev(){rp.hidden=true;rt.value='';req=null;}
async function copy(t){try{await navigator.clipboard.writeText(t);return true;}
catch(e){const a=document.createElement('textarea');a.value=t;body.append(a);a.select();const done=document.execCommand('copy');a.remove();return done;}}
async function send(){const ask=rt.value.trim();if(!ask||!req)return;
const text='Revisit '+repo+'/'+req.file+(req.section?', section "'+req.section+'"':'')+(req.view==='old'?' (old version)':'')+':\\n> '+req.quote.replace(/\\n/g,'\\n> ')+'\\n\\n'+ask;
const note=[await copy(text)?'copied, paste it in the chat':'copy failed'];
try{await post('revisit',{...req,prompt:ask,text});note.push('queued');}catch(e){note.push('not queued');}
closeRev();pos.textContent='revisit: '+note.join(' · ');}
rev.onmousedown=e=>e.preventDefault();rev.onclick=()=>openRev(grab());
$('rs').onclick=send;$('rc').onclick=closeRev;
rt.addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();send();}if(e.key==='Escape')closeRev();});
next.onclick=()=>go(1);prev.onclick=()=>go(-1);acc.onclick=accept;vb.forEach(b=>b.onclick=()=>view(b.dataset.v));
document.addEventListener('keydown',e=>{if(e.target.closest('input,textarea')||e.metaKey||e.ctrlKey||e.altKey)return;
if(e.key==='Escape')hidePop();
if(e.key==='v')view(views[(views.indexOf(cur)+1)%views.length]);
if(e.key==='r'){e.preventDefault();openRev(grab());}
if(cur!=='review'&&cur!=='diff')return;
if(e.key==='n'||e.key==='j')go(1);if(e.key==='p'||e.key==='k')go(-1);if(e.key==='a')accept();});
status();fetch('accepted.json').then(r=>r.ok?r.json():{}).catch(()=>({})).then(d=>{ok=d;paint();});
'''


def build(repo, base, files):
    sections, nav = [], []
    for k, (old, new) in enumerate(files):
        name = new or old
        old_md = git(repo, 'show', f'{base}:{old}').stdout if old else b''
        new_md = (repo / new).read_bytes() if new else b''
        rel = os.path.dirname(name)
        old_html = inline_images(render(old_md), repo, os.path.dirname(old or ''), base, False) if old else ''
        new_html = inline_images(render(new_md), repo, rel, base, True) if new else ''
        if new:
            body, n = diff(old_html, new_html, f'f{k}')
        else:
            body, n = f'<del class="chg" data-g="f{k}-1" data-k="deleted">{old_html}</del>', 1
        if not n:
            continue
        state = 'new file' if not old else 'deleted' if not new else f'renamed from {old}' if old != new else ''
        sid = f'f{k}'
        label = html.escape(name)
        nav.append(f'<a href="#{sid}">{label}</a> <span class="n">{n} change{"s" if n != 1 else ""}'
                   f'<span id="t-{sid}"></span></span>')
        plain = lambda h, missing: re.sub(r'\sid="[^"]*"', '', h) or f'<p class="n">({missing})</p>'
        sections.append(f'<section id="{sid}" data-file="{label}"><div class="file">{label}'
                        f'{f" <span class=state>· {html.escape(state)}</span>" if state else ""}'
                        f'<button class="accf" hidden>accept file</button></div>'
                        f'<div class="v v-diff">{body}</div><div class="v v-old">{plain(old_html, "new file")}</div>'
                        f'<div class="v v-new">{plain(new_html, "deleted")}</div></section>')
    title = f'docdiff · {repo.name} vs {base}'
    buttons = [('review', 'To review'), ('diff', f'Diff vs {base}'), ('old', 'Old'), ('new', 'New')]
    seg = ''.join(f'<button data-v="{v}" aria-pressed="{str(v == "review").lower()}">{label}</button>' for v, label in buttons)
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{STYLE}</style></head><body class="show-review" data-view="review" data-repo="{html.escape(str(repo))}">
<header>{" · ".join(nav)}<span style="flex:1"></span>
<span class="seg">{seg}</span>
<span id="pos" class="n"></span><button id="prev" class="step">↑ prev</button><button id="next" class="step">next change ↓</button>
<button id="acc" class="step">✓ accept</button><button id="rev">revisit</button></header>
<main>{"".join(sections)}</main>
<div id="rp" hidden><blockquote id="rq"></blockquote><textarea id="rt" placeholder="What should change? ⌘↵ sends"></textarea>
<button id="rs">Send</button> <button id="rc">Cancel</button></div>
<div id="pop" hidden><button id="pa">✓</button><button id="pr" title="Revisit (r)">✎</button></div>
<script>{SCRIPT}</script></body></html>''', len(sections)


def handler(out):
    """Static files from out with caching off, so a reload after a rebuild shows the new page, plus
    the page's two POSTs: /accept updates accepted.json, /revisit appends to revisit.jsonl."""
    lock = threading.Lock()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(out), **kwargs)

        def end_headers(self):
            self.send_header('Cache-Control', 'no-store')
            super().end_headers()

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with lock:
                if self.path == '/accept':
                    path = out / 'accepted.json'
                    data = json.loads(path.read_text()) if path.exists() else {}
                    keys = data.setdefault(req['file'], {})
                    keys.update(req.get('add', {}))
                    for k in req.get('remove', []):
                        keys.pop(k, None)
                    if not keys:
                        del data[req['file']]
                    path.write_text(json.dumps(data, indent=1, ensure_ascii=False))
                    reply = data
                elif self.path == '/revisit':
                    with open(out / 'revisit.jsonl', 'a') as f:
                        f.write(json.dumps({'time': time.strftime('%Y-%m-%dT%H:%M:%S'), **req},
                                           ensure_ascii=False) + '\n')
                    reply = {}
                else:
                    return self.send_error(404)
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return Handler


def serve(out):
    state = out / '.server.json'
    if state.exists():
        port = json.loads(state.read_text())['port']
        try:
            served = urllib.request.urlopen(f'http://127.0.0.1:{port}/.server.json', timeout=1).read()
        except OSError:
            served = None
        if served is not None and served.decode() == state.read_text():
            print(f'URL: http://127.0.0.1:{port}/index.html (server already running)', flush=True)
            return
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler(out))
    port = server.server_address[1]
    state.write_text(json.dumps({'port': port, 'pid': os.getpid()}))
    print(f'URL: http://127.0.0.1:{port}/index.html', flush=True)
    server.serve_forever()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--repo', default='.')
    p.add_argument('--base', default='HEAD')
    p.add_argument('--out')
    p.add_argument('--serve', action='store_true')
    p.add_argument('paths', nargs='*')
    a = p.parse_args()
    if not shutil.which('pandoc'):
        sys.exit('pandoc not found: brew install pandoc')
    top = git(pathlib.Path(a.repo), 'rev-parse', '--show-toplevel').stdout.decode().strip()
    repo = pathlib.Path(top)
    if git(repo, 'rev-parse', '--verify', '--quiet', f'{a.base}^{{commit}}', check=False).returncode:
        sys.exit(f'unknown revision: {a.base}')
    out = pathlib.Path(a.out) if a.out else pathlib.Path(tempfile.gettempdir()) / f'docdiff-{repo.name}'
    out.mkdir(parents=True, exist_ok=True)
    files = changed_files(repo, a.base, a.paths)
    page, shown = build(repo, a.base, files)
    (out / 'index.html').write_text(page)
    print(f'{shown} changed Markdown file(s) in {repo} vs {a.base} → {out / "index.html"}', flush=True)
    if not shown:
        return
    if a.serve:
        serve(out)


if __name__ == '__main__':
    main()
