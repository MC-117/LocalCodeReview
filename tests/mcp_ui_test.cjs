/* Real stdio server + sandboxed opaque-origin iframe; the host is a test double. */
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const http=require('node:http');
const {spawn}=require('node:child_process');
const {once}=require('node:events');
const readline=require('node:readline');
const {chromium}=require(process.argv[2]||'playwright');
const base=path.resolve(__dirname,'..');
(async()=>{
 let browser,host,mcp;
 const fixture=spawn(process.env.REVIEW_TEST_PYTHON||'python',[path.join(__dirname,'browser_fixture.py')],{stdio:['ignore','pipe','pipe']});
 const fixtureLines=readline.createInterface({input:fixture.stdout});
 try{
  const [line]=await once(fixtureLines,'line'),sample=JSON.parse(line);
  mcp=spawn(process.env.REVIEW_TEST_PYTHON||'python',[path.join(base,'server.py'),'--mcp','--sessions',path.join(path.dirname(sample.root),'sessions')],{stdio:['pipe','pipe','pipe']});
  const mcpLines=readline.createInterface({input:mcp.stdout}),pending=new Map();let id=0;
  mcpLines.on('line',line=>{const r=JSON.parse(line),p=pending.get(r.id);if(p){pending.delete(r.id);r.error?p.reject(Error(r.error.message)):p.resolve(r.result);}});
  const rpc=(method,params)=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});mcp.stdin.write(JSON.stringify({jsonrpc:'2.0',id:n,method,params})+'\n');});
  await rpc('initialize',{protocolVersion:'2025-06-18',capabilities:{}});
  const tools=await rpc('tools/list',{});let render=await rpc('tools/call',{name:'review_open',arguments:{projectRoot:sample.root,threadId:'browser-fixture',query:{defaults:true},reason:'助手根据样例上下文选择本轮修改'}});
  const resource=await rpc('resources/read',{uri:tools.tools.find(t=>t.name==='review_open')._meta.ui.resourceUri});
  const html=resource.contents[0].text.replace('<head>','<head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; connect-src \'none\'">');
  const messages=[],calls=[];let holdBatch=false,releaseBatch=null,failBatch=false;
  const until=async predicate=>{const deadline=Date.now()+10000;while(!predicate()){if(Date.now()>deadline)throw Error('Timed out waiting for held MCP batch');await new Promise(r=>setTimeout(r,20));}};
  host=http.createServer(async(req,res)=>{
   if(req.url==='/rpc'){
    let data='';for await(const chunk of req)data+=chunk;
    try{const params=JSON.parse(data);calls.push(params);if(params.arguments?.route==='batch'){if(failBatch){failBatch=false;throw Error('测试保存失败');}if(holdBatch){holdBatch=false;await new Promise(resolve=>releaseBatch=resolve);releaseBatch=null;}}const result=await rpc('tools/call',params);res.setHeader('Content-Type','application/json');res.end(JSON.stringify({result}));}catch(e){res.end(JSON.stringify({error:{message:e.message}}));}return;
   }
   if(req.url==='/message'){let data='';for await(const chunk of req)data+=chunk;messages.push(JSON.parse(data));res.end('{}');return;}
   res.setHeader('Content-Type','text/html; charset=utf-8');
   res.end(`<!doctype html><html><head><meta charset="utf-8"><title>MCP Apps · 本地协议验证</title><style>body{margin:0;background:#f4f3f0;font:13px 'Segoe UI';}header{padding:12px 20px;color:#686863}iframe{display:block;width:calc(100% - 32px);height:780px;margin:0 16px;border:1px solid #ddd;border-radius:10px;background:#fff}</style></head><body><header>MCP Apps 协议测试宿主 · 独立样例（非 Codex 原生面板）</header><iframe sandbox="allow-scripts allow-downloads" title="代码审查"></iframe><script>
   const frame=document.querySelector('iframe');let widgetState={};
   window.addEventListener('message',async event=>{
    if(event.source!==frame.contentWindow)return;
    const m=event.data;if(!m||m.jsonrpc!=='2.0')return;let result={},error;
    const send=value=>frame.contentWindow.postMessage(value,'*');
    if(m.method==='ui/initialize')result={protocolVersion:'2026-01-26',hostInfo:{name:'Test host',version:'1'},hostCapabilities:{serverTools:{},message:{text:{}}},hostContext:{theme:'light',locale:'zh-CN',availableDisplayModes:['inline','fullscreen'],displayMode:'inline'}};
    else if(m.method==='ui/notifications/initialized'){send({jsonrpc:'2.0',method:'ui/notifications/tool-result',params:${JSON.stringify(render)}});return;}
    else if(m.method==='tools/call'){const response=await(await fetch('/rpc',{method:'POST',body:JSON.stringify(m.params)})).json();result=response.result;error=response.error;}
    else if(m.method==='ui/message'){await fetch('/message',{method:'POST',body:JSON.stringify(m.params)});}
    else if(m.method==='ui/request-display-mode'){result={mode:m.params.mode};frame.style.height='calc(100vh - 52px)';}
    else if(m.id===undefined)return;
    if(m.id!==undefined)send({jsonrpc:'2.0',id:m.id,...(error?{error}:{result})});
   });
   frame.srcdoc=${JSON.stringify(html).replace(/<\/script/gi,'<\\/script')};
   </script></body></html>`);
  });
  host.listen(0,'127.0.0.1');await once(host,'listening');const url='http://127.0.0.1:'+host.address().port+'/';
  const profile=path.join(base,'.test-artifacts','browser-profiles','mcp-'+Date.now());await fs.mkdir(profile,{recursive:true});
  browser=await chromium.launchPersistentContext(profile,{headless:true,executablePath:process.argv[3]||'C:/Program Files/Google/Chrome/Application/chrome.exe',viewport:{width:1380,height:960}});
  const page=await browser.newPage(),errors=[],requests=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>requests.push(r.url()));
  await page.goto(url);const app=page.frameLocator('iframe');
  await app.locator('[data-check="3"]').waitFor();
  assert.match(await app.locator('#scopeReason').innerText(),/助手根据样例上下文/);
  assert.equal(render.structuredContent.reviewScope.threadId,'browser-fixture');
  const second=await rpc('tools/call',{name:'review_open',arguments:{projectRoot:sample.root,threadId:'browser-fixture',query:{include:'src/06-languages/*'},reason:'另一个面板仅看多语言示例'}});
  assert.notEqual(second._meta.reviewContext.viewToken,render._meta.reviewContext.viewToken);
  const originalMeta=await rpc('tools/call',{name:'review_ui_request',arguments:{...render._meta.reviewContext,route:'meta'}});
  assert.equal(originalMeta.structuredContent.reviewScope.query.include,undefined);
  assert.equal(originalMeta.structuredContent.reviewScope.threadId,'browser-fixture');
  await app.locator('.full-file .source').filter({hasText:'// unchanged context 179'}).waitFor();
  assert.equal(await app.locator('textarea:visible').count(),1);
  assert.equal(await app.locator('.full-file .source').filter({hasText:'// unchanged context 179'}).count(),1);
  await app.locator('#splitView').click();await app.locator('.split-table').waitFor();
  assert.equal(await app.locator('.full-file .source').filter({hasText:'// unchanged context 179'}).count(),2);
  assert(await app.locator('.tok-comment').count()>100);
  await app.locator('#nextChange').click();assert.match(await app.locator('#changeCount').innerText(),/2 \/ 3/);
  await app.locator('#currentView').click();await app.locator('#jumpDiff').click();
  await app.locator('#patchView[aria-pressed="true"]').waitFor();
  assert.match(await app.locator('#changeCount').innerText(),/1 \/ 3/);
  assert.equal(await app.locator('.focused-change').count(),1);
  assert.equal(await app.locator('#save').count(),0);
  assert.equal(await app.locator('#sendFeedback').innerText(),'提交意见给助手');
  assert.equal(await app.locator('#export').isVisible(),false);
  const saved=()=>app.locator('#saveState').filter({hasText:'已自动保存'}).waitFor();
  holdBatch=true;
  await app.locator('#notes').fill('R1：整个接口与加载功能保留原接口；请先提方案。');
  await until(()=>releaseBatch);
  await app.locator('#notes').fill('R1：整个接口与加载功能保留原接口；保存中继续输入也必须保留。');
  await app.locator('[data-check="1"]').uncheck();
  releaseBatch();await saved();
  const call=(route,payload)=>rpc('tools/call',{name:'review_ui_request',arguments:{...render._meta.reviewContext,route,payload}});
  const state=(await call('state')).structuredContent;assert.equal(state.annotations[0].id,1);assert.match(state.annotations[0].text,/继续输入/);assert.equal(state.annotations.length,1);assert.equal(state.items.filter(i=>i.status==='changes').length,2);assert.equal(messages.length,0,'autosave never sends chat messages');
  await app.locator('[data-item="0"]').click();
  holdBatch=true;await app.locator('#notes').fill('保存中又撤销的临时意见');await until(()=>releaseBatch);
  await app.locator('#notes').fill(state.annotations[0].text);releaseBatch();await saved();
  assert.equal((await call('state')).structuredContent.annotations[0].text,state.annotations[0].text,'reverting during a write preserves the reverted text');
  await app.locator('[data-nav="directory"]').click();await app.locator('[data-nav="file"]').click();
  assert.equal((await call('state')).structuredContent.annotations[0].id,1);
  await app.locator('#expandReview').click();assert.equal(await page.locator('iframe').evaluate(e=>e.style.height),'calc(-52px + 100vh)');
  await app.locator('#notes').fill('功能意见：提交时必须保存最后输入');
  await app.locator('#sendFeedback').click();await app.locator('#toast').filter({hasText:'意见已提交'}).waitFor();assert((await call('state')).structuredContent.annotations.some(n=>n.text==='功能意见：提交时必须保存最后输入'));assert.match(messages[0].content[0].text,/确认方案/);
  const workflow=JSON.parse(await fs.readFile(path.join(sample.data,'workflow.json'),'utf8').catch(()=>'{}'));assert.notEqual(workflow.status,'confirmed');
  await app.locator('#copyLocation').click();await page.waitForFunction(()=>true);assert.equal(messages.length,2);assert.match(messages[1].content[0].text,/不修改代码/);
  await app.locator('#settingsToggle').click();await app.locator('#snapshotButton').click();await app.locator('#snapshotName').fill('MCP 沙箱内基线');await app.locator('#captureSnapshot').click();await app.locator('#toast').filter({hasText:'已记录'}).waitFor();assert((await call('meta')).structuredContent.snapshots.some(s=>s.name==='MCP 沙箱内基线'));await app.locator('#settingsToggle').click();
  await app.locator('#settingsToggle').click();await app.locator('#include').fill('src/06-languages/*');await app.locator('#applyScope').click();
  await app.locator('#scopeLabel').filter({hasText:'3 文件'}).waitFor();assert.match(await app.locator('#scopeReason').innerText(),/手动调整/);
  await app.locator('#useSuggestedScope').click();await app.locator('#scopeLabel').filter({hasText:'9 文件'}).waitFor();assert.match(await app.locator('#scopeReason').innerText(),/助手根据样例上下文/);await app.locator('#settingsToggle').click();
  await app.locator('#moreMenu > summary').click();await app.locator('#export').click();await app.locator('#dialog[open]').waitFor();assert.match(await app.locator('#dialogText').inputValue(),/意见 R1/);await app.locator('#closeDialog').click();
  // A failed write must preserve the input and block sending a stale review.
  failBatch=true;await app.locator('#notes').fill('离线期间的意见必须保留');
  await app.locator('#saveState').filter({hasText:'保存失败'}).waitFor();
  assert.equal(await app.locator('#notes').inputValue(),'离线期间的意见必须保留');
  failBatch=true;await app.locator('#sendFeedback').click();
  await app.locator('#notice').filter({hasText:'测试保存失败'}).waitFor();
  assert.equal(messages.length,2,'failed save prevents feedback message');
  await app.locator('#moreMenu > summary').click();await app.locator('#retrySave').click();await saved();
  assert((await call('state')).structuredContent.annotations.some(n=>n.text==='离线期间的意见必须保留'));
  await fs.appendFile(path.join(sample.root,'src/01-core/item.cpp'),'// edited after acceptance\n');await app.locator('#refresh').click();await app.locator('.status-warn').filter({hasText:'需复审'}).waitFor();
  assert.equal((await call('state')).structuredContent.annotations[0].stale,true);
  // Invalid token/route cannot become code edits or workflow confirmation.
  await assert.rejects(rpc('tools/call',{name:'review_ui_request',arguments:{...render._meta.reviewContext,route:'confirm_plan',payload:{userConfirmation:'yes'}}}));
  assert(calls.every(c=>c.name==='review_ui_request'));assert.equal(errors.length,0,errors.join('\n'));
  await page.evaluate(()=>document.querySelector('iframe').contentWindow.postMessage({jsonrpc:'2.0',method:'ui/notifications/host-context-changed',params:{theme:'dark',availableDisplayModes:['inline']}},'*'));
  await app.locator('#expandReview').waitFor({state:'hidden'});
  await page.evaluate(()=>document.querySelector('iframe').contentWindow.postMessage({jsonrpc:'2.0',method:'ui/notifications/host-context-changed',params:{theme:'light',availableDisplayModes:['inline','fullscreen']}},'*'));
  assert(requests.every(r=>r.startsWith(url)||r==='about:srcdoc'),requests.join('\n'));
  // New single-turn source uses actual begin/finish tools, with immutable complete endpoints.
  const roundCall=(arguments_)=>rpc('tools/call',{name:'review_round',arguments:{projectRoot:sample.root,...arguments_}});
  const started=(await roundCall({action:'begin',threadId:'browser-fixture',turnId:'fixture-turn',label:'本轮：脚本与中文新增'})).structuredContent;
  const mainFile=path.join(sample.root,'src/01-core/item.cpp');
  const oldText=await fs.readFile(mainFile,'utf8');
  await fs.writeFile(mainFile,oldText.replace('int current = 2;','int thisRound = 77;'));
  await fs.writeFile(path.join(sample.root,'本轮 新文件.ts'),'const greeting: string = "你好";\n');
  const finished=(await roundCall({action:'finish',roundId:started.id})).structuredContent;
  assert.equal(finished.changedFiles,2);
  await fs.appendFile(mainFile,'// LATER_UNRELATED_EDIT_MUST_NOT_APPEAR\n');
  render=await rpc('tools/call',{name:'review_open',arguments:{projectRoot:sample.root,threadId:'browser-fixture',query:{source:'round',round:'latest'},reason:'仅本轮，固定前后端点'}});
  await page.reload();
  await app.locator('#scopeLabel').filter({hasText:'本轮：脚本与中文新增'}).waitFor();
  await app.locator('#scopeEndpoints').filter({hasText:'固定轮前 → 轮后'}).waitFor();
  assert.match(await app.locator('#scopeEndpoints').getAttribute('title'),/fixture-turn/);
  await app.locator('[data-nav="file"]').click();
  await app.locator('.item-select').filter({hasText:'src/01-core/item.cpp'}).click();
  await app.locator('#code[data-file="src/01-core/item.cpp"] .source').filter({hasText:'int thisRound = 77;'}).waitFor();
  assert.equal(await app.locator('#code .source').filter({hasText:'LATER_UNRELATED_EDIT_MUST_NOT_APPEAR'}).count(),0);
  assert.equal(await app.locator('.item').filter({hasText:'src/01-core/item.cpp'}).locator('[data-check]').isChecked(),false);
  await app.locator('#splitView').click();await app.locator('.split-table').waitFor();
  assert.equal(await app.locator('.source').filter({hasText:'// unchanged context 179'}).count(),2);
  await app.locator('#nextChange').click();assert.match(await app.locator('#changeCount').innerText(),/1 \/ 1/);
  assert(await app.locator('.tok-keyword').count()>0);
  await app.locator('#currentView').click();assert.equal(await app.locator('#currentView').innerText(),'本轮修改后');
  assert.equal(await app.locator('#code .source').filter({hasText:'LATER_UNRELATED_EDIT_MUST_NOT_APPEAR'}).count(),0);
  await app.locator('#settingsToggle').click();assert.equal(await app.locator('#source').inputValue(),'round');
  assert.equal(await app.locator('#round').inputValue(),started.id);await app.locator('#settingsToggle').click();
  assert.equal((await roundCall({action:'finish',roundId:started.id})).structuredContent.after,finished.after);
  assert.equal(errors.length,0,errors.join('\n'));
  const screenshot=path.join(base,'.test-artifacts','screenshots','mcp-app.png');await page.screenshot({path:screenshot,fullPage:true});
  console.log(JSON.stringify({result:'PASS',host:'protocol test double; actual Codex integration still needs verification',checks:['immutable single-turn endpoints and later edit isolation','round source labels and selector','real MCP stdio resources','opaque-origin sandbox without localStorage','no component network requests','full split files and syntax highlighting','stable opinions','debounced durable autosave','in-flight edits and checkbox changes preserved','failed save blocks submission and can retry','overflow export menu','stale invalidation','feedback message is not confirmation','native-file request','export through MCP','fullscreen capability'],screenshot}));
 }finally{if(browser)await browser.close();if(host)host.close();if(mcp)mcp.kill();fixture.kill();fixtureLines.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
