"""Review comments, and revisits with Claude's answers, on the page.

A comment is made on a change or on selected text (💬 in its popup, or c) and kept in <out>/review.json,
{comments: [{id, time, file, view, key, quote, side, before, after, section, body, github}]}: key the
change's (None on a selection), quote its text as it reads on its side, side L for the base's text (in
Old, or a change that only removes) and R otherwise, before and after the text of its block around it.
github is where a plugin's place() (github_pr.py's, with --pr) finds that text in the PR's files,
{pr, head, path, line, side, href}, found again when the run's PR or head is another.

The page marks each comment on its change in every view that has the change, and elsewhere where its
quote reads, the text before it picking among repeats: so a comment stays on a change once it is
accepted. Revisits are marked the same way, from revisit.jsonl, each with the answers Claude appends to
answers.jsonl, {time: the revisit's time, answer: Markdown}; one with none is pending. The page polls
GET /review for all of it, and posts /review to add, edit or delete a comment, shown in place.
"""
# docdiff, the running docdiff.py, is set by its load_plugins().
import html, json, pathlib, re, secrets, subprocess, threading, time

# Posts the page shows in place, with no rebuild.
IN_PLACE = ['/review']
# What the page sends of a new comment; the server adds its id, time and place in the PR.
FIELDS = ('file', 'view', 'key', 'quote', 'side', 'before', 'after', 'section', 'body')
lock = threading.Lock()
# Answers rendered, by their Markdown.
rendered = {}

STYLE = '''
.rvb{display:inline-block;margin-left:3px;padding:0 5px;border:1px solid var(--line);border-radius:9px;background:var(--bg);color:var(--muted);font:400 11px/15px -apple-system,BlinkMacSystemFont,sans-serif;vertical-align:2px;cursor:pointer;user-select:none;-webkit-user-select:none}
.rvb-c::before{content:'💬'} .rvb-r::before{content:'✎'} .rvb-r{color:var(--insb);border-color:var(--insb)}
.rvb-wait{color:var(--rw);border-color:var(--rw);border-style:dashed} .rvb-wait::after{content:' …'} .rvb:hover{background:var(--code)} .rvb-hit{outline:2px solid #bf8700}
#cp{z-index:5} #rvp[hidden],#rvc[hidden]{display:none}
@media (min-width:840px){.rv-open main{margin-right:420px}}
#rvp{position:fixed;right:0;bottom:0;width:min(420px,100vw);box-sizing:border-box;overflow:auto;background:var(--bg);border-left:1px solid var(--line);padding:8px 14px 24px;z-index:2;font-size:14px}
.rvh{display:flex;gap:8px;align-items:center;margin-bottom:6px} .rvh .n{flex:1} .rvh button,.rvx{font-size:12px;padding:1px 8px}
.rvi{padding:8px 4px;border-top:1px solid var(--line);cursor:pointer} .rvi:hover{background:var(--code)}
.rvm{font-size:12px;color:var(--muted)} .rvt{white-space:pre-wrap;margin:2px 0} .rvx{margin:4px 6px 0 0}
#rvp blockquote,#rvc blockquote{margin:4px 0;padding:0 8px;border-left:3px solid var(--line);color:var(--muted);max-height:4.8em;overflow:hidden}
.rva{border-left:3px solid var(--insb);padding-left:8px;margin-top:6px} .rva>:first-child{margin-top:0} .rva>:last-child{margin-bottom:0}
#rvc{position:absolute;z-index:3;width:min(560px,90vw);box-sizing:border-box;background:var(--bg);border:1px solid var(--line);border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.12);padding:8px 12px;font-size:14px}
'''

SCRIPT = '''
// Review comments (💬, c) and revisits with Claude's answers (plugins/review.py): each marked on its
// change in every view that has it, and elsewhere where its quote reads. The page polls the server for
// them, and shows a comment it saves in place.
(()=>{const mk=(t,p)=>Object.assign(document.createElement(t),p),norm=t=>(t||'').replace(/\\s+/g,' ').trim();
let data={comments:[],revisits:[]},seen=null,items=[],draft=null;
const cp=mk('div',{id:'cp',className:'panel',hidden:true}),cq=mk('blockquote'),ct=mk('textarea',{placeholder:'Comment · ↵ saves, ⇧↵ adds a line'});
const cs=mk('button',{textContent:'Save'}),cd=mk('button',{textContent:'Delete'}),cc=mk('button',{textContent:'Cancel'});cp.append(cq,ct,cs,' ',cd,' ',cc);
const lp=mk('aside',{id:'rvp',hidden:true}),card=mk('div',{id:'rvc',hidden:true}),rvt=mk('button',{id:'rvt',textContent:'💬'});
const pc=mk('button',{id:'pc',textContent:'💬',title:'Comment (c)'});body.append(cp,lp,card);pr.after(pc);rev.after(rvt);
const vorder=['diff','review','acc','new','old'];
// A range's text as it reads on one side: L the base's (removed words and folded old blocks), R the new.
const drop=(f,side)=>{f.querySelectorAll(side==='L'?'ins,.now,summary':'del,.was').forEach(e=>e.remove());return f.textContent;};
// The block of the diff that node n is in, as the server pairs them: a child of the view, or an item of
// a list that is one.
function blockOf(n){let e=n.nodeType===1?n:n.parentElement;if(e.classList.contains('v'))return e;
for(let p=e.parentElement;!p.classList.contains('v');e=p,p=p.parentElement)if(/^[OU]L$/.test(p.tagName)&&p.parentElement.classList.contains('v'))break;return e;}
// A range's text on its side (L in Old, or where it reads only there), and the rest of its block before
// and after it; a whole block's change is its block.
function anchor(r,blk,old){const qr=norm(drop(r.cloneContents(),'R')),side=old||!qr?'L':'R',b=!blk&&blockOf(r.startContainer);
const a={quote:side==='R'?qr:norm(drop(r.cloneContents(),'L')),side,before:'',after:''},t=document.createRange();
if(b){t.selectNodeContents(b);t.setEnd(r.startContainer,r.startOffset);a.before=norm(drop(t.cloneContents(),side)).slice(-400);
if(b.contains(r.endContainer)){t.selectNodeContents(b);t.setStart(r.endContainer,r.endOffset);a.after=norm(drop(t.cloneContents(),side)).slice(0,400);}}return a;}
const span=els=>{const r=document.createRange();r.setStartBefore(els[0]);r.setEndAfter(els[els.length-1]);return r;};
// What a new comment is on: the selected text, or else the current change.
function anchorOf(){const s=getSelection(),sel=!s.isCollapsed&&s.rangeCount&&s.anchorNode.parentElement?.closest('main .v');if(!sel&&!c)return null;
const el=sel?s.anchorNode.parentElement:c.els[0],a=sel?anchor(s.getRangeAt(0),false,cur==='old'):anchor(span(c.els),el.classList.contains('blk'));
return a.quote?{file:el.closest('section').dataset.file,view:cur,key:sel?null:c.k,...a,section:heading(el)}:null;}
// A view's text on one side, spaces collapsed, with the text node and offset of each character.
function textOf(v,side){const skip=side==='L'?'ins,.now,summary,.rvb':'del,.was,.rvb',ns=[],os=[];let s='',sp=true,n;
const w=document.createTreeWalker(v,NodeFilter.SHOW_TEXT,{acceptNode:t=>t.parentElement.closest(skip)?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
while(n=w.nextNode())for(let i=0;i<n.data.length;i++){const ch=/\\s/.test(n.data[i])?' ':n.data[i];if(ch===' '&&sp)continue;sp=ch===' ';s+=ch;ns.push(n);os.push(i);}
return{s,ns,os};}
// Where q ends in that text; among repeats, the one the text before it, b, best leads up to.
function findEnd(x,q,b){q=norm(q);b=norm(b);if(!q)return null;let best=-1,most=-1;
for(let at=x.s.indexOf(q);at>=0;at=x.s.indexOf(q,at+1)){const p=x.s.slice(Math.max(0,at-b.length-1),at).trimEnd();let k=0;
while(k<b.length&&k<p.length&&p[p.length-1-k]===b[b.length-1-k])k++;if(k>most){most=k;best=at;}}
return best<0?null:[x.ns[best+q.length-1],x.os[best+q.length-1]+1];}
// On a change in view v: after its last inline part, or at the end of its block's new text (in its
// folded summary when it only removes).
function onChange(v,k){const els=v.querySelectorAll('.chg[data-k="'+k+'"]'),e=els[els.length-1];if(!e)return null;
if(!e.classList.contains('blk'))return b=>e.after(b);let t=e.querySelector('.now');if(!t){const m=e.querySelector('summary');return b=>m.append(b);}
while(t.lastElementChild&&/^(P|UL|OL|LI|DL|DT|DD|DIV|BLOCKQUOTE|H[1-6])$/.test(t.lastElementChild.tagName))t=t.lastElementChild;return b=>t.append(b);}
// A revisit of a change is placed by the change's own text and the text before it, from a view that has
// the change; failing that, by its quote, "−removed +added": the added text, or else the removed.
function asked(r){const q=r.quote||'',p=q.match(/^(?:−(.*?) )?\\+(.*)$/s),d=!p&&q.match(/^−(.*)$/s),sel='.chg[data-k="'+r.key+'"]';
const s=r.key&&[...document.querySelectorAll('section')].find(s=>s.dataset.file===r.file),v=s&&[...s.querySelectorAll('.v')].find(v=>v.querySelector(sel));
const els=v?[...v.querySelectorAll(sel)]:[],{quote,side,before}=els.length?anchor(span(els),els[0].classList.contains('blk')):{quote:p?p[2]:d?d[1]:q,side:r.view==='old'||d?'L':'R',before:''};
return{...r,kind:'r',sent:q,quote,side,before};}
function badge(it){const wait=it.kind==='r'&&!it.answers.length,b=mk('span',{className:'rvb rvb-'+it.kind+(wait?' rvb-wait':''),
title:it.kind==='c'?it.body:it.prompt+(wait?'\\n\\n(pending: no answer yet)':'\\n\\n(answered: click to read)')});
b.onmousedown=e=>e.preventDefault();b.onclick=e=>{e.preventDefault();e.stopPropagation();it.kind==='c'?openCm(it):showCard(it,b);};return b;}
// Marks every comment and revisit; it.at holds its mark per view.
function paint(){document.querySelectorAll('.rvb').forEach(b=>{const p=b.parentNode;b.remove();p.normalize();});
const texts=new Map(),text=(v,side)=>{let m=texts.get(v);if(!m)texts.set(v,m={});return m[side]||(m[side]=textOf(v,side));},put=[];
items=[...data.comments.map(x=>({...x,kind:'c'})),...data.revisits.map(asked)];
for(const it of items){it.at={};for(const s of document.querySelectorAll('section'))if(s.dataset.file===it.file)for(const v of s.querySelectorAll('.v')){
const b=badge(it),on=it.key&&onChange(v,it.key),end=!on&&findEnd(text(v,it.side),it.quote,it.before);
if(on)on(b);else if(end)put.push([...end,b]);else continue;it.at[v.dataset.view]=b;}}
// From the end of the doc back, so splitting a text node leaves the places before it where they were.
put.sort((x,y)=>x[0]===y[0]?y[1]-x[1]:x[0].compareDocumentPosition(y[0])&Node.DOCUMENT_POSITION_FOLLOWING?1:-1);
for(const[t,o,b]of put){t.splitText(o);t.after(b);}
const nr=data.revisits.length,np=data.revisits.filter(r=>!r.answers.length).length;
rvt.textContent='💬 '+data.comments.length+(nr?' · ✎ '+nr+(np?' ('+np+' pending)':''):'');if(!lp.hidden)list();}
function take(d){data=d;seen=JSON.stringify(d);paint();}
async function load(){try{const r=await fetch('review');if(!r.ok)throw new Error(r.status);const d=await r.json();rvt.disabled=false;rvt.title='Review: comments and revisits';
if(JSON.stringify(d)!==seen)take(d);}catch(e){rvt.disabled=true;rvt.title='review: not served (restart the docdiff server)';}}
function openCm(it){if(!it){pos.textContent='select text, or step to a change, then press c';return;}
hidePop();card.hidden=true;draft=it;cq.textContent=it.quote;ct.value=it.body||'';cd.hidden=!it.id;cp.hidden=false;ct.focus();}
function closeCm(){cp.hidden=true;ct.value='';draft=null;}
async function save(req){try{take(await post('review',req));closeCm();}catch(e){pos.textContent='comment not saved: '+e.message+' (is the docdiff server running?)';}}
cs.onclick=()=>{const t=ct.value.trim();if(!draft)return;if(!t)return draft.id?save({id:draft.id,delete:true}):closeCm();
save(draft.id?{id:draft.id,body:t}:{...draft,body:t});};
cd.onclick=()=>draft?.id&&save({id:draft.id,delete:true});cc.onclick=closeCm;
ct.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();cs.click();}if(e.key==='Escape')closeCm();});
const answers=it=>it.answers.length?it.answers.map(h=>{const d=mk('div',{className:'rva'});d.innerHTML=h;return d;}):[mk('div',{className:'rvm',textContent:'pending: Claude has not answered yet'})];
function showCard(it,b){card.replaceChildren(mk('div',{className:'rvm',textContent:'✎ revisit'+(it.section?' · '+it.section:'')+' · '+it.time.replace('T',' ')}),
mk('blockquote',{textContent:it.sent}),mk('div',{className:'rvt',textContent:it.prompt}),...answers(it));card.hidden=false;
const r=b.getBoundingClientRect();card.style.left=Math.max(8,Math.min(r.left,innerWidth-card.offsetWidth-8))+scrollX+'px';card.style.top=r.bottom+scrollY+4+'px';}
// The Review panel: comments and revisits in the order they come in the doc, each one going to its mark.
const where=it=>{const g=it.github;return g&&g.path?g.path+(g.line?':'+g.line+(g.side==='L'?' (old)':''):''):'';};
function order(){const secs=[...document.querySelectorAll('section')].map(s=>s.dataset.file);
const at=it=>{const v=it.at[cur]?cur:vorder.find(x=>it.at[x]);if(!v)return 2;const b=it.at[v],w=b.closest('.v'),r=document.createRange();
r.setStart(w,0);r.setEndBefore(b);return r.toString().length/Math.max(1,w.textContent.length);};
return items.map(it=>[(i=>i<0?1e9:i)(secs.indexOf(it.file)),at(it),it]).sort((x,y)=>x[0]-y[0]||x[1]-y[1]||(x[2].time<y[2].time?-1:1)).map(x=>x[2]);}
function jump(it){const v=it.at[cur]?cur:vorder.find(x=>it.at[x]);if(!v)return;if(v!==cur)view(v);
const g=it.key&&groups.find(x=>x.view===cur&&x.file===it.file&&x.k===it.key),b=it.at[cur];
if(g)focus(g);else{focus(null,false);b.scrollIntoView({block:'center',behavior:'smooth'});}
b.classList.add('rvb-hit');setTimeout(()=>b.classList.remove('rvb-hit'),1500);}
function row(it,files){const d=mk('div',{className:'rvi'}),m=mk('div',{className:'rvm'}),w=where(it);
m.append((it.kind==='c'?'💬 ':'✎ ')+[files>1&&it.file,it.section].filter(Boolean).join(' · ')+(Object.keys(it.at).length?'':' · not in the page now'));
if(w)m.append(' · ',mk('a',{href:it.github.href,target:'_blank',rel:'noopener',textContent:w.split('/').pop()+' ↗'}));
d.append(m);if(it.quote)d.append(mk('blockquote',{textContent:it.kind==='r'?it.sent:it.quote}));
d.append(mk('div',{className:'rvt',textContent:it.kind==='c'?it.body:it.prompt}));
if(it.kind==='r')d.append(...answers(it));else{const e=mk('button',{className:'rvx',textContent:'edit'}),x=mk('button',{className:'rvx',textContent:'delete'});
e.onclick=()=>openCm(it);x.onclick=()=>x.textContent==='delete'?x.textContent='delete?':save({id:it.id,delete:true});d.append(e,x);}
d.onclick=e=>{if(!e.target.closest('a,button'))jump(it);};return d;}
function list(){const its=order(),nc=data.comments.length,files=document.querySelectorAll('section').length;
const h=mk('div',{className:'rvh'}),cm=mk('button',{textContent:'Copy as Markdown',title:'The comments, each with its file:line and quote, to paste into GitHub'}),x=mk('button',{textContent:'×',title:'Close'});
cm.onclick=async()=>{cm.textContent=!nc?'no comments':await copy(markdown(its))?'copied '+nc:'copying failed';setTimeout(()=>cm.textContent='Copy as Markdown',1500);};
x.onclick=()=>toggle(false);h.append(mk('b',{textContent:'Review'}),mk('span',{className:'n',textContent:nc+' comment(s) · '+data.revisits.length+' revisit(s)'}),cm,x);
lp.replaceChildren(h,...its.map(it=>row(it,files)));}
// Paste-ready: per comment, its file:line (linked to it on GitHub) or else its file and section, the
// quoted text and the comment.
function markdown(its){return its.filter(it=>it.kind==='c').map(it=>{const w=where(it),loc=w||it.file+(it.section?' · '+it.section:'');
const q=it.quote.length>300?it.quote.slice(0,300).replace(/\\s+\\S*$/,'')+'…':it.quote;
return '**'+(w&&it.github.href?'['+loc+']('+it.github.href+')':loc)+'**\\n\\n'+(q?'> '+q+'\\n\\n':'')+it.body;}).join('\\n\\n---\\n\\n');}
function toggle(on){lp.hidden=!on;body.classList.toggle('rv-open',on);if(on){lp.style.top=document.querySelector('header').getBoundingClientRect().bottom+'px';list();}
try{sessionStorage.setItem('docdiff-review',on?'1':'');}catch(e){}}
rvt.onclick=()=>toggle(lp.hidden);addEventListener('resize',()=>{if(!lp.hidden)lp.style.top=document.querySelector('header').getBoundingClientRect().bottom+'px';});
pc.onmousedown=e=>e.preventDefault();pc.onclick=()=>{const a=anchorOf();hidePop();openCm(a);};
// The doc's own click and selection handling leaves the panel and the card alone.
for(const e of [lp,card])for(const t of ['click','mouseup'])e.addEventListener(t,ev=>ev.stopPropagation());
document.addEventListener('click',e=>{if(!e.target.closest('#rvc'))card.hidden=true;});
document.addEventListener('keydown',e=>{if(e.target.closest('input,textarea')||e.metaKey||e.ctrlKey||e.altKey)return;
if(e.key==='Escape')card.hidden=true;if(e.key==='c'){e.preventDefault();openCm(anchorOf());}});
try{if(sessionStorage.getItem('docdiff-review'))toggle(true);}catch(e){}
load();setInterval(load,2000);})();
'''


def get(run, path, query):
    """The comments, and the revisits with their answers, for the page."""
    if path != '/review':
        return None
    return json.dumps(state(run), ensure_ascii=False).encode(), 'application/json'


def post(out, path, req):
    """Adds a comment, or edits or deletes the one req['id'] names; returns what get() does."""
    if path != '/review':
        return None
    run = json.loads((out / '.run.json').read_text())
    with lock:
        cs = comments(out)
        was = next((c for c in cs if req.get('id') and c['id'] == req['id']), None)
        if was and req.get('delete'):
            cs.remove(was)
        elif was:
            was['body'], was['edited'] = str(req['body']), now()
        elif not req.get('id'):
            cs.append({'id': secrets.token_hex(4), 'time': now(), **{k: req.get(k) for k in FIELDS}})
            locate(run, cs[-1:])
        save(out, cs)
    return state(run)


def state(run):
    out = pathlib.Path(run['out'])
    with lock:
        cs = comments(out)
        # Another PR or head (the PR moved on): find the comments in its files again.
        moved = [c for c in cs
                 if [(c.get('github') or {}).get(k) for k in ('pr', 'head')] != [run.get('pr'), run.get('head')]]
        if moved:
            locate(run, moved)
            save(out, cs)
    answers = {}
    for a in records(out / 'answers.jsonl'):
        answers.setdefault(a.get('time'), []).append(markdown(str(a.get('answer') or '')))
    return {'comments': cs,
            'revisits': [{**{k: r.get(k) for k in ('time', 'file', 'section', 'quote', 'view', 'prompt', 'key')},
                          'answers': answers.get(r.get('time'), [])} for r in records(out / 'revisit.jsonl')]}


def locate(run, cs):
    """Sets each comment's github to where a plugin's place() finds its text in the PR's files: {pr,
    head, path, line, side, href}, with no path, line or href where none does."""
    finder = next((m for m in docdiff.PLUGINS if hasattr(m, 'place')), None)
    files = {}
    if finder and run.get('pr'):
        repo = pathlib.Path(run['repo'])
        files = {new or old: (old, new) for old, new in docdiff.files_for(repo, run['base'], run.get('head'), run['paths'])[0]}
    for c in cs:
        got = c['file'] in files and finder.place(run, *files[c['file']], c['side'], c['before'] or '',
                                                  f"{c['quote'] or ''} {c['after'] or ''}")
        c['github'] = {'pr': run.get('pr'), 'head': run.get('head'), 'side': c['side'],
                       **(dict(zip(('path', 'line', 'href'), got)) if got else {})}


def comments(out):
    f = out / 'review.json'
    return json.loads(f.read_text())['comments'] if f.exists() else []


def save(out, cs):
    # Written whole and then moved in, so a crash never leaves half a file.
    f = out / 'review.json'
    f.with_suffix('.tmp').write_text(json.dumps({'comments': cs}, ensure_ascii=False, indent=1))
    f.with_suffix('.tmp').replace(f)


def records(path):
    """A .jsonl file's records; a line that does not parse is skipped."""
    out = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def markdown(text):
    """An answer rendered, raw HTML in it shown as text, and no javascript: links. pandoc's Markdown, as
    its gfm reader keeps raw HTML even without raw_html; with GFM's bare links, lists and straight quotes."""
    if text not in rendered:
        r = subprocess.run(['pandoc', '-f', 'markdown-raw_html-raw_tex-raw_attribute-smart+autolink_bare_uris'
                            '+lists_without_preceding_blankline', '-t', 'html'], input=text, capture_output=True, text=True)
        rendered[text] = (re.sub(r'\shref="\s*javascript:[^"]*"', '', r.stdout, flags=re.I) if not r.returncode
                          else f'<pre>{html.escape(text)}</pre>')
    return rendered[text]


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%S')
