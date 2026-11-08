
// ---- Gala and Checklist tabs: the gala's files by session, sorted from the inbox; per-event progress; ticks
let GALA=null,galaSig='',GSESS=+(store('qa-gsess')||0);
function galaHref(rel,dl){const u=url('/api/gala/file');u.searchParams.set('path',rel);if(dl)u.searchParams.set('dl','1');return u.toString()}
async function galaPost(path,body){return api(path,Object.assign({by:me()||'someone'},body||{}))}
const ST_TXT={waiting:'waiting',ready:'ready',asked:'with Claude',audited:'audited',changed:'new files'};
const FOLDER_TXT={meet:['Meet Manager files','Entries, merge results and final exports (.zip / .hy3)'],gen7:['Gen7 race PDFs','One PDF per race'],
 scans:['Scans','Lane sheet packs (one per event), DQ slips, ETD programme'],ocr:['OCR (Claude)','JSON per lane sheet, DQ slip and ETD page; dq-codes.json'],
 reports:['Reports (Claude)','Event and session audits, merge results .hy3, final audit']};
function curSession(){if(!GALA||!GALA.sessions.length)return null;
 return GALA.sessions.find(s=>s.n===GSESS)||GALA.sessions[GALA.sessions.length-1]}
async function loadGala(){
 let j;try{j=await api('/api/gala/overview')}catch(e){$('#g-root').textContent='Could not load the data folder: '+e.message;return}
 const sig=JSON.stringify(j);if(sig===galaSig)return;galaSig=sig;GALA=j;
 if(!j.root){$('#g-root').textContent='This portal was started without a data folder (--gala-data).';return}
 const cs=curSession();if(cs)GSESS=cs.n;
 renderGalaHead();renderPills('#g-sess');renderPills('#cl-sess');renderSession();renderActivity();renderChecklist()}
function renderGalaHead(){
 $('#g-root').innerHTML='Data folder: <code>'+esc(GALA.root)+'</code><br>Inbox: <code>'+esc(GALA.inbox)+'</code>'
  +(GALA.waiting.length?` · <span class="gwarn">${GALA.waiting.length} file${GALA.waiting.length>1?'s':''} arriving</span>`:'');
 $('#g-auto').checked=!!GALA.auto;
 const miss=Object.entries(GALA.tools||{}).filter(([k,v])=>!v).map(([k])=>k);
 $('#g-tools').innerHTML=miss.length?'<span class="gwarn">Missing: '+esc(miss.join(', '))+' (install poppler-utils)</span>':'poppler ✓'}
function renderPills(sel){
 const box=$(sel);if(!GALA.sessions.length){box.innerHTML='<span class="meta">No sessions yet: drop the first session\'s entries export or any gala file above.</span> <button type="button" data-newsess>+ Session</button>';return}
 box.innerHTML=GALA.sessions.map(s=>{const a=s.events.filter(e=>e.status==='audited').length;
  return `<button type="button" data-sess="${s.n}" class="${s.n===GSESS?'on':''}">Session ${s.n} <span class="meta">${a}/${s.events.length}</span></button>`}).join('')
  +'<button type="button" data-newsess title="Add an empty session">+</button>'}
function fileList(s,key,files,limit){
 if(!files.length)return '<div class="meta" style="margin-top:8px">Empty.</div>';
 return '<ul class="gfiles">'+files.filter(f=>!f.dir).slice(0,limit||500).map(f=>{const rel='sessions/S'+s.n+'/'+key+'/'+f.name;
  return `<li><a href="${esc(galaHref(rel))}" target="_blank" rel="noopener" title="${esc(f.name)}">${esc(f.name)}</a><span class="meta">${size(f.size)} · ${agoShort(f.mtime)}</span><a class="meta" href="${esc(galaHref(rel,1))}" title="Download">⤓</a></li>`}).join('')+'</ul>'}
function renderSession(){
 const s=curSession(),box=$('#g-session');if(!s){box.innerHTML='';return}
 const c=k=>s.events.filter(e=>e.status===k).length;
 const stat=(n,t)=>`<div class="gstat"><b>${n}</b><span class="meta">${t}</span></div>`;
 const scans=s.scans.length?s.scans.map(x=>{const pct=x.pages?Math.round(100*x.done/x.pages):0;
  const what=x.kind==='dq'?'DQ slips':x.kind==='etd'?'ETD':'lane sheets'+(x.event!=null?' · event '+x.event:'');
  return `<div class="gscan"><span class="nm" title="${esc(x.name)}">${esc(x.name)} <span class="meta">${esc(what)}</span></span>
   <span class="gbar" title="${pct}%"><i style="width:${pct}%"></i></span>
   <span class="ct">${x.pages==null?'? pages':`${x.done} / ${x.pages} pages`}${x.review?` · <span class="gwarn">${x.review} to check</span>`:''}${x.bad?` · <span class="gwarn">${x.bad} unreadable</span>`:''}</span>
   ${x.missingCount&&x.done?`<span class="miss meta">Not done yet: pages ${esc(x.missing.join(', '))}${x.missingCount>x.missing.length?' …':''}</span>`:''}</div>`}).join('')
  :'<div class="meta">No scans in this session yet.</div>';
 const ent=s.meet.entries.length?`${s.expected.events} events, ${s.expected.heats} heats expected (from ${esc(s.meet.entries[s.meet.entries.length-1])})`:'<span class="gwarn">No entries export yet</span>: events are only known as their files arrive.';
 box.innerHTML=`<div class="card"><div class="gh-top"><div><h3 style="margin:0">Session ${s.n}</h3><div class="meta">${ent} · ${s.races} Gen7 race${s.races===1?'':'s'}</div></div>
   <a href="#checklist" data-goto="checklist">Checklist →</a></div>
  <div class="gstats">${stat(c('audited'),'audited')}${stat(c('asked'),'with Claude')}${stat(c('ready'),'ready')}${stat(c('waiting'),'waiting')}${c('changed')?stat(c('changed'),'new files'):''}</div>
  <h4 style="margin:10px 0 2px">OCR progress</h4>${scans}
  <div class="gask"><span class="meta" style="align-self:center">Ask Claude:</span>
   <button type="button" data-ask="S${s.n}:check">Check the files</button>
   <button type="button" data-ask="S${s.n}:merge"${s.meet.results.length&&s.meet.entries.length?'':' disabled title="Needs the entries and merge results exports"'}>Merge results</button>
   <button type="button" data-ask="S${s.n}:final"${s.meet.final.length&&s.merged.length?'':' disabled title="Needs our merge results and Meet Manager\'s final export"'}>Final audit</button>
   <span class="meta" data-askmsg></span></div></div>
  <div class="gfolders">${Object.keys(FOLDER_TXT).map(k=>{const f=s.folders[k]||[];const mine=k==='ocr'||k==='reports';
   return `<div class="card gfolder" data-gfolder="S${s.n}/${k}"><h3><span>${esc(FOLDER_TXT[k][0])} <span class="meta">(${k==='ocr'?s.ocrCount:f.filter(x=>!x.dir).length})</span></span><code>sessions/S${s.n}/${k}</code></h3>
    <div class="meta">${esc(FOLDER_TXT[k][1])}</div>${fileList(s,k,f)}${k==='ocr'&&s.ocrCount>f.length?`<div class="meta">Newest ${f.length} of ${s.ocrCount}.</div>`:''}
    ${mine?'':`<div class="drop gdrop">Drop files here, or <label class="lnk">choose files<input type="file" multiple hidden data-gup="S${s.n}/${k}"></label><div class="meta" data-gmsg></div></div>`}</div>`}).join('')}</div>`}
function renderActivity(){
 const un=GALA.unsorted.filter(f=>!f.dir);
 const opts=GALA.sessions.map(s=>`<option value="${s.n}">Session ${s.n}</option>`).join('');
 const unsorted=un.length?`<h3 style="margin:0 0 4px" class="gwarn">Not sorted (${un.length})</h3><div class="meta">The portal couldn't tell what these are. Choose where each one goes.</div><ul class="gfiles">`
  +un.map(f=>`<li><a href="${esc(galaHref('unsorted/'+f.name))}" target="_blank" rel="noopener">${esc(f.name)}</a>
   <select data-mvs>${opts||'<option value="1">Session 1</option>'}</select><select data-mvf>${['scans','gen7','meet'].map(k=>`<option value="${k}">${k}</option>`).join('')}</select>
   <button type="button" data-move="${esc('unsorted/'+f.name)}">Move</button></li>`).join('')+'</ul>':'';
 const log=GALA.log.length?'<ul class="glog">'+GALA.log.slice(0,25).map(l=>`<li><span class="meta">${esc(agoShort(l.ts))}</span><span>${esc(l.text)}</span></li>`).join('')+'</ul>':'<div class="meta">Nothing yet.</div>';
 $('#g-activity').innerHTML=unsorted+'<h3 style="margin:'+(un.length?'14px':'0')+' 0 4px">Activity</h3>'+log}
function tickBox(key,label){const t=GALA.ticks[key];return `<input type="checkbox" data-tick="${esc(key)}" ${t?'checked':''} aria-label="Tick off: ${esc(label)}" title="${t?esc('Ticked by '+t.by+' · '+agoShort(t.at)):'Tick off when checked'}">`}
function clRow(s,key,title,why,found,folder,claude){
 const t=GALA.ticks[key];
 const f=found&&found.length?'<span class="ok">✓</span> '+found.join(', '):`<span class="no">${claude?'Not made yet':'Not in yet'}</span>`;
 return `<div class="cl-row${t?' done':''}"${folder?` data-cldrop="${esc(folder)}"`:''}>${tickBox(key,title)}
  <div><div class="cl-t">${esc(title)}</div><div class="cl-why">${why}</div><div class="cl-f">${f}</div>
  ${t?`<div class="cl-by">Ticked by ${esc(t.by)} · ${esc(agoShort(t.at))}</div>`:''}<div class="meta" data-gmsg></div></div>
  <div class="cl-act">${folder?`<label class="lnk">add file<input type="file" multiple hidden data-gup="${esc(folder)}"></label>`:''}</div></div>`}
function evRange(evs){const out=[];for(let i=0;i<evs.length;i++){let j=i;while(j+1<evs.length&&evs[j+1]===evs[j]+1)j++;out.push(j>i?evs[i]+'–'+evs[j]:''+evs[i]);i=j}return out.join(', ')}
function link(s,sub,name){return `<a href="${esc(galaHref('sessions/S'+s.n+'/'+sub+'/'+name))}" target="_blank" rel="noopener">${esc(name)}</a>`}
function renderChecklist(){
 const s=curSession(),box=$('#cl-body');
 if(!s){box.innerHTML='<div class="card meta">No sessions yet. Drop the session\'s Meet Manager entries export on the Gala tab (or in the inbox folder) to start.</div>';return}
 const qfKey=(e,f)=>'S'+s.n+':e'+e.event+':qf:'+f.heat+'.'+f.lane;
 const keys=['entries','lanesprint','mmresults',...s.events.map(e=>'e'+e.event),'dq','etd','codes','merged','mmfinal','final'].map(k=>'S'+s.n+':'+k)
  .concat(s.events.flatMap(e=>(e.fixes||[]).map(f=>qfKey(e,f))));
 const ticked=keys.filter(k=>GALA.ticks[k]).length,pct=Math.round(100*ticked/keys.length);
 const dq=s.scans.filter(x=>x.kind==='dq'),etd=s.scans.filter(x=>x.kind==='etd');
 const ev=s.events.length?`<div class="tblwrap"><table class="cl-ev"><tr><th></th><th>Event</th><th class="c">Printed</th><th>Lane sheets</th><th class="c">Gen7 heats</th><th class="c">OCR lanes</th><th>Audit</th><th>Merge</th><th class="c">Quick fixes</th><th>Status</th><th></th></tr>`
  +s.events.map(e=>{const key='S'+s.n+':e'+e.event,t=GALA.ticks[key];
   const g7=e.heats?`${e.gen7.length}/${e.heats}`:(e.gen7.length||'–');
   const fx=e.fixes||null,fxDone=fx?fx.filter(f=>GALA.ticks[qfKey(e,f)]).length:0;
   const fxCell=fx==null?'<span class="no">–</span>':fx.length?`<span class="${fxDone===fx.length?'ok':'gwarn'}">${fxDone}/${fx.length}</span>`:'<span class="ok" title="No quick fixes">none ✓</span>';
   const regen=`<div><button type="button" class="sm" data-ask="S${s.n}:remerge:${e.event}" ${e.canMerge&&e.ocrLanes?'':'disabled'} title="${esc(e.canMerge?(e.ocrLanes?'Write the merge results again from what is already transcribed, with every DQ slip read so far (no scans are read again)':'The lane sheets aren\'t transcribed yet'):'Needs the entries export and the empty merge results export')}">${e.merge?'Regenerate':'Generate'}</button>${e.dqSlips?`<span class="sm"> ${e.dqSlips} DQ</span>`:''}</div>`;
   const mergeCell=(e.merge?link(s,'reports',e.merge):e.canMerge?'<span class="no">–</span>':`<span class="no" title="${esc(!s.meet.entries.length?'No entries export':'No empty merge results export')}">waiting</span>`)+regen;
   const fxRows=fx&&fx.length?`<tr class="qfrow"><td></td><td colspan="10"><details${fxDone<fx.length?' open':''}><summary>Quick fixes for event ${e.event} (${fxDone} of ${fx.length} done)${e.fixesFile?` · <a href="${esc(galaHref('sessions/S'+s.n+'/reports/'+e.fixesFile))}" target="_blank" rel="noopener">printable list</a>`:''}</summary>
     <ul class="qf">${fx.map(f=>{const k=qfKey(e,f),t=GALA.ticks[k];
      return `<li class="${t?'done':''}">${tickBox(k,'Heat '+f.heat+' lane '+f.lane)}<div><b>Heat ${esc(f.heat)} · Lane ${esc(f.lane)}</b> <span class="meta">in the merge file: ${esc(f.inFile||'–')}</span>
       <ul>${f.checks.map(c=>`<li>${esc(c)}</li>`).join('')}</ul>
       ${f.scans.length?`<div class="sm">${f.scans.map(sc=>`<a href="${esc(galaHref('sessions/S'+s.n+'/scans/'+sc.scan))}#page=${esc(sc.page)}" target="_blank" rel="noopener">${esc(sc.label)}, page ${esc(sc.page)}</a>`).join(' · ')}</div>`:''}
       ${t?`<div class="sm">fixed · ticked by ${esc(t.by)} · ${esc(agoShort(t.at))}</div>`:''}</div></li>`}).join('')}</ul></details></td></tr>`:'';
   return `<tr class="${t?'done':''}" data-cldrop="S${s.n}/scans">
    <td>${tickBox(key,'Event '+e.event)}</td>
    <td><span class="evname">Event ${e.event}</span>${e.desc?`<div class="sm">${esc(e.desc)}</div>`:''}${!e.expected&&s.meet.entries.length?'<div class="sm gwarn">not in the entries</div>':''}${t?`<div class="sm">ticked by ${esc(t.by)}</div>`:''}</td>
    <td class="c">${e.printed.length?'<span class="ok" title="'+esc(e.printed.map(x=>x.split('/').pop()).join(', '))+'">✓</span>':'<span class="no" title="No printed lane sheets for this event">–</span>'}</td>
    <td>${e.packs.length?e.packs.map(p=>link(s,'scans',p)).join('<br>'):'<span class="no">–</span>'}</td>
    <td class="c">${g7}${e.gen7Missing.length&&e.gen7.length?`<div class="sm">missing ${esc(e.gen7Missing.join(', '))}</div>`:''}</td>
    <td class="c">${e.ocrLanes?e.ocrLanes+(e.lanes?'/'+e.lanes:''):'–'}</td>
    <td>${e.report?link(s,'reports',e.report):'–'}</td>
    <td>${mergeCell}</td><td class="c">${fxCell}</td>
    <td><span class="st ${e.status}">${esc(ST_TXT[e.status]||e.status)}</span></td>
    <td><button type="button" data-ask="${key}" ${e.packs.length?'':'disabled title="No lane sheet pack yet"'}>${e.asked?'Ask again':'Ask Claude'}</button></td></tr>${fxRows}`}).join('')+'</table></div>'
  :'<div class="meta" style="padding:8px 0">No events yet. They come from the entries export, or appear as lane sheet packs and Gen7 races arrive.</div>';
 const scanFound=list=>list.map(x=>link(s,'scans',x.name)+` <span class="meta">(${x.done}/${x.pages??'?'} pages read)</span>`);
 box.innerHTML=`<div class="card clhead"><div class="gh-top"><div><h2>Session ${s.n} checklist</h2>
   <div class="meta">What the session's audit needs, in order. Files are found automatically when they arrive; tick each item once you've checked it. Ticks are shared.</div></div>
   <div style="font-weight:600;white-space:nowrap">${ticked} of ${keys.length} ticked</div></div><div class="gbar"><i style="width:${pct}%"></i></div></div>
  <div class="card">
  <div class="cl-sec">Start of the session</div>
  ${clRow(s,'S'+s.n+':entries','Meet entries export','From Meet Manager, for this session: the events, heats and lanes to expect (<code>Meet Entries-…zip</code> or its <code>Hfile…hy3</code>).',
    s.meet.entries.map(n=>link(s,'meet',n)+(s.expected.events?` <span class="meta">(${s.expected.events} events, ${s.expected.heats} heats)</span>`:'')
     +(s.events.some(e=>e.expected&&!e.seeded)?' <span class="meta">(not seeded: a pre-gala export; the merge results seed each swim from the lane sheets)</span>':'')),'S'+s.n+'/meet')}
  ${clRow(s,'S'+s.n+':lanesprint','Printed lane sheets (Meet Manager)','The lane sheet PDF Meet Manager printed for the session (not the scan). Claude copies the names, ages, teams and seed times from it, which is much more accurate than reading them off the scans. Bring it in before the first event.',
    s.lanePrints.map(p=>`<a href="${esc(galaHref(p.path))}" target="_blank" rel="noopener">${esc(p.name)}</a>`+` <span class="meta">(${p.sheets} sheet${p.sheets===1?'':'s'}, event${p.events.length===1?'':'s'} ${esc(evRange(p.events))})</span>`),'S'+s.n+'/meet')}
  ${clRow(s,'S'+s.n+':mmresults','Empty merge results export (Meet Manager)','Export merge results from Meet Manager for this session before any results are in (name it with "Results"). Claude copies its header and layout into each event\'s merge results file.',s.meet.results.map(n=>link(s,'meet',n)),'S'+s.n+'/meet')}
  <div class="cl-sec">Each event, as it is swum</div>
  <div class="meta" style="margin-bottom:4px">An event is <b>ready</b> when its lane sheet pack and every heat's Gen7 race PDF are in${GALA.auto?'; Claude is then asked to transcribe and audit it':''}. Claude then writes the event's <b>merge results</b> (<code>.hy3</code>, to import into Meet Manager) and its <b>quick fixes</b>: the swims to check by hand, listed under the event to tick off as they're fixed. Drop a lane sheet pack on an event to add it.</div>
  ${ev}
  <div class="cl-sec">During and after the session</div>
  ${clRow(s,'S'+s.n+':dq','DQ slips','The referees\' DQ slips, scanned (name the file with "DQ"). Claude reads each batch as it arrives.',scanFound(dq),'S'+s.n+'/scans')}
  ${clRow(s,'S'+s.n+':etd','ETD programme','The programme with the Gen7 race numbers written by each heat (name the file with "ETD"). Needed for combined events and re-started heats.',scanFound(etd),'S'+s.n+'/scans')}
  ${clRow(s,'S'+s.n+':codes','DQ codes','Each slip\'s code, given by Claude from '+(s.meet.codes.length?'the meet\'s own list ('+link(s,'meet',s.meet.codes[s.meet.codes.length-1])+')':'the full FINA list in the plugin (<code>gala-audit/data/dqcodesFINA.txt</code>); drop a <code>.txt</code> with the meet\'s own list in the inbox to use that instead')+'. Codes it wasn\'t sure of need the referee\'s OK.',s.dqCodes?[link(s,'ocr','dq-codes.json')]:[],null,true)}
  ${clRow(s,'S'+s.n+':merged','Session merge results (all events)','Written by Claude once every event is audited: one file with the whole session, and the session report.',s.merged.map(n=>link(s,'reports',n)).concat(s.sessionReport?['session report: '+link(s,'reports',s.sessionReport)]:[]),null,true)}
  ${clRow(s,'S'+s.n+':mmfinal','Final merge results export','Exported from Meet Manager after the audited results were imported and finalised (name it with "final").',s.meet.final.map(n=>link(s,'meet',n)),'S'+s.n+'/meet')}
  ${clRow(s,'S'+s.n+':final','Final audit report','Written by Claude: every change made by hand in Meet Manager.',s.finalReports.map(n=>link(s,'reports',n)),null,true)}
  </div>`}
async function galaUpload(folder,files,card){
 const msg=card&&card.querySelector('[data-gmsg]');const say=t=>{if(msg)msg.textContent=t};
 for(const f of files){
  say('Uploading '+f.name+'…');
  const u=url('/api/gala/upload');u.searchParams.set('folder',folder);u.searchParams.set('filename',f.name);
  const r=await fetch(u,{method:'POST',body:f});const jj=await r.json().catch(()=>({}));
  if(!r.ok){say('Could not upload '+f.name+': '+(jj.error||r.status));return}}
 say(folder==='inbox'?`Uploaded ${files.length} file${files.length>1?'s':''}: sorting…`:`Uploaded ${files.length} file${files.length>1?'s':''}.`);
 galaSig='';loadGala();if(folder==='inbox')setTimeout(()=>{galaSig='';loadGala()},9000)}
function galaClicks(e){
 const sb=e.target.closest('[data-sess]');if(sb){GSESS=+sb.dataset.sess;store('qa-gsess',GSESS);galaSig='';loadGala();return}
 if(e.target.closest('[data-newsess]')){galaPost('/api/gala/session').then(()=>{GSESS=(GALA&&GALA.sessions.length?Math.max(...GALA.sessions.map(s=>s.n)):0)+1;store('qa-gsess',GSESS);galaSig='';loadGala()});return}
 const go=e.target.closest('[data-goto]');if(go){e.preventDefault();showView(go.dataset.goto);return}
 const ab=e.target.closest('[data-ask]');if(ab){ab.disabled=true;
  galaPost('/api/gala/ask',{key:ab.dataset.ask}).then(j=>{ab.textContent='Asked (#'+j.question+')';galaSig='';setTimeout(loadGala,1500)})
   .catch(err=>{ab.disabled=false;alert(err.message)});return}
 const mv=e.target.closest('[data-move]');if(mv){const li=mv.closest('li');
  galaPost('/api/gala/move',{path:mv.dataset.move,session:+li.querySelector('[data-mvs]').value,folder:li.querySelector('[data-mvf]').value})
   .then(()=>{galaSig='';loadGala()}).catch(err=>alert(err.message))}}
function galaChange(e){
 const up=e.target.closest('[data-gup]');if(up&&up.files.length){galaUpload(up.dataset.gup,[...up.files],up.closest('[data-gfolder],.cl-row,tr,.drop')||up.parentElement);return}
 const tk=e.target.closest('[data-tick]');if(tk){galaPost('/api/gala/tick',{key:tk.dataset.tick,checked:tk.checked}).then(()=>{galaSig='';loadGala()})
  .catch(err=>{tk.checked=!tk.checked;alert('Could not save: '+err.message)})}}
for(const sec of ['#v-gala','#v-checklist']){const el=$(sec);el.addEventListener('click',galaClicks);el.addEventListener('change',galaChange);
 el.addEventListener('dragover',e=>{const c=e.target.closest('[data-gfolder],[data-cldrop],#g-inbox');if(!c)return;e.preventDefault();c.classList.add('over')});
 el.addEventListener('dragleave',e=>{const c=e.target.closest('[data-gfolder],[data-cldrop],#g-inbox');if(c&&!c.contains(e.relatedTarget))c.classList.remove('over')});
 el.addEventListener('drop',e=>{const c=e.target.closest('[data-gfolder],[data-cldrop],#g-inbox');if(!c)return;e.preventDefault();c.classList.remove('over');
  const fs=[...(e.dataTransfer.files||[])];if(!fs.length)return;
  const folder=c.id==='g-inbox'?'inbox':(c.dataset.gfolder||c.dataset.cldrop);
  if(/\/(ocr|reports)$/.test(folder)){alert('That folder is written by Claude. Drop the file on the inbox instead.');return}
  galaUpload(folder,fs,c)})}
$('#g-inboxfile').addEventListener('change',e=>{if(e.target.files.length)galaUpload('inbox',[...e.target.files],$('#g-inbox'))});
$('#g-auto').addEventListener('change',e=>galaPost('/api/gala/auto',{auto:e.target.checked}).then(()=>{galaSig='';loadGala()}));
setInterval(()=>{if(!document.hidden&&(view==='gala'||view==='checklist'))loadGala()},4000);
