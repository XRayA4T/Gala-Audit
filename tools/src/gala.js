
// ---- Gala and Checklist tabs: the gala's files by session, sorted from the inbox; per-event progress; ticks
let GALA=null,galaSig='',GSESS=+(store('qa-gsess')||0);
function galaHref(rel,dl){const u=url('/api/gala/file');u.searchParams.set('path',rel);if(dl)u.searchParams.set('dl','1');return u.toString()}
async function galaPost(path,body){return api(path,Object.assign({by:me()||'someone'},body||{}))}
const ST_TXT={waiting:'waiting',ready:'ready',asked:'with Claude',transcribed:'transcribed',computing:'working…',audited:'audited',changed:'new files'};
const FOLDER_TXT={meet:['Meet Manager files','Entries, merge results and final exports (.zip / .hy3)'],gen7:['Gen7 race PDFs','One PDF per race'],
 scans:['Scans','Lane sheet packs (one per event), DQ slips, ETD programme'],ocr:['OCR (Claude)','JSON per lane sheet, DQ slip and ETD page; dq-codes.json'],
 reports:['Reports (Claude)','Event and session audits, merge results .hy3, final audit']};
function curSession(){if(!GALA||!GALA.sessions.length)return null;
 return GALA.sessions.find(s=>s.n===GSESS)||GALA.sessions[GALA.sessions.length-1]}
function galaEditing(){const a=document.activeElement;return !!(a&&a.closest&&a.closest('form.qffix'))||!!document.querySelector('form.qffix[data-dirty]')}
let CODES={},CODES_FOR=null;
async function loadCodes(n){if(CODES_FOR===n)return;CODES_FOR=n;
 try{const j=await api('/api/gala/codes?session='+n);CODES=j;let dl=$('#dqcodes');if(!dl){dl=document.createElement('datalist');dl.id='dqcodes';document.body.appendChild(dl)}
  dl.innerHTML=(j.codes||[]).map(c=>`<option value="${esc(c[0])}">${esc(c[0]+' – '+c[1]+(c[2]?' ('+c[2]+')':''))}</option>`).join('')}catch(e){CODES_FOR=null}}
async function loadGala(){
 if(galaEditing())return;
 let j;try{j=await api('/api/gala/overview')}catch(e){$('#g-root').textContent='Could not load the data folder: '+e.message;return}
 const sig=JSON.stringify(j);if(sig===galaSig)return;galaSig=sig;GALA=j;
 if(!j.root){$('#g-root').textContent='This portal was started without a data folder (--gala-data).';return}
 const cs=curSession();if(cs)GSESS=cs.n;
 renderGalaHead();renderRestart();renderPills('#g-sess');renderPills('#cl-sess');renderSession();renderActivity();renderChecklist()}
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
  <div class="gstats">${stat(c('audited'),'audited')}${stat(c('asked'),'with Claude')}${c('transcribed')?stat(c('transcribed'),'transcribed'):''}${c('computing')?stat(c('computing'),'working'):''}${stat(c('ready'),'ready')}${stat(c('waiting'),'waiting')}${c('changed')?stat(c('changed'),'new files'):''}</div>
  <h4 style="margin:10px 0 2px">OCR progress</h4>${scans}
  <div class="gask"><span class="meta" style="align-self:center" title="Done by the portal itself, from what Claude has read (no scans are read)">Run now:</span>
   <button type="button" data-ask="S${s.n}:check">Check the files</button>
   <button type="button" data-ask="S${s.n}:merge"${s.template&&s.entriesFile?'':' disabled title="Needs the entries export and the empty merge results export"'}>Merge results</button>
   <button type="button" data-ask="S${s.n}:final"${s.meet.final.length&&s.merged.length?'':' disabled title="Needs our merge results and Meet Manager\'s final export"'}>Final audit</button>
   <span class="meta" data-askmsg>${['merge','final','etd'].map(k=>{const j=(s.jobs||{})[k];if(!j)return '';
     const t={merge:'Merge results',final:'Final audit',etd:'ETD check'}[k];
     return j.state==='running'?`${t}: working…`:j.state==='failed'?`<span class="gwarn">${t} failed: ${esc(j.msg||'')}</span>`:`${t}: done ${esc(agoShort(j.at))}`}).filter(Boolean).join(' · ')}</span></div></div>
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
 const ig=(GALA.ignored||[]).filter(f=>!f.dir);
 const ignored=ig.length?`<details class="gign"><summary class="meta">Ignored (${ig.length}): Gen7 races for events not in the meet entries, e.g. a test race. Move one into a session if it does belong.</summary><ul class="gfiles">`
  +ig.map(f=>`<li><a href="${esc(galaHref('ignored/'+f.name))}" target="_blank" rel="noopener">${esc(f.name)}</a>
   <select data-mvs>${opts||'<option value="1">Session 1</option>'}</select><select data-mvf><option value="gen7">gen7</option><option value="meet">meet</option><option value="scans">scans</option></select>
   <button type="button" data-move="${esc('ignored/'+f.name)}">Move</button></li>`).join('')+'</ul></details>':'';
 const log=GALA.log.length?'<ul class="glog">'+GALA.log.slice(0,25).map(l=>`<li><span class="meta">${esc(agoShort(l.ts))}</span><span>${esc(l.text)}</span></li>`).join('')+'</ul>':'<div class="meta">Nothing yet.</div>';
 $('#g-activity').innerHTML=unsorted+ignored+'<h3 style="margin:'+(un.length||ig.length?'14px':'0')+' 0 4px">Activity</h3>'+log}
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
function renderChecklist(){setTimeout(swAll,0);
 const s=curSession(),box=$('#cl-body');if(s)loadCodes(s.n);
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
   const fxCell=fx==null?'<span class="no">–</span>':fx.length?(fxDone===fx.length?`<span class="ok" title="All ${fx.length} ticked">done ✓ <span class="sm">(${fx.length})</span></span>`:`<span class="gwarn">${fxDone}/${fx.length}</span>`):'<span class="ok" title="No quick fixes">none ✓</span>';
   const lastFix=Math.max(0,...Object.values(e.handFixes||{}).map(h=>h.at||0)),needRegen=fx&&fx.length&&fxDone===fx.length&&lastFix>(e.mergeAt||0);
   const regen=`<div><button type="button" class="primary regen" data-ask="S${s.n}:remerge:${e.event}" ${e.canMerge&&e.ocrLanes?'':'disabled'} title="${esc(e.canMerge?(e.ocrLanes?'The portal writes the merge results, audit and quick fixes again from what is already transcribed, with the fixes made here and every DQ slip read and coded so far (no scans are read, no Claude)':'The lane sheets aren\'t transcribed yet'):'Needs the entries export and the empty merge results export')}">${e.merge?'Regenerate':'Generate'}</button>${e.dqSlips?`<span class="sm"> ${e.dqSlips} DQ</span>`:''}</div>`;
   const mergeCell=(e.merge?link(s,'reports',e.merge):e.canMerge?'<span class="no">–</span>':`<span class="no" title="${esc(!s.entriesFile?'No entries export':'No empty merge results export (one for the whole gala)')}">waiting</span>`)+regen;
   const fxRows=fx&&fx.length?`<tr class="qfrow"><td></td><td colspan="10">${needRegen?`<div class="qfdone">All quick fixes for event ${e.event} are done, and fixes were made since its merge results were written. <button type="button" class="primary" data-ask="S${s.n}:remerge:${e.event}" ${e.canMerge?'':'disabled title="Needs the entries and the empty merge results export"'}>Regenerate the merge results</button> <span class="sm">with the fixes made here and every DQ slip read so far</span></div>`:''}<details data-qfd="S${s.n}:e${e.event}"${(QF_OPEN['S'+s.n+':e'+e.event]??(fxDone<fx.length))?' open':''}><summary>Quick fixes for event ${e.event} (${fxDone} of ${fx.length} ticked${fx.some(x=>x.cleared)?', '+fx.filter(x=>x.cleared).length+' settled by the re-check':''})${e.recheck?` · <span class="${e.recheck.state==='failed'?'gwarn':''}">${e.recheck.state==='running'?'re-checking…':e.recheck.state==='failed'?'re-check failed: '+esc(e.recheck.msg||''):'re-checked '+esc(agoShort(e.recheck.at))+(e.recheck.msg?' ('+esc(e.recheck.msg)+')':'')}</span>`:''} · <button type="button" class="sm" data-recheck="${s.n}|${e.event}">Check again</button>${Object.keys(e.handFixes||{}).length?` · ${Object.keys(e.handFixes).length} fixed in the portal`:''}${e.fixesFile?` · <a href="${esc(galaHref('sessions/S'+s.n+'/reports/'+e.fixesFile))}" target="_blank" rel="noopener">printable list</a>`:''}</summary>
     <ul class="qf">${fx.map(f=>{const k=qfKey(e,f),t=GALA.ticks[k],hf=(e.handFixes||{})[f.heat+'.'+f.lane]||{};
      return `<li class="${t?'done':''}${f.cleared?' cleared':''}">${tickBox(k,'Lane '+f.lane+' heat '+f.heat)}<div><b>Lane ${esc(f.lane)} · Heat ${esc(f.heat)}</b>${f.cleared?` <span class="st audited" title="${esc('No longer flagged since '+agoShort(f.clearedAt))}">settled by the re-check</span>`:''} <span class="meta">in the merge file: ${esc(f.inFile||'–')}</span>
       <ul>${f.checks.map(c=>`<li>${esc(c)}</li>`).join('')}</ul>
       ${f.scans.length?`<div class="sm">${f.scans.map(sc=>`<a href="${esc(galaHref('sessions/S'+s.n+'/scans/'+sc.scan))}#page=${esc(sc.page)}" target="_blank" rel="noopener">${esc(sc.label)}, page ${esc(sc.page)}</a>`).join(' · ')}</div>`:''}
       <form class="qffix" data-fix="${s.n}|${e.event}|${esc(f.heat)}|${esc(f.lane)}" autocomplete="off">
        ${(()=>{const w=qfTimes(f,hf),sw=qfSwimmer(s,e,f,hf);f._block=!!(w||sw);f._watch=!!w;return w+sw})()}
        <label>Result <select name="status"><option value="">as audited</option><option value="OK"${hf.status==='OK'?' selected':''}>Swam</option><option value="NS"${hf.status==='NS'?' selected':''}>No-show</option><option value="DQ"${hf.status==='DQ'?' selected':''}>DQ</option></select></label>
        ${f._block&&!hf.time?'':`<label>Time <input name="time" value="${esc(hf.time||'')}" placeholder="m:ss.hh" size="8"></label>`}
        <label class="qfcode"${hf.status==='DQ'?'':' hidden'}>Code <input name="code" value="${esc(hf.code||'')}" list="dqcodes" size="6"></label>
        <label class="qfnote">Note <input name="note" value="${esc(hf.note||'')}" placeholder="what was fixed"></label>
        ${(()=>{const mi=/not in the merge file/i.test(f.checks.join(' ')),wr=!!(f.row&&f.row.writtenIn)||/written in/i.test(f.checks.join(' '));
          return `<button type="submit"${mi||wr||f._watch?' class="primary"':''}>${mi||wr?(hf.add?'Confirmed · update':mi?'Confirm swim':'Confirm swimmer'):f._watch?'Save &amp; recalculate':hf.by?'Update':'Save fix'}</button>`})()}${hf.by?'<button type="button" data-clearfix>Clear</button>':''}
        <span class="sm" data-fixmsg>${hf.by?esc('Fixed by '+hf.by+' · '+agoShort(hf.at)):'Or just tick it if nothing needs changing.'}</span></form>
       ${t?`<div class="sm">done · ticked by ${esc(t.by)} · ${esc(agoShort(t.at))}</div>`:''}</div></li>`}).join('')}</ul></details></td></tr>`:'';
   return `<tr class="${t?'done':''}" data-cldrop="S${s.n}/scans">
    <td>${tickBox(key,'Event '+e.event)}</td>
    <td><span class="evname">Event ${e.event}</span>${e.desc?`<div class="sm">${esc(e.desc)}</div>`:''}${!e.expected&&s.meet.entries.length?'<div class="sm gwarn">not in the entries</div>':''}${t?`<div class="sm">ticked by ${esc(t.by)}</div>`:''}</td>
    <td class="c">${e.printed.length?'<span class="ok" title="'+esc(e.printed.map(x=>x.split('/').pop()).join(', '))+'">✓</span>':'<span class="no" title="No printed lane sheets for this event">–</span>'}</td>
    <td>${e.packs.length?e.packs.map(p=>link(s,'scans',p)).join('<br>'):'<span class="no">–</span>'}</td>
    <td class="c">${g7}${e.gen7Missing.length&&e.gen7.length?`<div class="sm">missing ${esc(e.gen7Missing.join(', '))}</div>`:''}${(e.notSwum||[]).length?`<div class="sm" title="${esc('Confirmed by '+(e.notSwumBy||'someone'))}">not swum ${esc(e.notSwum.join(', '))}</div>`:''}</td>
    <td class="c">${e.ocrLanes?e.ocrLanes+(e.lanes?'/'+e.lanes:''):'–'}</td>
    <td>${e.report?link(s,'reports',e.report):'–'}</td>
    <td>${mergeCell}</td><td class="c">${fxCell}</td>
    <td><span class="st ${e.status}">${esc(ST_TXT[e.status]||e.status)}</span>${e.computed&&!e.computed.ok&&e.status!=='computing'?`<div class="sm gwarn" title="${esc(e.computed.msg||'')}">portal: ${esc((e.computed.msg||'failed').slice(0,90))}</div>`:''}</td>
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
  ${clRow(s,'S'+s.n+':mmresults','Empty merge results export (Meet Manager)','<b>Once for the whole gala</b>: export merge results from Meet Manager before any results are in (name it with "Results"). Its header and layout are copied into every merge results file, in every session.',
    s.template?[`<a href="${esc(galaHref(s.template))}" target="_blank" rel="noopener">${esc(s.templateName)}</a>`+(s.templateSession!==s.n?` <span class="meta">(in session ${s.templateSession}; used for the whole gala)</span>`:'')]:[],'S'+s.n+'/meet')}
  <div class="cl-sec">Each event, as it is swum</div>
  <div class="meta" style="margin-bottom:4px">An event is <b>ready</b> when its lane sheet pack and every heat's Gen7 race PDF are in${GALA.auto?'; Claude is then asked to transcribe and audit it':''}. Claude then writes the event's <b>merge results</b> (<code>.hy3</code>, to import into Meet Manager) and its <b>quick fixes</b>: the swims to check by hand, listed under the event to tick off as they're fixed. Drop a lane sheet pack on an event to add it.</div>
  ${ev}
  <div class="cl-sec">During and after the session</div>
  ${clRow(s,'S'+s.n+':dq','DQ slips','The referees\' DQ slips, scanned (name the file with "DQ"). Claude reads each batch as it arrives.',scanFound(dq),'S'+s.n+'/scans')}
  ${clRow(s,'S'+s.n+':etd','ETD programme','The programme with the Gen7 race numbers written by each heat (name the file with "ETD"). Needed for combined events and re-started heats.',scanFound(etd),'S'+s.n+'/scans')}
  ${clRow(s,'S'+s.n+':codes','DQ codes','Each slip\'s code, given by Claude from '+(s.meet.codes.length?'the meet\'s own list ('+link(s,'meet',s.meet.codes[s.meet.codes.length-1])+')':'the full FINA list in the plugin (<code>gala-audit/data/dqcodesFINA.txt</code>); drop a <code>.txt</code> with the meet\'s own list in the inbox to use that instead')+'. Codes it wasn\'t sure of need the referee\'s OK.',s.dqCodes?[link(s,'ocr','dq-codes.json')]:[],null,true)}
  ${clRow(s,'S'+s.n+':merged','Session merge results (all events)','Written by Claude once every event is audited: one file with the whole session, and the session report.',s.merged.map(n=>link(s,'reports',n)).concat(s.sessionReport?['session report: '+link(s,'reports',s.sessionReport)]:[]),null,true)}
  ${clRow(s,'S'+s.n+':mmfinal','Final merge results export','Exported from Meet Manager after the audited results were imported and finalised (name it with "final").',s.meet.final.map(n=>link(s,'meet',n)),'S'+s.n+'/meet')}
  ${clRow(s,'S'+s.n+':final','Final audit report','Written by the portal when the final export is in: every change made by hand in Meet Manager.',s.finalReports.map(n=>link(s,'reports',n)),null,true)}
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
  galaPost('/api/gala/ask',{key:ab.dataset.ask}).then(j=>{
    if(j.report){ab.disabled=false;galaDialog({title:'Session files',text:j.report,ok:'OK',cancel:false});return}
    if(j.job){ab.disabled=false;galaNotice(j.job==='already running'?'Already working on it.':'Working on it: the result shows here and in the activity log in a minute.');galaSig='';setTimeout(loadGala,1500);return}
    ab.textContent='Asked (#'+j.question+')';galaSig='';setTimeout(loadGala,1500)})
   .catch(err=>{ab.disabled=false;galaNotice(err.message,'error')});return}
 const mv=e.target.closest('[data-move]');if(mv){const li=mv.closest('li');
  galaPost('/api/gala/move',{path:mv.dataset.move,session:+li.querySelector('[data-mvs]').value,folder:li.querySelector('[data-mvf]').value})
   .then(()=>{galaSig='';loadGala()}).catch(err=>galaNotice(err.message,'error'))}}
function galaChange(e){
 const fm=e.target.closest('form.qffix');if(fm){fm.dataset.dirty='1';if(e.target.name==='status')fm.querySelector('.qfcode').hidden=e.target.value!=='DQ';return}
 const up=e.target.closest('[data-gup]');if(up&&up.files.length){galaUpload(up.dataset.gup,[...up.files],up.closest('[data-gfolder],.cl-row,tr,.drop')||up.parentElement);return}
 const tk=e.target.closest('[data-tick]');if(tk)galaTick(tk)}
// Ticking an event complete while heats have no Gen7 race: confirm those races weren't swum.
async function galaTick(tk){const key=tk.dataset.tick,body={key,checked:tk.checked};
 const m=/^S(\d+):e(\d+)$/.exec(key);
 if(m&&tk.checked){const s=GALA.sessions.find(x=>x.n===+m[1]),ev=s&&s.events.find(x=>x.event===+m[2]),miss=ev?ev.gen7Missing:[];
  if(miss.length){const many=miss.length>1;
   const ok=await galaDialog({title:'Event '+ev.event+' is short of results',
    text:'There is no Gen7 result for heat'+(many?'s ':' ')+miss.join(', ')+' (of '+ev.heats+'). Were '+(many?'these races':'this race')+' not swum? '
     +'Ticking the event complete records '+(many?'them':'it')+' as not swum, and Claude audits the event without '+(many?'them':'it')+'.',
    ok:'Not swum · tick complete',cancel:'Keep it open'});
   if(!ok){tk.checked=false;return}
   body.notSwum=miss}}
 galaPost('/api/gala/tick',body).then(()=>{if(tk.checked)offerRegen(key,key);galaSig='';loadGala()})
  .catch(err=>{tk.checked=!tk.checked;galaNotice('Could not save: '+err.message,'error')})}
for(const sec of ['#v-gala','#v-checklist']){const el=$(sec);el.addEventListener('click',galaClicks);el.addEventListener('change',galaChange);
 el.addEventListener('dragover',e=>{const c=e.target.closest('[data-gfolder],[data-cldrop],#g-inbox');if(!c)return;e.preventDefault();c.classList.add('over')});
 el.addEventListener('dragleave',e=>{const c=e.target.closest('[data-gfolder],[data-cldrop],#g-inbox');if(c&&!c.contains(e.relatedTarget))c.classList.remove('over')});
 el.addEventListener('drop',e=>{const c=e.target.closest('[data-gfolder],[data-cldrop],#g-inbox');if(!c)return;e.preventDefault();c.classList.remove('over');
  const fs=[...(e.dataTransfer.files||[])];if(!fs.length)return;
  const folder=c.id==='g-inbox'?'inbox':(c.dataset.gfolder||c.dataset.cldrop);
  if(/\/(ocr|reports)$/.test(folder)){galaNotice('That folder is written by Claude. Drop the file on the inbox instead.','error');return}
  galaUpload(folder,fs,c)})}
$('#g-inboxfile').addEventListener('change',e=>{if(e.target.files.length)galaUpload('inbox',[...e.target.files],$('#g-inbox'))});
$('#g-auto').addEventListener('change',e=>galaPost('/api/gala/auto',{auto:e.target.checked}).then(()=>{galaSig='';loadGala()}));
setInterval(()=>{if(!document.hidden&&(view==='gala'||view==='checklist'||view==='restart'))loadGala()},4000);

// After the last quick fix of an event is done (ticked or fixed here), offer to write its merge results again.
function offerRegen(key,justDone){
 const m=/^S(\d+):e(\d+):qf:/.exec(key);if(!m)return;const n=+m[1],ev=+m[2];
 const s=GALA&&GALA.sessions.find(x=>x.n===n),e=s&&s.events.find(x=>x.event===ev);if(!e||!e.fixes||!e.canMerge)return;
 const left=e.fixes.filter(f=>{const k='S'+n+':e'+ev+':qf:'+f.heat+'.'+f.lane;return k!==justDone&&!GALA.ticks[k]});
 if(left.length)return;
 galaDialog({title:'All quick fixes for event '+ev+' are done',
  text:'Regenerate the event\'s merge results now, with the fixes made in the portal and every DQ slip read so far? No scans are read again.',
  ok:'Regenerate the merge results',cancel:'Not now'}).then(yes=>{if(!yes)return;
  galaPost('/api/gala/ask',{key:'S'+n+':remerge:'+ev}).then(j=>{galaNotice('Regenerating event '+ev+'\'s merge results (the portal does it; a minute).');galaSig='';setTimeout(loadGala,1500)})
   .catch(err=>galaNotice(err.message,'error'))})}
document.addEventListener('submit',async ev=>{const fm=ev.target.closest('form.qffix');if(!fm)return;ev.preventDefault();
 const [n,event,heat,lane]=fm.dataset.fix.split('|'),d=Object.fromEntries([...new FormData(fm).entries()].filter(([k])=>!/^(w\d+|wt\d+|swpick|swq)$/.test(k)));
 const watches={};let bad='';
 fm.querySelectorAll('input[data-swcol]').forEach(i=>{const c=i.dataset.swcol,tv=i.value.trim(),orig=i.dataset.orig||'',acc=fm.querySelector('input[data-swacc="'+c+'"]');
  if(tv&&tv!==orig){if(!/^(\d{1,2}:)?\d{1,2}\.\d{2}$/.test(tv))bad='SW'+c+': type the time off the sheet, like 1:05.32';else watches[c]={time:tv}}
  else if(acc&&acc.checked&&orig)watches[c]={accepted:true}});
 if(bad){fm.querySelector('[data-fixmsg]').textContent=bad;return}
 d.watches=watches;
 const pick=fm.querySelector('input[name=swpick]:checked');if(pick&&pick.value)d.swimmer={id:pick.value,name:pick.dataset.name};
 const swb=fm.querySelector('.qfsw[data-add]');if(swb){if(!d.swimmer){fm.querySelector('[data-fixmsg]').textContent='Pick the swimmer first';return}d.add=true}
 if(d.time&&!/^(\d{1,2}:)?\d{1,2}\.\d{2}$/.test(d.time)){fm.querySelector('[data-fixmsg]').textContent='Time like 1:05.32 or 32.10';return}
 if(d.status==='DQ'&&!d.code){fm.querySelector('[data-fixmsg]').textContent='A DQ needs its code';return}
 if(d.code&&CODES.codes&&CODES.codes.length&&!CODES.codes.some(c=>c[0]===d.code)&&
   !await galaDialog({title:'DQ code '+d.code+' is not in the list',text:d.code+' is not in '+(CODES.source||'the code list')+'. Save it anyway?',ok:'Save it',cancel:'Change it'}))return;
 galaPost('/api/gala/fix',Object.assign({session:+n,event:+event,heat:+heat,lane:+lane},d)).then(()=>{
  delete fm.dataset.dirty;document.activeElement&&document.activeElement.blur();
  if(Object.keys(d.watches||{}).length||d.swimmer||fm.querySelector('.qft'))galaPost('/api/gala/recheck',{session:+n,event:+event}).catch(err=>galaNotice('Could not re-check: '+err.message,'error'));
  offerRegen('S'+n+':e'+event+':qf:'+heat+'.'+lane,'S'+n+':e'+event+':qf:'+heat+'.'+lane);galaSig='';loadGala()})
  .catch(err=>{fm.querySelector('[data-fixmsg]').textContent='Could not save: '+err.message})});
document.addEventListener('click',ev=>{const b=ev.target.closest('[data-clearfix]');if(!b)return;const fm=b.closest('form.qffix');
 const [n,event,heat,lane]=fm.dataset.fix.split('|');
 galaPost('/api/gala/fix',{session:+n,event:+event,heat:+heat,lane:+lane,status:'',time:'',note:''}).then(()=>{delete fm.dataset.dirty;galaSig='';loadGala()})});

// ---- quick fixes: watches read with medium/low confidence (accept as read, or type the reading), and swimmers
// written in on the sheet (pick the entered swimmer: suggestions from the entries file, or search for another).
function qfWatches(f,hf){
 const row=f.row;if(!row)return '';
 const text=f.checks.join(' ');
 // The watches a check is about: "SW timer N" names one; "In doubt" / "Poor SW" / "SW median" are about them all.
 const named=new Set([...text.matchAll(/\bSW timer (\d+)/gi)].map(m=>+m[1]));
 const all=/in doubt|poor sw|sw median|sw times?\b/i.test(text);
 if(!named.size&&!all&&!/watch/i.test(text))return '';
 const ws=row.watches.filter(w=>all||named.has(w.column)||!w.normalized||w.confidence!=='HIGH'||w.corrected);
 if(!ws.length)return '';
 const fixed=hf.watches||{};
 return '<div class="qfws"><div class="sm">Check the watch times on the sheet; correct any that were misread, save, and the event is checked again.</div>'+ws.map(w=>{const c=w.column,fx=fixed[c]||{},readable=!!w.normalized;
  const sure=readable&&w.confidence==='HIGH'&&!w.corrected;
  const how=readable?`read as <b>${esc(w.normalized)}</b>${sure?'':` (${esc((w.confidence||'').toLowerCase())} confidence${w.corrected?', overwritten on the sheet':''})`}`:'<b class="gwarn">couldn\'t be read</b>';
  return `<div class="qfw" data-col="${esc(c)}"><span>Timer ${esc(c)}: written “${esc(w.written||'?')}”, ${how}</span>
   ${readable&&!sure?`<label><input type="radio" name="w${esc(c)}" value="accept"${fx.accepted?' checked':''}> accept ${esc(w.normalized)}</label>`:''}
   <label><input type="radio" name="w${esc(c)}" value="capture"${fx.time?' checked':''}> it reads</label>
   <input name="wt${esc(c)}" value="${esc(fx.time||'')}" placeholder="m:ss.hh" size="8">
   ${(fx.accepted||fx.time)?'':`<label class="sm"><input type="radio" name="w${esc(c)}" value="" checked> leave</label>`}</div>`}).join('')+'</div>'}
function qfSwimmer(s,e,f,hf){
 const row=f.row;
 const missing=/not in the merge file/i.test(f.checks.join(' ')),written=!!(row&&row.writtenIn)||/written in/i.test(f.checks.join(' '));
 if(!written&&!missing&&!/written in|swimmer is not the one|different swimmer|no seeded entry|swimmer/i.test(f.checks.join(' ')))return '';
 const name=(row&&row.name)||'',chosen=hf.swimmer;
 return `<div class="qfsw" data-q="${esc(name)}" data-event="${e.event}" data-session="${s.n}" data-chosen="${esc(chosen?chosen.id:'')}"${missing||written?' data-add="1"':''}>
  ${missing?`<div><b>Confirm the swim</b>: pick the swimmer (the best match from the entries is chosen) and press <b>Confirm swim</b>. It is put in the merge file for them, with an entry added if they weren't entered in this event.</div>`
   :`<div><b>Confirm the swimmer</b> written in on the sheet: pick them (the best match from the entries is chosen) and press <b>Confirm swimmer</b>. The swim is given to them in the merge file, with an entry added if they weren't entered in this event; the printed swimmer gets no result.</div>`}
  ${row&&row.notes?`<div class="sm">On the sheet: ${esc(row.notes)}</div>`:''}
  <div>Swimmer on the sheet: <b>${esc(name||'?')}</b>${chosen?` · <span class="ok">chosen: ${esc(chosen.name)}</span>`:''}</div>
  <div class="swlist sm">Looking for matches in the entries…</div>
  <label class="sm">Search for another swimmer <input name="swq" placeholder="surname or first name" size="22"></label></div>`}
const SWCACHE={};
async function swFill(box,q){
 const n=box.dataset.session,ev=box.dataset.event,key=n+'|'+ev+'|'+q,list=box.querySelector('.swlist');
 try{SWCACHE[key]=SWCACHE[key]||(await api('/api/gala/swimmers?session='+n+'&event='+ev+'&q='+encodeURIComponent(q))).swimmers}catch(err){list.textContent='Could not search the entries: '+err.message;return}
 const rows=SWCACHE[key],chosen=box.dataset.chosen,prev=(box.querySelector('input[name=swpick]:checked')||{}).value;
 list.innerHTML=rows.length?rows.map((w,i)=>`<label class="swopt"><input type="radio" name="swpick" value="${esc(w.id)}" data-name="${esc(w.name)}"${(prev||chosen)===w.id||(!prev&&!chosen&&i===0&&(w.entered||w.score>=0.8))?' checked':''}>
   ${esc(w.name)}${w.preferred&&w.preferred.toLowerCase()!==w.first.toLowerCase()?' <span class="meta">(first name '+esc(w.first)+')</span>':''} <span class="meta">${esc([w.sex==='F'?'girl':w.sex==='M'?'boy':'',w.age&&('age '+w.age),w.team].filter(Boolean).join(' · '))}</span>
   ${w.entered?'<span class="ok">entered in this event</span>':'<span class="gwarn">not entered in this event</span>'}${i===0&&q&&!prev&&!chosen?' <span class="meta">(best match)</span>':''}</label>`).join('')
  +'<label class="swopt"><input type="radio" name="swpick" value=""> none of these</label>'
  :'<span class="meta">No swimmer in the entries matches.</span>'}
function swAll(){document.querySelectorAll('.qfsw').forEach(b=>{if(!b.dataset.filled){b.dataset.filled='1';swFill(b,b.dataset.q)}})}
let swTimer=null;
document.addEventListener('input',ev=>{const i=ev.target.closest('.qfsw input[name=swq]');if(!i)return;const box=i.closest('.qfsw');
 clearTimeout(swTimer);swTimer=setTimeout(()=>swFill(box,i.value.trim()||box.dataset.q),300)});
document.addEventListener('focusin',ev=>{const i=ev.target.closest('.qfw input[name^=wt]');if(i){const r=i.closest('.qfw').querySelector('input[value=capture]');if(r)r.checked=true}});

document.addEventListener('click',ev=>{const b=ev.target.closest('[data-recheck]');if(!b)return;ev.preventDefault();const [n,e]=b.dataset.recheck.split('|');
 b.disabled=true;b.textContent='re-checking…';galaPost('/api/gala/recheck',{session:+n,event:+e}).then(()=>{galaSig='';setTimeout(loadGala,1500)}).catch(err=>{b.disabled=false;galaNotice(err.message,'error')})});

// ---- the times row at the top of every quick fix: TP, PB, each stopwatch (correctable), median and final.
// Typing a watch time recalculates the median and the differences here; "Save & recalculate" saves the corrected
// readings and re-runs the event's check against TP and PB.
function hund(t){const m=/^(?:(\d{1,2}):)?(\d{1,2})\.(\d{2,3})$/.exec((t||'').trim());return m?((+m[1]||0)*60+(+m[2]))*100+Math.floor(+(m[3]+'0').slice(0,3)/10):null}
function fmtH(h){if(h==null)return '';const neg=h<0;h=Math.abs(Math.round(h));const m=Math.floor(h/6000),s=Math.floor(h%6000/100),c=h%100;
 return (neg?'-':'')+(m?m+':'+String(s).padStart(2,'0'):s)+'.'+String(c).padStart(2,'0')}
function diffH(a,b){if(a==null||b==null)return '';const d=(a-b)/100;return (d>0?'+':'')+d.toFixed(2)+'s'}
function qfTimes(f,hf){
 const t=f.times,row=f.row;if(!t&&!row)return '';
 const fixed=hf.watches||{},tol=Math.round(((t&&t.tolerance)||0.30)*100);
 const byCol={};(row?row.watches:[]).forEach(w=>byCol[w.column]=w);(t?t.sw:[]).forEach(w=>{byCol[w.column]=Object.assign({normalized:w.time},byCol[w.column]||{},w)});
 const cols=[1,2,3];
 const tp=t?hund(t.tp):null,pb=t?hund(t.pb):null;
 const cell=(label,v,cls)=>`<td class="${cls||''}"><div class="tl">${label}</div><b>${esc(v||'–')}</b></td>`;
 const swCells=cols.map(c=>{const w=byCol[c]||{},orig=w.normalized||'',val=(fixed[c]&&fixed[c].time)||orig,sure=!w.confidence||(w.confidence==='HIGH'&&!w.corrected);
  return `<td class="sw${orig?'':' unread'}${sure?'':' unsure'}"><div class="tl">SW${c}${w.written&&w.written!==orig?` <span class="sm">“${esc(w.written)}”</span>`:''}</div>
   <input data-swcol="${c}" data-orig="${esc(orig)}" value="${esc(val)}" placeholder="${orig?'':'not read'}" size="8">
   ${orig&&!sure?`<label class="sm"><input type="checkbox" data-swacc="${c}"${fixed[c]&&fixed[c].accepted?' checked':''}> OK as read (${esc((w.confidence||'').toLowerCase())})</label>`:''}
   <div class="sm" data-swdiff="${c}"></div></td>`}).join('');
 return `<div class="qft" data-tp="${tp??''}" data-pb="${pb??''}" data-tol="${tol}"><table><tr>
   ${cell('TP',t&&t.tp)}${cell('PB',t&&t.pb)}${swCells}<td><div class="tl">SW median</div><b data-swmed>${esc(t&&t.median||'–')}</b><div class="sm" data-meddiff></div></td>${cell('Final',t&&t.final,'fin')}</tr></table>
  <div class="sm">Correct any stopwatch time that was misread on the sheet, then <b>Save &amp; recalculate</b>: the swim is checked again against TP and PB (tolerance ${(tol/100).toFixed(2)}s).</div></div>`}
function qftUpdate(box){
 const tp=box.dataset.tp?+box.dataset.tp:null,pb=box.dataset.pb?+box.dataset.pb:null,tol=+box.dataset.tol||30;
 const vals=[];box.querySelectorAll('input[data-swcol]').forEach(i=>{const v=hund(i.value);i.classList.toggle('bad',!!i.value.trim()&&v==null);
  const d=box.querySelector('[data-swdiff="'+i.dataset.swcol+'"]');
  if(v!=null){vals.push(v);const parts=[];if(tp!=null)parts.push(`<span class="${Math.abs(v-tp)>tol?'gwarn':'ok'}">TP ${diffH(v,tp)}</span>`);if(pb!=null)parts.push(`<span class="${Math.abs(v-pb)>tol?'gwarn':'ok'}">PB ${diffH(v,pb)}</span>`);d.innerHTML=parts.join(' ')}else d.innerHTML=''});
 vals.sort((a,b)=>a-b);const med=vals.length?(vals.length%2?vals[(vals.length-1)/2]:(vals[vals.length/2-1]+vals[vals.length/2])/2):null;
 const m=box.querySelector('[data-swmed]');if(m)m.textContent=med==null?'–':fmtH(med);
 const md=box.querySelector('[data-meddiff]');if(md)md.innerHTML=med==null?'':[tp!=null?`<span class="${Math.abs(med-tp)>tol?'gwarn':'ok'}">TP ${diffH(med,tp)}</span>`:'',pb!=null?`<span class="${Math.abs(med-pb)>tol?'gwarn':'ok'}">PB ${diffH(med,pb)}</span>`:''].join(' ')}
document.addEventListener('input',ev=>{const b=ev.target.closest('.qft');if(b)qftUpdate(b)});
const _qftObs=new MutationObserver(()=>document.querySelectorAll('.qft:not([data-init])').forEach(b=>{b.dataset.init='1';qftUpdate(b)}));
_qftObs.observe(document.body,{childList:true,subtree:true});

// Remember which quick-fix lists are open, so a refresh doesn't fold one you are looking at (or unfold one you closed).
const QF_OPEN={};
document.addEventListener('toggle',ev=>{const d=ev.target;if(d.matches&&d.matches('details[data-qfd]'))QF_OPEN[d.dataset.qfd]=d.open},true);

// ---- dialogs and notices that are part of the page (never the browser's alert/confirm/prompt boxes)
function galaDialog(o){
 return new Promise(res=>{
  let d=$('#gdlg');if(!d){d=document.createElement('div');d.id='gdlg';d.className='gdlg';d.setAttribute('role','dialog');d.setAttribute('aria-modal','true');document.body.appendChild(d)}
  d.innerHTML=`<div class="gdlg-box"><h3>${esc(o.title||'')}</h3>${o.text?`<p>${esc(o.text)}</p>`:''}
   ${o.check?`<label class="gdlg-ck"><input type="checkbox"${o.checked?' checked':''}> ${esc(o.check)}</label>`:''}
   ${o.input!==undefined?`<input class="gdlg-in" value="${esc(o.input)}" placeholder="${esc(o.placeholder||'')}"><div class="sm gwarn gdlg-err"></div>`:''}
   <div class="gdlg-btns">${o.cancel===false?'':`<button type="button" data-no>${esc(o.cancel||'Cancel')}</button>`}<button type="button" class="primary" data-yes>${esc(o.ok||'OK')}</button></div></div>`;
  d.hidden=false;const inp=d.querySelector('.gdlg-in'),tgt=inp||d.querySelector('[data-yes]');
  // the keyboard may still be in the terminal (an iframe): take it back, or typing goes to the terminal
  const af=document.activeElement;if(af&&af.tagName==='IFRAME')af.blur();window.focus();tgt.focus();setTimeout(()=>tgt.focus(),50);
  const done=v=>{if(o.input!==undefined&&v!==false&&o.validate){const err=o.validate(v);if(err){d.querySelector('.gdlg-err').textContent=err;return}}
   if(o.check){const c=d.querySelector('.gdlg-ck input');o.checked=!!(c&&c.checked)}   // read back by the caller
   d.hidden=true;d.innerHTML='';document.removeEventListener('keydown',key,true);res(v)};
  const key=ev=>{if(ev.key==='Escape'){ev.preventDefault();done(o.input!==undefined?null:false)}else if(ev.key==='Enter'&&(inp||document.activeElement.closest('#gdlg'))){ev.preventDefault();done(inp?inp.value:true)}};
  document.addEventListener('keydown',key,true);
  d.onclick=ev=>{if(ev.target===d)done(o.input!==undefined?null:false);else if(ev.target.closest('[data-yes]'))done(inp?inp.value:true);else if(ev.target.closest('[data-no]'))done(o.input!==undefined?null:false)}})}
function galaNotice(text,kind){
 let box=$('#gnotes');if(!box){box=document.createElement('div');box.id='gnotes';box.setAttribute('aria-live','polite');document.body.appendChild(box)}
 const n=document.createElement('div');n.className='gnote'+(kind==='error'?' err':'');n.innerHTML=`<span>${esc(text)}</span><button type="button" aria-label="Close">✕</button>`;
 n.querySelector('button').onclick=()=>n.remove();box.appendChild(n);setTimeout(()=>n.remove(),kind==='error'?12000:6000)}

// ---- Restart tab: archive or delete the gala and start again
function renderRestart(){
 if(!GALA||!GALA.summary)return;const s=GALA.summary;
 const stat=(n,t)=>`<div class="gstat"><b>${n}</b><span class="meta">${t}</span></div>`;
 $('#rs-sum').innerHTML=stat(s.sessions,'sessions')+stat(s.files,'files')+stat(size(s.bytes),'on disk')+stat(s.questions,'questions')+stat(s.documents,'documents')
  +(s.linked.length?`<div class="meta" style="align-self:center">Linked inbox folders, left alone: ${esc(s.linked.join(', '))}</div>`:'');
 const a=GALA.archives||[];
 $('#rs-arch').innerHTML=a.length?'<table>'+a.map(x=>`<tr><td><code>data/archive/${esc(x.name)}</code></td><td>${x.sessions} session${x.sessions===1?'':'s'} · ${x.files} files · ${size(x.bytes)}${x.questions?' · with questions':''}</td><td style="text-align:right;white-space:nowrap"><button type="button" data-rsres="${esc(x.name)}" data-q="${x.questions?1:''}">Restore</button> <button type="button" data-rsdel="${esc(x.name)}">Delete for good</button></td></tr>`).join('')+'</table>':'None yet.'}
function rsCheck(){const mode=(document.querySelector('input[name=rsmode]:checked')||{}).value,w=mode==='delete'?'DELETE':'RESTART';
 $('#rs-word').textContent=w;$('#rs-btn').disabled=$('#rs-confirm').value.trim().toUpperCase()!==w;$('#rs-btn').textContent=mode==='delete'?'Delete and restart':'Archive and restart'}
$('#v-restart').addEventListener('input',rsCheck);$('#v-restart').addEventListener('change',rsCheck);
$('#rs-btn').addEventListener('click',async()=>{
 const mode=document.querySelector('input[name=rsmode]:checked').value,q=$('#rs-q').checked;
 const ok=await galaDialog({title:mode==='delete'?'Delete this gala for good?':'Archive this gala and start again?',
  text:(mode==='delete'?'Every session\'s files, the ticks, fixes and history are deleted and can\'t be brought back.':'Everything is moved to data/archive and the portal starts empty.')+(q?' The questions, answers and documents are cleared too.':'')+' Claude carries on, but its earlier work will be gone from the portal.',
  ok:mode==='delete'?'Delete for good':'Archive and restart',cancel:'Cancel'});
 if(!ok)return;$('#rs-btn').disabled=true;$('#rs-msg').textContent='Working…';
 try{const j=await galaPost('/api/gala/restart',{mode,questions:q,reimport:$('#rs-re').checked,label:$('#rs-label').value,confirm:$('#rs-confirm').value});
  $('#rs-confirm').value='';rsCheck();$('#rs-msg').textContent='';galaNotice(j.archive?'Archived to data/archive/'+j.archive+'. The portal is empty and ready for the next gala.':'Deleted. The portal is empty and ready for the next gala.');
  try{localStorage.removeItem('qa-drafts')}catch(e){}GSESS=0;galaSig='';loadGala()}
 catch(err){$('#rs-msg').textContent='';galaNotice(err.message,'error');rsCheck()}});
$('#rs-arch').addEventListener('click',async ev=>{const r=ev.target.closest('[data-rsres]');if(r){rsRestore(r.dataset.rsres,!!r.dataset.q);return}
 const b=ev.target.closest('[data-rsdel]');if(!b)return;
 if(!await galaDialog({title:'Delete archive '+b.dataset.rsdel+' for good?',text:'Its files are deleted and can\'t be brought back.',ok:'Delete for good',cancel:'Keep it'}))return;
 galaPost('/api/gala/archive-delete',{name:b.dataset.rsdel}).then(()=>{galaNotice('Archive deleted.');galaSig='';loadGala()}).catch(err=>galaNotice(err.message,'error'))});

// ---- Restart tab: updates from GitHub (update.sh)
let UPD=null,updAt=0;
async function loadUpdate(fetch){
 try{UPD=await api('/api/gala/update-status'+(fetch?'?fetch=1':''));updAt=Date.now()}catch(e){$('#up-info').textContent='Could not check: '+e.message;return}
 renderUpdate()}
function renderUpdate(){const u=UPD;if(!u)return;const go=$('#up-go'),cl=$('#up-claude-l');go.hidden=cl.hidden=true;
 if(!u.git){$('#up-info').innerHTML='This copy of the portal (v'+esc(u.version)+') isn\'t a git clone, so it can\'t update itself. Install it with <code>install.sh</code> to get updates.';return}
 const cur=(u.current||'').split('|');let h='Installed: <b>v'+esc(u.version)+'</b> · <code>'+esc(cur[0]||'?')+'</code> '+esc(cur[1]||'')+(cur[2]?' – '+esc(cur[2]):'');
 if(u.fetched===false)h+='<br><span class="bad">Couldn\'t reach GitHub just now (no internet?).</span>';
 if(u.checkedAt)h+='<br>Checked '+new Date(u.checkedAt*1000).toLocaleTimeString()+'.';
 const b=u.behind||[];
 if(u.changed&&u.changed.length)h+='<br><span class="bad">Portal files were changed on this computer, so it won\'t update: '+esc(u.changed.join(', '))+'</span>';
 else if(u.ahead)h+='<br><span class="bad">This copy has '+u.ahead+' change(s) of its own that aren\'t on GitHub (the development copy?), so it won\'t update.</span>';
 if(b.length){h+='<br><b>'+b.length+' update'+(b.length>1?'s':'')+' available:</b><ul class="up-list">'+b.slice(0,15).map(c=>'<li><code>'+esc(c.sha)+'</code> '+esc(c.date)+' – '+esc(c.subject)+'</li>').join('')+'</ul>';
  if(!(u.changed&&u.changed.length)&&!u.ahead)go.hidden=cl.hidden=false}
 else if(u.fetched!==false)h+='<br>Up to date.';
 $('#up-info').innerHTML=h;
 if(u.log){$('#up-logd').hidden=false;$('#up-log').textContent=u.log}}
$('#up-check').addEventListener('click',()=>{$('#up-info').textContent='Checking GitHub…';loadUpdate(true)});
$('#up-go').addEventListener('click',async()=>{const claude=$('#up-claude').checked;
 if(!await galaDialog({title:'Update the portal now?',text:'The portal stops, updates from GitHub (and the gala-ocr plugin), and starts again, which takes about a minute; this page reloads itself. The gala\'s files, ticks, fixes and questions are kept.'+(claude?' Claude Code is restarted too: do this between events, not while Claude is auditing one.':' Claude Code keeps running.'),ok:'Update now',cancel:'Not now'}))return;
 $('#up-go').disabled=true;
 try{await galaPost('/api/gala/update',{claude})}catch(err){$('#up-go').disabled=false;galaNotice(err.message,'error');return}
 galaNotice('Updating… the page reloads when the portal is back.');$('#up-info').textContent='Updating…';
 const was=UPD&&UPD.current;let down=false;const t0=Date.now();
 const poll=async()=>{if(Date.now()-t0>180000){galaNotice('The portal hasn\'t come back after 3 minutes. Look at .portal/update.log, or run bash start-portal.sh.','error');return}
  try{const r=await fetch(url('/api/gala/update-status'),{cache:'no-store'});if(r.ok){const j=await r.json();
   if(down||(j.current&&j.current!==was)){location.reload();return}}else down=true}catch(e){down=true}
  setTimeout(poll,3000)};setTimeout(poll,4000)});
// check when the Restart tab is shown (every 10 minutes at most)
setInterval(()=>{if(!document.hidden&&view==='restart'&&Date.now()-updAt>600000)loadUpdate(false)},5000);

async function rsRestore(name,hasQ){const s=(GALA&&GALA.summary)||{};
 const busy=s.sessions||s.files;
 const o={title:'Restore '+name+'?',text:'Its sessions, files, checklist ticks, quick fixes and activity log come back into the portal, and the archive is emptied.'
  +(busy?' What is in the portal now ('+s.sessions+' session'+(s.sessions===1?'':'s')+', '+s.files+' files) is archived first, so nothing is lost.':'')
  +' Claude isn\'t asked again about events it already did.',ok:'Restore',cancel:'Cancel'};
 if(hasQ){o.check='Also restore its Questions, answers and Documents (those in the portal now are archived with the rest)';o.checked=true}
 if(!await galaDialog(o))return;
 try{const j=await galaPost('/api/gala/archive-restore',{name,questions:!!o.checked});
  galaNotice('Restored '+name+'.'+(j.archive?' What was here is in data/archive/'+j.archive+'.':''));GSESS=0;galaSig='';loadGala()}
 catch(err){galaNotice(err.message,'error')}}

