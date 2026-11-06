#!/usr/bin/env python3
"""Render a repo's uncommitted Markdown changes against a base revision as one inline-diff page.

Usage: docdiff.py [--repo PATH] [--base REV] [--head REV] [--out DIR] [--new-label TEXT] [--publish CMD]
                  [--serve] [plugins' options] [paths...]

paths are pathspecs relative to the repo root (default: every *.md, and the files plugins make docs of).

Old = the file at REV (default HEAD); new = the file at --head, or without it the working tree,
staged or not, untracked included. With --serve, serves --out on a free 127.0.0.1 port, or reuses a
server already serving it, and prints "URL: ...". The server also stores what the page sends, in
--out: accepted/<file>.html, the file as accepted so far, which To review is diffed against; revisit
requests in revisit.jsonl.

The page's header says what is compared (a plugin's title(), or else commits or the uncommitted edits)
and names both sides: a revision with its commit, or the working tree; --new-label TEXT names the new
side instead, for a working tree that stands for something else.

--publish CMD puts "publish accepted" in the header while any file has accepted changes; it publishes
each such file in turn. For each, it writes the accepted copy as Markdown and runs CMD in a shell, {file} replaced by the file's name and {target} by
that Markdown's path; CMD is to publish it and bring the base up to date, after which the accepted copy
starts over from the base. gdocsync's push --target, then its export, is such a command.

Plugins, the modules in plugins/, add their own arguments, docs and buttons (see load_plugins):
respec.py diffs ReSpec specs, gdoc_comments.py shows a Google Doc's comments (--comments), github_pr.py
links changes to a GitHub PR (--pr), review.py keeps review comments and shows revisits' answers.
"""
import argparse, base64, difflib, hashlib, html, http.server, importlib.util, json, mimetypes, os, pathlib, re
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
# Removed or rewritten text longer than this many words starts folded; shorter, it shows open.
FOLD = 200
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


def changed_files(repo, base, head, paths):
    """Changed Markdown files and plugins' docs, ReSpec specs say: [(old path or None, new path or None)]."""
    spec = ['--', *(paths or ['*.md', *(p for m in PLUGINS for p in getattr(m, 'PATHS', []))])]
    out = git(repo, 'diff', '--name-status', '--find-renames', base, *([head] if head else []), *spec).stdout.decode()
    names = []
    for line in out.splitlines():
        status, *ns = line.split('\t')
        names.append((None if status == 'A' else ns[0], None if status == 'D' else ns[-1]))
    if not head:
        untracked = git(repo, 'ls-files', '--others', '--exclude-standard', *spec).stdout.decode()
        names += [(None, f) for f in untracked.splitlines()]
    files, docs, memo = [], set(), {}
    for old, new in names:
        name = new or old
        if name.endswith('.md'):
            files.append((old, new))
            continue
        # Any other file stands for the doc a plugin makes of it, if one does.
        doc = next(filter(None, (m.doc(repo, (base, head), name, memo) for m in PLUGINS if hasattr(m, 'doc'))), None)
        if doc and doc not in docs:
            docs.add(doc)
            files.append(tuple(doc if read(repo, rev, doc) is not None else None for rev in (base, head)))
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


def nesting(page):
    """What a piece of a block does to its nesting: its block tags, less each open tag its close tag
    follows and each close tag an open tag of the same name follows (an item split in two)."""
    out = []
    for m in TAG.finditer(page):
        if not BLOCK.match(m.group(0)):
            continue
        t = (m.group(1), m.group(2).lower())
        if out and out[-1][1] == t[1] and out[-1][0] != t[0]:
            out.pop()
        else:
            out.append(t)
    return out


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
    # A change that opens block tags a later one closes (list items folded into a paragraph, say) is one
    # change with it: accepted alone, either would leave the accepted copy's tags unbalanced.
    olds, news, head, merged = {}, {}, None, {}
    for g, x, y in parts:
        if g:
            olds[g], news[g] = olds.get(g, '') + x, news.get(g, '') + y
    acc = ['', '']
    for g in sorted(sig):
        head = head or g
        merged[g] = head
        acc = [acc[0] + olds[g], acc[1] + news[g]]
        if nesting(acc[0]) == nesting(acc[1]):
            head, acc = None, ['', '']
    sigs = {}
    for g in sorted(sig):
        sigs.setdefault(merged[g], []).extend(sig[g])
    keys = {g: hashlib.sha1(' '.join(sigs[merged[g]]).encode()).hexdigest()[:12] for g in sig}
    page = re.sub(rf'data-g="{gid}-(\d+)"',
                  lambda m: f'data-g="{gid}-{merged[int(m.group(1))]}" data-k="{keys[int(m.group(1))]}"',
                  ''.join(out))
    return page, len({merged[g] for g in shown}), [(keys.get(g), x, y) for g, x, y in parts]


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
    """Match the blocks of a replaced run, keeping their order: [(old index or None, new index or None)].
    Between two matches, the removed blocks come before the added ones, as the old text came first."""
    out, i, added = [], 0, []
    for j, new in enumerate(news):
        best, at = PAIR, None
        for k in range(i, len(olds)):
            r = similarity(olds[k], new)
            if r > best:
                best, at = r, k
        if at is None:
            added.append((None, j))
            continue
        out += [(k, None) for k in range(i, at)] + added + [(at, j)]
        i, added = at + 1, []
    return out + [(k, None) for k in range(i, len(olds))] + added


def whole_blocks(run, gid):
    """A run of added, removed or rewritten blocks [(list tag, kind, old, new)] as one change under one
    key. Old text sits under a one-line summary, open when it is short. Consecutive removed blocks share
    one box, and so do consecutive added ones, paragraphs and list items alike."""
    key = hashlib.sha1('|'.join(f'{k}|{text(o)}|{text(w)}' for _, k, o, w in run).encode()).hexdigest()[:12]
    inner = lambda b: re.sub(r'^<li\b[^>]*>|</li>$', '', b)
    merged = []
    for lst, kind, old, new in run:
        if kind in ('del', 'add') and merged and merged[-1][1] == kind:
            merged[-1][2 if kind == 'del' else 3].append((lst, old or new))
        else:
            merged.append((lst, kind, [(lst, old)] if old else [], [(lst, new)] if new else []))
    out = []
    for lst, kind, olds, news in merged:
        # Several blocks go in one box outside any list, so one bar marks them all.
        lst = '' if len(olds) > 1 or len(news) > 1 else lst
        body = lambda bs: inner(bs[0][1]) if lst else wrap_lists(bs)
        was = ''
        if olds:
            lsts = {l for l, _ in olds}
            head = ' '.join(html.unescape(re.sub(r'<[^>]+>', lambda m: ' ' if BLOCK.match(m.group(0)) else '', olds[0][1])).split())
            head = head if len(head) <= 80 else head[:80].rsplit(' ', 1)[0] + '…'
            label = ('was' if kind == 'rw' else 'removed' if len(olds) == 1
                     else f'removed {len(olds)} {"paragraphs" if lsts == {""} else "items" if "" not in lsts else "blocks"}')
            short = len(' '.join(text(o) for _, o in olds).split()) <= FOLD
            was = (f'<details class="was"{" open" if short else ""}><summary>{label}: {html.escape(head)}</summary>'
                   f'<div class="old">{body(olds)}</div></details>')
        now = f'<div class="now">{body(news)}</div>' if news else ''
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
            # A change that adds or removes blocks reads as one rewrite: all its old text, then all its new
            # text. Only a change of one-to-one rewrites keeps each old block above its new one.
            if any(k != 'rw' for _, k, _, _ in run):
                both = list(zip(run, sides))
                run[:] = ([(s[0], 'del', r[2], None) for r, s in both if r[2]] +
                          [(s[1], 'add', None, r[3]) for r, s in both if r[3]])
                sides[:] = ([(s[0], '', s[2], '') for r, s in both if r[2]] +
                            [('', s[1], '', s[3]) for r, s in both if r[3]])
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


# The actions' words. On your own docs (the working tree, or a Google Doc's export) an accept takes a change
# into the accepted copy and a revisit asks for an edit; on commits (--head: a PR, say) there is nothing to
# edit, so an accept only records that the change was reviewed, and a revisit is a question about it.
WORDS = {
    False: {'accept': '✓ accept', 'unaccept': '↺ unaccept', 'accept_key': 'Accept (a)', 'unaccept_key': 'Unaccept (a)',
            'accept_title': 'Accept the current change (a)', 'unaccept_title': 'Unaccept the current change (a)',
            'accepted': 'accepted', 'Accepted': 'Accepted', 'accept_file': 'accept file', 'unaccept_file': 'unaccept file',
            'revisit': '✎ revisit', 'revisit_key': 'Revisit (r)', 'revisit_title': 'Revisit the selection or the current change (r)',
            'select': 'select text to revisit', 'question': 'What should change?', 'prompt': 'Revisit ', 'sent': 'revisit',
            'revisits': 'revisits'},
    True: {'accept': '✓ looks good', 'unaccept': '↺ not reviewed', 'accept_key': 'Looks good (a)', 'unaccept_key': 'Not reviewed (a)',
           'accept_title': 'Looks good: the current change is reviewed (a)',
           'unaccept_title': 'Not reviewed: the current change goes back to review (a)',
           'accepted': 'reviewed', 'Accepted': 'Reviewed', 'accept_file': 'file looks good', 'unaccept_file': 'file not reviewed',
           'revisit': '✎ ask', 'revisit_key': 'Ask (r)', 'revisit_title': 'Ask Claude about the selection or the current change (r)',
           'select': 'select text to ask about', 'question': 'What do you want to know?', 'prompt': 'Ask about ', 'sent': 'ask',
           'revisits': 'questions',
           # For a change that carries remarks for its author, which a plugin tells (see wordsOf).
           'reviewed': '✓ reviewed', 'reviewed_key': 'Reviewed (a)',
           'reviewed_title': 'Reviewed: you commented on it, so it leaves To review without saying it looks good (a)'},
}

STYLE = '''
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--ins:#dafbe1;--insb:#1a7f37;--del:#ffebe9;--delb:#cf222e;--rw:#bf8700;--code:#f6f8fa;--link:#0969da;--btn:#f6f8fa;--btnh:#eff2f5}
@media (prefers-color-scheme: dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--line:#3d444d;--ins:#033a16;--insb:#3fb950;--del:#4b1113;--delb:#f85149;--rw:#d29922;--code:#161b22;--link:#4493f8;--btn:#212830;--btnh:#262c36}}
body{background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0}
main{max-width:920px;margin:0 auto;padding:16px 16px 40vh;overflow-wrap:anywhere}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;z-index:1;display:flex;flex-wrap:wrap;gap:6px 18px;align-items:center;font-size:14px}
header a{color:var(--fg);font-weight:600} .n{color:var(--muted)} .what{flex-basis:100%;font-size:13px} .what b{font-size:15px}
button:disabled{opacity:.4;cursor:default}
button{font:inherit;padding:4px 10px;border:1px solid var(--line);background:var(--btn);color:var(--fg);border-radius:6px;cursor:pointer;transition:background-color .1s}
button:not(:disabled):hover{background:var(--btnh)} button:focus-visible{outline:2px solid var(--link);outline-offset:-2px}
.file{margin:48px 0 8px;padding:6px 10px;background:var(--code);border:1px solid var(--line);border-radius:6px;font:600 14px ui-monospace,Menlo,monospace;display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px}
.fbar{margin-left:auto;display:flex;flex-wrap:wrap;gap:6px}
.state{color:var(--muted);font-weight:400}
.accf{font:12px -apple-system,BlinkMacSystemFont,sans-serif;padding:1px 8px}
ins{background:var(--ins);text-decoration:none;box-shadow:inset 0 -2px var(--insb)}
del{background:var(--del);color:var(--delb);text-decoration:line-through}
.chg{cursor:pointer}
.blk{border-left:3px solid var(--line);padding-left:10px;margin-left:-13px} li.blk{margin-left:0;padding-left:6px}
.blk[data-kind=add]{border-color:var(--insb)} .blk[data-kind=del]{border-color:var(--delb)} .blk[data-kind=rw]{border-color:var(--rw)}
.blk[data-kind=del] .was .old{border-left:0;padding-left:0}
.was summary{color:var(--muted);font-size:13px;cursor:pointer} .was summary~*{margin-top:4px}
.was .old{color:var(--muted);border-left:2px solid var(--delb);padding-left:8px} .old>:first-child,.now>:first-child{margin-top:0}
code{background:var(--code);padding:1px 4px;border-radius:4px;font-size:90%}
pre{background:var(--code);padding:12px;overflow-x:auto}
img{max-width:100%}
table{border-collapse:collapse;display:block;overflow-x:auto;font-size:14px} td,th{border:1px solid var(--line);padding:4px 8px;vertical-align:top}
a{color:var(--link)} .cur{outline:2px solid #bf8700;outline-offset:2px}
body:not(.show-review) .v-review,body:not(.show-acc) .v-acc,body:not(.show-diff) .v-diff,body:not(.show-old) .v-old,body:not(.show-new) .v-new{display:none}
.show-old .step,.show-new .step{display:none}
body:not(.show-review) .accf[data-a=accept],body:not(.show-acc) .accf[data-a=unaccept]{display:none}
.seg,.pair{display:inline-flex;max-width:100%} .seg button,.pair button{border-radius:0;margin-left:-1px;white-space:nowrap}
.seg button:first-child,.pair button:first-child{border-radius:6px 0 0 6px;margin-left:0} .seg button:last-child,.pair button:last-child{border-radius:0 6px 6px 0}
.seg button[aria-pressed=true]{background:var(--fg);color:var(--bg);border-color:var(--fg)} .seg button[data-v=diff]{min-width:0;max-width:11em;overflow:hidden;text-overflow:ellipsis}
.bar{flex:1 1 100%;display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px} #pos{flex:1 1 14em;min-width:0;font-size:13px;font-variant-numeric:tabular-nums}
.tools{display:flex;flex-wrap:wrap;gap:6px;margin-left:auto}
header button,.panel button{height:28px;padding:0 12px;line-height:26px;font-size:13px;font-weight:500}
.primary{background:var(--ins);color:var(--insb);border-color:color-mix(in srgb,var(--insb) 40%,transparent)} .primary:not(:disabled):hover{background:color-mix(in srgb,var(--ins),var(--insb) 14%)}
.tools .step:disabled{opacity:1;color:color-mix(in srgb,var(--muted) 60%,var(--bg));background:var(--bg);border-color:var(--line)}
[data-key]::after{content:attr(data-key);display:inline-block;margin-left:7px;padding:0 4px;min-width:7px;border:1px solid color-mix(in srgb,currentColor 25%,transparent);border-radius:4px;font:11px/15px ui-monospace,SFMono-Regular,Menlo,monospace;color:color-mix(in srgb,currentColor 70%,transparent);text-align:center;vertical-align:1px}
.seg[data-key]::after{align-self:center;color:var(--muted);border-color:var(--line)} @media (max-width:640px){[data-key]::after{display:none}}
.panel{position:fixed;left:0;right:0;bottom:0;background:var(--bg);border-top:1px solid var(--line);box-shadow:0 -2px 8px rgba(0,0,0,.06);padding:12px max(16px,calc(50% - 444px));z-index:2;font-size:14px;display:flex;flex-wrap:wrap;align-items:center;justify-content:flex-end;gap:8px}
.panel[hidden]{display:none} #pt{flex:1 0 100%;white-space:pre-wrap;max-height:40vh;overflow:auto;font:13px ui-monospace,Menlo,monospace} .panel>blockquote,.panel>textarea,.panel>#rx{flex:1 0 100%;box-sizing:border-box} #rx{font-size:12px} .panel .hint{margin-right:auto;font-size:12px}
#pop{position:absolute;z-index:3;display:flex;background:var(--bg);border:1px solid var(--line);border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.12);overflow:hidden}
#pop[hidden]{display:none} #pop button,#pop a{border:0;border-radius:0;background:none;color:var(--muted);padding:2px 9px;font-size:14px;line-height:1.5;text-decoration:none}
#pop button:hover,#pop a:hover{background:var(--code);color:var(--fg)} #pop button+button,#pop button+a{border-left:1px solid var(--line)}
.panel blockquote{margin:0;padding:2px 10px;border-left:3px solid var(--line);color:var(--muted);font-size:13px;max-height:6em;overflow:auto;white-space:pre-wrap}
.panel textarea{display:block;width:100%;box-sizing:border-box;min-height:4.5em;margin:0;font:inherit;line-height:1.5;resize:vertical;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 10px}
.panel textarea:focus{outline:none;border-color:var(--link);box-shadow:0 0 0 3px color-mix(in srgb,var(--link) 20%,transparent)}
'''

SCRIPT = '''
const $=id=>document.getElementById(id),body=document.body,repo=body.dataset.repo,W=JSON.parse($('words').textContent);
// The words for accepting change x (null: none current); a plugin may set its own, per change.
let wordsOf=x=>W;
// Runs before changes xs are accepted (accepting) or taken back, one or a whole file's; a plugin may set
// its own, to act on what is on them.
let onAccept=async(xs,accepting)=>{};
const pos=$('pos'),next=$('next'),prev=$('prev'),acc=$('acc'),rev=$('rev'),rp=$('rp'),rq=$('rq'),rt=$('rt'),pop=$('pop'),pa=$('pa'),pr=$('pr');
// Changes per view: To review (accepted copy to now), Accepted (base to accepted copy), Diff vs base.
const groups=[],byG=new Map();
for(const e of document.querySelectorAll('.v-review .chg,.v-acc .chg,.v-diff .chg')){const v=e.closest('.v').dataset.view;
let x=byG.get(v+e.dataset.g);if(!x){const s=e.closest('section');x={k:e.dataset.k,file:s.dataset.file,sec:s,view:v,els:[]};
byG.set(v+e.dataset.g,x);groups.push(x);}x.els.push(e);}
let cur=body.dataset.view,c=null,anchor=null,mark=null,req=null;
const gi=x=>groups.indexOf(x),L=()=>groups.filter(x=>x.view===cur),verb={review:'accept',acc:'unaccept'};
// A change's removed (-) or added (+) text: an inline <del>/<ins>, or a whole block's old/new part.
const part=(e,s)=>e.classList.contains('blk')?e.querySelector(s==='-'?'.old':'.now')?.textContent||'':e.tagName===(s==='-'?'DEL':'INS')?e.textContent:'';
const txt=(x,s)=>x.els.map(e=>part(e,s)).join(' ').replace(/\\s+/g,' ').trim();
function summary(x){const d=txt(x,'-'),i=txt(x,'+');return [d&&'−'+d,i&&'+'+i].filter(Boolean).join(' ').slice(0,300);}
// n and p step from the current change; with none, from the height clicked last (mark, in page
// coordinates) or else the change current last, while on screen; otherwise from the top of the screen.
function step(d){const l=L(),top=document.querySelector('header').getBoundingClientRect().bottom;
const m=c&&c.view===cur?c:mark===null&&anchor&&anchor.view===cur&&inView(anchor)?anchor:null;
if(m)return (d>0?l.find(x=>gi(x)>gi(m)):l.findLast(x=>gi(x)<gi(m)))||null;
const y=mark!==null&&mark-scrollY>top&&mark-scrollY<innerHeight?mark-scrollY:top,r=x=>x.els.find(e=>e.offsetParent)?.getBoundingClientRect();
return (d>0?l.find(x=>r(x)?.bottom>y):l.findLast(x=>r(x)?.top<y))||null;}
function status(){const l=L(),i=l.indexOf(c);prev.disabled=!step(-1);next.disabled=!step(1);
const w=wordsOf(c);acc.disabled=!c||!verb[cur];acc.textContent=cur==='acc'?w.unaccept:w.accept;acc.classList.toggle('primary',cur!=='acc');
acc.title=cur==='acc'?w.unaccept_title:w.accept_title;
pos.textContent=cur==='old'||cur==='new'?W.select:i>=0?'change '+(i+1)+' of '+l.length+' · '+c.file:
(l.length||'nothing')+({review:' to review',acc:' '+W.accepted}[cur]||' changes');}
function focus(x,scroll=true){hidePop();if(c)c.els.forEach(e=>e.classList.remove('cur'));c=x;if(x){anchor=x;mark=null;}
if(c){c.els.forEach(e=>e.classList.add('cur'));if(scroll)c.els.find(e=>e.offsetParent)?.scrollIntoView({block:'center',behavior:'smooth'});}status();}
function go(d){const x=step(d);if(x)focus(x);}
async function post(url,data){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
if(!r.ok)throw new Error(url+' '+r.status);return r.json();}
// An accept moves the change into the accepted copy, an unaccept takes it back out; the server rebuilds
// the page and it reloads. It stays where it is: the next change becomes current only if it is already
// in view; otherwise nothing is current and n goes to it.
async function accept(){if(!c||!verb[cur])return;const x=c,l=L(),i=l.indexOf(x),nx=l[i+1]||l[i-1]||null;
try{await onAccept([x],cur==='review').catch(()=>{});await post(verb[cur],{file:x.file,key:x.k});}catch(e){pos.textContent='not saved: '+e.message+' (is the docdiff server running?)';return;}
focus(nx&&inView(nx)?nx:null,false);if(!c)anchor=l[i-1]||null;location.reload();}
function inView(x){const top=document.querySelector('header').getBoundingClientRect().bottom;
return x.els.some(e=>{const r=e.getBoundingClientRect();return e.offsetParent&&r.bottom>top&&r.top<innerHeight;});}
document.querySelectorAll('.accf').forEach(b=>b.onclick=async()=>{const s=b.closest('section'),a=b.dataset.a==='accept';
try{await onAccept(groups.filter(x=>x.sec===s&&x.view===(a?'review':'acc')),a).catch(()=>{});await post(b.dataset.a,{file:s.dataset.file,all:true});
location.reload();}catch(e){pos.textContent='not saved: '+e.message;}});
// Publish runs the --publish command on each file's accepted changes in turn. Its panel asks first, shows
// which file is being published, then what each printed; the page reloads on Done. (No confirm() or
// alert(): an embedded browser may block them, and then nothing would happen.)
const pb=document.getElementById('pub');if(pb){const pp=document.getElementById('pp'),pt=document.getElementById('pt'),
pc=document.getElementById('pc'),ps=document.getElementById('ps'),fs=JSON.parse(pb.dataset.files);
pb.onclick=()=>{pt.textContent='Publish the accepted changes?\\n\\n'+fs.map(([f,n])=>f+': '+n+' change(s)').join('\\n');pp.hidden=false;ps.focus();};
pc.onclick=()=>{pp.hidden=true;};
ps.onclick=async()=>{if(ps.dataset.done){location.reload();return;}pc.hidden=true;ps.disabled=pb.disabled=true;const said=[];
for(const[i,[f]]of fs.entries()){pb.textContent=ps.textContent='publishing '+(i+1)+' of '+fs.length+'…';pt.textContent=[...said,'Publishing '+f+'…'].join('\\n\\n');
try{const r=await fetch('publish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:f})});
said.push((r.ok?'✓ Published ':'✗ Not published ')+f+'\\n'+(await r.text()).trim());}catch(e){said.push('✗ Not published '+f+': '+e.message);}}
pt.textContent=said.join('\\n\\n');ps.textContent='Done';ps.disabled=false;ps.dataset.done=1;ps.focus();};}
function hidePop(){pop.hidden=true;popX=null;}
// Plugins' buttons per change (see load_plugins): [label, key, href], href opened in a new tab.
const acts=new Map([...document.querySelectorAll('script.acts')].map(j=>[j.closest('section'),JSON.parse(j.textContent)]));
const actsOf=x=>x&&acts.get(x.sec)?.[x.k]||[];let popX=null;
// Under a clicked change x: accept, revisit and the plugins' buttons. Under the end of a text selection: revisit only.
function showPop(r,withAccept,x){pa.hidden=!withAccept;pa.textContent=cur==='acc'?'↺':'✓';pa.title=cur==='acc'?wordsOf(x).unaccept_key:wordsOf(x).accept_key;popX=x||null;
pop.querySelectorAll('.act').forEach(b=>b.remove());
for(const[label,key,href]of actsOf(x)){const b=document.createElement('a');b.className='act';b.textContent=label;b.title=label+' ('+key+')';
b.href=href;b.target='_blank';b.rel='noopener';b.onmousedown=e=>e.preventDefault();b.onclick=hidePop;pop.append(b);}
pop.hidden=false;
pop.style.left=Math.max(8,Math.min(r.left,innerWidth-pop.offsetWidth-8))+scrollX+'px';pop.style.top=r.bottom+scrollY+4+'px';}
// A link in the doc does not move the page off what is being read: one into the doc, or to another doc
// (links are not rewritten), does nothing, and one to the web opens in a new tab; with a modifier key
// the browser follows it as usual.
document.addEventListener('click',e=>{const a=e.target.closest('main a[href]');if(!a||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||e.button)return;
e.preventDefault();if(/^https?:/.test(a.getAttribute('href')))window.open(a.href,'_blank','noopener');});
// Clicks on the page's controls (plugins mark theirs .ui) are not clicks on the text.
document.addEventListener('click',e=>{if(e.target.closest('header,#pop,.panel,.ui')||!getSelection().isCollapsed)return;
// A removed block's summary only folds and unfolds it.
if(e.target.closest('summary')){hidePop();return;}
const t=e.target.closest('.v-'+cur+' .chg');
if(t){focus(groups.find(x=>x.view===cur&&x.els.includes(t)),false);showPop(t.getBoundingClientRect(),!!verb[cur],c);}
else{mark=e.clientY+scrollY;focus(null,false);}});
// Without a current change, where n and p start moves with the screen.
addEventListener('scroll',()=>{if(!c)status();},{passive:true});
// Runs once the click has settled: clicking inside a selection clears it only after the click handler.
document.addEventListener('mouseup',e=>{if(e.target.closest('header,#pop,.panel,.ui'))return;setTimeout(()=>{const s=getSelection();
if(s.isCollapsed||!s.rangeCount){if(!popX)hidePop();return;}
if(!s.anchorNode.parentElement?.closest('main .v'))return;
mark=e.clientY+scrollY;if(!c)status();
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
return c?{quote:summary(c),el:c.els[0],key:c.k}:null;}
function heading(el){const v=el.closest('.v');if(!v)return '';
const h=[...v.querySelectorAll('h1,h2,h3,h4')].filter(h=>h.compareDocumentPosition(el)&Node.DOCUMENT_POSITION_FOLLOWING).pop();
if(!h)return '';const k=h.cloneNode(true);k.querySelectorAll('del').forEach(d=>d.remove());return k.textContent.trim();}
// g: the text revisited (quote), an element in it (el), its change's key, fields plugins add to the
// request, and words on what it goes on from (context), shown above the quote.
function openRev(g){const s=g&&g.el.closest('section');if(!s){pos.textContent='select text, or step to a change, then press r';return;}
req={file:s.dataset.file,section:heading(g.el),quote:g.quote,view:cur,key:g.key,...g.fields};rq.textContent=g.quote;
$('rx').textContent=g.context||'';$('rx').hidden=!g.context;rp.hidden=false;rt.focus();}
function closeRev(){rp.hidden=true;rt.value='';req=null;}
async function copy(t){try{await navigator.clipboard.writeText(t);return true;}
catch(e){const a=document.createElement('textarea');a.value=t;body.append(a);a.select();const done=document.execCommand('copy');a.remove();return done;}}
async function send(){const ask=rt.value.trim();if(!ask||!req)return;
const text=W.prompt+repo+'/'+req.file+(req.section?', section "'+req.section+'"':'')+(req.view==='old'?' (old version)':'')+':\\n> '+req.quote.replace(/\\n/g,'\\n> ')+'\\n\\n'+ask;
let queued=true;try{await post('revisit',{...req,prompt:ask,text});}catch(e){queued=false;}
// A watcher touches the file every second while it waits; with none, the clipboard is the way in.
const watched=queued&&await watching();
closeRev();pos.textContent=W.sent+': '+(watched?'sent, Claude is watching':await copy(text)?
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
if(e.key==='n'||e.key==='j')go(1);if(e.key==='p'||e.key==='k')go(-1);if(e.key==='a')accept();
const a=actsOf(c).find(a=>a[1]===e.key);if(a)open(a[2],'_blank','noopener');});
// Every reload keeps the view, the current change and the place in the doc; a rebuild reloads the page,
// unless a panel is open (a revisit, or a plugin's, being typed).
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
setInterval(()=>fetch('build.json').then(r=>r.json()).then(b=>{if(b.id!==body.dataset.build&&!document.querySelector('.panel:not([hidden])'))location.reload();}).catch(()=>{}),1500);
restore();status();
// A link to #file=<name> (from gdocsync's docs list, say) opens the page at that doc, just below the header,
// and &view=<view> in that view.
{const at=()=>{const q=new URLSearchParams(location.hash.slice(1)),want=q.get('file')||'',s=want&&[...document.querySelectorAll('section')].find(s=>s.dataset.file===want);
if(!want)return;history.replaceState(null,'',location.pathname);if(views.includes(q.get('view')))view(q.get('view'));
const go=()=>scrollTo(0,s.getBoundingClientRect().top+scrollY-document.querySelector('header').offsetHeight-8);
if(s)document.readyState==='complete'?go():addEventListener('load',go);};at();addEventListener('hashchange',at);}
'''


def htmls(repo, base, head, old, new, out):
    """The file at base and at head (or in the working tree), rendered; '' for a side it does not exist
    on. Also, while a plugin's doc is not rendered yet, what to show for it instead (see load_plugins);
    None for a doc that is."""
    for m in PLUGINS:
        got = hasattr(m, 'htmls') and m.htmls(repo, base, head, old, new, out)
        if got:
            return got
    old_html = inline_images(render(git(repo, 'show', f'{base}:{old}').stdout), repo, os.path.dirname(old), base,
                             False) if old else ''
    new_html = inline_images(render(read(repo, head, new)), repo, os.path.dirname(new), head or base,
                             not head) if new else ''
    return old_html, new_html, None


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
        then_html, _, wait = htmls(repo, then, head, old, new, out)
        if wait:
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


def load_plugins():
    """The modules in plugins/, each with this module as `docdiff` in it, for git(), read() and the
    like. run below is the run's arguments as .run.json holds them, repo and out as full paths. A plugin
    may define:

    add_args(parser): its arguments, which go into .run.json with the rest.
    PATHS and doc(repo, (base, head), path, memo): pathspecs of the files it makes docs of, beside *.md,
      and the doc a changed file stands for, None for none; memo lasts one listing of the changes.
    htmls(repo, base, head, old, new, out): None for a doc not its own, else as htmls() returns it: both
      sides rendered, and while they are not, (state, html) for the file's section, words for its bar
      and what gets the page to render it.
    section(run, name): None, or (state, html) to add to a file's section: words for its bar, data for
      the page.
    title(run): None where the plugin does not apply, or else (what the page compares, a link or None)
      for the header's first line, in place of "Uncommitted edits" or "Commits".
    top(run): None, or html to put above the docs, such as gdoc_docs.py's list of them; a page with it
      is served even when no doc changed.
    actions(run): None where the plugin does not apply, or else a function (old path, new path, blocks)
      that returns buttons for the popup of a file's changes, {change key: [(label, key, href)]}; the
      button opens href in a new tab, and so does the key on the current change. blocks is the file's
      diff as text, per block [(change key or None, old text, new text)], as block_diff() pairs them.
    place(run, old path, new path, side, before, after): where text of a file's block is in its source,
      for other plugins: github_pr.py's (see there).
    remarks(run, out, refresh): comments made on the docs elsewhere, for other plugins, one source per
      plugin: github_pr.py's reads the PR's on GitHub, gdoc_comments.py's the Google Docs' (see there).
    STYLE, SCRIPT: CSS and JavaScript for the page, after docdiff's own. An element of class panel holds
      off the reload after a rebuild while it shows, and clicks in it leave the doc alone, as they do in
      an element of class ui (a card or side panel), which does not hold off the reload; openRev(g) opens
      the revisit panel on g.quote, g.fields adding to the request it sends and g.context shown above
      it; wordsOf(x), which a plugin may replace, gives the words for accepting change x, W (WORDS for
      the run's mode) by default; onAccept(xs, accepting), which it may replace too, runs before changes
      xs are accepted or taken back.
    get(run, path, query), post(out, path, req): the server's routes. None for a path not its own, else
      (data, content type) with data None for a 404, or the reply to a post, after which the page
      rebuilds, unless the path is in the plugin's IN_PLACE: posts the page shows in place."""
    mods = []
    for f in sorted((pathlib.Path(__file__).parent / 'plugins').glob('*.py')):
        spec = importlib.util.spec_from_file_location(f'docdiff_{f.stem}', f)
        mods.append(importlib.util.module_from_spec(spec))
        mods[-1].docdiff = sys.modules[__name__]
        spec.loader.exec_module(mods[-1])
    return mods


PLUGINS = load_plugins()


def plugin_actions(actors, old, new, *diffs):
    """The buttons plugins give a file's changes, from the pieces of each view's block_diff()."""
    acts = {}
    for act in actors:
        got = {}
        for pieces in diffs:
            got.update(act(old, new, [[(key, text(o), text(w)) for key, o, w in parts] for _, _, parts in pieces]))
        for key, a in got.items():
            acts.setdefault(key, []).extend(a)
    return acts


def build(repo, base, head, files, build_id, out, keep=(), run=None, new_label=None, publish=False, actors=()):
    sections, nav, words, pubs = [], [], WORDS[bool(head)], []
    none_yet = f'<p class=n>(nothing {words["accepted"]} yet)</p>'
    for k, (old, new) in enumerate(files):
        name = new or old
        old_html, new_html, wait = htmls(repo, base, head, old, new, out)
        if wait:
            sections.append(f'<section id="f{k}" data-file="{html.escape(name)}"><div class="file">{html.escape(name)}'
                            f' <span class=state>· {html.escape(wait[0])}</span></div>{wait[1]}</section>')
            continue
        body, n, pieces = block_diff(old_html, new_html, f'f{k}')
        if not n and name not in keep:
            continue
        copy = accepted_html(repo, base, head, out, old, new, old_html, new_html)
        review, todo, review_pieces = block_diff(copy, new_html, f'r{k}')
        done, accepted, done_pieces = block_diff(old_html, copy, f'a{k}')
        acts = plugin_actions(actors, old, new, review_pieces, done_pieces, pieces)
        state = 'new file' if not old else 'deleted' if not new else f'renamed from {old}' if old != new else ''
        sid = f'f{k}'
        label = html.escape(name)
        # A doc shown only because it was named, with nothing changed, stays out of the header.
        if n:
            nav.append(f'<a href="#{sid}">{label}</a> <span class="n">{todo} to review · {accepted} {words["accepted"]}</span>')
        plain = lambda h, missing: re.sub(r'\sid="[^"]*"', '', h) or f'<p class="n">({missing})</p>'
        bar = ((f'<button class="accf" data-a="accept">{words["accept_file"]}</button>' if todo else '') +
               (f'<button class="accf" data-a="unaccept">{words["unaccept_file"]}</button>' if accepted else ''))
        if publish and accepted:
            pubs.append([name, accepted])
        view = lambda v, h: f'<div class="v v-{v}" data-view="{v}">{h}</div>'
        # What plugins add to the file: words for its bar, data for the page.
        extra = [x for x in (m.section(run, name) for m in PLUGINS if hasattr(m, 'section')) if x]
        state = ' · '.join(filter(None, [state, *(s for s, _ in extra)]))
        data = ''.join(h for _, h in extra) + (('<script type="application/json" class="acts">' +
                                                json.dumps(acts, ensure_ascii=False).replace('</', '<\\/') + '</script>') if acts else '')
        sections.append(f'<section id="{sid}" data-file="{label}">{data}<div class="file">{label}'
                        f'{f" <span class=state>· {html.escape(state)}</span>" if state else ""}'
                        f'{f"<span class=fbar>{bar}</span>" if bar else ""}</div>'
                        f'{view("review", review)}{view("acc", done if accepted else none_yet)}'
                        f'{view("diff", body)}{view("old", plain(old_html, "new file"))}{view("new", plain(new_html, "deleted"))}'
                        f'</section>')
    # What is compared: a plugin's words (a PR, a Google Doc), or else commits or the uncommitted edits.
    what, href = next((t for t in (m.title(run) for m in PLUGINS if run and hasattr(m, 'title')) if t), None) or (
        (f'Commits · {repo.name} · {base}..{head}' if head else f'Uncommitted edits · {repo.name}'), None)
    shown = f'<a href="{html.escape(href)}" target="_blank" rel="noopener">{html.escape(what)}</a>' if href else html.escape(what)
    title = f'docdiff · {what}'
    # One button publishes every file with accepted changes, each in turn.
    pub = (f'<button id="pub" data-files="{html.escape(json.dumps(pubs, ensure_ascii=False))}" '
           f'title="Publish the accepted changes of {len(pubs)} file(s)">⇪ publish accepted ({sum(n for _, n in pubs)})</button>'
           if pubs else '')
    buttons = [('review', 'To review'), ('acc', words['Accepted']), ('diff', f'Diff vs {base}'), ('old', 'Old'), ('new', 'New')]
    seg = ''.join(f'<button data-v="{v}" aria-pressed="{str(v == "review").lower()}">{label}</button>' for v, label in buttons)
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{STYLE}{"".join(getattr(m, 'STYLE', '') for m in PLUGINS)}</style></head><body class="show-review" data-view="review" data-repo="{html.escape(str(repo))}" data-build="{build_id}">
<header><span class="what"><b>{shown}</b><span class="n"> · Old: {html.escape(side(repo, base))} — New: {html.escape(new_label or side(repo, head))}</span></span>
{" · ".join(nav)}<span style="flex:1"></span>
<span class="seg" data-key="v" title="v cycles the views">{seg}</span>
<div class="bar"><span id="pos" class="n"></span><span class="tools"><span class="pair step"><button id="prev" class="step" data-key="p" title="Previous change (p or k)">↑ prev</button><button id="next" class="step" data-key="n" title="Next change (n or j)">next change ↓</button></span>
<button id="acc" class="step primary" data-key="a">{words['accept']}</button><button id="rev" data-key="r" title="{words['revisit_title']}">{words['revisit']}</button>{pub}</span></div></header>
<main>{page_top(run)}{"".join(sections)}</main>
<div id="rp" class="panel" hidden><div id="rx" class="n" hidden></div><blockquote id="rq"></blockquote><textarea id="rt" placeholder="{words['question']}"></textarea>
<span class="hint n">⇧↵ adds a line</span><button id="rc" data-key="esc">Cancel</button><button id="rs" class="primary" data-key="↵">Send</button></div>
{'<div id="pp" class="panel" hidden><div id="pt"></div><button id="pc">Cancel</button><button id="ps" class="primary">Publish</button></div>' if pub else ''}
<div id="pop" hidden><button id="pa">✓</button><button id="pr" title="{words['revisit_key']}">✎</button></div>
<script type="application/json" id="words">{json.dumps(words, ensure_ascii=False)}</script>
<script>{SCRIPT}{"".join(getattr(m, 'SCRIPT', '') for m in PLUGINS)}</script></body></html>''', len(sections)


def page_top(run):
    """What plugins put above the docs (their top())."""
    return ''.join(t for t in (m.top(run) for m in PLUGINS if run and hasattr(m, 'top')) if t)


def files_for(repo, base, head, paths):
    """Changed files [(old, new)], plus files named in paths that have no changes, so they can be
    revised as edits land, files named in paths in their order; and the names of those."""
    files = changed_files(repo, base, head, paths)
    named = [p for p in paths if (repo / p).is_file() and p not in {new for _, new in files}]
    place = {p: i for i, p in enumerate(paths)}
    return sorted(files + [(p, p) for p in named], key=lambda f: place.get(f[1] or f[0], len(place))), named


def rebuild(out):
    """The page for the run recorded in out, then build.json, so an open page reloads into it."""
    run = json.loads((out / '.run.json').read_text())
    repo = pathlib.Path(run['repo'])
    files, named = files_for(repo, run['base'], run.get('head'), run['paths'])
    build_id = str(time.time_ns())
    actors = [f for f in (m.actions(run) for m in PLUGINS if hasattr(m, 'actions')) if f]
    page, shown = build(repo, run['base'], run.get('head'), files, build_id, out, named, run, run.get('new_label'),
                        bool(run.get('publish')), actors)
    (out / 'index.html').write_text(page)
    (out / 'build.json').write_text(json.dumps({'id': build_id, 'from': fingerprint(run)}))
    return repo, run['base'], shown


def fingerprint(run):
    """What a build was made from: the revisions, and the working tree's changed files with their sizes and
    times, so the server can tell when the repo has moved on (a commit, an edit, a new export)."""
    repo = pathlib.Path(run['repo'])
    revs = [git(repo, 'rev-parse', '--verify', '--quiet', r, check=False).stdout.decode().strip()
            for r in filter(None, (run['base'], run.get('head')))]
    if run.get('head'):
        return ' '.join(revs)
    marks = []
    for line in git(repo, 'status', '--porcelain', '-uall', check=False).stdout.decode().splitlines():
        try:
            s = (repo / line[3:].split(' -> ')[-1]).stat()
            marks.append(f'{line}:{s.st_size}:{s.st_mtime_ns}')
        except OSError:
            marks.append(line)
    return ' '.join(revs) + '|' + '|'.join(marks)


def move(out, req, accept):
    """Accept one change, or the whole file, into the accepted copy, or take it back out; then rebuild."""
    run = json.loads((out / '.run.json').read_text())
    repo, base, head, name = pathlib.Path(run['repo']), run['base'], run.get('head'), req['file']
    old, new = next((f for f in files_for(repo, base, head, run['paths'])[0] if (f[1] or f[0]) == name), (None, None))
    old_html, new_html, wait = htmls(repo, base, head, old, new, out)
    if wait:
        raise LookupError('that file is not rendered yet; reload the page')
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
    the page; /revisit appends to revisit.jsonl; /publish runs the --publish command on a file's
    accepted changes (see publish()). Plugins add routes of their own (see load_plugins)."""
    lock = threading.Lock()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(out), **kwargs)

        def end_headers(self):
            self.send_header('Cache-Control', 'no-store')
            super().end_headers()

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            run = json.loads((out / '.run.json').read_text())
            # An open page asks for build.json every second or two: when the repo has moved on since the
            # build, rebuild first, so the page reloads into what the repo now holds.
            if url.path == '/build.json':
                with lock:
                    if json.loads((out / 'build.json').read_text()).get('from') != fingerprint(run):
                        rebuild(out)
            got = next(filter(None, (m.get(run, urllib.parse.unquote(url.path), url.query)
                                     for m in PLUGINS if hasattr(m, 'get'))), None)
            if not got:
                return super().do_GET()
            data, mime = got
            if data is None:
                return self.send_error(404)
            self.send_response(200)
            self.send_header('Content-Type', mime)
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
                else:
                    reply = next(filter(lambda r: r is not None, (m.post(out, self.path, req)
                                                                  for m in PLUGINS if hasattr(m, 'post'))), None)
                    if reply is None:
                        return self.send_error(404)
                    if self.path not in {p for m in PLUGINS for p in getattr(m, 'IN_PLACE', ())}:
                        rebuild(out)
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return Handler


def serve(out):
    state = out / '.server.json'
    last = json.loads(state.read_text())['port'] if state.exists() else 0
    if last:
        try:
            served = urllib.request.urlopen(f'http://127.0.0.1:{last}/.server.json', timeout=1).read()
        except OSError:
            served = None
        if served is not None and served.decode() == state.read_text():
            print(f'URL: http://127.0.0.1:{last}/index.html (server already running)', flush=True)
            return
    # The last server's port first, so its address, open tabs and links to it outlive a restart.
    try:
        server = http.server.ThreadingHTTPServer(('127.0.0.1', last), handler(out))
    except OSError:
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
    p.add_argument('--new-label')
    p.add_argument('--publish')
    p.add_argument('--serve', action='store_true')
    p.add_argument('paths', nargs='*')
    for m in PLUGINS:
        if hasattr(m, 'add_args'):
            m.add_args(p)
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
    # The server rebuilds from this after an accept; plugins read their arguments from it.
    run = {**vars(a), 'repo': str(repo), 'out': str(out.resolve())}
    (out / '.run.json').write_text(json.dumps(run))
    _, _, shown = rebuild(out)
    print(f'{shown} changed doc(s) in {repo} vs {a.base}{f"..{a.head}" if a.head else ""} → {out / "index.html"}',
          flush=True)
    if not shown and not page_top(run):
        return
    if a.serve:
        serve(out)


if __name__ == '__main__':
    main()
