"""Google Doc comments: --comments FILE holds the open comments made on the old side, {file: [{quote,
author, date, time, text, replies: [{author, time, text}], fate: "goes", "edited" or null, url}]} as
gdocsync's export writes them. They go to review.py as comments made elsewhere (remarks()), as the PR's
on GitHub do: each thread a mark where its quote reads in the old text, its card the comment, its replies
and what a push does to its text, with a link to it in the Google Doc; one whose quote no longer reads
there (edited after the comment was made) is listed in the Review panel, On Google Docs.
"""
import json, pathlib

NOTE = {'goes': 'a push deletes its text', 'edited': 'a push edits its text'}


def add_args(p):
    p.add_argument('--comments', type=lambda f: str(pathlib.Path(f).resolve()),
                   help="comments made on the old side, as gdocsync's export writes them")


def title(run):
    """A Google Doc against its Markdown, as gdocsync's export (<repo>-google) holds them."""
    if not run.get('comments'):
        return None
    return f'Google Docs vs Markdown · {pathlib.Path(run["repo"]).name.removesuffix("-google")}', None


def section(run, name):
    """How many open comments the file has, for its bar."""
    cs = notes(run).get(name, [])
    if not cs:
        return None
    touched = sum(1 for c in cs if c.get('fate'))
    return f'{len(cs)} open comment(s)' + (f', {touched} in text that changes' if touched else ''), ''


def remarks(run, out, refresh=None):
    """For review.py: the Google Docs' open comments as comments made elsewhere, read when the export was
    made (fetched); a comment's replies follow it, reply_to its id."""
    f = pathlib.Path(run['comments']) if run.get('comments') else None
    if not f or not f.exists():
        return None
    cs = []
    for name, comments in notes(run).items():
        for i, c in enumerate(comments):
            cid = (c.get('url') or '').partition('disco=')[2] or f'{name}#{i}'
            time = c.get('time') or c.get('date')
            cs.append({'id': cid, 'kind': 'line', 'author': c['author'], 'time': time, 'body': c['text'],
                       'url': c.get('url'), 'file': name, 'quote': c['quote'], 'before': '', 'side': 'L',
                       'note': NOTE.get(c.get('fate'))})
            cs += [{'id': f'{cid}-{j}', 'kind': 'line', 'reply_to': cid, 'author': r['author'],
                    'time': r.get('time') or time, 'body': r['text'], 'url': c.get('url')}
                   for j, r in enumerate(c.get('replies', []))]
    return {'source': 'Google Docs', 'open': 'Open in Google Docs ↗', 'fetched': f.stat().st_mtime, 'checked': None,
            'error': None, 'me': None, 'refresh': False, 'posting': False, 'comments': cs}


def notes(run):
    f = pathlib.Path(run['comments']) if run.get('comments') else None
    return json.loads(f.read_text()) if f and f.exists() else {}
