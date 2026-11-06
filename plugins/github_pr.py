"""--pr: a change's popup opens the change on GitHub, in the PR's Files changed view at its line, where
review comments go.

A change goes to the line of its first added word in the head, or, when it only removes, of its first
removed word in the base. The change's block (paragraph, list item, heading...) is matched word by word
against the files the PR changes: the file holding the block's longest run of words is the one, and
aligning the block with it puts the change's word on a line. Tags and punctuation do not count, so
ReSpec's rewrites ([=term=], {{IDL}}, |var|) cost a spec's source only the words they change. A Markdown
file's words take their lines from pandoc's source positions. A change with no run of RUN words in a
row anywhere links to its file without a line, or to the PR's files when a spec has several. place()
finds other text of a block the same way, for other plugins: review comments.

remarks() reads the PR's own comments from GitHub, for other plugins too, and puts each line comment
back on the diff the other way round: the words of its line against the blocks of the diff, with me, your
login (--me, or GITHUB_TOKEN's user), so yours count as your review. Read only: nothing is ever posted.
"""
# docdiff, the running docdiff.py, is set by its load_plugins().
import difflib, hashlib, html, json, os, pathlib, re, subprocess, sys, threading, time, urllib.error, urllib.request

LABEL, KEY = 'GitHub ↗', 'g'
WORD = re.compile(r'\w+')
# Words of the change's block matched on each side of the change, at most.
CONTEXT = 40
# The fewest words in a row that place a change.
RUN = 4
API, AGENT = 'https://api.github.com', 'docdiff (github.com/asurkov/docdiff; reads PR comments)'
# A page load asks GitHub for the PR's comments again once they are older than this, in seconds.
FRESH = 300
lock = threading.Lock()


def add_args(p):
    p.add_argument('--pr', help="the GitHub PR that --base..--head is: its URL, owner/repo#N, or N for a PR "
                                "of the origin remote; a change's popup then opens the change on GitHub")
    p.add_argument('--pr-comments', type=lambda f: str(pathlib.Path(f).resolve()),
                   help="the PR's comments from this file instead of GitHub, {review, issue, reviews} as its API "
                        "lists them (offline, or a test)")
    p.add_argument('--me', help="your GitHub login: your comments on the PR count as your review comments "
                                "(default: GITHUB_TOKEN's user, if set)")


def title(run):
    if not run.get('pr'):
        return None
    slug, n = pr_of(run['repo'], run['pr'])
    return f'GitHub PR · {slug}#{n}', f'https://github.com/{slug}/pull/{n}'


def actions(run):
    if not run.get('pr'):
        return None
    repo, base, head = run['repo'], run['base'], run.get('head')
    if not head:
        sys.exit('--pr needs --head: the PR\'s head commit')
    url = files_url(repo, run['pr'])
    sources, done = {}, {}

    def act(old, new, blocks):
        if (old, new) not in sources:
            sources[old, new] = {side: [f for p in paths if (f := source(repo, rev, p))]
                                 for side, rev, paths in (('L', base, changed(repo, base, head, old, new, 'L')),
                                                          ('R', head, changed(repo, base, head, old, new, 'R')))}
        # Per change and side: the words of its block on that side, and where its first word is (or, for
        # punctuation, the word after it).
        first = {}
        for b in blocks:
            for side, i in (('L', 1), ('R', 2)):
                ws = []
                for part in b:
                    w = WORD.findall(html.unescape(part[i]).lower())
                    if part[0] and part[i].strip() and (part[0], side) not in first:
                        first[part[0], side] = (ws, len(ws))
                    ws.extend(w)
        for k in {k for k, _ in first}:
            if (old, new, k) not in done:
                side = 'R' if (k, 'R') in first else 'L'
                path, line, href = link(url, sources[old, new][side], side, *first[k, side])
                done[old, new, k] = [(label(path, line, side), KEY, href)]
        return {k: done[old, new, k] for k, _ in first}
    return act


def place(run, old, new, side, before, after):
    """For other plugins, review comments say: where the text `after` starts, `before` being the text of
    its block before it, on that side of the file ('L' the base, 'R' the head), found in the PR's files
    as a change's button is: (path, line, link) as link() returns them; None without --pr."""
    if not run.get('pr') or not run.get('head'):
        return None
    repo, rev = run['repo'], run['base'] if side == 'L' else run['head']
    files = [f for p in changed(repo, run['base'], run['head'], old, new, side) if (f := source(repo, rev, p))]
    ws = WORD.findall(before.lower())
    return link(files_url(repo, run['pr']), files, side, ws + WORD.findall(after.lower()), len(ws))


def label(path, line, side):
    """The button names the file and line, as GitHub highlights the line but does not scroll to it."""
    if not path:
        return LABEL
    return f'{LABEL} {os.path.basename(path)}' + (f':{line}' + (' (old)' if side == 'L' else '') if line else '')


def link(url, files, side, ws, s):
    """Where the word ws[s] comes from, in the file that holds the longest run of the words around it:
    (path, line, link), the line None where no run places the word, and the path too when the spec has
    several files."""
    lo = max(0, s - CONTEXT)
    win, s = ws[lo:s + CONTEXT], s - lo
    best, at = None, (0, 0, 0)
    for f in files:
        f['sm'].set_seq1(win)
        m = f['sm'].find_longest_match()
        if m.size > at[2]:
            best, at = f, m
    if not best or at[2] < min(RUN, len(win)):
        if len(files) != 1:
            return None, None, url
        return files[0]['path'], None, f'{url}#diff-{anchor(files[0]["path"])}'
    # Aligned near the run: the word matched nearest the change's own.
    i, j, _ = at
    a0 = max(0, j - 2 * i - 20)
    ms = [m for m in difflib.SequenceMatcher(None, win, best['words'][a0:j + 2 * (len(win) - i) + 20],
                                             autojunk=False).get_matching_blocks() if m.size]
    a, b, n = min(ms, key=lambda m: max(m.a - s, s - (m.a + m.size - 1), 0))
    line = best['lines'][a0 + b + min(max(s - a, 0), n - 1)]
    return best['path'], line, f'{url}#diff-{anchor(best["path"])}{side}{line}'


def anchor(path):
    return hashlib.sha256(path.encode()).hexdigest()


def files_url(repo, pr):
    slug, n = pr_of(repo, pr)
    return f'https://github.com/{slug}/pull/{n}/files'


def pr_of(repo, pr):
    """The PR's owner/repo and number."""
    m = re.fullmatch(r'(?:https://github\.com/)?(?:([\w.-]+/[\w.-]+)(?:/pull/|#))?#?(\d+)(?:/.*)?', pr.strip())
    if not m:
        sys.exit(f'--pr {pr}: not a PR URL, owner/repo#N or N')
    slug = m.group(1)
    if not slug:
        origin = git(repo, 'remote', 'get-url', 'origin')
        o = re.search(r'github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$', origin)
        if not o:
            sys.exit(f'--pr {pr}: the origin remote is not on GitHub ({origin or "none"}); use owner/repo#N')
        slug = o.group(1)
    return slug, m.group(2)


def remarks(run, out, refresh=None):
    """For other plugins (review.py): the PR's comments on GitHub, read only, {fetched, checked, error, me,
    comments}, me the user's login (--me, or GITHUB_TOKEN's user): its line comments, each placed on the diff where it can be (see place_lines()), its own
    comments and its reviews' bodies. Kept in <out>/github.json, which refresh 'auto' (a page load) asks
    GitHub to bring up to date when it is older than FRESH, and 'now' (a button) always; each page is
    asked for with its last ETag, so a list that did not change comes back empty (304), though without a
    token GitHub still counts it against its 60 requests an hour. A token only from GITHUB_TOKEN, never
    kept. None without --pr."""
    if not run.get('pr') or not run.get('head'):
        return None
    f = pathlib.Path(out) / 'github.json'
    with lock:
        cache = json.loads(f.read_text()) if f.exists() else {}
        was = json.dumps(cache, sort_keys=True)
        if run.get('pr_comments'):
            cache = {**cache, 'data': json.loads(pathlib.Path(run['pr_comments']).read_text()), 'error': None,
                     'fetched': cache.get('fetched') or time.time()}
        elif refresh == 'now' or refresh == 'auto' and time.time() - cache.get('checked', 0) > FRESH:
            cache = fetch(run, cache)
            if not run.get('me') and not cache.get('me') and os.environ.get('GITHUB_TOKEN'):
                cache['me'] = whoami()
        # Placed again when the comments, the base or the head are others.
        digest = hashlib.sha1(json.dumps([run['base'], run['head'], cache.get('data')], sort_keys=True).encode()).hexdigest()
        if cache.get('placed_for') != digest and 'data' in cache:
            cs, done = comments(run, out, cache['data'])
            cache.update(comments=cs, placed_for=digest if done else None)
        if json.dumps(cache, sort_keys=True) != was:
            f.with_suffix('.tmp').write_text(json.dumps(cache, ensure_ascii=False))
            f.with_suffix('.tmp').replace(f)
    return {k: cache.get(k) for k in ('fetched', 'checked', 'error')} | {'me': run.get('me') or cache.get('me'),
                                                                         'comments': cache.get('comments', [])}


def whoami():
    """GITHUB_TOKEN's user (GET /user), or None when GitHub does not say; kept by remarks(), the token never."""
    ask = {'User-Agent': AGENT, 'Accept': 'application/vnd.github+json', 'Authorization': f'Bearer {os.environ["GITHUB_TOKEN"]}'}
    try:
        with urllib.request.urlopen(urllib.request.Request(f'{API}/user', headers=ask), timeout=10) as r:
            return json.load(r).get('login')
    except (urllib.error.URLError, OSError, ValueError):
        return None


def fetch(run, cache):
    """Asks GitHub for the PR's review comments, comments and reviews, page by page, each with its last
    ETag; offline or rate limited, keeps what it had and says why."""
    slug, n = pr_of(run['repo'], run['pr'])
    token, pages, data, now = os.environ.get('GITHUB_TOKEN'), dict(cache.get('pages', {})), {}, time.time()
    try:
        for kind, path in (('review', f'pulls/{n}/comments'), ('issue', f'issues/{n}/comments'), ('reviews', f'pulls/{n}/reviews')):
            url, data[kind] = f'{API}/repos/{slug}/{path}?per_page=100', []
            while url:
                was = pages.get(url)
                ask = {'User-Agent': AGENT, 'Accept': 'application/vnd.github+json',
                       **({'If-None-Match': was['etag']} if was and was.get('etag') else {}),
                       **({'Authorization': f'Bearer {token}'} if token else {})}
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, headers=ask), timeout=10) as r:
                        got = pages[url] = {'etag': r.headers.get('ETag'), 'items': json.load(r),
                                            'next': next(iter(re.findall(r'<([^>]+)>;\s*rel="next"', r.headers.get('Link') or '')), None)}
                except urllib.error.HTTPError as e:
                    if e.code != 304 or not was:
                        raise
                    got = was
                data[kind] += got['items']
                url = got['next']
    except urllib.error.HTTPError as e:
        reset = e.headers.get('X-RateLimit-Reset')
        why = (f'rate limited until {time.strftime("%H:%M", time.localtime(int(reset)))}' if e.headers.get('X-RateLimit-Remaining') == '0'
               and reset else f'GitHub answered {e.code}')
        return {**cache, 'checked': now, 'error': why}
    except (urllib.error.URLError, OSError, ValueError):
        return {**cache, 'checked': now, 'error': 'GitHub not reached (offline?)'}
    return {**cache, 'pages': pages, 'data': data, 'fetched': now, 'checked': now, 'error': None}


def comments(run, out, data):
    """The PR's comments as the page shows them, and whether every doc could be looked at (a spec waiting
    for its render could not): line comments (outdated ones too, with their old line), with where they
    are when place_lines() finds it, then the PR's own comments and its reviews with a body."""
    who = lambda c: (c.get('user') or {}).get('login') or '?'
    cs = [{'id': c['id'], 'kind': 'line', 'author': who(c), 'time': c.get('created_at'), 'body': c.get('body') or '',
           'url': c.get('html_url'), 'reply_to': c.get('in_reply_to_id'), 'path': c.get('path'),
           'side': 'L' if c.get('side') == 'LEFT' else 'R', 'line': c.get('line') or c.get('original_line'),
           'outdated': c.get('line') is None} for c in data.get('review', [])]
    cs += [{'id': c['id'], 'kind': 'pr', 'author': who(c), 'time': c.get('created_at'), 'body': c.get('body') or '',
            'url': c.get('html_url')} for c in data.get('issue', [])]
    cs += [{'id': r['id'], 'kind': 'review', 'author': who(r), 'time': r.get('submitted_at'), 'body': r.get('body') or '',
            'url': r.get('html_url'), 'state': r.get('state')} for r in data.get('reviews', []) if (r.get('body') or '').strip()]
    done = place_lines(run, out, [c for c in cs if c['kind'] == 'line' and not c['outdated'] and not c['reply_to'] and c['line']])
    return cs, done


def place_lines(run, out, cs):
    """Puts line comments on the diff, as link() puts a change on a line the other way round: the words of
    the comment's line, with CONTEXT words on each side, are matched against each block of the file's
    diff on that side, and the block whose run of them passes through the line's first word is the
    longest holds it (or, with none, the block with the longest run); aligning the two gives that word
    there. A comment gets its doc (file), that word's change (key, None in unchanged
    text), and, to be found by in any view, the block's text from that word on (quote) and before it.
    False when a doc could not be looked at yet."""
    repo, done = pathlib.Path(run['repo']), True
    for old, new in docdiff.files_for(repo, run['base'], run['head'], run['paths'])[0]:
        o, n, wait = docdiff.htmls(repo, run['base'], run['head'], old, new, pathlib.Path(out))
        if wait:
            done = False
            continue
        pieces = docdiff.block_diff(o, n, 'x')[2]
        for side, rev in (('L', run['base']), ('R', run['head'])):
            srcs = {p: f for p in changed(str(repo), run['base'], run['head'], old, new, side) if (f := source(str(repo), rev, p))}
            mine = [c for c in cs if c['side'] == side and c['path'] in srcs and 'file' not in c]
            blocks = [block_words(b, side) for b in pieces] if mine else []
            for c in mine:
                f = srcs[c['path']]
                at = next((i for i, line in enumerate(f['lines']) if line >= c['line']), None)
                if at is None:
                    continue
                lo = max(0, at - CONTEXT)
                win, s = f['words'][lo:at + CONTEXT], at - lo
                best, score, ms = None, (0, 0), None
                for b in blocks:
                    ys = [y for y in difflib.SequenceMatcher(None, win, b['words'], autojunk=False).get_matching_blocks() if y.size]
                    x = (max((y.size for y in ys if y.a <= s < y.a + y.size), default=0), max((y.size for y in ys), default=0))
                    if x > score:
                        best, score, ms = b, x, ys
                if not best or max(score) < min(RUN, len(win)):
                    continue
                a, j, size = min(ms, key=lambda y: max(y.a - s, s - (y.a + y.size - 1), 0))
                start, part = best['at'][j + min(max(s - a, 0), size - 1)]
                c.update(file=new or old, key=best['keys'][part], quote=' '.join(best['text'][start:].split()[:8]),
                         before=' '.join(best['text'][max(0, start - 300):start].split()))
    return done


def block_words(piece, side):
    """A block of a diff (as block_diff() makes its pieces) on one side: its text as it reads, its words,
    and per word where it starts in the text and in which part, whose key keys has."""
    text, words, at, keys = '', [], [], []
    for i, (k, o, w) in enumerate(piece[2]):
        t = html.unescape(re.sub(r'<!--.*?-->|<[^>]+>', '', o if side == 'L' else w, flags=re.S))
        for m in WORD.finditer(t):
            words.append(m.group(0).lower())
            at.append((len(text) + m.start(), i))
        text += t
        keys.append(k)
    return {'text': text, 'words': words, 'at': at, 'keys': keys}


def changed(repo, base, head, old, new, side):
    """The files a change on that side can come from: the Markdown file itself, or what the PR changes
    in a spec's directory."""
    path = old if side == 'L' else new
    if not path or path.endswith('.md'):
        return [path] if path else []
    d = os.path.dirname(path) or '.'
    return git(repo, 'diff', '--name-only', '--no-renames', base, head, '--', d).splitlines()


def source(repo, rev, path):
    """A file at rev as its words and their lines, and a matcher with it as the second sequence;
    None where it does not exist or is not text."""
    r = subprocess.run(['git', '-C', repo, 'show', f'{rev}:{path}'], capture_output=True)
    if r.returncode or b'\0' in r.stdout:
        return None
    page, md = r.stdout.decode(errors='replace'), path.endswith('.md')
    if md:
        page = subprocess.run(['pandoc', '-f', 'gfm+sourcepos', '-t', 'html', '--wrap=none'], input=page,
                              capture_output=True, text=True).stdout
    words, lines, line = [], [], 1

    def add(t, line):
        t = html.unescape(t)
        for w in WORD.finditer(t):
            words.append(w.group(0).lower())
            lines.append(line + t.count('\n', 0, w.start()))
    for m in re.finditer(r'<!--.*?-->|<[^>]*>|[^<]+', page, re.S):
        t = m.group(0)
        if t[0] != '<':
            add(t, line)
        elif md:
            pos = re.search(r'\bdata-pos="(\d+):', t)
            line = int(pos.group(1)) if pos else line
        else:
            # ReSpec shows a note's or an example's title as its caption.
            for a in re.finditer(r'\stitle="([^"]*)"', t):
                add(a.group(1), line + t.count('\n', 0, a.start()))
        if not md:
            line += t.count('\n')
    sm = difflib.SequenceMatcher(None, autojunk=False)
    sm.set_seq2(words)
    return {'path': path, 'words': words, 'lines': lines, 'sm': sm}


def git(repo, *args):
    return subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True).stdout.strip()
