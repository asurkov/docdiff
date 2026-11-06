"""The repo's docs and their Google Docs: --docs FILE, as gdocsync's export writes it, [{file, url, state,
since, comments}], lists every Markdown file above the diff: its name, a link to the doc below, a link to
its Google Doc, where the two stand and its open comments.
"""
import html, json, pathlib

STYLE = '''
.docs{margin:16px 0 8px;font-size:14px} .docs table{border-collapse:collapse;width:100%}
.docs th,.docs td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.docs th{color:var(--muted);font-weight:400;font-size:12px} .docs td:first-child{font:600 13px ui-monospace,Menlo,monospace}
.docs .ok{color:var(--muted)} .docs .dl{color:var(--fg)}
'''

SCRIPT = '''
// Each doc's name scrolls to its section, just below the sticky header; a doc with no section (no Google
// Doc) is plain text.
for(const a of document.querySelectorAll('.docs .dl')){const s=[...document.querySelectorAll('section')].find(s=>s.dataset.file===a.dataset.file);
if(!s){a.replaceWith(a.textContent);continue;}
a.onclick=e=>{e.preventDefault();scrollTo({top:s.getBoundingClientRect().top+scrollY-document.querySelector('header').offsetHeight-8,behavior:'smooth'});};}
'''


def add_args(p):
    p.add_argument('--docs', type=lambda f: str(pathlib.Path(f).resolve()),
                   help="the repo's docs and where each stands against its Google Doc, as gdocsync's export "
                        "writes them")


def top(run):
    """The docs as a table, of class ui so a click in it is not a click on the text."""
    f = pathlib.Path(run['docs']) if run.get('docs') else None
    if not f or not f.exists():
        return None
    rows = ''.join(row(d) for d in json.loads(f.read_text()))
    return ('<div class="docs ui"><table><tr><th>Markdown</th><th>Google Doc</th><th>Where they stand</th>'
            f'<th>Open comments</th></tr>{rows}</table></div>')


def row(d):
    e = html.escape
    name = f'<a class="dl" href="#" data-file="{e(d["file"])}">{e(d["file"])}</a>'
    link = f'<a href="{e(d["url"])}" target="_blank" rel="noopener">Google Doc ↗</a>' if d['url'] else ''
    stand = e('; '.join(filter(None, [d['state'], d['since']])))
    return (f'<tr><td>{name}</td><td>{link}</td><td class="{"ok" if d["state"] == "in step" else ""}">{stand}</td>'
            f'<td>{d["comments"] or ""}</td></tr>')
