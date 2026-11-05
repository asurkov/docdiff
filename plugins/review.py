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
answers.jsonl, {time: the revisit's time, answer: Markdown}, and an answer's blockquotes are its drafted
comments. Revisits of one change or one quote are one thread under one mark: pending while the last has
no answer, new while an answer is unread, and resolved, folded to a dot, while all of them are; asking
again reopens it. review.json keeps that by revisit time, {revisits: {time: {resolved, seen}}}, seen the
number of its answers read; the two .jsonl files are logs, only ever appended to. The page polls GET
/review for all of it, and posts /review to add, edit or delete a comment, or to resolve, reopen or read
revisits, shown in place.
"""
# docdiff, the running docdiff.py, is set by its load_plugins().
import collections, html, json, pathlib, re, secrets, subprocess, threading, time

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
.rvb-new,.rvb-new:hover{background:var(--insb);color:var(--bg);font-weight:700} #rvt.rvt-new{font-weight:700;border-color:var(--insb)}
.rvb-done{width:7px;height:7px;padding:0;border:0;border-radius:50%;background:var(--muted);opacity:.35;vertical-align:middle} .rvb-done::before{content:none} .rvb-done:hover{background:var(--muted);opacity:.8}
#cp{z-index:5} #rvp[hidden],#rvc[hidden]{display:none}
@media (min-width:840px){.rv-open main{margin-right:420px}}
#rvp{position:fixed;right:0;bottom:0;width:min(420px,100vw);box-sizing:border-box;overflow:auto;background:var(--bg);border-left:1px solid var(--line);padding:8px 14px 24px;z-index:2;font-size:14px}
.rvh{display:flex;gap:8px;align-items:center;margin-bottom:6px} .rvh .n{flex:1} .rvh button,.rvx{font-size:12px;padding:1px 8px}
.rvi{padding:8px 4px;border-top:1px solid var(--line);cursor:pointer} .rvi:hover{background:var(--code)}
.rvm{font-size:12px;color:var(--muted)} .rvt{white-space:pre-wrap;margin:2px 0} .rvx{margin:4px 6px 0 0}
#rvp blockquote,#rvc blockquote{margin:4px 0;padding:0 8px;border-left:3px solid var(--line);color:var(--muted);max-height:4.8em;overflow:hidden}
.rva{border-left:3px solid var(--insb);padding-left:8px;margin-top:6px} .rva>:first-child{margin-top:0} .rva>:last-child{margin-bottom:0}
#rvp .rva blockquote,#rvc .rva blockquote{max-height:none;margin:6px 0 2px} .rvi-new{box-shadow:inset 3px 0 var(--insb)} .rvi-new>.rvm{font-weight:600}
.rvf{margin:0 0 6px} .rvf button{font-size:12px;padding:1px 8px} .rvg>summary{cursor:pointer;color:var(--muted);font-size:13px;padding:6px 0;border-top:1px solid var(--line)}
#rvc{position:absolute;z-index:0;width:min(560px,90vw);overflow:auto;box-sizing:border-box;background:var(--bg);border:1px solid var(--line);border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.12);padding:8px 12px;font-size:14px}
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
// A view's text on one side, spaces collapsed, with the text node and offset of each character, and
// where each heading (as heading() names it) starts in it.
function textOf(v,side){const skip=side==='L'?'ins,.now,summary,.rvb':'del,.was,.rvb',ns=[],os=[],first=new Map();let s='',sp=true,n;
const w=document.createTreeWalker(v,NodeFilter.SHOW_TEXT,{acceptNode:t=>t.parentElement.closest(skip)?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
while(n=w.nextNode()){first.set(n,s.length);for(let i=0;i<n.data.length;i++){const ch=/\\s/.test(n.data[i])?' ':n.data[i];if(ch===' '&&sp)continue;sp=ch===' ';s+=ch;ns.push(n);os.push(i);}}
const hs=[...v.querySelectorAll('h1,h2,h3,h4')].map(h=>{const t=document.createTreeWalker(h,NodeFilter.SHOW_TEXT),k=h.cloneNode(true);let m;
while((m=t.nextNode())&&!first.has(m));k.querySelectorAll('del').forEach(d=>d.remove());return{at:m?first.get(m):-1,name:k.textContent.trim()};}).filter(h=>h.at>=0);
return{s,ns,os,hs};}
// Where q ends in that text, or the word it ends in; among repeats, one in its section sec first, then one
// of whole words, then one in an element that matches chg, then the one the text before it, b, best leads up to.
function findEnd(x,q,b,sec,chg){q=norm(q);b=norm(b);if(!q)return null;let best=-1,most=-1;const w=ch=>/\\w/.test(ch||'');
const secs=x.hs.flatMap((h,i)=>h.name===sec?[[h.at,(x.hs[i+1]||{at:Infinity}).at]]:[]);
for(let at=x.s.indexOf(q);at>=0;at=x.s.indexOf(q,at+1)){const p=x.s.slice(Math.max(0,at-b.length-1),at).trimEnd(),e=at+q.length;let k=0;
while(k<b.length&&k<p.length&&p[p.length-1-k]===b[b.length-1-k])k++;
k+=(secs.some(([l,h])=>at>=l&&at<h)?2e6:0)+(w(q[0])&&w(x.s[at-1])||w(q[q.length-1])&&w(x.s[e])?0:1e6)+(chg&&x.ns[at].parentElement.closest(chg)?5e5:0);
if(k>most){most=k;best=at;}}
if(best<0)return null;let e=best+q.length;while(w(x.s[e-1])&&w(x.s[e])&&x.ns[e]===x.ns[e-1])e++;return[x.ns[e-1],x.os[e-1]+1];}
// On a change in view v: after its last inline part, or at the end of its block's new text (in its
// folded summary when it only removes).
function onChange(v,k){const els=v.querySelectorAll('.chg[data-k="'+k+'"]'),e=els[els.length-1];if(!e)return null;
if(!e.classList.contains('blk'))return b=>e.after(b);let t=e.querySelector('.now');if(!t){const m=e.querySelector('summary');return b=>m.append(b);}
while(t.lastElementChild&&/^(P|UL|OL|LI|DL|DT|DD|DIV|BLOCKQUOTE|H[1-6])$/.test(t.lastElementChild.tagName))t=t.lastElementChild;return b=>t.append(b);}
// Where a revisit is: by its change's own text and the text before it, from a view that has the change;
// failing that, by its quote, "−removed +added": the added text, best where the diff inserts it, or else
// the removed text, best where it deletes it.
function asked(r){const q=r.quote||'',p=q.match(/^(?:−(.*?) )?\\+(.*)$/s),d=!p&&q.match(/^−(.*)$/s),sel='.chg[data-k="'+r.key+'"]';
const s=r.key&&[...document.querySelectorAll('section')].find(s=>s.dataset.file===r.file),v=s&&[...s.querySelectorAll('.v')].find(v=>v.querySelector(sel));
const els=v?[...v.querySelectorAll(sel)]:[];
return els.length?anchor(span(els),els[0].classList.contains('blk')):{quote:p?p[2]:d?d[1]:q,side:r.view==='old'||d?'L':'R',before:'',after:'',chg:p?'ins,.now':d?'del,.old':''};}
// Revisits of one change, or of one quote, are one thread under one mark, so asking again adds to it and
// reopens it. It is resolved while all of them are, pending while the last has no answer, and new while
// an answer is unread.
function threads(rs){const ts=[];
for(const r of rs){const t=ts.find(t=>t.file===r.file&&t.members.some(m=>r.key&&m.key===r.key||m.quote===r.quote));t?t.members.push(r):ts.push({file:r.file,members:[r]});}
return ts.map(({file,members:ms})=>{const l=ms[ms.length-1],k=ms.findLast(m=>m.key);
return{kind:'r',file,members:ms,time:ms[0].time,view:l.view,section:l.section,sent:l.quote,key:k?k.key:null,...asked(k||l),
done:ms.every(m=>m.resolved),wait:!l.answers.length,unread:ms.some(m=>m.answers.length>(m.seen||0))};});}
function badge(it){const r=it.kind==='r',st=!r?'':it.done?' rvb-done':it.wait?' rvb-wait':it.unread?' rvb-new':'';
const b=mk('span',{className:'rvb rvb-'+it.kind+st,title:!r?it.body:it.members.map(m=>m.prompt).join('\\n')+
(it.done?'\\n\\n(resolved: click to reopen)':it.wait?'\\n\\n(pending: no answer yet)':it.unread?'\\n\\n(new answer: click to read)':'')});
b.onmousedown=e=>e.preventDefault();b.onclick=e=>{e.preventDefault();e.stopPropagation();it.kind==='c'?openCm(it):showCard(it,b);};return b;}
// Marks every comment and revisit; it.at holds its mark per view.
function paint(){document.querySelectorAll('.rvb').forEach(b=>{const p=b.parentNode;b.remove();p.normalize();});
const texts=new Map(),text=(v,side)=>{let m=texts.get(v);if(!m)texts.set(v,m={});return m[side]||(m[side]=textOf(v,side));},put=[];
items=[...data.comments.map(x=>({...x,kind:'c'})),...threads(data.revisits)];
for(const it of items){it.at={};for(const s of document.querySelectorAll('section'))if(s.dataset.file===it.file)for(const v of s.querySelectorAll('.v')){
const b=badge(it),on=it.key&&onChange(v,it.key),end=!on&&findEnd(text(v,it.side),it.quote,it.before,it.section,it.chg);
if(on)on(b);else if(end)put.push([...end,b]);else continue;it.at[v.dataset.view]=b;}}
// From the end of the doc back, so splitting a text node leaves the places before it where they were;
// in a removed block's folded text, on its summary.
put.sort((x,y)=>x[0]===y[0]?y[1]-x[1]:x[0].compareDocumentPosition(y[0])&Node.DOCUMENT_POSITION_FOLLOWING?1:-1);
for(const[t,o,b]of put){const f=t.parentElement.closest('.was');if(f)f.querySelector('summary').append(b);else{t.splitText(o);t.after(b);}}
const ts=items.filter(i=>i.kind==='r'&&!i.done),np=ts.filter(t=>t.wait).length,nn=ts.filter(t=>t.unread).length;
rvt.textContent='💬 '+data.comments.length+(ts.length?' · ✎ '+ts.length:'')+(np?' · '+np+' pending':'')+(nn?' · '+nn+' new':'');
rvt.classList.toggle('rvt-new',nn>0);if(!lp.hidden)list();}
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
const act=(t,title,f)=>{const b=mk('button',{className:'rvx',textContent:t,title});b.onclick=e=>{e.stopPropagation();f();};return b;};
// A thread's requests, each with its answers; under each drafted comment (a blockquote), Make comment,
// and under an answer with several, one comment of all of them.
const asks=it=>it.members.flatMap(m=>[mk('div',{className:'rvt',textContent:m.prompt}),...answers(it,m)]);
function answers(it,m){if(!m.answers.length)return[mk('div',{className:'rvm',textContent:'pending: Claude has not answered yet'})];
return m.answers.map(a=>{const d=mk('div',{className:'rva'}),mc='A new comment of this draft, open to edit; resolves the revisit';d.innerHTML=a.html;
const qs=d.querySelectorAll(':scope>blockquote');if(qs.length===a.drafts.length)qs.forEach((q,i)=>q.after(act('💬 Make comment',mc,()=>makeComment(it,a.drafts[i]))));
if(a.drafts.length>1)d.append(act('💬 One comment of all '+a.drafts.length,mc,()=>makeComment(it,a.drafts.join('\\n\\n'))));return d;});}
const resolveButton=it=>act(it.done?'↺ Reopen':'✓ Resolve',it.done?'Back to the open revisits':'Done with it: its mark folds to a dot',()=>{card.hidden=true;setThread(it,{resolve:!it.done});});
async function setThread(it,req){try{take(await post('review',{revisits:it.members.map(m=>m.time),...req}));}catch(e){pos.textContent='not saved: '+e.message+' (is the docdiff server running?)';}}
// The draft becomes a comment on the thread's change or quote, opened to edit, and the thread resolves.
async function makeComment(it,body){try{const st=await post('review',{file:it.file,view:it.view,key:it.key,quote:it.quote,side:it.side,before:it.before,after:it.after,section:it.section,body});
const cm=st.comments[st.comments.length-1];await setThread(it,{resolve:true});card.hidden=true;openCm({...cm,kind:'c'});}
catch(e){pos.textContent='comment not made: '+e.message+' (is the docdiff server running?)';}}
// A thread's card, under its mark or, with more room there, over it; opening it reads its answers.
function showCard(it,b){card.replaceChildren(mk('div',{className:'rvm',textContent:'✎ revisit'+(it.section?' · '+it.section:'')+' · '+it.time.replace('T',' ')+(it.done?' · resolved':'')}),
mk('blockquote',{textContent:it.sent}),...asks(it),resolveButton(it));card.hidden=false;
const r=b.getBoundingClientRect(),below=innerHeight-r.bottom-12,above=r.top-document.querySelector('header').getBoundingClientRect().bottom-12,down=below>=300||below>=above;
card.style.maxHeight=Math.max(120,down?below:above)+'px';card.style.left=Math.max(8,Math.min(r.left,innerWidth-card.offsetWidth-8))+scrollX+'px';
card.style.top=(down?r.bottom+4:r.top-4-card.offsetHeight)+scrollY+'px';
if(it.unread)setThread(it,{seen:true});}
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
function row(it,files){const d=mk('div',{className:'rvi'+(it.unread&&!it.done?' rvi-new':'')}),m=mk('div',{className:'rvm'}),w=where(it);
m.append((it.kind==='c'?'💬 ':'✎ ')+[files>1&&it.file,it.section].filter(Boolean).join(' · ')+(Object.keys(it.at).length?'':' · not in the page now'));
if(w)m.append(' · ',mk('a',{href:it.github.href,target:'_blank',rel:'noopener',textContent:w.split('/').pop()+' ↗'}));
d.append(m);if(it.quote)d.append(mk('blockquote',{textContent:it.kind==='r'?it.sent:it.quote}));
if(it.kind==='r')d.append(...asks(it),resolveButton(it));else{const e=mk('button',{className:'rvx',textContent:'edit'}),x=mk('button',{className:'rvx',textContent:'delete'});
e.onclick=()=>openCm(it);x.onclick=()=>x.textContent==='delete'?x.textContent='delete?':save({id:it.id,delete:true});d.append(mk('div',{className:'rvt',textContent:it.body}),e,x);}
d.onclick=e=>{if(e.target.closest('a,button'))return;jump(it);if(it.unread)setThread(it,{seen:true});};return d;}
// Open (comments and unresolved revisits), then the resolved revisits folded; or either alone.
let filter='all',folded=true;
function list(){const its=order(),nc=data.comments.length,files=document.querySelectorAll('section').length,done=its.filter(i=>i.done);
const f=mk('div',{className:'seg rvf'}),g=mk('details',{className:'rvg',open:!folded});g.ontoggle=()=>{folded=!g.open;keep();};
for(const[v,l]of[['open','Open'],['resolved','Resolved'],['all','All']]){const b=mk('button',{textContent:l});b.setAttribute('aria-pressed',v===filter);
b.onclick=()=>{filter=v;keep();list();};f.append(b);}
g.append(mk('summary',{textContent:'Resolved ('+done.length+')'}),...done.map(it=>row(it,files)));
const h=mk('div',{className:'rvh'}),cm=mk('button',{textContent:'Copy as Markdown',title:'The comments, each with its file:line and quote, to paste into GitHub'}),x=mk('button',{textContent:'×',title:'Close'});
cm.onclick=async()=>{cm.textContent=!nc?'no comments':await copy(markdown(its))?'copied '+nc:'copying failed';setTimeout(()=>cm.textContent='Copy as Markdown',1500);};
x.onclick=()=>toggle(false);h.append(mk('b',{textContent:'Review'}),mk('span',{className:'n',textContent:nc+' comment(s) · '+(its.length-nc-done.length)+' open · '+done.length+' resolved'}),cm,x);
lp.replaceChildren(h,f,...(filter==='resolved'?done:its.filter(i=>!i.done)).map(it=>row(it,files)),...(filter==='all'&&done.length?[g]:[]));}
// Paste-ready: per comment, its file:line (linked to it on GitHub) or else its file and section, the
// quoted text and the comment.
function markdown(its){return its.filter(it=>it.kind==='c').map(it=>{const w=where(it),loc=w||it.file+(it.section?' · '+it.section:'');
const q=it.quote.length>300?it.quote.slice(0,300).replace(/\\s+\\S*$/,'')+'…':it.quote;
return '**'+(w&&it.github.href?'['+loc+']('+it.github.href+')':loc)+'**\\n\\n'+(q?'> '+q+'\\n\\n':'')+it.body;}).join('\\n\\n---\\n\\n');}
function toggle(on){lp.hidden=!on;body.classList.toggle('rv-open',on);if(on){lp.style.top=document.querySelector('header').getBoundingClientRect().bottom+'px';list();}keep();}
// The panel, its filter and its fold outlast reloads.
function keep(){try{sessionStorage.setItem('docdiff-review',JSON.stringify({on:!lp.hidden,filter,folded}));}catch(e){}}
rvt.onclick=()=>toggle(lp.hidden);addEventListener('resize',()=>{if(!lp.hidden)lp.style.top=document.querySelector('header').getBoundingClientRect().bottom+'px';});
pc.onmousedown=e=>e.preventDefault();pc.onclick=()=>{const a=anchorOf();hidePop();openCm(a);};
// The doc's own click and selection handling leaves the panel and the card alone.
for(const e of [lp,card])for(const t of ['click','mouseup'])e.addEventListener(t,ev=>ev.stopPropagation());
document.addEventListener('click',e=>{if(!e.target.closest('#rvc'))card.hidden=true;});
document.addEventListener('keydown',e=>{if(e.target.closest('input,textarea')||e.metaKey||e.ctrlKey||e.altKey)return;
if(e.key==='Escape')card.hidden=true;if(e.key==='c'){e.preventDefault();openCm(anchorOf());}});
try{const st=JSON.parse(sessionStorage.getItem('docdiff-review'))||{};filter=st.filter||filter;folded=st.folded!==false;if(st.on)toggle(true);}catch(e){}
load();setInterval(load,2000);})();
'''


def get(run, path, query):
    """The comments, and the revisits with their answers, for the page."""
    if path != '/review':
        return None
    return json.dumps(state(run), ensure_ascii=False).encode(), 'application/json'


def post(out, path, req):
    """Adds a comment, or edits or deletes the one req['id'] names; or resolves (resolve true), reopens
    (false) or reads (seen) the revisits req['revisits'] names by time. Returns what get() does."""
    if path != '/review':
        return None
    run = json.loads((out / '.run.json').read_text())
    with lock:
        doc = load(out)
        cs = doc['comments']
        was = next((c for c in cs if req.get('id') and c['id'] == req['id']), None)
        if req.get('revisits'):
            read = collections.Counter(a.get('time') for a in records(out / 'answers.jsonl'))
            for t in map(str, req['revisits']):
                st = doc['revisits'].setdefault(t, {})
                if req.get('resolve'):
                    st['resolved'] = now()
                elif 'resolve' in req:
                    st.pop('resolved', None)
                if req.get('seen'):
                    st['seen'] = read[t]
        elif was and req.get('delete'):
            cs.remove(was)
        elif was:
            was['body'], was['edited'] = str(req['body']), now()
        elif not req.get('id'):
            cs.append({'id': secrets.token_hex(4), 'time': now(), **{k: req.get(k) for k in FIELDS}})
            locate(run, cs[-1:])
        save(out, doc)
    return state(run)


def state(run):
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
        answers.setdefault(a.get('time'), []).append({'html': markdown(text), 'drafts': drafts(text)})
    return {'comments': doc['comments'],
            'revisits': [{**{k: r.get(k) for k in ('time', 'file', 'section', 'quote', 'view', 'prompt', 'key')},
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
    return {'comments': doc.get('comments', []), 'revisits': doc.get('revisits', {})}


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
