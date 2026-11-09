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
answers.jsonl, {time: the revisit's time, answer: Markdown}. Revisits of one change or one quote are one
thread under one mark: pending while the last has no answer, new while an answer is unread, and resolved,
folded to a dot, while all of them are; asking again reopens it. On commits, accepting a change resolves the answered threads on it alone, and taking it
back reopens those. review.json keeps that by revisit time, {revisits: {time: {resolved, resolved_by,
seen}}}, resolved_by 'accept' for a thread its change's accept resolved, seen the number of its answers
read.

An answer's blockquotes are its drafted comments, each a proposal: used (made into a comment), dismissed,
or replaced (a revisit that revises it, its fields thread and revises, got a new draft). review.json keeps
the first two by the time of the revisit that first proposed the draft and the draft's hash,
{proposals: {"time#hash": {state, comment}}}; deleting the comment a proposal made frees it again. The two
.jsonl files are logs, only ever appended to. The page polls GET /review for all of it, and posts /review
to add (using proposals), edit or delete a comment, to resolve, reopen or read revisits, or to set the
state of proposals, shown in place.
"""
# docdiff, the running docdiff.py, is set by its load_plugins().
import collections, hashlib, html, json, pathlib, re, secrets, subprocess, threading, time

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
.rvb-wait{color:var(--rw);border-color:var(--rw);border-style:dashed} .rvb-wait::after{content:' …'} .rvb:hover{background:var(--code)} .rvb-hit,.rvi-hit{outline:2px solid #bf8700} .rvi-hit{outline-offset:2px} .rvb-near{border-style:dashed}
.rvb-new,.rvb-new:hover{background:var(--insb);color:var(--bg);font-weight:700} #rvt.rvt-new{font-weight:700;border-color:var(--insb)}
.rvb-done{width:7px;height:7px;padding:0;border:4px solid transparent;border-radius:50%;background:var(--muted) content-box;opacity:.35;vertical-align:middle} .rvb-done::before{content:none} .rvb-done:hover{background:var(--muted) content-box;opacity:.8}
#cp{z-index:5} #rvp[hidden],#rvc[hidden]{display:none}
@media (min-width:840px){.rv-open main{margin-right:420px}}
@media (max-width:839px){#rvp{top:auto!important;height:45vh;width:100vw!important;border-left:0!important;border-top:1px solid var(--line)} .rv-open main{padding-bottom:50vh}}
.rvh{flex-wrap:wrap} .rvonly{flex-basis:100%;text-align:left;font-size:12px;padding:1px 8px}
#rvp{position:fixed;right:0;bottom:0;width:min(420px,100vw);box-sizing:border-box;overflow:auto;overscroll-behavior:contain;background:var(--bg);border-left:1px solid var(--line);padding:8px 14px 24px;z-index:2;font-size:14px}
.rvh{display:flex;gap:8px;align-items:center;margin-bottom:6px} .rvh .n{flex:1} .rvh button,.rvx{font-size:12px;padding:1px 8px}
.rvi{padding:8px 4px;border-top:1px solid var(--line);cursor:pointer} .rvi:hover{background:var(--code)}
.rvm{font-size:12px;color:var(--muted)} .rvt{white-space:pre-wrap;margin:2px 0} .rvx{margin:4px 6px 0 0}
.rvch,.rvih{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px} .rvch .rvm,.rvih .rvm{flex:1 1 12em} .rvch .rvx,.rvih .rvx{margin:0}
.rvx.rvsec{font-size:11px;padding:0 6px;color:var(--muted)}
.rvch{position:sticky;top:0;z-index:1;padding:6px 12px;background:var(--bg);border-bottom:1px solid var(--line)} .rvcb{padding:4px 12px 10px}
.rvfold{max-height:9em;overflow:hidden;-webkit-mask-image:linear-gradient(#000 60%,transparent);mask-image:linear-gradient(#000 60%,transparent)}
#rvp .rvmore,#rvp .rvmore:hover{border:0;background:none;color:var(--link);padding:0;height:auto;font-size:12px;cursor:pointer}
.rvpr{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;margin:2px 0 10px} .rvpr .rvx{margin:0} .rvpr .rvm{margin-right:2px}
.rvgone{display:none} .rvstub{opacity:.65} .rvdup{opacity:.6} .rvrep>summary{font-size:12px;color:var(--muted);cursor:pointer} .rvrep[open]>blockquote{opacity:.7}
.rvused{color:var(--insb)}
.rvok{display:inline-block;margin-left:3px;padding:0 5px;border-radius:9px;font:600 11px/15px -apple-system,BlinkMacSystemFont,sans-serif;vertical-align:2px;color:var(--insb);background:var(--ins);user-select:none;-webkit-user-select:none}
.rvok-c{color:var(--rw);background:color-mix(in srgb,var(--rw) 16%,transparent)}
.rvfc{font:12px -apple-system,BlinkMacSystemFont,sans-serif;color:var(--link);text-decoration:none} .rvfc:hover{text-decoration:underline}
.rvb-g{color:var(--fg)} .rvb-g::before{content:'';display:inline-block;width:10px;height:10px;margin-right:3px;vertical-align:-1px;background:currentColor;
-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M1.5 2.5h13v8.5h-8l-3.5 3v-3h-1.5z' fill='none' stroke='black' stroke-width='1.6' stroke-linejoin='round'/%3E%3C/svg%3E") center/contain no-repeat;
mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M1.5 2.5h13v8.5h-8l-3.5 3v-3h-1.5z' fill='none' stroke='black' stroke-width='1.6' stroke-linejoin='round'/%3E%3C/svg%3E") center/contain no-repeat}
.rvb-g::after{content:attr(data-n)} .rvb-gme{color:var(--rw);border-color:var(--rw)} .rvb-past{filter:grayscale(1) opacity(.5)} .rvb-past:hover{filter:none} .rvb-posted{border-color:var(--insb)} .rvb-posted::after{content:' ✓';color:var(--insb)}
.rvgc{margin:6px 0} .rvgr{margin-left:16px} .rva.rvgh{border-color:var(--line)} .rvgs{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;margin:0 0 6px}
.rv-settled .v-acc del.chg:not(.rvshow){display:none} .rv-settled .v-acc ins.chg:not(.rvshow){background:none;box-shadow:none}
.rv-settled .v-acc .blk:not(.rvshow){border-left-color:color-mix(in srgb,var(--muted) 45%,transparent)} .rv-settled .v-acc .blk[data-kind=rw]:not(.rvshow)>.was{display:none} .rv-settled .v-acc .blk[data-kind=del]:not(.rvshow)>.was>.old{display:none}
.rv-settled .v-acc .rvbar{box-shadow:-8px 0 0 -6px color-mix(in srgb,var(--muted) 55%,transparent)} .rv-settled .v-acc .rvok:not(.rvok-c){display:none}
::highlight(rvcm){text-decoration:underline dotted;text-decoration-color:var(--rw);text-decoration-thickness:2px;text-underline-offset:3px}
body:not(.show-acc) #rvsd{display:none}
header .rvn button,header .rvn button:hover{border:0;background:none;padding:0;height:auto;line-height:inherit;font:inherit;color:var(--link);cursor:pointer}
#rvp blockquote,#rvc blockquote{margin:4px 0;padding:0 8px;border-left:3px solid var(--line);color:var(--muted);max-height:4.8em;overflow:hidden}
.rva{border-left:3px solid var(--insb);padding-left:8px;margin-top:6px} .rva>:first-child{margin-top:0} .rva>:last-child{margin-bottom:0}
#rvp .rva blockquote,#rvc .rva blockquote{max-height:none;margin:6px 0 2px} .rvi-new{box-shadow:inset 3px 0 var(--insb)} .rvi-new>.rvm{font-weight:600}
.rvf{margin:0 0 6px} .rvf button{font-size:12px;padding:1px 8px} .rvg>summary{cursor:pointer;color:var(--muted);font-size:13px;padding:6px 0;border-top:1px solid var(--line)}
#rvc{position:absolute;z-index:0;overflow:auto;overscroll-behavior:contain;box-sizing:border-box;background:var(--bg);border:1px solid var(--line);border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.12);padding:0;font-size:14px}
'''

SCRIPT = '''
// Review comments (💬, c) and revisits with Claude's answers (plugins/review.py): each marked on its
// change in every view that has it, and elsewhere where its quote reads. The page polls the server for
// them, and shows a comment it saves in place.
(()=>{const mk=(t,p)=>Object.assign(document.createElement(t),p),norm=t=>(t||'').replace(/\\s+/g,' ').trim();
let data={comments:[],revisits:[]},seen=null,items=[],draft=null,mine={};
const cp=mk('div',{id:'cp',className:'panel',hidden:true}),cq=mk('blockquote'),ct=mk('textarea',{placeholder:'Comment · ↵ saves, ⇧↵ adds a line'});
const cs=mk('button',{textContent:'Save'}),cd=mk('button',{textContent:'Delete'}),cc=mk('button',{textContent:'Cancel'});cp.append(cq,ct,cs,' ',cd,' ',cc);
const lp=mk('aside',{id:'rvp',className:'ui',hidden:true}),card=mk('div',{id:'rvc',className:'ui',hidden:true}),rvt=mk('button',{id:'rvt',textContent:'💬'});
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
// Your remarks: your 💬 comments, and threads of yours on GitHub.
const said=it=>it.kind==='c'||it.kind==='g'&&it.yours;
// The text a comment is on: its change's new text, or its quote where findEnd() found it (e).
const spans=(v,k)=>[...v.querySelectorAll('.chg[data-k="'+k+'"]')].map(e=>e.classList.contains('blk')?e.querySelector('.now'):e.matches('ins')?e:null)
.filter(Boolean).map(e=>{const r=document.createRange();r.selectNodeContents(e);return r;});
const range=(x,e)=>{const r=document.createRange();r.setStart(x.ns[e[2]],x.os[e[2]]);r.setEnd(x.ns[e[3]-1],x.os[e[3]-1]+1);return r;};
// A view's text on one side, spaces collapsed, with the text node and offset of each character, and
// where each heading (as heading() names it) starts in it.
function textOf(v,side){const skip=side==='L'?'ins,.now,summary,.rvb':'del,.was,.rvb',ns=[],os=[],first=new Map();let s='',sp=true,n;
const w=document.createTreeWalker(v,NodeFilter.SHOW_TEXT,{acceptNode:t=>t.parentElement.closest(skip)?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
while(n=w.nextNode()){first.set(n,s.length);for(let i=0;i<n.data.length;i++){const ch=/\\s/.test(n.data[i])?' ':n.data[i];if(ch===' '&&sp)continue;sp=ch===' ';s+=ch;ns.push(n);os.push(i);}}
const hs=[...v.querySelectorAll('h1,h2,h3,h4')].map(h=>{const t=document.createTreeWalker(h,NodeFilter.SHOW_TEXT),k=h.cloneNode(true);let m;
while((m=t.nextNode())&&!first.has(m));k.querySelectorAll('del').forEach(d=>d.remove());return{at:m?first.get(m):-1,name:k.textContent.trim()};}).filter(h=>h.at>=0);
return{s,ns,os,hs};}
// Where q ends in that text, or the word it ends in, and where it starts and ends in it. Among repeats:
// one in its section sec first, then one of whole words, then one partly in an element that matches chg,
// then the one the text before it, b, best leads up to.
function findEnd(x,q,b,sec,chg){q=norm(q);b=norm(b);if(!q)return null;let best=-1,bestE=-1,most=-1;const w=ch=>/\\w/.test(ch||'');
const secs=x.hs.flatMap((h,i)=>h.name===sec?[[h.at,(x.hs[i+1]||{at:Infinity}).at]]:[]);
for(const[at,e]of hits(x,q)){const p=x.s.slice(Math.max(0,at-b.length-1),at).trimEnd();let k=0;
while(k<b.length&&k<p.length&&p[p.length-1-k]===b[b.length-1-k])k++;
k+=(secs.some(([l,h])=>at>=l&&at<h)?2e6:0)+(w(q[0])&&w(x.s[at-1])||w(q[q.length-1])&&w(x.s[e])?0:1e6)+(chg&&x.ns.slice(at,e).some(n=>n.parentElement.closest(chg))?5e5:0);
if(k>most){most=k;best=at;bestE=e;}}
if(best<0)return null;let e=bestE;while(w(x.s[e-1])&&w(x.s[e])&&x.ns[e]===x.ns[e-1])e++;return[x.ns[e-1],x.os[e-1]+1,best,e];}
// Where q starts and ends in that text: as it reads, or else with spaces ignored, as a selection across
// blocks keeps no space between them.
function hits(x,q){const r=[];for(let at=x.s.indexOf(q);at>=0;at=x.s.indexOf(q,at+1))r.push([at,at+q.length]);
const c=q.replace(/ /g,'');if(r.length||!c)return r;
if(!x.c){x.c='';x.ci=[];for(let i=0;i<x.s.length;i++)if(x.s[i]!==' '){x.c+=x.s[i];x.ci.push(i);}}
for(let at=x.c.indexOf(c);at>=0;at=x.c.indexOf(c,at+1))r.push([x.ci[at],x.ci[at+c.length-1]+1]);return r;}
// On a change in view v: after its last inline part, or at the end of its block's new text (in its
// folded summary when it only removes).
function onChange(v,k){const els=v.querySelectorAll('.chg[data-k="'+k+'"]'),e=els[els.length-1];if(!e)return null;
if(!e.classList.contains('blk'))return b=>e.after(b);let t=e.querySelector('.now');if(!t){const m=e.querySelector('summary');return b=>m.append(b);}
while(t.lastElementChild&&/^(P|UL|OL|LI|DL|DT|DD|DIV|BLOCKQUOTE|H[1-6])$/.test(t.lastElementChild.tagName))t=t.lastElementChild;return b=>t.append(b);}
// Where view v has neither its change nor its quote (an accepted change, removed words): at the end of the
// paragraph or list item holding the words before it in its block, c.before (the most of its last 8 that
// are there, 3 at least), or else the words after it, c.after, found as findEnd() finds a quote, in the
// text that reads now first; in a removed block's folded text, on its summary.
function near(v,c,sec,text){const b=norm(c.before).split(' ').filter(Boolean),a=norm(c.after).split(' ').filter(Boolean);
for(const side of['R','L']){const x=text(v,side);let n=null,e;
for(let i=Math.min(8,b.length);!n&&i>=3;i--)if(e=findEnd(x,b.slice(-i).join(' '),b.slice(0,-i).join(' '),sec,''))n=e[0];
for(let i=Math.min(8,a.length);!n&&i>=3;i--)if(e=findEnd(x,a.slice(0,i).join(' '),'',sec,''))n=x.ns[e[2]];
if(!n)continue;let t=n.parentElement.closest('p,li,dt,dd,td,th,h1,h2,h3,h4,h5,h6,pre,blockquote,figcaption');if(!t||!v.contains(t))t=blockOf(n);
const f=t.closest('.was');if(f)return b=>f.querySelector('summary').append(b);
const k=[...t.children].find(e=>/^(P|UL|OL|DL|DIV|BLOCKQUOTE|PRE|TABLE)$/.test(e.tagName));return b=>k?k.before(b):t.append(b);}
return null;}
// The words around a quote found at e in text x, as far as its block goes, for near() where only the quote
// was kept (a revisit on selected text).
function around(x,e){const k=blockOf(x.ns[e[2]]);let i=e[2],j=e[3];
while(i>0&&e[2]-i<400&&k.contains(x.ns[i-1]))i--;while(j<x.s.length&&j-e[3]<400&&k.contains(x.ns[j]))j++;
return{before:x.s.slice(i,e[2]),after:x.s.slice(e[3],j)};}
// Where a revisit is: by its change's own text and the text before it, from a view that has the change;
// failing that, by its quote, "−removed +added": the added text, best where the diff inserts it, or else
// the removed text, best where it deletes it.
function asked(r){const q=r.quote||'',p=q.match(/^(?:−(.*?) )?\\+(.*)$/s),d=!p&&q.match(/^−(.*)$/s),sel='.chg[data-k="'+r.key+'"]';
const s=r.key&&[...document.querySelectorAll('section')].find(s=>s.dataset.file===r.file),v=s&&[...s.querySelectorAll('.v')].find(v=>v.querySelector(sel));
const els=v?[...v.querySelectorAll(sel)]:[];
return els.length?anchor(span(els),els[0].classList.contains('blk')):{quote:p?p[2]:d?d[1]:q,side:r.view==='old'||d?'L':'R',before:'',after:'',chg:p?'ins,.now':d?'del,.old':''};}
// Revisits of one change, or of one quote, are one thread under one mark, so asking again adds to it and
// reopens it, and so does revising one of its drafts (thread names it). It is resolved while all of them
// are, pending while the last has no answer, and new while an answer is unread. It is where its last
// revisit that is not a revision is.
function threads(rs){const ts=[];
for(const r of rs){const t=ts.find(t=>t.file===r.file&&t.members.some(m=>r.thread===m.time||r.key&&m.key===r.key||m.quote===r.quote));t?t.members.push(r):ts.push({file:r.file,members:[r]});}
return ts.map(({file,members:ms})=>{const l=ms[ms.length-1],a=ms.findLast(m=>!m.revises)||l,k=ms.findLast(m=>m.key&&!m.revises);
// Its proposals: each distinct draft once, under the revisit that first proposed it.
const ps=new Map();for(const m of ms)for(const x of m.answers)for(const d of x.drafts)if(!ps.has(d.hash))ps.set(d.hash,proposal(m,d,ms));
return{kind:'r',file,members:ms,time:ms[0].time,view:a.view,section:a.section,sent:a.quote,key:k?k.key:null,...asked(k||a),proposals:ps,
handled:ps.size>0&&[...ps.values()].every(p=>p.state!=='proposed'),
done:ms.every(m=>m.resolved),wait:!l.answers.length,unread:ms.some(m=>m.answers.length>(m.seen||0))};});}
// A drafted comment's state: used while the comment made of it exists, else replaced once a revision of
// it has an answer with another draft, else dismissed, else proposed; revising while a revision waits.
function proposal(m,d,ms){const id=m.time+'#'+d.hash,st=data.proposals[id]||{},by=ms.filter(x=>x.revises===id),c=st.state==='used'&&data.comments.find(c=>c.id===st.comment);
return{id,text:d.text,comment:c||null,revising:by.length>0&&!by[by.length-1].answers.length,
state:c?'used':by.some(x=>x.answers.some(a=>a.drafts.some(e=>e.hash!==d.hash)))?'replaced':st.state==='dismissed'?'dismissed':'proposed'};}
function badge(it){const r=it.kind==='r',st=!r?'':it.done?' rvb-done':it.wait?' rvb-wait':it.unread?' rvb-new':'';
if(it.kind==='g'){const b=mk('span',{className:'rvb rvb-g'+(it.yours?' rvb-gme':''),title:'On '+it.src.source+': '+it.root.author+': '+short(it.root.body)+(it.yours?'\\n\\n(yours)':'')});b.dataset.n=it.shown;
b.onmousedown=e=>e.preventDefault();b.onclick=e=>{e.preventDefault();e.stopPropagation();showGh(it,b);};return b;}
const b=mk('span',{className:'rvb rvb-'+it.kind+st+(it.posted?' rvb-posted':''),title:!r?it.body+(it.posted?'\\n\\n(posted on GitHub)':''):it.members.map(m=>m.prompt).join('\\n')+
(it.done?'\\n\\n(resolved: click to reopen)':it.wait?'\\n\\n(pending: no answer yet)':it.unread?'\\n\\n(new answer: click to read)':'')});
b.onmousedown=e=>e.preventDefault();b.onclick=e=>{e.preventDefault();e.stopPropagation();it.kind==='c'?openCm(it):showCard(it,b);};return b;}
// Marks every comment and revisit; it.at holds its mark per view, and it.on the keys of the changes it is
// on there: its change's, or those its quote's text is part of.
function paint(){document.querySelectorAll('.rvb,.rvok').forEach(b=>{const p=b.parentNode;b.remove();p.normalize();});
const texts=new Map(),text=(v,side)=>{let m=texts.get(v);if(!m)texts.set(v,m={});return m[side]||(m[side]=textOf(v,side));},put=[];
const cx=it=>it.before||it.after?it:it.ctx,near1=(it,v,b)=>{const at=cx(it)&&near(v,cx(it),it.section,text);if(at){at(b);it.on[v.dataset.view]=new Set();}return!!at;},late=[];
const gts=ghThreads();items=[...data.comments.map(x=>({...x,kind:'c'})),...threads(data.revisits),...gts];
for(const it of items){it.at={};it.on={};if(it.hidden)continue;for(const s of document.querySelectorAll('section'))if(s.dataset.file===it.file)for(const v of s.querySelectorAll('.v')){
// Made on a change, it is looked for in change markup first where its change has another key.
const w=v.dataset.view,b=badge(it),on=it.key&&onChange(v,it.key),x=!on&&text(v,it.side),
end=x&&findEnd(x,it.quote,it.before,it.section,it.chg||(it.key?it.side==='L'?'del,.old':'ins,.now':''));
if(on){on(b);it.on[w]=new Set([it.key]);}
else if(end){put.push([end[0],end[1],b]);it.on[w]=new Set(x.ns.slice(end[2],end[3]).map(n=>n.parentElement.closest('.chg')?.dataset.k).filter(Boolean));if(!cx(it))it.ctx=around(x,end);}
else if(!near1(it,v,b)){if(!it.before&&!it.after)late.push([it,v,b]);continue;}
it.at[w]=b;if(said(it)&&w==='acc'&&it.on[w].size)it.ranges=on?spans(v,it.key):[range(x,end)];}}
for(const[it,v,b]of late)if(near1(it,v,b))it.at[v.dataset.view]=b;
// One whose text reads in no view (rewritten since it was asked): on its section's heading.
for(const it of items)if(it.kind!=='g'&&!it.hidden&&it.section&&!Object.keys(it.at).length)for(const s of document.querySelectorAll('section'))if(s.dataset.file===it.file)for(const v of s.querySelectorAll('.v')){
const h=[...v.querySelectorAll('h1,h2,h3,h4')].find(h=>{const k=h.cloneNode(true);k.querySelectorAll('del').forEach(d=>d.remove());return k.textContent.trim()===it.section;});
if(h){const b=badge(it);h.append(b);it.at[v.dataset.view]=b;it.on[v.dataset.view]=new Set();}}
// One from elsewhere whose quote reads in no view (its text was edited after the comment was made): at the
// end of the paragraph that shares the most of its words, half of them at least, marked as the closest match.
const words=t=>new Set(norm(t).toLowerCase().match(/[\\p{L}\\p{N}]+/gu)||[]);
for(const it of items)if(it.kind==='g'&&!it.hidden&&it.quote&&!Object.keys(it.at).length){const q=words(it.quote);if(q.size<2)continue;
for(const s of document.querySelectorAll('section'))if(s.dataset.file===it.file)for(const v of s.querySelectorAll('.v')){let best=null,most=0;
for(const p of v.querySelectorAll('p,li,td,th,dt,dd,h1,h2,h3,h4')){if(p.querySelector('p,li,td'))continue;const k=p.cloneNode(true);k.querySelectorAll(it.side==='L'?'ins,.now,.rvb':'del,.was,.rvb').forEach(e=>e.remove());
const ws=words(k.textContent);let n=0;for(const w of q)if(ws.has(w))n++;if(n>most){most=n;best=p;}}
if(best&&most>=Math.max(2,q.size/2)){it.near=true;const b=badge(it);b.classList.add('rvb-near');b.title+='\\n\\n(its text has changed: here at the closest match)';best.append(b);it.at[v.dataset.view]=b;it.on[v.dataset.view]=new Set();}}}
// In To review, a mark on text already reviewed (on changes in the Accepted view, on none here) is muted.
for(const it of items){const b=it.at.review;if(b&&!it.on.review.size&&it.on.acc?.size){b.classList.add('rvb-past');b.title+='\\n\\n(on '+W.accepted+' text)';}}
// From the end of the doc back, so splitting a text node leaves the places before it where they were;
// in a removed block's folded text, on its summary; after removed words, which settled Reviewed hides.
put.sort((x,y)=>x[0]===y[0]?y[1]-x[1]:x[0].compareDocumentPosition(y[0])&Node.DOCUMENT_POSITION_FOLLOWING?1:-1);
for(const[t,o,b]of put){const f=t.parentElement.closest('.was'),d=t.parentElement.closest('del.chg');if(f)f.querySelector('summary').append(b);else if(d)d.after(b);else{t.splitText(o);t.after(b);}}
// Your comments' wording on reviewed changes, underlined in Reviewed: the change's new text, or the quote.
if(window.Highlight&&CSS.highlights)CSS.highlights.set('rvcm',new Highlight(...items.flatMap(i=>i.ranges||[])));
// Which changes your comments are on, per view, and how many: on commits, accepting one of them says
// ✓ reviewed, not looks good, and in Reviewed each accepted change says which it got, ✓ or ✓ 💬 N.
mine={};for(const it of items)if(said(it))for(const[w,ks]of Object.entries(it.on))for(const k of ks)(mine[w]=mine[w]||new Map()).set(k,(mine[w].get(k)||0)+1);
if(W.reviewed)for(const g of groups.filter(g=>g.view==='acc')){const n=mine.acc?.get(g.k),on=onChange(g.els[0].closest('.v'),g.k);
if(on)on(mk('span',{className:'rvok'+(n?' rvok-c':''),textContent:n?'✓ 💬 '+n:'✓',title:n?'Reviewed, with '+n+' comment'+(n>1?'s':''):'Looks good'}));}
status();
const ts=items.filter(i=>i.kind==='r'&&!i.done),np=ts.filter(t=>t.wait).length,nn=ts.filter(t=>t.unread).length;
rvt.textContent='💬 '+data.comments.length+(ts.length?' · ✎ '+ts.length:'')+(np?' · '+np+' pending':'')+(nn?' · '+nn+' new':'');
rvt.classList.toggle('rvt-new',nn>0);if(!lp.hidden)list();
const open=!card.hidden&&items.find(i=>i.kind==='r'&&i.time===carded);if(open){const y=card.scrollTop;fillCard(open);card.scrollTop=y;}tally();chips();}
// Each doc's bar counts the comments made elsewhere on it (on GitHub, in the Google Docs); a click opens the
// Review panel on that doc's comments, where each entry goes to its place.
function chips(){document.querySelectorAll('.rvfc').forEach(e=>e.remove());const all=order();
for(const s of document.querySelectorAll('section')){const ts=all.filter(i=>i.kind==='g'&&!i.hidden&&i.file===s.dataset.file);if(!ts.length)continue;
const a=mk('a',{className:'rvfc ui',href:'#',textContent:'💬 '+ts.length+' comment'+(ts.length>1?'s':''),
title:'Comments on this doc, from '+[...new Set(ts.map(t=>t.src.source))].join(' and ')+': open them in the Review panel'});
a.onclick=e=>{e.preventDefault();only=s.dataset.file;filter='open';toggle(true);lp.scrollTop=0;};
const bar=s.querySelector('.file');bar.insertBefore(a,bar.querySelector('.fbar'));}}
// Accepted (reviewed) changes that carry comments or open questions, counted on each file's line in the
// header, so they are not lost once they leave To review; a count steps through them in the Accepted view.
function tally(){document.querySelectorAll('.rvn').forEach(e=>e.remove());
for(const s of document.querySelectorAll('section')){const n=document.querySelector('header a[href="#'+s.id+'"]')?.nextElementSibling;if(!n)continue;
const on=f=>new Set(items.filter(it=>f(it)&&it.at.acc?.closest('section')===s).flatMap(it=>[...it.on.acc]));
for(const[ks,w]of[[on(said),k=>k>1?' with comments':' with a comment'],[on(it=>it.kind==='r'&&!it.done),k=>' open question'+(k>1?'s':'')]]){if(!ks.size)continue;
const b=mk('button',{textContent:ks.size+w(ks.size),title:'Step through them in the '+W.Accepted+' view'}),x=mk('span',{className:'rvn'});
b.onclick=()=>{if(cur!=='acc')view('acc');const l=groups.filter(g=>g.view==='acc'&&g.sec===s&&ks.has(g.k));if(l.length)focus(l[(l.indexOf(c)+1)%l.length]);};x.append(' · ',b);n.append(x);}}}
function take(d){data=d;seen=JSON.stringify(d);paint();}
wordsOf=x=>x&&W.reviewed&&mine[x.view]?.has(x.k)?{...W,accept:W.reviewed,accept_key:W.reviewed_key,accept_title:W.reviewed_title}:W;
// On commits, accepting a change resolves the answered threads on it alone (not pending ones, nor ones on
// text outside a change or across several); taking it back reopens those, not ones resolved by hand.
onAccept=async(xs,accepting)=>{if(!W.reviewed)return;const ts=[];
for(const x of xs)for(const it of items)if(it.kind==='r'&&!ts.includes(it)&&it.on[x.view]?.size===1&&it.on[x.view].has(x.k)&&
(accepting?!it.done&&!it.wait:it.done&&it.members.every(m=>m.resolved_by==='accept')))ts.push(it);
if(ts.length)await post('review',{revisits:ts.flatMap(t=>t.members.map(m=>m.time)),resolve:accepting,by:'accept'});};
// On commits, Reviewed shows reviewed changes settled: the new text only, a thin bar beside a block with a
// change in it, and only the commented ones' chips. Clicking a change shows its diff again; ± diffs, kept
// per viewer, shows them all.
let diffs=false;try{diffs=localStorage.getItem('docdiff-diffs')==='1';}catch(e){}
const sd=mk('button',{id:'rvsd',textContent:'± diffs',title:'Show the diffs of reviewed changes'});
function settle(){body.classList.toggle('rv-settled',!!W.reviewed&&!diffs);sd.setAttribute('aria-pressed',diffs);}
sd.onclick=()=>{diffs=!diffs;try{localStorage.setItem('docdiff-diffs',diffs?'1':'');}catch(e){}settle();};
if(W.reviewed){rvt.before(sd);for(const g of groups)if(g.view==='acc'&&!g.els[0].classList.contains('blk'))
g.els[0].closest('p,li,dt,dd,td,th,h1,h2,h3,h4,h5,h6,pre,blockquote,figcaption')?.classList.add('rvbar');}
document.addEventListener('click',e=>{if(!body.classList.contains('rv-settled')||!getSelection().isCollapsed||e.target.closest('.rvb,.rvok,a,summary,.ui'))return;
const t=e.target.closest('.v-acc .chg'),g=t&&groups.find(x=>x.view==='acc'&&x.els.includes(t));if(g)g.els.forEach(el=>el.classList.toggle('rvshow'));});
settle();
// Comments made elsewhere, read only, one source per plugin that has them (remarks(): github_pr.py's on
// the PR, gdoc_comments.py's in the Google Docs), each named by its source: a thread is a comment with its
// replies, on the change or text it was placed on, or else in the panel's group for its source, with the
// PR's own comments and reviews. A comment of yours whose text one of them has (or has all of) was posted,
// on a source you post to (posting: GitHub).
const same=(a,b)=>(a=norm(a),b=norm(b),a&&b&&(a===b||a.length>=20&&b.includes(a)||b.length>=20&&a.includes(b)));
// With your login on a source known (me: GitHub's --me, or the token's user), a thread you wrote in (yours)
// counts as your comment does: its change is commented. A comment of yours that one of your 💬 holds shows
// as that 💬 alone (posted), so a thread of nothing else has no mark, and one with replies counts only those.
const isMe=c=>!!c.src?.me&&c.author===c.src.me;
function ghThreads(){const out=[],told=new Set();data.comments.forEach(c=>c.posted=null);
for(const[k,src]of(data.remotes||[]).entries()){const cs=src.comments||[],by=new Map(cs.map(c=>[c.id,c])),ts=new Map();cs.forEach(c=>c.src=src);
for(const c of cs){let r=c;while(r.reply_to&&by.has(r.reply_to))r=by.get(r.reply_to);if(!ts.has(r.id))ts.set(r.id,[]);ts.get(r.id).push(c);}
if(src.posting)for(const c of data.comments){const g=cs.find(g=>(!src.me||isMe(g))&&same(g.body,c.body));if(g){c.posted=g.url||null;if(src.me)told.add(g);}}
out.push(...[...ts].map(([id,ms])=>{const r=by.get(id),left=ms.filter(c=>!told.has(c));return{kind:'g',id:'g'+k+'-'+id,src,root:r,
members:ms.sort((a,b)=>a.time<b.time?-1:a.time>b.time?1:0),time:r.time,file:r.file||null,key:r.key||null,quote:r.quote||'',before:r.before||'',
side:r.side||'R',section:'',yours:left.some(isMe),shown:left.length,hidden:!left.length};}));}
return out;}
const ago=t=>{const s=(Date.now()-t)/1000;return s<90?'just now':s<5400?Math.round(s/60)+' min ago':s<129600?Math.round(s/3600)+' h ago':Math.round(s/86400)+' days ago';};
const at=c=>c.path?c.path.split('/').pop()+(c.line?':'+c.line:'')+(c.side==='L'?' (old)':''):'';
// Where a comment is, in its source's words: its line, or the PR or a review; a source's own note, such as
// what a push does to a Google Doc comment's text, comes after.
const ghLabel=c=>c.kind==='pr'?'on the PR':c.kind==='review'?'review'+(c.state?': '+c.state.toLowerCase().replace(/_/g,' '):''):
[at(c),c.outdated&&'outdated',!c.file&&'not placed',c.note].filter(Boolean).join(' · ');
const ghBodies=it=>it.members.map(c=>{const d=mk('div',{className:'rvgc'+(c.reply_to?' rvgr':'')}),x=mk('div',{className:'rva rvgh'});x.innerHTML=c.html;
d.append(mk('div',{className:'rvm',textContent:c.author+(isMe(c)?' (you)':'')+' · '+ago(new Date(c.time))}),x);return d;});
const ghOpen=it=>mk('a',{href:it.root.url,target:'_blank',rel:'noopener',textContent:it.src.open||'Open ↗'});
function showGh(it,b){const h=mk('div',{className:'rvch'}),bd=mk('div',{className:'rvcb'});carded=null;
h.append(mk('span',{className:'rvm',textContent:['On '+it.src.source,ghLabel(it.root),it.near&&'its text has changed: here at the closest match',
it.members.length+' comment'+(it.members.length>1?'s':'')].filter(Boolean).join(' · ')}),ghOpen(it));
bd.append(...ghBodies(it));card.replaceChildren(h,bd);place(b);}
// When each source was last read, and why not since; ↻ asks again where the source can be asked (refresh).
function ghStatus(){return(data.remotes||[]).map(g=>{const r=mk('div',{className:'rvgs rvm'});
r.append((g.fetched?g.source+' read '+ago(g.fetched*1e3):g.source+' not read yet')+(g.error?' · '+g.error+(g.fetched?', showing that copy':''):''));
if(g.refresh){const b=act('↻ '+g.source,'Read the comments on '+g.source+' again',async()=>{b.textContent='reading…';
try{take(await post('review',{github:'now'}));}catch(e){b.textContent='↻ '+g.source;}});r.append(b);}return r;});}
async function load(){try{const r=await fetch('review');if(!r.ok)throw new Error(r.status);const d=await r.json();rvt.disabled=false;rvt.title='Review: comments and '+W.revisits;
if(JSON.stringify(d)!==seen)take(d);}catch(e){rvt.disabled=true;rvt.title='review: not served (restart the docdiff server)';}}
function openCm(it){if(!it){pos.textContent='select text, or step to a change, then press c';return;}
hidePop();card.hidden=true;draft=it;cq.textContent=it.quote;ct.value=it.body||'';cd.hidden=!it.id;cp.hidden=false;ct.focus();}
function closeCm(){cp.hidden=true;ct.value='';draft=null;}
async function save(req){try{take(await post('review',req));closeCm();}catch(e){pos.textContent='comment not saved: '+e.message+' (is the docdiff server running?)';}}
cs.onclick=()=>{const t=ct.value.trim();if(!draft)return;if(!t)return draft.id?save({id:draft.id,delete:true}):closeCm();
save(draft.id?{id:draft.id,body:t}:{...draft,body:t});};
cd.onclick=()=>draft?.id&&save({id:draft.id,delete:true});cc.onclick=closeCm;
ct.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();cs.click();}if(e.key==='Escape')closeCm();});
const act=(t,title,f)=>{const b=mk('button',{className:'rvx',textContent:t,title});b.onclick=e=>{e.stopPropagation();f();};return b;};
const short=t=>(t=norm(t)).length>70?t.slice(0,70).replace(/\\s+\\S*$/,'')+'…':t;
// A thread's requests, each with its answers, a revision with the draft it revises.
const asks=(it,shown=new Set())=>it.members.flatMap(m=>[...(m.revises?[mk('div',{className:'rvm',textContent:'↻ revise the draft “'+short(m.quote)+'”'})]:[]),
mk('div',{className:'rvt',textContent:m.prompt}),...answers(it,m,shown)]);
// Each drafted comment (a blockquote) shows as its proposal: with its state and actions under it, folded
// to a stub when dismissed and into a summary when replaced; one proposed again (shown has it) shows once,
// its repeats faded, or gone with it once it is dismissed or replaced.
function answers(it,m,shown){if(!m.answers.length)return[mk('div',{className:'rvm',textContent:'pending: Claude has not answered yet'})];
return m.answers.map(a=>{const d=mk('div',{className:'rva'});d.innerHTML=a.html;const qs=d.querySelectorAll(':scope>blockquote'),open=[];
if(qs.length!==a.drafts.length)return d;
qs.forEach((q,i)=>{const p=it.proposals.get(a.drafts[i].hash);if(shown.has(p.id)){if(p.state==='dismissed'||p.state==='replaced')q.classList.add('rvgone');else{q.classList.add('rvdup');q.after(mk('div',{className:'rvm rvpr',textContent:'the same draft as above'}));}return;}
shown.add(p.id);decorate(q,p,it);if(p.state==='proposed')open.push(p);});
if(open.length>1)d.append(act('💬 Use all '+open.length+' as one comment','One new comment of these drafts, open to edit',()=>use(it,open,open.map(p=>p.text).join('\\n\\n'))));return d;});}
function decorate(q,p,it){const r=mk('div',{className:'rvpr'});
if(p.state==='replaced'){const f=mk('details',{className:'rvrep'});q.replaceWith(f);f.append(mk('summary',{textContent:'replaced draft: '+short(p.text)}),q);return;}
if(p.state==='dismissed'){q.classList.add('rvgone');r.classList.add('rvstub');r.append(mk('span',{className:'rvm',textContent:'dismissed draft: '+short(p.text)}),act('↶ Undo','Show it as proposed again',()=>setProposal(p,null)));q.after(r);return;}
if(p.state==='used'){const u=act('↶ Undo','Delete the comment made of it',()=>{if(norm(p.comment.body)!==norm(p.text)&&u.textContent==='↶ Undo'){u.textContent='↶ Undo: delete your edited comment?';return;}save({id:p.comment.id,delete:true});});
r.append(mk('span',{className:'rvm rvused',textContent:'✓ used'}),act('💬 Open comment','Edit the comment made of it',()=>{card.hidden=true;openCm({...p.comment,kind:'c'});}),u);q.after(r);return;}
r.append(...(p.revising?[mk('span',{className:'rvm',textContent:'revising…'})]:[]),act('💬 Use','A new comment of this draft, open to edit',()=>use(it,[p],p.text)),
act('✎ Revise','Ask Claude to rework this draft',()=>revise(it,p)),act('Dismiss','Fold it away',()=>setProposal(p,'dismissed')));q.after(r);}
const state=it=>it.done?'resolved':it.wait?'pending':it.unread?'new answer':it.handled?'drafts handled':'answered';
// A thread's actions, at the top of its card and of its panel entry, so they stay in reach of a long
// thread: Ask more (r while its card is open), and Resolve (urged once every draft is used or dismissed)
// or Reopen. On a card whose thread is on one change in To review (g, on commits), that change's accept
// comes first, as the toolbar words it (a while the card is open), and Resolve is the lesser choice.
const actions=(it,g)=>{const b=act(it.done?'↺ Reopen':'✓ Resolve',it.done?'Back to the open revisits':'Done with it: its mark folds to a dot',()=>{card.hidden=true;setThread(it,{resolve:!it.done});});
if(g){b.classList.add('rvsec');if(!it.done)b.title='Done asking, not done reviewing: resolves this thread alone, and its change stays in To review';}
else if(it.handled&&!it.done)b.classList.add('primary');
const w=g&&wordsOf(g),ok=g&&act(w.accept,w.accept_title.replace('the current change','its change').replace(/ \\(a\\)$/,'')+
(it.done?'':it.wait?'. This thread has no answer yet, so it stays open':'. Its answered threads resolve with it, this one too')+' (a)',()=>acceptFor(g));
if(ok)ok.classList.add('primary');
return[...(ok?[ok]:[]),act('✎ '+W.sent[0].toUpperCase()+W.sent.slice(1)+' more','Go on in this thread (r): it gets the new '+(W.sent==='ask'?'question':'request')+', and reopens if resolved',()=>more(it)),b];};
// The change a thread's card accepts for it: its one change, while that is in To review, on commits.
const toAccept=it=>W.reviewed&&cur==='review'&&it.on.review?.size===1&&groups.find(x=>x.view==='review'&&x.file===it.file&&it.on.review.has(x.k))||null;
function acceptFor(g){card.hidden=true;focus(g,false);accept();}
async function setThread(it,req){try{take(await post('review',{revisits:it.members.map(m=>m.time),...req}));}catch(e){pos.textContent='not saved: '+e.message+' (is the docdiff server running?)';}}
const setProposal=(p,state)=>post('review',{proposals:[p.id],state}).then(take).catch(e=>{pos.textContent='not saved: '+e.message+' (is the docdiff server running?)';});
// Use: the drafts become a comment on the thread's change or quote, opened to edit.
async function use(it,ps,body){try{const st=await post('review',{file:it.file,view:it.view,key:it.key,quote:it.quote,side:it.side,before:it.before,after:it.after,section:it.section,body,uses:ps.map(p=>p.id)});
take(st);card.hidden=true;openCm({...st.comments[st.comments.length-1],kind:'c'});}catch(e){pos.textContent='comment not made: '+e.message+' (is the docdiff server running?)';}}
// Revise: a revisit of the draft, in the thread; its answer's draft replaces it. Ask more: a revisit of the
// thread's own text that adds to the thread. Both name the thread (thread: its first revisit's time), so
// they join it whatever text the request quotes, and the panel says which thread they go on from.
const elOf=it=>it.at[cur]||Object.values(it.at)[0]||[...document.querySelectorAll('section')].find(s=>s.dataset.file===it.file);
const first=it=>short((it.members.find(m=>!m.revises)||it.members[0]).prompt);
function revise(it,p){card.hidden=true;openRev({quote:p.text,el:elOf(it),key:it.key,fields:{thread:it.time,revises:p.id,section:it.section},
context:'revises a draft in “'+first(it)+'”'});}
function more(it){card.hidden=true;openRev({quote:it.sent,el:elOf(it),key:it.key,fields:{thread:it.time,section:it.section},context:'goes on from “'+first(it)+'”'});}
// A thread's card, under its mark or, with more room there, over it: its header (state and actions) stays
// at the top while the thread scrolls under it. It follows the thread's changes while open; opening it
// reads its answers.
let carded=null;
function fillCard(it){const h=mk('div',{className:'rvch'}),bd=mk('div',{className:'rvcb'});carded=it.time;
h.append(mk('span',{className:'rvm',textContent:'✎ '+[it.section,it.time.replace('T',' '),state(it)].filter(Boolean).join(' · ')}),...actions(it,toAccept(it)));
bd.append(mk('blockquote',{textContent:it.sent}),...asks(it));card.replaceChildren(h,bd);}
function showCard(it,b){fillCard(it);place(b);if(it.unread)setThread(it,{seen:true});}
// A card is placed and sized in the visual viewport, which a page wider than the screen does not stretch,
// and left of the Review panel while that is open beside the doc.
function place(b){const v=visualViewport||{width:innerWidth,offsetLeft:0},p=lp.hidden?null:lp.getBoundingClientRect(),
right=p&&p.left>v.offsetLeft+v.width/2?p.left:v.offsetLeft+v.width,w=Math.min(560,(right-v.offsetLeft)*.95);
card.style.left=v.offsetLeft+scrollX+'px';card.style.width=w+'px';card.hidden=false;card.scrollTop=0;
const r=b.getBoundingClientRect(),below=innerHeight-r.bottom-12,above=r.top-document.querySelector('header').getBoundingClientRect().bottom-12,down=below>=300||below>=above;
card.style.maxHeight=Math.max(120,down?below:above)+'px';card.style.left=Math.max(v.offsetLeft+8,Math.min(r.left,right-card.offsetWidth-8))+scrollX+'px';
card.style.top=(down?r.bottom+4:r.top-4-card.offsetHeight)+scrollY+'px';}
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
// A panel entry: its line (where, state, actions), then its body, folded to a few lines when long.
function row(it,files){const r=it.kind==='r',d=mk('div',{className:'rvi'+(it.unread&&!it.done?' rvi-new':'')}),h=mk('div',{className:'rvih'}),m=mk('span',{className:'rvm'}),bd=mk('div',{className:'rvib'}),w=where(it);
d.dataset.id=r?'r'+it.time:it.kind==='g'?it.id:'c'+it.id;
if(it.kind==='g'){m.append(['On '+it.src.source,files>1&&it.file,ghLabel(it.root)].filter(Boolean).join(' · '));h.append(m,ghOpen(it));
bd.append(...ghBodies(it));d.append(h,bd);d.onclick=e=>{if(!e.target.closest('a,button'))jump(it);};return d;}
m.append((r?'✎ ':'💬 ')+[files>1&&it.file,it.section,r&&state(it)].filter(Boolean).join(' · ')+(Object.keys(it.at).length?'':' · not in the page now'));
if(w)m.append(' · ',mk('a',{href:it.github.href,target:'_blank',rel:'noopener',textContent:w.split('/').pop()+' ↗'}));
if(it.posted)m.append(' · ',mk('a',{href:it.posted,target:'_blank',rel:'noopener',className:'rvused',textContent:'posted ↗'}));
if(it.quote)bd.append(mk('blockquote',{textContent:r?it.sent:it.quote}));
if(r)h.append(m,...actions(it)),bd.append(...asks(it));else{const e=act('edit','',()=>openCm(it)),x=act('delete','',()=>x.textContent==='delete'?x.textContent='delete?':save({id:it.id,delete:true})),
c=act('copy','Copy as Markdown: its file:line, the quoted text and the comment',async()=>{c.textContent=await copy(markdown([it]))?'copied':'copying failed';setTimeout(()=>c.textContent='copy',1500);});
h.append(m,e,c,x);bd.append(mk('div',{className:'rvt',textContent:it.body}));}
d.append(h,bd);d.onclick=e=>{if(e.target.closest('a,button'))return;jump(it);if(it.unread)setThread(it,{seen:true});};return d;}
// Folds an entry's body when it is longer than a few lines; Show more and Show less, kept across re-renders.
const unfolded=new Set();
function fold(bd){const id=bd.closest('.rvi').dataset.id;bd.nextElementSibling?.classList.contains('rvmore')&&bd.nextElementSibling.remove();
bd.classList.add('rvfold');const long=bd.scrollHeight>bd.clientHeight+4;bd.classList.toggle('rvfold',long&&!unfolded.has(id));if(!long)return;
bd.after(act(unfolded.has(id)?'Show less':'Show more','',()=>{unfolded.has(id)?unfolded.delete(id):unfolded.add(id);fold(bd);}));bd.nextElementSibling.classList.add('rvmore');}
// Open (comments and unresolved revisits), then the resolved revisits folded; or either alone. The panel
// opens on Open; a reload keeps whichever was chosen.
let filter='open',folded=true;
// Comments from elsewhere that have no place in the doc fold into a group per source (On GitHub, On Google
// Docs), under the open ones.
let ghFolded=false;
// One doc's entries only, from its bar's comment count; the panel's 💬 button shows every doc's again.
let only=null;
function list(){const all=order().filter(i=>!only||i.file===only),nc=data.comments.length,files=document.querySelectorAll('section').length,done=all.filter(i=>i.done),
loose=all.filter(i=>i.kind==='g'&&!i.hidden&&!Object.keys(i.at).length),its=all.filter(i=>!loose.includes(i)&&!i.hidden);
const f=mk('div',{className:'seg rvf'}),g=mk('details',{className:'rvg',open:!folded});g.ontoggle=()=>{folded=!g.open;keep();if(g.open)g.querySelectorAll('.rvib').forEach(fold);};
for(const[v,l]of[['open','Open'],['resolved','Resolved'],['all','All']]){const b=mk('button',{textContent:l});b.setAttribute('aria-pressed',v===filter);
b.onclick=()=>{filter=v;keep();list();};f.append(b);}
g.append(mk('summary',{textContent:'Resolved ('+done.length+')'}),...done.map(it=>row(it,files)));
// Copying all of your 💬 comments is offered only while there are some; each one has its own copy too.
const h=mk('div',{className:'rvh'}),cm=mk('button',{textContent:'Copy all as Markdown',title:'Your comments, each with its file:line and quote, to paste into GitHub'}),x=mk('button',{textContent:'×',title:'Close'});
cm.onclick=async()=>{cm.textContent=await copy(markdown(its))?'copied '+nc:'copying failed';setTimeout(()=>cm.textContent='Copy all as Markdown',1500);};
x.onclick=()=>toggle(false);h.append(mk('b',{textContent:'Review'}),mk('span',{className:'n',textContent:nc+' comment(s) · '+its.filter(i=>i.kind==='r'&&!i.done).length+' open · '+done.length+' resolved'}),...(nc?[cm]:[]),x);
if(only){const o=mk('button',{className:'rvonly',textContent:only+' ×',title:'Show the entries on every doc'});o.onclick=()=>{only=null;list();};h.append(o);}
const bySource=new Map();for(const it of loose)bySource.set(it.src.source,[...(bySource.get(it.src.source)||[]),it]);
const ghs=[...bySource].map(([n,l])=>{const gh=mk('details',{className:'rvg',open:!ghFolded});gh.ontoggle=()=>{ghFolded=!gh.open;if(gh.open)gh.querySelectorAll('.rvib').forEach(fold);};
gh.append(mk('summary',{textContent:'On '+n+' ('+l.length+')'}),...l.map(it=>row(it,files)));return gh;});
lp.replaceChildren(h,...ghStatus(),f,...(filter==='resolved'?done:its.filter(i=>!i.done)).map(it=>row(it,files)),
...(filter!=='resolved'?ghs:[]),...(filter==='all'&&done.length?[g]:[]));
lp.querySelectorAll('.rvib').forEach(fold);}
// Paste-ready: per comment, its file:line (linked to it on GitHub) or else its file and section, the
// quoted text and the comment.
function markdown(its){return its.filter(it=>it.kind==='c').map(it=>{const w=where(it),loc=w||it.file+(it.section?' · '+it.section:'');
const q=it.quote.length>300?it.quote.slice(0,300).replace(/\\s+\\S*$/,'')+'…':it.quote;
return '**'+(w&&it.github.href?'['+loc+']('+it.github.href+')':loc)+'**\\n\\n'+(q?'> '+q+'\\n\\n':'')+it.body;}).join('\\n\\n---\\n\\n');}
function toggle(on){lp.hidden=!on;body.classList.toggle('rv-open',on);if(on){lp.style.top=document.querySelector('header').getBoundingClientRect().bottom+'px';list();}keep();}
// The panel, its filter and its fold outlast reloads.
function keep(){try{sessionStorage.setItem('docdiff-review',JSON.stringify({on:!lp.hidden,filter,folded}));}catch(e){}}
rvt.onclick=()=>{only=null;if(lp.hidden)filter='open';toggle(lp.hidden);};addEventListener('resize',()=>{if(!lp.hidden)lp.style.top=document.querySelector('header').getBoundingClientRect().bottom+'px';});
pc.onmousedown=e=>e.preventDefault();pc.onclick=()=>{const a=anchorOf();hidePop();openCm(a);};
// The doc's own click and selection handling leaves the panel and the card alone.
for(const e of [lp,card])for(const t of ['click','mouseup'])e.addEventListener(t,ev=>ev.stopPropagation());
document.addEventListener('click',e=>{if(!e.target.closest('#rvc'))card.hidden=true;});
// With a thread's card open, r asks more in that thread, and a accepts the change the card offers, before
// the page's own r and a see them.
addEventListener('keydown',e=>{if(e.key!=='r'&&e.key!=='a'||card.hidden||!carded||e.target.closest('input,textarea')||e.metaKey||e.ctrlKey||e.altKey)return;
const it=items.find(i=>i.kind==='r'&&i.time===carded),g=it&&e.key==='a'&&toAccept(it);if(!it||e.key==='a'&&!g)return;
e.preventDefault();e.stopImmediatePropagation();e.key==='r'?more(it):acceptFor(g);},true);
document.addEventListener('keydown',e=>{if(e.target.closest('input,textarea')||e.metaKey||e.ctrlKey||e.altKey)return;
if(e.key==='Escape')card.hidden=true;if(e.key==='c'){e.preventDefault();openCm(anchorOf());}});
try{const st=JSON.parse(sessionStorage.getItem('docdiff-review'))||{};filter=st.filter||filter;folded=st.folded!==false;if(st.on)toggle(true);}catch(e){}
load().then(()=>post('review',{github:'auto'})).then(take).catch(()=>{});setInterval(load,2000);})();
'''


def get(run, path, query):
    """The comments, and the revisits with their answers, for the page."""
    if path != '/review':
        return None
    return json.dumps(state(run), ensure_ascii=False).encode(), 'application/json'


def post(out, path, req):
    """Adds a comment, marking the proposals req['uses'] names as used by it, or edits or deletes the one
    req['id'] names; or resolves (resolve true), reopens (false) or reads (seen) the revisits
    req['revisits'] names by time (by 'accept': as their change was accepted); or sets the state of the
    proposals req['proposals'] names, dismissed or none; or brings the PR's comments on GitHub up to date
    (req['github']: 'auto' or 'now', as remarks() in github_pr.py takes it). Returns what get() does."""
    if path != '/review':
        return None
    run = json.loads((out / '.run.json').read_text())
    if req.get('github'):
        return state(run, req['github'])
    with lock:
        doc = load(out)
        cs = doc['comments']
        was = next((c for c in cs if req.get('id') and c['id'] == req['id']), None)
        if req.get('proposals'):
            for p in map(str, req['proposals']):
                if req.get('state') == 'dismissed':
                    doc['proposals'][p] = {'state': 'dismissed', 'time': now()}
                else:
                    doc['proposals'].pop(p, None)
        elif req.get('revisits'):
            read = collections.Counter(a.get('time') for a in records(out / 'answers.jsonl'))
            for t in map(str, req['revisits']):
                st = doc['revisits'].setdefault(t, {})
                # Resolved by accepting its change (by 'accept'), it reopens when that is taken back; by hand, not.
                if req.get('resolve'):
                    st.update(resolved=now(), **({'resolved_by': 'accept'} if req.get('by') == 'accept' else {}))
                    if req.get('by') != 'accept':
                        st.pop('resolved_by', None)
                elif 'resolve' in req:
                    st.pop('resolved', None)
                    st.pop('resolved_by', None)
                if req.get('seen'):
                    st['seen'] = read[t]
        elif was and req.get('delete'):
            cs.remove(was)
            doc['proposals'] = {p: st for p, st in doc['proposals'].items() if st.get('comment') != was['id']}
        elif was:
            was['body'], was['edited'] = str(req['body']), now()
        elif not req.get('id'):
            cs.append({'id': secrets.token_hex(4), 'time': now(), **{k: req.get(k) for k in FIELDS}})
            locate(run, cs[-1:])
            for p in map(str, req.get('uses') or []):
                doc['proposals'][p] = {'state': 'used', 'comment': cs[-1]['id'], 'time': now()}
        save(out, doc)
    return state(run)


def state(run, refresh=None):
    out = pathlib.Path(run['out'])
    with lock:
        doc = load(out)
        # Another PR or head (the PR moved on): find the comments in its files again.
        moved = [c for c in doc['comments']
                 if [(c.get('github') or {}).get(k) for k in ('pr', 'head')] != [run.get('pr'), run.get('head')]]
        if moved:
            locate(run, moved)
            save(out, doc)
    answers = {}
    for a in records(out / 'answers.jsonl'):
        text = str(a.get('answer') or '')
        answers.setdefault(a.get('time'), []).append({'html': markdown(text), 'drafts': [
            {'text': d, 'hash': hashlib.sha1(' '.join(d.split()).encode()).hexdigest()[:8]} for d in drafts(text)]})
    # Comments made elsewhere, read only, from each plugin that has them: github_pr.py's on the PR (with
    # --pr), gdoc_comments.py's in the Google Docs (with --comments).
    remotes = [r for r in (m.remarks(run, out, refresh) for m in docdiff.PLUGINS if hasattr(m, 'remarks')) if r]
    for r in remotes:
        for c in r.get('comments', []):
            c['html'] = markdown(c['body'])
    return {'comments': doc['comments'], 'proposals': doc['proposals'], 'remotes': remotes,
            'revisits': [{**{k: r.get(k) for k in ('time', 'file', 'section', 'quote', 'view', 'prompt', 'key',
                                                    'thread', 'revises')},
                          'answers': answers.get(r.get('time'), []), **doc['revisits'].get(r.get('time'), {})}
                         for r in records(out / 'revisit.jsonl')]}


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


def load(out):
    f = out / 'review.json'
    doc = json.loads(f.read_text()) if f.exists() else {}
    return {'comments': doc.get('comments', []), 'revisits': doc.get('revisits', {}), 'proposals': doc.get('proposals', {})}


def save(out, doc):
    # Written whole and then moved in, so a crash never leaves half a file.
    f = out / 'review.json'
    f.with_suffix('.tmp').write_text(json.dumps(doc, ensure_ascii=False, indent=1))
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


def drafts(text):
    """An answer's drafted comments: its blockquotes, as Markdown without the quote marks."""
    out, inside = [], False
    for line in text.splitlines():
        m = re.match(r' {0,3}> ?(.*)', line)
        if m and not inside:
            out.append([])
        if m:
            out[-1].append(m.group(1))
        inside = bool(m)
    return [d for d in ('\n'.join(q).strip() for q in out) if d]


def markdown(text):
    """An answer rendered, raw HTML in it shown as text, and no javascript: links. pandoc's Markdown, as
    its gfm reader keeps raw HTML even without raw_html; with GFM's bare links, straight quotes, and lists,
    quotes and headings right after a line of text."""
    if text not in rendered:
        r = subprocess.run(['pandoc', '-f', 'markdown-raw_html-raw_tex-raw_attribute-smart-blank_before_blockquote'
                            '-blank_before_header+autolink_bare_uris+lists_without_preceding_blankline', '-t', 'html'],
                           input=text, capture_output=True, text=True)
        rendered[text] = (re.sub(r'\shref="\s*javascript:[^"]*"', '', r.stdout, flags=re.I) if not r.returncode
                          else f'<pre>{html.escape(text)}</pre>')
    return rendered[text]


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%S')
