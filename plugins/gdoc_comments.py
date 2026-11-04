"""Google Doc comments: --comments FILE holds comments made on the old side, {file: [{quote, author,
date, text, replies: [{author, text}], fate: "goes", "edited" or null}]} as gdocsync's export writes
them; each is marked where its quote first reads in the old text, its thread in the mark's tooltip.
"""
import json, pathlib

STYLE = '''
mark.cm{background:rgba(212,167,44,.25);color:inherit;border-bottom:2px solid #d4a72c;cursor:help}
mark.cm-goes{border-bottom-color:var(--delb)} mark.cm-edited{border-bottom-style:dashed}
.cmx{font-size:13px;color:var(--muted);margin:0 0 8px} .cmx summary{cursor:pointer} .cmx li{cursor:help}
'''

SCRIPT = '''
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
'''


def add_args(p):
    p.add_argument('--comments', type=lambda f: str(pathlib.Path(f).resolve()),
                   help="comments made on the old side, as gdocsync's export writes them")


def section(run, name):
    """The file's comments for the page, and how many there are for its bar."""
    notes = pathlib.Path(run['comments']) if run.get('comments') else None
    cs = json.loads(notes.read_text()).get(name, []) if notes and notes.exists() else []
    if not cs:
        return None
    touched = sum(1 for c in cs if c.get('fate'))
    return (f'{len(cs)} open comment(s)' + (f', {touched} in text that changes' if touched else ''),
            '<script type="application/json" class="cm">' + json.dumps(cs, ensure_ascii=False).replace('</', '<\\/') +
            '</script>')
