#!/usr/bin/env python3
"""Render a repo's uncommitted Markdown changes against a base revision as one inline-diff page.

Usage: docdiff.py [--repo PATH] [--base REV] [--out DIR] [--serve] [paths...]

paths are pathspecs relative to the repo root (default: every *.md).

Old = the file at REV (default HEAD); new = the working tree, staged or not, untracked included.
With --serve, serves --out on a free 127.0.0.1 port, or reuses a server already serving it, and
prints "URL: ...".
"""
import argparse, base64, difflib, html, json, mimetypes, os, pathlib, re, socket, subprocess, sys
import shutil, tempfile, urllib.request

# Tags, entities, words and single punctuation marks, each with its trailing space: "nothing," and
# "nothing" share the word.
TOKEN = re.compile(r"<[^>]+>|&#?\w+;\s*|\w+(?:['’]\w+)*\s*|[^\w\s<]\s*|\s+")
BLOCK = re.compile(r'</?(?:p|li|ul|ol|h\d|tr|td|th|table|pre|blockquote|div)\b')
# Edits this few words apart in one block are one change to step to.
MERGE_WORDS = 3
# http.server with caching off, so a reload after a rebuild shows the new page. Args: port, dir.
NO_CACHE_SERVER = '''
import functools, http.server, sys
class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()
handler = functools.partial(Handler, directory=sys.argv[2])
http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), handler).serve_forever()
'''


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


def diff(old, new, gid):
    a, b = TOKEN.findall(old), TOKEN.findall(new)
    key = lambda t: t if is_tag(t) else t.strip()
    sm = difflib.SequenceMatcher(None, [key(t) for t in a], [key(t) for t in b], autojunk=False)
    out, n, split = [], 0, True

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
            words = sum(1 for t in seg if not is_tag(t) and re.match(r'\w', t))
            split = split or words > MERGE_WORDS or any(BLOCK.match(t) for t in seg)
            out.extend(seg)
            continue
        if split:
            n, split = n + 1, False
        if i2 > i1:
            wrap(a[i1:i2], 'del', False)
        if j2 > j1:
            wrap(b[j1:j2], 'ins', True)
    return ''.join(out), n


STYLE = '''
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--ins:#dafbe1;--insb:#1a7f37;--del:#ffebe9;--delb:#cf222e;--code:#f6f8fa;--link:#0969da}
@media (prefers-color-scheme: dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--line:#3d444d;--ins:#033a16;--insb:#3fb950;--del:#4b1113;--delb:#f85149;--code:#161b22;--link:#4493f8}}
body{background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0}
main{max-width:920px;margin:0 auto;padding:16px}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;z-index:1;display:flex;flex-wrap:wrap;gap:6px 18px;align-items:center;font-size:14px}
header a{color:var(--fg);font-weight:600} .n{color:var(--muted)}
button:disabled{opacity:.4;cursor:default}
button{font:inherit;padding:4px 10px;border:1px solid var(--line);background:var(--code);color:var(--fg);border-radius:6px;cursor:pointer}
.file{margin:48px 0 8px;padding:6px 10px;background:var(--code);border:1px solid var(--line);border-radius:6px;font:600 14px ui-monospace,Menlo,monospace}
.state{color:var(--muted);font-weight:400}
ins{background:var(--ins);text-decoration:none;box-shadow:inset 0 -2px var(--insb)}
del{background:var(--del);color:var(--delb);text-decoration:line-through}
code{background:var(--code);padding:1px 4px;border-radius:4px;font-size:90%}
pre{background:var(--code);padding:12px;overflow-x:auto}
img{max-width:100%}
table{border-collapse:collapse;display:block;overflow-x:auto;font-size:14px} td,th{border:1px solid var(--line);padding:4px 8px;vertical-align:top}
a{color:var(--link)} .cur{outline:2px solid #bf8700;outline-offset:2px}
body:not(.show-last) .v-last,body:not(.show-diff) .v-diff,body:not(.show-old) .v-old,body:not(.show-new) .v-new{display:none}
body:not(.show-diff):not(.show-last) .step{display:none}
.seg{display:inline-flex} .seg button{border-radius:0} .seg button:first-child{border-radius:6px 0 0 6px}
.seg button:last-child{border-radius:0 6px 6px 0} .seg button[aria-pressed=true]{background:var(--fg);color:var(--bg)}
'''

SCRIPT = '''
const pos=document.getElementById('pos'),next=document.getElementById('next'),prev=document.getElementById('prev');
const lists={};
for(const v of ['last','diff']){const seen=new Set();
lists[v]=[...document.querySelectorAll('.v-'+v+' .chg')].filter(e=>!seen.has(e.dataset.g)&&seen.add(e.dataset.g));}
const vb=[...document.querySelectorAll('.seg button')],views=vb.map(b=>b.dataset.v);
let cur=document.body.dataset.view,i=-1;
const L=()=>lists[cur]||[];
function ends(){prev.disabled=i<=0;next.disabled=i>=L().length-1;}
function reset(){i=-1;pos.textContent=L().length+' changes · n / p to step · v to switch view';ends();}
function go(d){const c=L(),j=Math.min(Math.max(i+d,0),c.length-1);if(!c.length||j===i)return;
if(i>=0)c[i].classList.remove('cur');i=j;c[i].classList.add('cur');ends();
c[i].scrollIntoView({block:'center',behavior:'smooth'});pos.textContent='change '+(i+1)+' of '+c.length+' · '+c[i].closest('section').dataset.file;}
next.onclick=()=>go(1);prev.onclick=()=>go(-1);
function view(v){if(v===cur)return;
const secs=[...document.querySelectorAll('section')];let s=secs[0],r=0;
for(const x of secs){const b=x.getBoundingClientRect();if(b.bottom>80){s=x;r=Math.max(0,80-b.top)/Math.max(1,b.height);break;}}
const c=L();if(i>=0&&c[i])c[i].classList.remove('cur');
document.body.classList.replace('show-'+cur,'show-'+v);cur=v;vb.forEach(b=>b.setAttribute('aria-pressed',b.dataset.v===v));reset();
if(s){const b=s.getBoundingClientRect();scrollTo(0,scrollY+b.top+r*b.height-80);}}
vb.forEach(b=>b.onclick=()=>view(b.dataset.v));reset();
document.addEventListener('keydown',e=>{if(e.target.closest('input,textarea'))return;
if(e.key==='v')view(views[(views.indexOf(cur)+1)%views.length]);
if(!lists[cur])return;
if(e.key==='n'||e.key==='j')go(1);if(e.key==='p'||e.key==='k')go(-1);});
'''


def build(repo, base, files, snapshot):
    sections, nav, had_snapshot, total_last = [], [], False, 0
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
            body, n = f'<del class="chg" data-g="f{k}-1">{old_html}</del>', 1
        if not n:
            continue
        snap = snapshot / new if new else None
        if snap and snap.is_file():
            had_snapshot = True
            if snap.read_bytes() == new_md:
                last_body, ln = '<p class="n">(no change since the last run)</p>', 0
            else:
                last_body, ln = diff(inline_images(render(snap.read_bytes()), repo, rel, base, True), new_html, f'l{k}')
        elif new and snapshot.is_dir():
            # A file missing from the last run's snapshot had no changes then: it matched the base.
            had_snapshot = True
            last_body, ln = diff(old_html, new_html, f'l{k}')
        else:
            last_body, ln = '<p class="n">(not in the last run)</p>', 0
        total_last += ln
        state = 'new file' if not old else 'deleted' if not new else f'renamed from {old}' if old != new else ''
        sid = f'f{k}'
        label = html.escape(name)
        since = f' · {ln} since last run' if ln else ''
        nav.append(f'<a href="#{sid}">{label}</a> <span class="n">{n} change{"s" if n != 1 else ""}{since}</span>')
        plain = lambda h, missing: re.sub(r'\sid="[^"]*"', '', h) or f'<p class="n">({missing})</p>'
        sections.append(f'<section id="{sid}" data-file="{label}"><div class="file">{label}'
                        f'{f" <span class=state>· {html.escape(state)}</span>" if state else ""}</div>'
                        f'<div class="v v-last">{last_body}</div><div class="v v-diff">{body}</div><div class="v v-old">{plain(old_html, "new file")}</div>'
                        f'<div class="v v-new">{plain(new_html, "deleted")}</div></section>')
    title = f'docdiff · {repo.name} vs {base}'
    first = 'last' if total_last else 'diff'
    buttons = [('last', 'Since last run')] if had_snapshot else []
    buttons += [('diff', f'Diff vs {base}'), ('old', 'Old'), ('new', 'New')]
    seg = ''.join(f'<button data-v="{v}" aria-pressed="{str(v == first).lower()}">{label}</button>' for v, label in buttons)
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{STYLE}</style></head><body class="show-{first}" data-view="{first}">
<header>{" · ".join(nav)}<span style="flex:1"></span>
<span class="seg">{seg}</span>
<span id="pos" class="n step"></span><button id="prev" class="step">↑ prev</button><button id="next" class="step">next change ↓</button></header>
<main>{"".join(sections)}</main><script>{SCRIPT}</script></body></html>''', len(sections)


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
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    state.write_text(json.dumps({'port': port, 'pid': os.getpid()}))
    print(f'URL: http://127.0.0.1:{port}/index.html', flush=True)
    os.execvp(sys.executable, [sys.executable, '-I', '-c', NO_CACHE_SERVER, str(port), str(out)])


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
    snapshot = out / 'snapshot'
    page, shown = build(repo, a.base, files, snapshot)
    (out / 'index.html').write_text(page)
    for _, new in files:
        if new:
            (snapshot / new).parent.mkdir(parents=True, exist_ok=True)
            (snapshot / new).write_bytes((repo / new).read_bytes())
    print(f'{shown} changed Markdown file(s) in {repo} vs {a.base} → {out / "index.html"}', flush=True)
    if not shown:
        return
    if a.serve:
        serve(out)


if __name__ == '__main__':
    main()
