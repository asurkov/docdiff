"""--pr: a change's popup opens the change on GitHub, in the PR's Files changed view at its line, where
review comments go.

A change goes to the line of its first added word in the head, or, when it only removes, of its first
removed word in the base. The change's block (paragraph, list item, heading...) is matched word by word
against the files the PR changes: the file holding the block's longest run of words is the one, and
aligning the block with it puts the change's word on a line. Tags and punctuation do not count, so
ReSpec's rewrites ([=term=], {{IDL}}, |var|) cost a spec's source only the words they change. A Markdown
file's words take their lines from pandoc's source positions. A change with no run of RUN words in a
row anywhere links to its file without a line, or to the PR's files when a spec has several.
"""
import difflib, hashlib, html, os, re, subprocess, sys

LABEL, KEY = 'GitHub ↗', 'g'
WORD = re.compile(r'\w+')
# Words of the change's block matched on each side of the change, at most.
CONTEXT = 40
# The fewest words in a row that place a change.
RUN = 4


def add_args(p):
    p.add_argument('--pr', help="the GitHub PR that --base..--head is: its URL, owner/repo#N, or N for a PR "
                                "of the origin remote; a change's popup then opens the change on GitHub")


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
                label, href = link(url, sources[old, new][side], side, *first[k, side])
                done[old, new, k] = [(label, KEY, href)]
        return {k: done[old, new, k] for k, _ in first}
    return act


def link(url, files, side, ws, s):
    """The change's button label and link: to the line its word ws[s] comes from, in the file that holds
    the longest run of the words around it. The label names the file and line, as GitHub highlights the
    line but does not scroll to it."""
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
            return LABEL, url
        return f'{LABEL} {os.path.basename(files[0]["path"])}', f'{url}#diff-{anchor(files[0]["path"])}'
    # Aligned near the run: the word matched nearest the change's own.
    i, j, _ = at
    a0 = max(0, j - 2 * i - 20)
    ms = [m for m in difflib.SequenceMatcher(None, win, best['words'][a0:j + 2 * (len(win) - i) + 20],
                                             autojunk=False).get_matching_blocks() if m.size]
    a, b, n = min(ms, key=lambda m: max(m.a - s, s - (m.a + m.size - 1), 0))
    line = best['lines'][a0 + b + min(max(s - a, 0), n - 1)]
    label = f'{LABEL} {os.path.basename(best["path"])}:{line}' + (' (old)' if side == 'L' else '')
    return label, f'{url}#diff-{anchor(best["path"])}{side}{line}'


def anchor(path):
    return hashlib.sha256(path.encode()).hexdigest()


def files_url(repo, pr):
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
    return f'https://github.com/{slug}/pull/{m.group(2)}/files'


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
