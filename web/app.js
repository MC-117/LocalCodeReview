'use strict';
document.body.classList.toggle('compact',new URLSearchParams(location.search).get('compact')==='1');
document.getElementById('toggleCode').textContent=document.body.classList.contains('compact')?'浏览修改':'仅审查清单';
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function canonicalScope(q){const v={source:q.source,include:q.include||'',exclude:q.exclude||'',defaults:q.defaults??true};if(q.source==='session')Object.assign(v,{session:q.session||'',start:String(q.start||''),end:String(q.end||''),attempts:q.attempts??true,turnId:q.turnId||''});if(q.source==='snapshot')v.snapshot=q.snapshot||'';if(q.source==='round')v.round=q.round||'';return v;}
function equal(a,b){if(a?.source&&b?.source)return JSON.stringify(canonicalScope(a))===JSON.stringify(canonicalScope(b));return JSON.stringify(a)===JSON.stringify(b);}
const preferences=ReviewHost.storage;
let meta,state,changes,scope,receipts=[],groups=[],active=0,file='',seen={},baseRevision=0,dirty=false,busy=false,renderId=0,timer;
let autosaveTimer,savePromise=null,saveError=null,editVersion=0,submitting=false;
let navMode=preferences.getItem('review-navigation')||'feature',mode=preferences.getItem('review-code-layout')||'single';
if(!['feature','directory','file'].includes(navMode))navMode='feature';
if(!['single','split','current'].includes(mode))mode='single';
let target=null,targetRefs=[],drafts=new Map(),scopeAttempts=true,scopeTurnId='';
const draftKey=()=> 'review-feedback-v2:'+meta.root+':'+(meta.reviewScope?.threadId||'standalone');
let progressBlocks=null;
const viewer=new FullCodeViewer($('code'),(blocks,index)=>{
  $('changeCount').textContent=blocks.length?(index<0?`${blocks.length} 处修改`:`第 ${index+1} / ${blocks.length} 处修改`):'0 处修改';
  $('prevChange').disabled=$('nextChange').disabled=!blocks.length;$('changeSelect').disabled=!blocks.length;
  if(progressBlocks!==blocks){progressBlocks=blocks;$('changeSelect').innerHTML='<option value="-1">定位修改…</option>'+blocks.map((b,i)=>`<option value="${i}">${esc(b.label)}</option>`).join('');}
  $('changeSelect').value=String(index);
});
function toast(t){$('toast').textContent=t;$('toast').hidden=false;clearTimeout(timer);timer=setTimeout(()=>$('toast').hidden=true,5000);}
function fail(e){$('notice').hidden=false;$('notice').textContent=e.message;toast(e.message);}
async function api(path,body){if(path==='meta'&&!ReviewHost.embedded){const view=new URLSearchParams(location.search).get('view');if(view)path+='?view='+encodeURIComponent(view);}if(ReviewHost.embedded){const [route,query]=path.split('?');return ReviewHost.call(route,body??Object.fromEntries(new URLSearchParams(query)));}const r=await fetch('/api/'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':meta.token},body:JSON.stringify(body)});if(path==='export'){if(!r.ok)throw Error('导出失败');return {text:await r.text()};}const v=await r.json();if(!r.ok)throw Error(v.error||'本地请求失败');return v;}
function bind(id,event,fn){$(id).addEventListener(event,e=>Promise.resolve().then(()=>fn(e)).catch(fail));}
function readScope(){const source=$('source').value;const q={source,session:source==='session'?$('session').value:'',snapshot:source==='snapshot'?$('snapshot').value:'',start:source==='session'?$('start').value:'',end:source==='session'?$('end').value:'',include:$('include').value.trim(),exclude:$('exclude').value.trim(),defaults:$('defaults').checked,attempts:scopeAttempts};if(source==='round')q.round=$('round').value;if(source==='session'){const thread=meta.sessions.find(s=>s.id===q.session)?.threadId;if(thread)q.threadId=thread;if(scopeTurnId)q.turnId=scopeTurnId;}return q;}
function applyScope(q){
  $('source').value=q.source;for(const key of ['session','snapshot','start','end','include','exclude'])$(key).value=q[key]??'';
  $('round').value=q.round??'';scopeTurnId=q.turnId||'';$('defaults').checked=q.defaults??true;scopeAttempts=q.attempts??true;
}
function scopeExplanation(){
  const suggested=meta.reviewScope;
  $('scopeReason').textContent=suggested?.query&&equal(scope,suggested.query)?suggested.reason:suggested&&!suggested.query?suggested.reason:'当前范围由你手动调整。';
  $('useSuggestedScope').hidden=!suggested?.query;
}
function describe(q){if(q.source==='round'){const r=changes?.round;return '单轮 · '+(r?.label||'固定前后快照');}return {session:'会话补丁',snapshot:'快照 → 当前',git:'HEAD → 当前'}[q.source]+(q.start||q.end?` · 第 ${q.start||'首'} 至 ${q.end||'末'} 轮`:'');}
function allPaths(){return [...new Set(changes.records.map(r=>r.path))].sort();}
function notes(){return state.annotations.filter(n=>n.status!=='withdrawn'&&equal(n.scope,scope));}
function checked(r){return r.status==='approved'&&!r.stale;}
function currentGroup(){return groups[active];}
function currentRows(){const paths=new Set(currentGroup()?.refs.map(r=>r.path));return receipts.filter(r=>paths.has(r.refs[0].path));}
function noteFor(t){return notes().find(n=>equal(n.target,t));}
function keyFor(t){return JSON.stringify(t);}
function remember(){preferences.setItem(draftKey(),JSON.stringify({scope,receipts,drafts:[...drafts],target,targetRefs,baseRevision,seen,seenDiff:changes.fingerprint,navMode,activeKey:currentGroup()?.key}));}
function scheduleAutosave(){
  clearTimeout(autosaveTimer);
  if(dirty&&!saveError)autosaveTimer=setTimeout(()=>{if(busy)scheduleAutosave();else save().catch(fail);},700);
}
function markDirty(){dirty=true;editVersion++;saveError=null;remember();renderSummary();scheduleAutosave();}
function syncNote(){
  if(!target)return;
  const text=$('notes').value,previous=noteFor(target),key=keyFor(target);
  if(text===(previous?.text||'')){drafts.delete(key);return;}
  drafts.set(key,{id:previous?.id??null,text,target:structuredClone(target),refs:structuredClone(targetRefs),scope:structuredClone(scope),seenHashes:{...seen}});
}
function navTarget(){const g=currentGroup();return g?{kind:navMode,key:g.key,label:g.title}:null;}
function fileTarget(){return file?{kind:'file',key:'file:'+file,label:file}:null;}
function generalTarget(){return {kind:'general',key:'all',label:'本轮全部修改'};}
function setTarget(t,refs){syncNote();target=t;targetRefs=refs||[];const n=t?noteFor(t):null,d=t?drafts.get(keyFor(t)):null;$('notes').value=d?.text??n?.text??'';renderEditor();}
function renderEditor(){
  const g=navTarget(),f=fileTarget(),options=[];
  if(g)options.push({value:'group',target:g});if(f&&!equal(g,f))options.push({value:'file',target:f});options.push({value:'general',target:generalTarget()});
  let value=options.find(o=>equal(o.target,target))?.value;
  if(!value&&target){value='selected';options.push({value,target});}
  $('commentTarget').innerHTML=options.map(o=>`<option value="${o.value}">${esc(o.target.label)}</option>`).join('');$('commentTarget').value=value||'general';
  const note=target?noteFor(target):null,draft=target?drafts.get(keyFor(target)):null;
  $('opinionLabel').textContent='意见';$('noteMeta').textContent=(note?'R'+note.id+' · ':'')+(draft?'待自动保存':note?.stale?'代码已变，定位需复核':note?'已保存':'可留空');
  $('notes').disabled=!target||submitting;
  $('noteList').innerHTML=notes().map(n=>`<button data-note="${n.id}" title="${esc(n.target.label)}"><strong>R${n.id}</strong> ${esc(n.text.slice(0,55))}${n.stale?' · 定位需复核':''}</button>`).join('');
  const legacy=state.items.filter(i=>!i.groupKey?.startsWith('file-receipt:')&&i.notes);
  if(legacy.length)$('noteList').innerHTML+='<details><summary>旧版审查意见</summary>'+legacy.map(i=>`<p>第 ${i.id} 项 · ${esc(i.title)}：${esc(i.notes)}</p>`).join('')+'</details>';
}
function buildReceipts(){
  receipts=allPaths().map(path=>{
    const saved=state.items.find(i=>equal(i.scope,scope)&&i.groupKey==='file-receipt:'+path);
    const receipt=saved?{...structuredClone(saved),confirmApproval:false}:{title:path,groupKey:'file-receipt:'+path,purpose:'文件审查记录；与浏览分组独立。',validation:'未记录验证',notes:'',status:'approved',scope:structuredClone(scope),refs:[{path}],confirmApproval:true};
    if(scope.source==='round'&&changes.records.some(r=>r.path===path&&r.currentHash!==r.afterHash)){receipt.stale=true;receipt.confirmApproval=false;if(!saved)receipt.status='pending';}
    return receipt;
  });
}
function buildGroups(preferred){
  const paths=allPaths(),guides=state.guides.filter(g=>equal(g.scope,scope));groups=[];
  if(navMode==='feature'){
    const assigned=new Set();
    for(const guide of guides){const refs=guide.refs.filter(r=>paths.includes(r.path));if(refs.length){groups.push({...guide,key:'feature:'+guide.key,refs});refs.forEach(r=>assigned.add(r.path));}}
    const rest=paths.filter(p=>!assigned.has(p));if(rest.length)groups.push({key:'ungrouped',title:guides.length?'尚未归入功能的文件':'等待助手整理功能导览',refs:rest.map(path=>({path})),purpose:'助手可根据本轮修改和聊天上下文整理跨文件功能分组。当前仍可切换目录或文件浏览。',validation:'未记录验证'});
  }else if(navMode==='file')groups=paths.map(path=>({key:'file:'+path,title:path,refs:[{path}]}));
  else for(const directory of [...new Set(paths.map(p=>p.includes('/')?p.slice(0,p.lastIndexOf('/')):'.'))])groups.push({key:'directory:'+directory,title:directory==='.'?'根目录':directory,refs:paths.filter(p=>(p.includes('/')?p.slice(0,p.lastIndexOf('/')):'.')===directory).map(path=>({path}))});
  active=Math.max(0,groups.findIndex(g=>g.key===preferred));
}
function renderWorkflow(){const w=state?.workflow||{},labels={feedback_ready:'意见已保存。助手先提出修改方案，等待你确认后再修改。',awaiting_confirmation:'修改方案已提出，等待你在聊天中确认；尚未授权修改。',confirmed:'你已确认方案，助手将按该方案修改。',applying:'正在执行已确认的方案。',completed:'本轮修改已完成，可继续审查。',invalidated:'意见或代码已变化，旧方案确认失效。'};$('workflowStatus').textContent=labels[w.status]||'只需批注有问题的地方。助手先提方案，等你确认后再改代码。';$('planPanel').hidden=!w.text;$('planText').textContent=w.text||'';}
function renderSummary(){const yes=receipts.filter(checked).length;$('progressText').textContent=receipts.length?`${yes} / ${receipts.length} 文件通过`:'';$('counts').textContent=`${groups.length} 组 · ${notes().length} 条意见`;$('saveState').textContent=saveError?'保存失败 · 草稿保留':savePromise?'自动保存中…':dirty?'等待自动保存…':receipts.some(r=>!r.id)?'默认通过 · 提交时记录':'已自动保存';$('retrySave').hidden=!saveError;$('retrySave').disabled=busy;$('sendFeedback').disabled=busy||submitting||!receipts.length;for(const id of ['refresh','applyScope','useSuggestedScope'])$(id).disabled=busy||submitting;renderWorkflow();}
function renderItems(){
  renderSummary();document.querySelectorAll('[data-nav]').forEach(b=>{b.classList.toggle('active',b.dataset.nav===navMode);b.setAttribute('aria-pressed',String(b.dataset.nav===navMode));});
  $('itemList').innerHTML=groups.map((g,i)=>{const ps=new Set(g.refs.map(r=>r.path)),rs=receipts.filter(r=>ps.has(r.refs[0].path)),yes=rs.length&&rs.every(checked),stale=rs.some(r=>r.stale),count=notes().filter(n=>n.refs.some(r=>ps.has(r.path))).length;return `<div class="item ${i===active?'selected':''}"><input type="checkbox" data-check="${i}" ${submitting?'disabled':''} aria-label="${esc(g.title)}通过" ${yes?'checked':''}><button class="item-select" data-item="${i}"><span class="item-title">${esc(g.title)}</span><span class="item-meta"><span>${ps.size} 文件</span><span class="${stale||!yes?'status-warn':'status-good'}">${stale?'需复审':yes?(rs.every(r=>r.id)?'通过':'默认通过'):'需修改'}</span>${count?`<span>${count} 条意见</span>`:''}</span></button></div>`;}).join('')||'<div class="empty">当前范围没有修改</div>';
  document.querySelectorAll('[data-check]').forEach(e=>{const ps=new Set(groups[Number(e.dataset.check)].refs.map(r=>r.path)),rs=receipts.filter(r=>ps.has(r.refs[0].path));e.indeterminate=rs.some(checked)&&!rs.every(checked);});
}
async function selectItem(index){syncNote();active=index;renderItems();setTarget(navTarget()||generalTarget(),currentGroup()?.refs||[]);await renderFiles();renderEditor();await publishSelection();}
async function renderFiles(){
  const g=currentGroup(),search=$('fileSearch').value.toLowerCase(),paths=g?[...new Set(g.refs.map(r=>r.path))].filter(p=>p.toLowerCase().includes(search)):[];
  if(!paths.includes(file))file=paths[0]||'';$('fileCount').textContent=paths.length+' 文件';
  $('files').innerHTML=paths.map(p=>`<button class="file ${p===file?'active':''}" data-path="${esc(p)}" title="${esc(p)}">${esc(p.split('/').pop())}</button>`).join('');
  $('guideSummary').hidden=!g?.purpose&&!g?.validation;$('guideTitle').textContent='修改目的与验证';$('guideBody').innerHTML=`<p>${esc(g?.purpose||'未记录修改目的')}</p><p>验证：${esc(g?.validation||'未记录验证')}</p>`;await renderCode();
}
async function renderCode(){const request=++renderId;viewer.detach();viewer.onProgress([],-1);$('jumpDiff').disabled=true;for(const [id,value] of [['patchView','single'],['splitView','split'],['currentView','current']]){$(id).classList.toggle('active',mode===value);$(id).setAttribute('aria-pressed',String(mode===value));}$('filePath').textContent=file;if(!file){$('code').innerHTML='<div class="empty">没有匹配的文件</div>';viewer.clear();return;}const requestedFile=file;$('code').removeAttribute('data-file');$('code').innerHTML='<div class="empty">正在读取代码…</div>';const data=await api('compare',{query:scope,path:requestedFile});if(request!==renderId)return;viewer.render(data,mode);$('jumpDiff').disabled=!(data.ops.some(op=>op[0]!=='equal')||data.anchors.length);$('code').dataset.file=requestedFile;$('filePath').textContent=requestedFile+' · '+data.after.language;if(seen[file]&&seen[file]!==data.currentHash)toast('文件已变化，通过前请刷新修改。');}
async function publishSelection(){if(scope)await api('selection',{path:file||null,annotationId:target?noteFor(target)?.id:null,target,scope});}
async function sourceChanged(){const s=$('source').value;$('sessionField').hidden=s!=='session';$('snapshotField').hidden=s!=='snapshot';$('roundField').hidden=s!=='round';document.querySelectorAll('.turn-field').forEach(e=>e.hidden=s!=='session');}
async function refresh(){
  clearTimeout(autosaveTimer);
  if(savePromise)try{await savePromise;}catch{/* Keep drafts available for refresh after a failed save. */}
  if(busy)return;syncNote();const q=readScope();if(dirty&&!equal(q,scope))await ensureSaved();
  if(q.source==='session'&&!q.session)throw Error('没有可读会话，请先记录快照或选择其他来源。');if(q.source==='snapshot'&&!q.snapshot)throw Error('请先记录快照。');if(q.start&&q.end&&Number(q.start)>Number(q.end))throw Error('起始轮次不能大于结束轮次。');
  if(q.source==='round'&&!q.round)throw Error('还没有已完成的单轮快照，请让助手在修改前后记录。');
  const old=dirty?structuredClone(receipts):null,oldSeen=seen,selected=currentGroup()?.key;busy=true;renderSummary();
  try{const [c,s]=await Promise.all([api('changes',q),api('state')]);if(dirty&&s.revision!==baseRevision)throw Error('其他窗口已更新记录；本地草稿保留，请先核对，或在选项中重新读取已保存记录。');
    changes=c;state=s;scope=q;baseRevision=s.revision;seen=Object.fromEntries(c.records.map(r=>[r.path,q.source==='round'?r.afterHash:r.currentHash]));buildReceipts();
    if(old)for(const r of receipts){const previous=old.find(x=>x.groupKey===r.groupKey);if(previous&&oldSeen[r.refs[0].path]===seen[r.refs[0].path]&&!r.stale){r.status=previous.status;r.confirmApproval=previous.confirmApproval;}else if(previous&&!r.id){r.status='pending';r.stale=true;r.confirmApproval=false;}}
    buildGroups(selected);scopeExplanation();$('notice').hidden=true;$('scopeLabel').textContent=describe(scope)+` · ${allPaths().length} 文件`;
    const round=changes.round;$('scopeEndpoints').hidden=!round;$('scopeEndpoints').textContent=round?'固定轮前 → 轮后 · '+new Date(round.startedAt).toLocaleString('zh-CN')+' → '+new Date(round.completedAt).toLocaleString('zh-CN'):'';
    $('scopeEndpoints').title=round?`请求：${round.request}\n轮次 ID：${round.turnId}\n轮前：${round.before}\n轮后：${round.after}`:'';
    $('currentView').textContent=round?'本轮修改后':'当前文件';$('footerText').textContent='仅本机 · 代码只读 · 意见编号不随分组切换';$('lastCheck').textContent='检查于 '+new Date().toLocaleTimeString('zh-CN');renderItems();await renderFiles();
    if(!target)setTarget(navTarget()||generalTarget(),currentGroup()?.refs||[]);else renderEditor();await publishSelection();if(dirty)remember();
  }finally{busy=false;renderItems();if(dirty)scheduleAutosave();}
}
async function save(){
  clearTimeout(autosaveTimer);
  if(savePromise){await savePromise;return dirty?save():undefined;}
  if(busy)throw Error('正在读取修改，请稍后再试。');
  if(!receipts.length||(!dirty&&!receipts.some(r=>!r.id)))return;
  syncNote();const version=editVersion,sentReceipts=structuredClone(receipts);
  const payload={revision:baseRevision,items:sentReceipts.map(r=>({...r,id:r.id??null,seenHashes:{...seen},seenDiff:changes.fingerprint})),annotations:structuredClone([...drafts.values()])};
  busy=true;saveError=null;
  savePromise=(async()=>{
    try{
      const saved=await api('batch',payload);
      // Input and navigation remain usable while a write is in flight.
      syncNote();const newer=editVersion!==version,latestReceipts=receipts,latestDrafts=new Map(drafts);
      // Reverting to the old text during a write must undo the in-flight text too.
      if(newer)for(const sent of payload.annotations){
        const key=keyFor(sent.target);
        if(!latestDrafts.has(key))latestDrafts.set(key,{...sent,text:noteFor(sent.target)?.text||''});
      }
      state=saved;baseRevision=state.revision;buildReceipts();drafts.clear();dirty=newer;
      if(newer){
        for(const r of receipts){
          const latest=latestReceipts.find(x=>x.groupKey===r.groupKey),sent=sentReceipts.find(x=>x.groupKey===r.groupKey);
          if(latest&&sent&&['status','stale','confirmApproval'].some(k=>latest[k]!==sent[k])){
            for(const k of ['status','stale','confirmApproval'])r[k]=latest[k];
          }
        }
        for(const [key,draft] of latestDrafts){
          const persisted=noteFor(draft.target);
          if(draft.text!==(persisted?.text||''))drafts.set(key,{...draft,id:persisted?.id??null});
        }
        remember();
      }else preferences.removeItem(draftKey());
      if($('notice').dataset.saveError){$('notice').hidden=true;delete $('notice').dataset.saveError;toast('已恢复自动保存');}
      renderItems();renderEditor();
    }catch(e){
      saveError=e.message;remember();$('notice').dataset.saveError='true';throw e;
    }finally{
      busy=false;savePromise=null;renderSummary();if(dirty&&!saveError)scheduleAutosave();
    }
  })();
  renderSummary();return savePromise;
}
async function ensureSaved(){
  clearTimeout(autosaveTimer);
  if(savePromise)await savePromise;
  while(receipts.length&&(dirty||receipts.some(r=>!r.id)))await save();
}
async function submitFeedback(){
  if(submitting)return;submitting=true;renderItems();renderEditor();
  try{
    await ensureSaved();
    await ReviewHost.message('请读取审查项目 '+JSON.stringify(meta.root)+' 的 .code-review 中已保存的审查意见，结合本对话和相关代码提出具体修改方案。先等我在聊天中确认方案，再修改代码。');
    toast('意见已提交；助手先提方案，等你确认后再修改。');
  }finally{submitting=false;renderItems();renderEditor();}
}
function showText(title,text){$('dialogTitle').textContent=title;$('dialogText').value=text;$('dialog').showModal();}
async function copy(text){try{await navigator.clipboard.writeText(text);toast('已复制');}catch{showText('复制文本',text);}}
function snapshots(){$('snapshot').innerHTML=meta.snapshots.map(s=>`<option value="${s.id}">${esc(s.name)} · ${s.count} 文件</option>`).join('');}
async function init(){
  await ReviewHost.initialize();
  navMode=preferences.getItem('review-navigation')||navMode;mode=preferences.getItem('review-code-layout')||mode;
  const hostActions=()=>{$('expandReview').hidden=!ReviewHost.embedded||!ReviewHost.hostContext.availableDisplayModes?.includes('fullscreen');};
  window.addEventListener('review-host-context',hostActions);hostActions();
  $('sendFeedback').hidden=!ReviewHost.embedded||!ReviewHost.canMessage;
  if(ReviewHost.embedded&&ReviewHost.canMessage){$('copyLocation').textContent='当前文件（原生）';$('copyLocation').title='查看当前文件；当前审查范围的 diff 请使用跳转 diff';}
  bind('expandReview','click',()=>ReviewHost.fullscreen());
  bind('sendFeedback','click',submitFeedback);
  meta=await api('meta');state=await api('state');$('projectName').textContent=meta.root.split(/[\\/]/).pop();$('session').innerHTML=meta.sessions.map(s=>`<option value="${s.id}">${esc(new Date(s.time).toLocaleString('zh-CN'))}</option>`).join('');snapshots();
  $('round').innerHTML=(meta.rounds||[]).filter(r=>r.status==='completed').map(r=>`<option value="${r.id}">${esc(r.label)} · ${r.changedFiles} 文件</option>`).join('');
  for(const id of ['session','start','end'])bind(id,'change',()=>{scopeTurnId='';});
  if(meta.reviewScope?.query){applyScope(meta.reviewScope.query);await sourceChanged();await refresh();}
  else if(meta.reviewScope){
    $('session').insertAdjacentHTML('afterbegin','<option value="">等待助手设置范围</option>');$('session').value='';await sourceChanged();
    scope=readScope();changes={records:[],fingerprint:''};seen={};baseRevision=state.revision;buildReceipts();buildGroups();renderItems();await renderFiles();scopeExplanation();$('scopeLabel').textContent='尚未绑定比较范围';
  }else{if(meta.defaultSession)$('session').value=meta.defaultSession;else if(!meta.sessions.length)$('source').value=meta.snapshots.length?'snapshot':'git';await sourceChanged();await refresh();}
  const stored=preferences.getItem(draftKey());if(stored){try{const d=JSON.parse(stored);if(equal(d.scope,scope)){drafts=new Map(d.drafts);for(const r of receipts){const old=d.receipts.find(x=>x.groupKey===r.groupKey);if(!old)continue;if(d.seenDiff===changes.fingerprint&&d.seen[r.refs[0].path]===seen[r.refs[0].path]&&!r.stale){r.status=old.status;r.confirmApproval=old.confirmApproval;}else{r.stale=true;r.confirmApproval=false;if(!r.id)r.status='pending';}}baseRevision=d.baseRevision;dirty=true;navMode=d.navMode;buildGroups(d.activeKey);renderItems();await renderFiles();target=null;setTarget(d.target,d.targetRefs);toast('已恢复草稿；变化的代码需重新确认。');}}catch{toast('草稿无法恢复，已保留原始缓存。');}}
  bind('useSuggestedScope','click',async()=>{if(dirty||savePromise)await ensureSaved();target=null;applyScope(meta.reviewScope.query);await sourceChanged();await refresh();});
  bind('settingsToggle','click',()=>{$('settings').hidden=!$('settings').hidden;$('settingsToggle').setAttribute('aria-expanded',String(!$('settings').hidden));});
  bind('discardDrafts','click',async()=>{clearTimeout(autosaveTimer);if(savePromise)try{await savePromise;}catch{}saveError=null;preferences.removeItem(draftKey());drafts.clear();dirty=false;target=null;await refresh();});
  bind('toggleCode','click',()=>{document.body.classList.toggle('compact');$('toggleCode').textContent=document.body.classList.contains('compact')?'浏览修改':'仅审查清单';});
  bind('source','change',sourceChanged);bind('applyScope','click',async()=>{const same=equal(readScope(),scope);if(!same&&!dirty)target=null;await refresh();});bind('refresh','click',refresh);bind('retrySave','click',async()=>{await ensureSaved();$('moreMenu').open=false;});
  bind('notes','input',()=>{syncNote();markDirty();renderEditor();});
  document.querySelectorAll('[data-nav]').forEach(b=>b.addEventListener('click',()=>{syncNote();navMode=b.dataset.nav;preferences.setItem('review-navigation',navMode);buildGroups();selectItem(0).catch(fail);if(dirty)remember();}));
  bind('itemList','click',e=>{const b=e.target.closest('[data-item]');if(b)return selectItem(Number(b.dataset.item));});
  bind('itemList','change',async e=>{const c=e.target.closest('[data-check]');if(!c)return;syncNote();const ps=new Set(groups[Number(c.dataset.check)].refs.map(r=>r.path));for(const r of receipts)if(ps.has(r.refs[0].path)){r.status=c.checked?'approved':'changes';r.stale=false;r.confirmApproval=c.checked;}markDirty();await selectItem(Number(c.dataset.check));});
  bind('files','click',async e=>{const b=e.target.closest('[data-path]');if(b){syncNote();file=b.dataset.path;await renderFiles();renderEditor();await publishSelection();}});
  bind('commentTarget','change',()=>{const v=$('commentTarget').value;if(v==='group')setTarget(navTarget(),currentGroup()?.refs||[]);else if(v==='file')setTarget(fileTarget(),[{path:file}]);else if(v==='general')setTarget(generalTarget(),allPaths().map(path=>({path})));return publishSelection();});
  bind('noteList','click',async e=>{const b=e.target.closest('[data-note]'),n=b&&notes().find(n=>n.id===Number(b.dataset.note));if(!n)return;setTarget(n.target,n.refs);if(n.refs[0]){const group=groups.findIndex(g=>g.refs.some(r=>r.path===n.refs[0].path));if(group>=0){active=group;file=n.refs[0].path;renderItems();await renderFiles();if(n.refs[0].line&&!viewer.locate(n.refs[0].line,n.refs[0].side||'after',n.refs[0].end||n.refs[0].line))toast('原定位已变化，请结合完整文件复核意见。');}}await publishSelection();});
  bind('code','click',async e=>{const symbol=e.target.closest('[data-symbol]'),cell=e.target.closest('[data-line]');if(!symbol&&!cell)return;const row=e.target.closest('tr'),lineCell=symbol?symbol.closest('td'):cell;const line=Number(symbol?lineCell?.dataset.sourceLine:lineCell?.dataset.line);if(!line)return;const side=lineCell.dataset.side||'after';let start=line,end=line;if(e.shiftKey&&target?.kind==='line'&&target.path===file&&target.side===side){start=Math.min(target.line,line);end=Math.max(target.end||target.line,line);}const t=symbol?{kind:'symbol',key:file+':'+side+':'+line+':'+symbol.dataset.symbol,label:(symbol.dataset.symbolKind==='class'?'类 ':'符号 ')+symbol.dataset.symbol,path:file,line,side}:{kind:'line',key:file+':'+side+':'+start+':'+end,label:file+` · ${side==='before'?'旧侧':'当前'} ${start}${end>start?'–'+end:''} 行`,path:file,line:start,end,side};setTarget(t,[{path:file,line:start,end,side}]);$('code').querySelectorAll('.annotation-anchor').forEach(r=>r.classList.remove('annotation-anchor'));row?.classList.add('annotation-anchor');$('notes').focus();await publishSelection();});
  bind('fileSearch','input',renderFiles);for(const [id,value] of [['patchView','single'],['splitView','split'],['currentView','current']])bind(id,'click',()=>{mode=value;preferences.setItem('review-code-layout',mode);return renderCode();});
  bind('prevChange','click',()=>viewer.jump(viewer.index<0?viewer.blocks.length-1:viewer.index-1));bind('nextChange','click',()=>viewer.jump(viewer.index+1));bind('changeSelect','change',()=>{if(Number($('changeSelect').value)>=0)viewer.jump(Number($('changeSelect').value));});
  bind('jumpDiff','click',async()=>{if(mode==='current'){mode='single';preferences.setItem('review-code-layout',mode);await renderCode();}viewer.jump(Math.max(0,viewer.index));});
  bind('copyLocation','click',()=>{const path=meta.root.replace(/\\/g,'/')+'/'+file;return ReviewHost.embedded&&ReviewHost.canMessage?ReviewHost.message('请仅使用 Codex 原生文件面板查看此文件，不修改代码：'+JSON.stringify(path)):copy(path);});bind('sourceHelp','click',async()=>{
    let detail='';
    if(scope.source==='session'&&scope.session){const s=await api('session?id='+encodeURIComponent(scope.session)+(scope.threadId?'&threadId='+encodeURIComponent(scope.threadId):''));detail='\n\n所选日志段轮次：\n'+s.turns.map(t=>`第 ${t.number} 轮 · ${t.label||'无请求摘要'}\n${t.id||'旧日志无稳定 ID'} · ${t.patchCount||0} 个可解码补丁`).join('\n\n');}
    showText('来源与轮次','单轮修改：助手在修改前后记录固定快照；后续修改不混入，历史端点不足时不能事后还原。\n\n会话补丁：仅筛选历史操作；脚本、压缩日志和外部编辑不保证覆盖，不等于完整单轮代码。单轮数字筛选可令起始=结束。\n\n功能、目录、文件仅改变浏览方式，R 编号独立持久化。收到意见后仍先提方案、等聊天确认再改代码。'+detail);
  });
  bind('snapshotButton','click',()=>{$('snapshotDialog').showModal();$('snapshotName').focus();});
  bind('cancelSnapshot','click',()=>$('snapshotDialog').close());
  bind('captureSnapshot','click',async()=>{const s=await api('snapshot',{name:$('snapshotName').value||'修改前基线'});meta.snapshots.unshift(s);snapshots();$('snapshotDialog').close();toast(`已记录 ${s.count} 个文件`);});
  bind('export','click',async()=>{await ensureSaved();$('moreMenu').open=false;showText('编号审查意见',(await api('export')).text);});
  bind('closeDialog','click',()=>$('dialog').close());bind('copyText','click',()=>copy($('dialogText').value));bind('downloadText','click',()=>{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([$('dialogText').value],{type:'text/markdown;charset=utf-8'}));a.download='review-export.md';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);});
  document.addEventListener('keydown',e=>{if(e.key==='Escape')$('moreMenu').open=false;if((e.ctrlKey||e.metaKey)&&e.key==='s'){e.preventDefault();ensureSaved().catch(fail);}if(e.altKey&&['ArrowDown','ArrowUp'].includes(e.key)){e.preventDefault();viewer.jump(e.key==='ArrowDown'?viewer.index+1:viewer.index<0?viewer.blocks.length-1:viewer.index-1);}});
  document.addEventListener('click',e=>{if(!$('moreMenu').contains(e.target))$('moreMenu').open=false;});
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  if(dirty)scheduleAutosave();
  setInterval(async()=>{if(document.hidden||busy)return;try{const fresh=await api('state');state.workflow=fresh.workflow;for(const r of receipts){const saved=fresh.items.find(i=>i.id===r.id);if(saved?.stale&&!r.confirmApproval)r.stale=true;}if(fresh.revision===baseRevision){state.annotations=fresh.annotations;renderEditor();}renderItems();$('lastCheck').textContent='结论检查于 '+new Date().toLocaleTimeString('zh-CN');}catch{$('lastCheck').textContent='本地服务未连接';}},15000);
}
init().catch(fail);
