#!/usr/bin/env python3
"""Render a repo's uncommitted Markdown changes against a base revision as one inline-diff page.

Usage: docdiff.py [--repo PATH] [--base REV] [--head REV] [--out DIR] [--comments FILE] [--new-label TEXT]
                  [--publish CMD] [--serve] [paths...]

paths are pathspecs relative to the repo root (default: every *.md and *.html).

Old = the file at REV (default HEAD); new = the file at --head, or without it the working tree,
staged or not, untracked included. A changed .html file under an index.html that holds a
respecConfig stands for that ReSpec spec, which the page renders in the browser (see SCRIPT).
With --serve, serves --out on a free 127.0.0.1 port, or reuses a server already serving it, and
prints "URL: ...". The server also stores what the page sends, in --out: accepted/<file>.html, the
file as accepted so far, which To review is diffed against; revisit requests in revisit.jsonl;
spec renders in rendered/<sources id>.html.

--comments FILE: comments made on the old side, {file: [{quote, author, date, text, replies: [{author,
text}], fate: "goes", "edited" or null}]} as gdocsync's export writes them; each is marked where its
quote first reads in the old text, its thread in the mark's tooltip.

The page's header names both sides: a revision with its commit, or the working tree; --new-label TEXT
names the new side instead, for a working tree that stands for something else.

--publish CMD puts "publish accepted" on the bar of each file with accepted changes. It writes the
accepted copy as Markdown and runs CMD in a shell, {file} replaced by the file's name and {target} by
that Markdown's path; CMD is to publish it and bring the base up to date, after which the accepted copy
starts over from the base. gdocsync's push --target, then its export, is such a command.
"""
import argparse, base64, difflib, hashlib, html, http.server, json, mimetypes, os, pathlib, re
import shlex, shutil, subprocess, sys, tempfile, threading, time, urllib.parse, urllib.request

# Tags, entities, words and single punctuation marks, each with its trailing space: "nothing," and
# "nothing" share the word.
TOKEN = re.compile(r"<[^>]+>|&#?\w+;\s*|\w+(?:['’]\w+)*\s*|[^\w\s<]\s*|\s+")
BLOCK = re.compile(r'</?(?:p|li|ul|ol|h\d|tr|td|th|table|pre|blockquote|div)\b')
# Edits this few words apart in one block are one change to step to.
MERGE_WORDS = 3
# Unchanged words on each side that go into a change's key, so an accepted change comes back when
# the text right around it is edited, and not when the doc changes elsewhere.
CONTEXT = 4
# Word similarity of an old and a new block: at REWRITE or above the block is word-diffed; below it
# the block shows as rewritten, its old text folded above the new; below PAIR the two are not matched
# and show as one removed block and one added.
REWRITE = 0.5
PAIR = 0.3
SHORT = 6
TAG = re.compile(r'<(/?)([a-zA-Z][\w-]*)[^>]*?(/?)>')
VOID = {'area', 'br', 'col', 'hr', 'img', 'input', 'wbr'}


def git(repo, *args, check=True):
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if check and r.returncode:
        sys.exit(f'git {" ".join(args)}: {r.stderr.decode().strip()}')
    return r


def side(repo, rev):
    """One side of the diff in words: the revision and its commit, or the working tree."""
    if not rev:
        return 'working tree of ' + str(repo).replace(str(pathlib.Path.home()), '~', 1)
    return f'{rev} ({git(repo, "log", "-1", "--format=%h %s", rev).stdout.decode().strip()})'


def read(repo, rev, path):
    """A file at rev, or in the working tree with rev None; None where it does not exist."""
    if rev is None:
        p = repo / path
        return p.read_bytes() if p.is_file() else None
    r = git(repo, 'show', f'{rev}:{path}', check=False)
    return r.stdout if r.returncode == 0 else None


def spec_index(repo, revs, path, memo):
    """The ReSpec spec a source file belongs to: the nearest index.html at or above it that holds a
    respecConfig on either side; None if there is none."""
    d = os.path.dirname(path)
    if d not in memo:
        index = os.path.join(d, 'index.html')
        if any(b'respecConfig' in (read(repo, rev, index) or b'') for rev in revs):
            memo[d] = index
        else:
            memo[d] = spec_index(repo, revs, d, memo) if d else None
    return memo[d]


def changed_files(repo, base, head, paths):
    """Changed Markdown files and ReSpec specs [(old path or None, new path or None)]."""
    spec = ['--', *(paths or ['*.md', '*.html'])]
    out = git(repo, 'diff', '--name-status', '--find-renames', base, *([head] if head else []), *spec).stdout.decode()
    names = []
    for line in out.splitlines():
        status, *ns = line.split('\t')
        names.append((None if status == 'A' else ns[0], None if status == 'D' else ns[-1]))
    if not head:
        untracked = git(repo, 'ls-files', '--others', '--exclude-standard', *spec).stdout.decode()
        names += [(None, f) for f in untracked.splitlines()]
    files, specs, memo = [], set(), {}
    for old, new in names:
        name = new or old
        if name.endswith('.md'):
            files.append((old, new))
        elif name.endswith('.html'):
            index = spec_index(repo, (base, head), name, memo)
            if index and index not in specs:
                specs.add(index)
                files.append(tuple(index if read(repo, rev, index) is not None else None for rev in (base, head)))
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
    # Changes with text to show; one that only changes a tag, a link target say, renders as nothing.
    shown = set()
    # Per change: CONTEXT words before it, what it removes and adds, the unchanged words inside it,
    # and CONTEXT words after it. Hashed, that is the key an accept is stored under.
    sig, last = {}, []
    # (group number, old text, new text) per opcode, 0 for unchanged text: what applying one change needs.
    parts = []

    def wrap(tokens, tag, keep_tags):
        run = []

        def flush():
            if ''.join(run).strip():
                out.append(f'<{tag} class="chg" data-g="{gid}-{n}">{"".join(run)}</{tag}>')
                shown.add(n)
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
            parts.append((0, ''.join(a[i1:i2]), ''.join(seg)))
            continue
        if split:
            n, split = n + 1, False
            sig[n] = [*last[-CONTEXT:], '|']
        sig[n] += ['-', *words(a[i1:i2]), '+', *words(b[j1:j2])]
        parts.append((n, ''.join(a[i1:i2]), ''.join(b[j1:j2])))
        if i2 > i1:
            wrap(a[i1:i2], 'del', False)
        if j2 > j1:
            wrap(b[j1:j2], 'ins', True)
    keys = {g: hashlib.sha1(' '.join(s).encode()).hexdigest()[:12] for g, s in sig.items()}
    page = re.sub(rf'data-g="{gid}-(\d+)"', lambda m: f'{m.group(0)} data-k="{keys[int(m.group(1))]}"',
                  ''.join(out))
    return page, len(shown), [(keys.get(g), x, y) for g, x, y in parts]


def split_blocks(page):
    """pandoc HTML as its top-level blocks, each list item a block of its own: [(list tag or '', html)]."""
    out, depth, start, lst = [], 0, None, ''
    for m in TAG.finditer(page):
        closing, name = m.group(1), m.group(2).lower()
        top = 1 if lst else 0
        if name in VOID or m.group(3):
            if depth == top and start is None:
                out.append((lst, m.group(0)))
            continue
        if not closing:
            if depth == 0 and not lst and name in ('ul', 'ol'):
                lst = m.group(0)
            elif depth == top and start is None:
                start = m.start()
            depth += 1
            continue
        depth -= 1
        if depth == top and start is not None:
            out.append((lst, page[start:m.end()]))
            start = None
        elif depth == 0 and lst:
            lst = ''
    return out


def text(block):
    return ' '.join(words(TOKEN.findall(block))) if block else ''


def similarity(a, b):
    """Share of words two blocks have in common, punctuation left out. A block of a few words, a
    heading say, has to keep most of them to count as similar at all."""
    wa, wb = ([w.lower() for w in text(x).split() if re.match(r'\w', w)] for x in (a, b))
    r = difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()
    return r if min(len(wa), len(wb)) >= SHORT or r >= REWRITE else 0


def pair(olds, news):
    """Match the blocks of a replaced run, keeping their order: [(old index or None, new index or None)]."""
    out, i = [], 0
    for j, new in enumerate(news):
        best, at = PAIR, None
        for k in range(i, len(olds)):
            r = similarity(olds[k], new)
            if r > best:
                best, at = r, k
        if at is None:
            out.append((None, j))
            continue
        out += [(k, None) for k in range(i, at)] + [(at, j)]
        i = at + 1
    return out + [(k, None) for k in range(i, len(olds))]


def whole_blocks(run, gid):
    """A run of added, removed or rewritten blocks [(list tag, kind, old, new)] as one change under one
    key. Old text folds under a one-line summary; consecutive removed blocks share one."""
    key = hashlib.sha1('|'.join(f'{k}|{text(o)}|{text(w)}' for _, k, o, w in run).encode()).hexdigest()[:12]
    inner = lambda b: re.sub(r'^<li\b[^>]*>|</li>$', '', b)
    merged = []
    for lst, kind, old, new in run:
        if kind == 'del' and merged and merged[-1][1] == 'del' and merged[-1][0] == lst:
            merged[-1][2].append(old)
        else:
            merged.append((lst, kind, [old] if old else [], new))
    out = []
    for lst, kind, olds, new in merged:
        was = ''
        if olds:
            head = ' '.join(html.unescape(re.sub(r'<[^>]+>', lambda m: ' ' if BLOCK.match(m.group(0)) else '', olds[0])).split())
            head = head if len(head) <= 80 else head[:80].rsplit(' ', 1)[0] + '…'
            label = ('was' if kind == 'rw' else 'removed' if len(olds) == 1
                     else f'removed {len(olds)} {"items" if lst else "paragraphs"}')
            body = (inner(olds[0]) if len(olds) == 1 else f'{lst}{"".join(olds)}</{lst[1:3]}>') if lst else ''.join(olds)
            was = (f'<details class="was"><summary>{label}: {html.escape(head)}</summary>'
                   f'<div class="old">{body}</div></details>')
        now = f'<div class="now">{inner(new)}</div>' if new else ''
        tag = 'li' if lst else 'div'
        out.append((lst, f'<{tag} class="chg blk" data-kind="{kind}" data-g="{gid}" data-k="{key}">{was}{now}</{tag}>'))
    return out, key


def block_diff(old, new, gid):
    """Diff block by block (paragraph, heading, list item, table...): unchanged blocks plain, added,
    removed and rewritten ones marked whole, and only a lightly edited one word by word. Also returns
    the pieces apply() takes: per block, its list tag on each side and [(change key or None, old, new)]."""
    a, b = split_blocks(old), split_blocks(new)
    norm = lambda h: re.sub(r'\s+', ' ', h).strip()
    sm = difflib.SequenceMatcher(None, [norm(h) for _, h in a], [norm(h) for _, h in b], autojunk=False)
    items, pieces, n = [], [], 0
    run, sides = [], []

    def flush():
        nonlocal n
        if run:
            out, key = whole_blocks(run, f'{gid}-b{len(items)}')
            items.extend(out)
            pieces.extend((lo, lw, [(key, o, w)]) for lo, lw, o, w in sides)
            n += 1
            run.clear()
            sides.clear()

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == 'equal':
            items += b[j1:j2]
            pieces += [(x[0], y[0], [(None, x[1], y[1])]) for x, y in zip(a[i1:i2], b[j1:j2])]
            continue
        for i, j in pair([h for _, h in a[i1:i2]], [h for _, h in b[j1:j2]]):
            o = a[i1 + i] if i is not None else None
            w = b[j1 + j] if j is not None else None
            lst = (w or o)[0]
            if o and w and similarity(o[1], w[1]) >= REWRITE:
                flush()
                body, k, parts = diff(o[1], w[1], f'{gid}-b{len(items)}')
                items.append((lst, body))
                pieces.append((o[0], w[0], parts))
                n += k
            else:
                # A new section starts a new change.
                if run and w and re.match(r'<h\d', w[1]):
                    flush()
                run.append((lst, 'rw' if o and w else 'add' if w else 'del', o and o[1], w and w[1]))
                sides.append((o[0] if o else '', w[0] if w else '', o[1] if o else '', w[1] if w else ''))
        flush()
    return wrap_lists(items), n, pieces


def wrap_lists(items):
    """Blocks [(list tag or '', html)] back into one page: consecutive items of one list go back into it."""
    out, open_ = [], ''
    for lst, h in items:
        if not h:
            continue
        if lst != open_:
            out += [f'</{open_[1:3]}>'] if open_ else []
            out += [lst] if lst else []
            open_ = lst
        out.append(h)
    out += [f'</{open_[1:3]}>'] if open_ else []
    return ''.join(out)


def apply(pieces, side, flip):
    """The page that block_diff() compared, with every change from `side` ('old' or 'new') except the
    changes whose keys are in `flip`, which come from the other side."""
    new = lambda k: (side == 'new') != (k in flip)
    items = []
    for lst_old, lst_new, parts in pieces:
        text = []
        for i, (k, o, w) in enumerate(parts):
            t = w if new(k) else o
            # Unchanged text before a change ends in the spacing of whichever side that change takes:
            # "End " before an inserted word, "End" before a deleted one.
            if k is None and i + 1 < len(parts):
                src = w if new(parts[i + 1][0]) else o
                t = t.rstrip() + src[len(src.rstrip()):]
            text.append(t)
        keyed = [k for k, _, _ in parts if k]
        items.append((lst_new if new(keyed[0] if keyed else None) else lst_old, ''.join(text)))
    return wrap_lists(items)


STYLE = '''
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--ins:#dafbe1;--insb:#1a7f37;--del:#ffebe9;--delb:#cf222e;--rw:#bf8700;--code:#f6f8fa;--link:#0969da}
@media (prefers-color-scheme: dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--line:#3d444d;--ins:#033a16;--insb:#3fb950;--del:#4b1113;--delb:#f85149;--rw:#d29922;--code:#161b22;--link:#4493f8}}
body{background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0}
main{max-width:920px;margin:0 auto;padding:16px 16px 40vh}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;z-index:1;display:flex;flex-wrap:wrap;gap:6px 18px;align-items:center;font-size:14px}
header a{color:var(--fg);font-weight:600} .n{color:var(--muted)} .sides{flex-basis:100%;font-size:13px}
button:disabled{opacity:.4;cursor:default}
button{font:inherit;padding:4px 10px;border:1px solid var(--line);background:var(--code);color:var(--fg);border-radius:6px;cursor:pointer}
.file{margin:48px 0 8px;padding:6px 10px;background:var(--code);border:1px solid var(--line);border-radius:6px;font:600 14px ui-monospace,Menlo,monospace}
.state{color:var(--muted);font-weight:400}
.accf,.pub{float:right;font:12px -apple-system,BlinkMacSystemFont,sans-serif;padding:1px 8px}
ins{background:var(--ins);text-decoration:none;box-shadow:inset 0 -2px var(--insb)}
del{background:var(--del);color:var(--delb);text-decoration:line-through}
.chg{cursor:pointer}
.blk{border-left:3px solid var(--line);padding-left:10px;margin-left:-13px} li.blk{margin-left:0;padding-left:6px}
.blk[data-kind=add]{border-color:var(--insb)} .blk[data-kind=del]{border-color:var(--delb)} .blk[data-kind=rw]{border-color:var(--rw)}
.was summary{color:var(--muted);font-size:13px;cursor:pointer} .was summary~*{margin-top:4px}
.was .old{color:var(--muted);border-left:2px solid var(--delb);padding-left:8px} .old>:first-child,.now>:first-child{margin-top:0}
code{background:var(--code);padding:1px 4px;border-radius:4px;font-size:90%}
pre{background:var(--code);padding:12px;overflow-x:auto}
img{max-width:100%}
table{border-collapse:collapse;display:block;overflow-x:auto;font-size:14px} td,th{border:1px solid var(--line);padding:4px 8px;vertical-align:top}
a{color:var(--link)} .cur{outline:2px solid #bf8700;outline-offset:2px}
mark.cm{background:rgba(212,167,44,.25);color:inherit;border-bottom:2px solid #d4a72c;cursor:help}
mark.cm-goes{border-bottom-color:var(--delb)} mark.cm-edited{border-bottom-style:dashed}
.cmx{font-size:13px;color:var(--muted);margin:0 0 8px} .cmx summary{cursor:pointer} .cmx li{cursor:help}
.render{position:fixed;right:0;bottom:0;width:800px;height:600px;border:0;opacity:0;pointer-events:none}
.note,.issue,.example,.ednote,.warning{border-left:4px solid var(--line);padding:2px 12px;margin:1em 0}
.issue,.warning{border-color:var(--rw)} .marker{font-weight:600;color:var(--muted)}
body:not(.show-review) .v-review,body:not(.show-acc) .v-acc,body:not(.show-diff) .v-diff,body:not(.show-old) .v-old,body:not(.show-new) .v-new{display:none}
.show-old .step,.show-new .step{display:none}
body:not(.show-review) .accf[data-a=accept],body:not(.show-acc) .accf[data-a=unaccept]{display:none}
.seg{display:inline-flex} .seg button{border-radius:0} .seg button:first-child{border-radius:6px 0 0 6px}
.seg button:last-child{border-radius:0 6px 6px 0} .seg button[aria-pressed=true]{background:var(--fg);color:var(--bg)}
#rp{position:fixed;left:0;right:0;bottom:0;background:var(--bg);border-top:1px solid var(--line);padding:10px 16px;z-index:2;font-size:14px}
#pop{position:absolute;z-index:3;display:flex;background:var(--bg);border:1px solid var(--line);border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.12);overflow:hidden}
#pop[hidden]{display:none} #pop button{border:0;border-radius:0;background:none;color:var(--muted);padding:2px 9px;font-size:14px;line-height:1.5}
#pop button:hover{background:var(--code);color:var(--fg)} #pop button+button{border-left:1px solid var(--line)}
#rp blockquote{margin:0 0 8px;padding:2px 10px;border-left:3px solid var(--line);color:var(--muted);max-height:6em;overflow:auto;white-space:pre-wrap}
#rp textarea{display:block;width:100%;box-sizing:border-box;min-height:4.5em;margin-bottom:8px;font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px}
'''

SCRIPT = '''
const $=id=>document.getElementById(id),body=document.body,repo=body.dataset.repo;
// Comments made on the old side (--comments): each marked where its quote first reads in the old text,
// in every view that has it, so not in inserted words, added blocks or the folded summaries; the mark's
// tooltip holds the thread and what the change does to the comment's anchor.
function markQuote(root,q,make){const map=[];let s='',sp=true,n;
const w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{acceptNode:t=>t.parentElement.closest('ins,summary,.now')?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
while(n=w.nextNode())for(let k=0;k<n.data.length;k++){const ch=/\\s/.test(n.data[k])?' ':n.data[k];if(ch===' '&&sp)continue;sp=ch===' ';s+=ch;map.push([n,k]);}
const at=s.indexOf(q);if(at<0)return false;const spans=[];
for(let k=at;k<at+q.length;k++){const[t,o]=map[k],l=spans[spans.length-1];if(l&&l.t===t)l.e=o+1;else spans.push({t,b:o,e:o+1});}
for(const x of spans.reverse()){let t=x.t;if(x.e<t.length)t.splitText(x.e);if(x.b)t=t.splitText(x.b);const m=make();t.replaceWith(m);m.append(t);}return true;}
// A comment whose quote no longer reads in the old text (edited there after the comment was made) is
// listed under the file's bar instead.
for(const s of document.querySelectorAll('section')){const j=s.querySelector('script.cm');if(!j)continue;const lost=[];
for(const c of JSON.parse(j.textContent)){const q=c.quote.replace(/\\s+/g,' ').trim();if(!q)continue;
const title=[c.author+', '+c.date+': '+c.text,...c.replies.map(r=>'↳ '+r.author+': '+r.text),
c.fate==='goes'?'The change deletes all of its anchor.':c.fate==='edited'?'The change edits its anchor.':''].filter(Boolean).join('\\n');
let placed=false;for(const v of s.querySelectorAll('.v-old,.v-diff,.v-review,.v-acc'))placed=markQuote(v,q,()=>{const m=document.createElement('mark');
m.className='cm'+(c.fate?' cm-'+c.fate:'');m.title=title;return m;})||placed;
if(!placed){const li=document.createElement('li');li.title=title;li.textContent=c.author+' on “'+q+'”: '+c.text;lost.push(li);}}
if(lost.length){const d=document.createElement('details');d.className='cmx';d.innerHTML='<summary></summary><ul></ul>';
d.firstChild.textContent=lost.length+' comment(s) on text that no longer reads here';d.lastChild.append(...lost);s.querySelector('.file').after(d);}}
const pos=$('pos'),next=$('next'),prev=$('prev'),acc=$('acc'),rev=$('rev'),rp=$('rp'),rq=$('rq'),rt=$('rt'),pop=$('pop'),pa=$('pa'),pr=$('pr');
// Changes per view: To review (accepted copy to now), Accepted (base to accepted copy), Diff vs base.
const groups=[],byG=new Map();
for(const e of document.querySelectorAll('.v-review .chg,.v-acc .chg,.v-diff .chg')){const v=e.closest('.v').dataset.view;
let x=byG.get(v+e.dataset.g);if(!x){const s=e.closest('section');x={k:e.dataset.k,file:s.dataset.file,sec:s,view:v,els:[]};
byG.set(v+e.dataset.g,x);groups.push(x);}x.els.push(e);}
let cur=body.dataset.view,c=null,anchor=null,req=null;
const gi=x=>groups.indexOf(x),L=()=>groups.filter(x=>x.view===cur),verb={review:'accept',acc:'unaccept'};
// A change's removed (-) or added (+) text: an inline <del>/<ins>, or a whole block's old/new part.
const part=(e,s)=>e.classList.contains('blk')?e.querySelector(s==='-'?'.old':'.now')?.textContent||'':e.tagName===(s==='-'?'DEL':'INS')?e.textContent:'';
const txt=(x,s)=>x.els.map(e=>part(e,s)).join(' ').replace(/\\s+/g,' ').trim();
function summary(x){const d=txt(x,'-'),i=txt(x,'+');return [d&&'−'+d,i&&'+'+i].filter(Boolean).join(' ').slice(0,300);}
function step(d){const l=L(),m=[c,anchor].find(x=>x&&x.view===cur),at=m?gi(m):d>0?-1:groups.length;return (d>0?l.find(x=>gi(x)>at):l.findLast(x=>gi(x)<at))||null;}
function status(){const l=L(),i=l.indexOf(c);prev.disabled=!step(-1);next.disabled=!step(1);
acc.disabled=!c||!verb[cur];acc.textContent=cur==='acc'?'↺ unaccept':'✓ accept';
pos.textContent=cur==='old'||cur==='new'?'select text, r to revisit · v view':i>=0?'change '+(i+1)+' of '+l.length+' · '+c.file:
(l.length||'nothing')+({review:' to review',acc:' accepted'}[cur]||' changes')+' · n / p step'+(verb[cur]?' · a '+verb[cur]:'')+' · r revisit · v view';}
function focus(x,scroll=true){hidePop();if(c)c.els.forEach(e=>e.classList.remove('cur'));c=x;if(x)anchor=x;
if(c){c.els.forEach(e=>e.classList.add('cur'));if(scroll)c.els.find(e=>e.offsetParent)?.scrollIntoView({block:'center',behavior:'smooth'});}status();}
function go(d){const x=step(d);if(x)focus(x);}
async function post(url,data){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
if(!r.ok)throw new Error(url+' '+r.status);return r.json();}
// An accept moves the change into the accepted copy, an unaccept takes it back out; the server rebuilds
// the page and it reloads. It stays where it is: the next change becomes current only if it is already
// in view; otherwise nothing is current and n goes to it.
async function accept(){if(!c||!verb[cur])return;const x=c,l=L(),i=l.indexOf(x),nx=l[i+1]||l[i-1]||null;
try{await post(verb[cur],{file:x.file,key:x.k});}catch(e){pos.textContent='not saved: '+e.message+' (is the docdiff server running?)';return;}
focus(nx&&inView(nx)?nx:null,false);if(!c)anchor=l[i-1]||null;location.reload();}
function inView(x){const top=document.querySelector('header').getBoundingClientRect().bottom;
return x.els.some(e=>{const r=e.getBoundingClientRect();return e.offsetParent&&r.bottom>top&&r.top<innerHeight;});}
document.querySelectorAll('.accf').forEach(b=>b.onclick=async()=>{try{await post(b.dataset.a,{file:b.closest('section').dataset.file,all:true});
location.reload();}catch(e){pos.textContent='not saved: '+e.message;}});
// Publish runs the --publish command on the accepted changes and shows what it printed.
document.querySelectorAll('.pub').forEach(b=>b.onclick=async()=>{const f=b.closest('section').dataset.file;
if(!confirm('Publish the '+b.dataset.n+' accepted change(s) of '+f+'?'))return;b.disabled=true;b.textContent='publishing…';
try{const r=await fetch('publish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:f})});
const t=await r.text();alert((r.ok?'Published:\\n\\n':'Not published:\\n\\n')+t);}catch(e){alert('Not published: '+e.message);}location.reload();});
function hidePop(){pop.hidden=true;}
// Under a clicked change: accept and revisit. Under the end of a text selection: revisit only.
function showPop(r,withAccept){pa.hidden=!withAccept;pa.textContent=cur==='acc'?'↺':'✓';pa.title=cur==='acc'?'Unaccept (a)':'Accept (a)';pop.hidden=false;
pop.style.left=Math.max(8,Math.min(r.left,innerWidth-pop.offsetWidth-8))+scrollX+'px';pop.style.top=r.bottom+scrollY+4+'px';}
document.addEventListener('click',e=>{if(e.target.closest('header,#pop,#rp')||!getSelection().isCollapsed)return;
const t=e.target.closest('.v-'+cur+' .chg');
if(t){focus(groups.find(x=>x.view===cur&&x.els.includes(t)),false);showPop(t.getBoundingClientRect(),!!verb[cur]);}else focus(null,false);});
// Runs once the click has settled: clicking inside a selection clears it only after the click handler.
document.addEventListener('mouseup',e=>{if(e.target.closest('header,#pop,#rp'))return;setTimeout(()=>{const s=getSelection();
if(s.isCollapsed||!s.rangeCount){if(pa.hidden)hidePop();return;}
if(!s.anchorNode.parentElement?.closest('main .v'))return;
const rs=s.getRangeAt(0).getClientRects();if(rs.length)showPop(rs[rs.length-1],false);});});
[pa,pr].forEach(b=>b.onmousedown=e=>e.preventDefault());
pa.onclick=()=>{hidePop();accept();};pr.onclick=()=>{hidePop();openRev(grab());};
const vb=[...document.querySelectorAll('.seg button')],views=vb.map(b=>b.dataset.v);
function view(v){if(v===cur)return;hidePop();
const secs=[...document.querySelectorAll('section')];let s=secs[0],r=0;
for(const x of secs){const b=x.getBoundingClientRect();if(b.bottom>80){s=x;r=Math.max(0,80-b.top)/Math.max(1,b.height);break;}}
body.classList.replace('show-'+cur,'show-'+v);cur=v;vb.forEach(b=>b.setAttribute('aria-pressed',b.dataset.v===v));
if(c&&c.view!==v)focus(null,false);else status();
if(s){const b=s.getBoundingClientRect();scrollTo(0,scrollY+b.top+r*b.height-80);}}
// The selected text as it now reads: deleted words and folded old blocks left out, unless that is
// all the selection holds.
function quoteOf(s){const f=s.getRangeAt(0).cloneContents();f.querySelectorAll('del,.was').forEach(e=>e.remove());
return f.textContent.replace(/\\s+/g,' ').trim()||s.toString().trim();}
function grab(){const s=getSelection();
if(!s.isCollapsed&&s.rangeCount&&!rp.contains(s.anchorNode))return{quote:quoteOf(s),el:s.anchorNode.parentElement};
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
let queued=true;try{await post('revisit',{...req,prompt:ask,text});}catch(e){queued=false;}
// A watcher touches the file every second while it waits; with none, the clipboard is the way in.
const watched=queued&&await watching();
closeRev();pos.textContent='revisit: '+(watched?'sent, Claude is watching':await copy(text)?
'copied; nobody is watching, paste it in the chat':'not sent: nobody is watching and copying failed');}
async function watching(){try{const r=await fetch('watcher',{method:'HEAD'});
return r.ok&&Date.now()-new Date(r.headers.get('Last-Modified'))<5000;}catch(e){return false;}}
rev.onmousedown=e=>e.preventDefault();rev.onclick=()=>openRev(grab());
$('rs').onclick=send;$('rc').onclick=closeRev;
rt.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send();}if(e.key==='Escape')closeRev();});
next.onclick=()=>go(1);prev.onclick=()=>go(-1);acc.onclick=accept;vb.forEach(b=>b.onclick=()=>view(b.dataset.v));
document.addEventListener('keydown',e=>{if(e.target.closest('input,textarea')||e.metaKey||e.ctrlKey||e.altKey)return;
if(e.key==='Escape')hidePop();
if(e.key==='v')view(views[(views.indexOf(cur)+1)%views.length]);
if(e.key==='r'){e.preventDefault();openRev(grab());}
if(cur==='old'||cur==='new')return;
if(e.key==='n'||e.key==='j')go(1);if(e.key==='p'||e.key==='k')go(-1);if(e.key==='a')accept();});
// Every reload keeps the view, the current change and the place in the doc; a rebuild reloads the page,
// unless a revisit is being typed.
history.scrollRestoration='manual';
function place(){const secs=[...document.querySelectorAll('section')];
for(const x of secs){const b=x.getBoundingClientRect();if(b.bottom>80)return{file:x.dataset.file,r:Math.max(0,80-b.top)/Math.max(1,b.height)};}return null;}
addEventListener('pagehide',()=>{try{sessionStorage.setItem('docdiff',JSON.stringify({view:cur,at:place(),c:c&&[c.file,c.k],ci:c?L().indexOf(c):-1,a:anchor&&[anchor.file,anchor.k]}));}catch(e){}});
function restore(){let st=null;try{st=JSON.parse(sessionStorage.getItem('docdiff'));}catch(e){}if(!st)return;
const find=p=>p&&groups.find(x=>x.view===cur&&x.file===p[0]&&x.k===p[1])||null;
if(views.includes(st.view)&&st.view!==cur){body.classList.replace('show-'+cur,'show-'+st.view);cur=st.view;vb.forEach(b=>b.setAttribute('aria-pressed',b.dataset.v===cur));}
anchor=find(st.a);const x=find(st.c)||(st.c&&L()[Math.min(st.ci,L().length-1)]);if(x)focus(x,false);
const back=()=>{const s=st.at&&[...document.querySelectorAll('section')].find(z=>z.dataset.file===st.at.file);
if(s){const b=s.getBoundingClientRect();scrollTo(0,scrollY+b.top+st.at.r*b.height-80);}};
document.readyState==='complete'?back():addEventListener('load',back);}
setInterval(()=>fetch('build.json').then(r=>r.json()).then(b=>{if(b.id!==body.dataset.build&&rp.hidden)location.reload();}).catch(()=>{}),1500);
// Specs not rendered yet: each side loads in a frame on localhost, another origin than this page's
// 127.0.0.1, so the spec's own scripts cannot reach this page or post to the server. ReSpec renders it
// there and RENDER posts the result up, which comes into this page only cleaned of anything that runs.
// The server keeps the renders and rebuilds, and the page reloads into the diff.
const frames=[...document.querySelectorAll('iframe.render')];
if(frames.length)Promise.all(frames.map(f=>new Promise(ok=>{const state=f.closest('section').querySelector('.state');
const fail=m=>{state.textContent='· not rendered: '+m;ok(null);},late=setTimeout(()=>fail('timed out'),120000);
addEventListener('message',e=>{if(e.source!==f.contentWindow)return;clearTimeout(late);
typeof e.data?.html==='string'?ok({id:f.dataset.id,html:inert(e.data.html)}):fail(e.data?.error||'no result');});
f.src='http://localhost:'+location.port+'/'+f.dataset.src;}))).then(rs=>{rs=rs.filter(Boolean);
if(rs.length)post('rendered',{renders:rs}).catch(e=>{pos.textContent='renders not saved: '+e.message;});});
function inert(h){const d=new DOMParser().parseFromString(h,'text/html');
d.querySelectorAll('script,iframe,frame,object,embed,base,meta,link,style,form').forEach(e=>e.remove());
for(const e of d.body.querySelectorAll('*'))for(const a of [...e.attributes])
if(/^on/i.test(a.name)||/^\\s*javascript:/i.test(a.value))e.removeAttribute(a.name);
return d.body.innerHTML;}
restore();status();
'''

# Runs in a spec's render frame, after the spec: once ReSpec is done, posts the page the rendered spec
# cut down to what is worth diffing. Navigation, the index and section numbers go (numbers would mark
# every heading after an added section); sections and heading wrappers are unwrapped, so headings,
# paragraphs and list items are the blocks the diff pairs. A framed ReSpec reports progress to its parent
# through parent.location, which a frame of another origin cannot read, and stops; so the frame
# keeps its parent for the result and tells ReSpec it has none.
RENDER = '''(async()=>{const up=parent,t=Date.now(),say=m=>up.postMessage(m,'*');window.parent=window;
while(!document.respec){if(Date.now()-t>60000)return say({error:'ReSpec did not load'});await new Promise(r=>setTimeout(r,200));}
try{await document.respec.ready;}catch(e){return say({error:'ReSpec failed: '+e});}
const b=document.body.cloneNode(true);
b.querySelectorAll('script,style,link,meta,iframe,#toc,#toc-nav,#index,#respec-ui,.dfn-panel,.self-link,.secno,.respec-button-copy-paste').forEach(e=>e.remove());
b.querySelectorAll('section,.header-wrapper').forEach(e=>e.replaceWith(...e.childNodes));
b.querySelectorAll('[id^="ref-for-"]').forEach(e=>e.removeAttribute('id'));
say({html:b.innerHTML});})();'''


def spec_id(repo, rev, index):
    """Names a spec's sources as they are at rev (None: the working tree): the hash of its directory."""
    d = os.path.dirname(index)
    if rev is not None:
        return git(repo, 'rev-parse', f'{rev}:{d}').stdout.decode().strip()
    h = hashlib.sha1()
    for f in sorted(git(repo, 'ls-files', '-co', '--exclude-standard', '--', d or '.').stdout.decode().splitlines()):
        h.update(f.encode() + b'\0' + (read(repo, None, f) or b'') + b'\0')
    return h.hexdigest()


def htmls(repo, base, head, old, new, out):
    """The file at base and at head (or in the working tree), rendered; '' for a side it does not exist
    on. Also the spec sides not rendered yet [(side, path, sources id)]: the page renders a spec."""
    if (new or old).endswith('.html'):
        sides = [(s, p, spec_id(repo, rev, p)) for s, rev, p in (('old', base, old), ('new', head, new)) if p]
        got = {s: out / 'rendered' / f'{i}.html' for s, _, i in sides}
        pending = [x for x in sides if not got[x[0]].exists()]
        text = lambda s: got[s].read_text() if s in got and got[s].exists() else ''
        return text('old'), text('new'), pending
    old_html = inline_images(render(git(repo, 'show', f'{base}:{old}').stdout), repo, os.path.dirname(old), base,
                             False) if old else ''
    new_html = inline_images(render(read(repo, head, new)), repo, os.path.dirname(new), head or base,
                             not head) if new else ''
    return old_html, new_html, []


def accepted_html(repo, base, head, out, old, new, old_html, new_html):
    """The file as accepted so far: the base until an accept moves a change in. When the base has moved
    since (a commit, a new export), the accepted changes that still read the same move onto the new base,
    and the others come back to review: the copy never keeps text the new base dropped. Accepts kept as
    keys in accepted.json, from before the copy existed, move in the first time it is needed."""
    name = new or old
    copy, was = out / 'accepted' / f'{name}.html', out / 'accepted' / f'{name}.base'
    if copy.exists():
        then = was.read_text() if was.exists() else None
        if then in (None, commit(repo, base)) or git(repo, 'cat-file', '-e', f'{then}^{{commit}}', check=False).returncode:
            return copy.read_text()
        then_html, _, pending = htmls(repo, then, head, old, new, out)
        if pending:
            return copy.read_text()
        keys = {k for _, _, ps in block_diff(then_html, copy.read_text(), 'x')[2] for k, _, _ in ps}
    else:
        legacy = out / 'accepted.json'
        keys = json.loads(legacy.read_text()).get(name, {}) if legacy.exists() else {}
    pieces = block_diff(old_html, new_html, 'x')[2] if keys else []
    found = {k for _, _, ps in pieces for k, _, _ in ps if k in keys}
    text = apply(pieces, 'old', found) if found else old_html
    if found or copy.exists():
        save_copy(repo, base, out, name, text)
    return text


def save_copy(repo, base, out, name, text):
    copy = out / 'accepted' / f'{name}.html'
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(text)
    (out / 'accepted' / f'{name}.base').write_text(commit(repo, base))


def commit(repo, rev):
    return git(repo, 'rev-parse', f'{rev}^{{commit}}').stdout.decode().strip()


def build(repo, base, head, files, build_id, out, keep=(), comments=None, new_label=None, publish=False):
    sections, nav = [], []
    for k, (old, new) in enumerate(files):
        name = new or old
        old_html, new_html, pending = htmls(repo, base, head, old, new, out)
        if pending:
            frames = ''.join(f'<iframe class="render" sandbox="allow-scripts allow-same-origin" data-id="{i}" '
                             f'data-src="src/{s}/{html.escape(p)}?render=1"></iframe>' for s, p, i in pending)
            sections.append(f'<section id="f{k}" data-file="{html.escape(name)}"><div class="file">{html.escape(name)}'
                            f' <span class=state>· rendering with ReSpec…</span></div>{frames}</section>')
            continue
        body, n, _ = block_diff(old_html, new_html, f'f{k}')
        if not n and name not in keep:
            continue
        copy = accepted_html(repo, base, head, out, old, new, old_html, new_html)
        review, todo, _ = block_diff(copy, new_html, f'r{k}')
        done, accepted, _ = block_diff(old_html, copy, f'a{k}')
        state = 'new file' if not old else 'deleted' if not new else f'renamed from {old}' if old != new else ''
        sid = f'f{k}'
        label = html.escape(name)
        nav.append(f'<a href="#{sid}">{label}</a> <span class="n">{todo} to review · {accepted} accepted</span>')
        plain = lambda h, missing: re.sub(r'\sid="[^"]*"', '', h) or f'<p class="n">({missing})</p>'
        bar = (('<button class="accf" data-a="accept">accept file</button>' if todo else '') +
               ('<button class="accf" data-a="unaccept">unaccept file</button>' if accepted else '') +
               (f'<button class="pub" data-n="{accepted}">publish accepted</button>' if publish and accepted else ''))
        view = lambda v, h: f'<div class="v v-{v}" data-view="{v}">{h}</div>'
        cs = (comments or {}).get(name, [])
        touched = sum(1 for c in cs if c.get('fate'))
        state = ' · '.join(filter(None, [state, cs and f'{len(cs)} open comment(s)' + (f', {touched} in text that changes' if touched else '')]))
        cm = ('<script type="application/json" class="cm">' + json.dumps(cs, ensure_ascii=False).replace('</', '<\\/') +
              '</script>') if cs else ''
        sections.append(f'<section id="{sid}" data-file="{label}">{cm}<div class="file">{label}'
                        f'{f" <span class=state>· {html.escape(state)}</span>" if state else ""}{bar}</div>'
                        f'{view("review", review)}{view("acc", done if accepted else "<p class=n>(nothing accepted yet)</p>")}'
                        f'{view("diff", body)}{view("old", plain(old_html, "new file"))}{view("new", plain(new_html, "deleted"))}'
                        f'</section>')
    title = f'docdiff · {repo.name} vs {base}' + (f'..{head}' if head else '')
    buttons = [('review', 'To review'), ('acc', 'Accepted'), ('diff', f'Diff vs {base}'), ('old', 'Old'), ('new', 'New')]
    seg = ''.join(f'<button data-v="{v}" aria-pressed="{str(v == "review").lower()}">{label}</button>' for v, label in buttons)
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{STYLE}</style></head><body class="show-review" data-view="review" data-repo="{html.escape(str(repo))}" data-build="{build_id}">
<header>{" · ".join(nav)}<span style="flex:1"></span>
<span class="seg">{seg}</span>
<span id="pos" class="n"></span><button id="prev" class="step">↑ prev</button><button id="next" class="step">next change ↓</button>
<button id="acc" class="step">✓ accept</button><button id="rev">revisit</button>
<span class="sides n">Old: {html.escape(side(repo, base))} — New: {html.escape(new_label or side(repo, head))}</span></header>
<main>{"".join(sections)}</main>
<div id="rp" hidden><blockquote id="rq"></blockquote><textarea id="rt" placeholder="What should change? ↵ sends, ⇧↵ adds a line"></textarea>
<button id="rs">Send</button> <button id="rc">Cancel</button></div>
<div id="pop" hidden><button id="pa">✓</button><button id="pr" title="Revisit (r)">✎</button></div>
<script>{SCRIPT}</script></body></html>''', len(sections)


def files_for(repo, base, head, paths):
    """Changed files [(old, new)], plus files named in paths that have no changes, so they can be
    revised as edits land; and the names of those."""
    files = changed_files(repo, base, head, paths)
    named = [p for p in paths if (repo / p).is_file() and p not in {new for _, new in files}]
    return files + [(p, p) for p in named], named


def rebuild(out):
    """The page for the run recorded in out, then build.json, so an open page reloads into it."""
    run = json.loads((out / '.run.json').read_text())
    repo = pathlib.Path(run['repo'])
    files, named = files_for(repo, run['base'], run.get('head'), run['paths'])
    build_id = str(time.time_ns())
    notes = pathlib.Path(run['comments']) if run.get('comments') else None
    comments = json.loads(notes.read_text()) if notes and notes.exists() else None
    page, shown = build(repo, run['base'], run.get('head'), files, build_id, out, named, comments, run.get('new_label'),
                        bool(run.get('publish')))
    (out / 'index.html').write_text(page)
    (out / 'build.json').write_text(json.dumps({'id': build_id}))
    return repo, run['base'], shown


def move(out, req, accept):
    """Accept one change, or the whole file, into the accepted copy, or take it back out; then rebuild."""
    run = json.loads((out / '.run.json').read_text())
    repo, base, head, name = pathlib.Path(run['repo']), run['base'], run.get('head'), req['file']
    old, new = next((f for f in files_for(repo, base, head, run['paths'])[0] if (f[1] or f[0]) == name), (None, None))
    old_html, new_html, pending = htmls(repo, base, head, old, new, out)
    if pending:
        raise LookupError('that spec is not rendered yet; reload the page')
    copy = accepted_html(repo, base, head, out, old, new, old_html, new_html)
    if req.get('all'):
        text = new_html if accept else old_html
    else:
        pieces = block_diff(copy, new_html, 'x')[2] if accept else block_diff(old_html, copy, 'x')[2]
        if req['key'] not in {k for _, _, ps in pieces for k, _, _ in ps}:
            raise LookupError('that change is gone; reload the page')
        text = apply(pieces, 'old' if accept else 'new', {req['key']})
    save_copy(repo, base, out, name, text)
    rebuild(out)


def publish(out, name):
    """Runs the --publish command on the file's accepted copy, written as Markdown, and returns what it
    printed; once it succeeds, the accepted copy starts over from the base, which the command updated."""
    run = json.loads((out / '.run.json').read_text())
    copy, target = out / 'accepted' / f'{name}.html', out / 'accepted' / f'{name}.md'
    if name not in {new or old for old, new in files_for(pathlib.Path(run['repo']), run['base'], run.get('head'), run['paths'])[0]}:
        raise LookupError('no such file in this diff')
    if not copy.exists():
        raise LookupError('nothing accepted in that file')
    # Links are the render's own (bare addresses), not the text's, and would come back as Markdown links.
    page = re.sub(r'</?a\b[^>]*>', '', copy.read_text())
    r = subprocess.run(['pandoc', '-f', 'html', '-t', 'gfm', '--wrap=none'], input=page, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f'pandoc: {r.stderr.strip()}')
    target.write_text(r.stdout)
    cmd = run['publish'].replace('{file}', shlex.quote(name)).replace('{target}', shlex.quote(str(target)))
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    printed = (r.stdout + r.stderr).strip()
    if r.returncode:
        raise RuntimeError(printed or f'{cmd}: exit {r.returncode}')
    for f in (copy, target, out / 'accepted' / f'{name}.base'):
        f.unlink(missing_ok=True)
    rebuild(out)
    return printed


def handler(out):
    """Static files from out with caching off, so a reload after a rebuild shows the new page, plus
    the page's POSTs: /accept and /unaccept move a change into or out of the accepted copy and rebuild
    the page; /revisit appends to revisit.jsonl; /rendered stores spec renders and rebuilds the page;
    /publish runs the --publish command on a file's accepted changes (see publish()).
    /src/old/<path> and /src/new/<path> serve a spec's sources from either side, for its render frame;
    with ?render=1 the entry file also gets RENDER, which posts the rendered spec to the page."""
    lock = threading.Lock()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(out), **kwargs)

        def end_headers(self):
            self.send_header('Cache-Control', 'no-store')
            super().end_headers()

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            m = re.fullmatch(r'/src/(old|new)/(.+)', urllib.parse.unquote(url.path))
            if not m:
                return super().do_GET()
            run = json.loads((out / '.run.json').read_text())
            path = os.path.normpath(m.group(2))
            rev = run['base'] if m.group(1) == 'old' else run.get('head')
            data = None if os.path.isabs(path) or path.startswith('..') else read(pathlib.Path(run['repo']), rev, path)
            if data is None:
                return self.send_error(404)
            if url.query == 'render=1':
                data += f'<script>{RENDER}</script>'.encode()
            self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(path)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            # Only the page posts. A spec's own scripts run in its render frame, on localhost: another
            # origin, though the same server.
            if self.headers.get('Origin') != f'http://127.0.0.1:{self.server.server_address[1]}':
                return self.send_error(403)
            req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with lock:
                if self.path in ('/accept', '/unaccept'):
                    try:
                        move(out, req, self.path == '/accept')
                    except LookupError as e:
                        return self.send_error(409, str(e))
                    reply = {}
                elif self.path == '/revisit':
                    with open(out / 'revisit.jsonl', 'a') as f:
                        f.write(json.dumps({'time': time.strftime('%Y-%m-%dT%H:%M:%S'), **req},
                                           ensure_ascii=False) + '\n')
                    reply = {}
                elif self.path == '/publish':
                    try:
                        data, code = publish(out, req['file']).encode(), 200
                    except (LookupError, RuntimeError) as e:
                        data, code = str(e).encode(), 409 if isinstance(e, LookupError) else 500
                    self.send_response(code)
                    self.send_header('Content-Type', 'text/plain; charset=utf-8')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    return self.wfile.write(data)
                elif self.path == '/rendered':
                    (out / 'rendered').mkdir(exist_ok=True)
                    for r in req['renders']:
                        if re.fullmatch(r'[0-9a-f]{40,64}', r['id']):
                            (out / 'rendered' / f'{r["id"]}.html').write_text(r['html'])
                    rebuild(out)
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
    p.add_argument('--head')
    p.add_argument('--out')
    p.add_argument('--comments')
    p.add_argument('--new-label')
    p.add_argument('--publish')
    p.add_argument('--serve', action='store_true')
    p.add_argument('paths', nargs='*')
    a = p.parse_args()
    if not shutil.which('pandoc'):
        sys.exit('pandoc not found: brew install pandoc')
    top = git(pathlib.Path(a.repo), 'rev-parse', '--show-toplevel').stdout.decode().strip()
    repo = pathlib.Path(top)
    for rev in filter(None, (a.base, a.head)):
        if git(repo, 'rev-parse', '--verify', '--quiet', f'{rev}^{{commit}}', check=False).returncode:
            sys.exit(f'unknown revision: {rev}')
    out = pathlib.Path(a.out) if a.out else pathlib.Path(tempfile.gettempdir()) / f'docdiff-{repo.name}'
    out.mkdir(parents=True, exist_ok=True)
    # The server rebuilds from this after an accept.
    (out / '.run.json').write_text(json.dumps({'repo': str(repo), 'base': a.base, 'head': a.head, 'paths': a.paths,
                                               'comments': a.comments and str(pathlib.Path(a.comments).resolve()),
                                               'new_label': a.new_label, 'publish': a.publish}))
    _, _, shown = rebuild(out)
    print(f'{shown} changed doc(s) in {repo} vs {a.base}{f"..{a.head}" if a.head else ""} → {out / "index.html"}',
          flush=True)
    if not shown:
        return
    if a.serve:
        serve(out)


if __name__ == '__main__':
    main()
