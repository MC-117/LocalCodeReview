/* Browser regression: scroll-linked navigation using the real full-file renderer. */
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require(process.argv[2]||'playwright');
const base=path.resolve(__dirname,'..');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.argv[3]||'C:/Program Files/Google/Chrome/Application/chrome.exe'});
 try{
  const page=await browser.newPage({viewport:{width:1180,height:760}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.setContent('<!doctype html><meta charset="utf-8"><div id="status"></div><button id="prev">上一处</button><button id="next">下一处</button><div id="code" class="code" style="height:420px"></div>');
  await page.addStyleTag({content:await fs.readFile(path.join(base,'web/style.css'),'utf8')});
  await page.addScriptTag({content:await fs.readFile(path.join(base,'web/i18n.js'),'utf8')});
  await page.addScriptTag({content:"const esc=v=>String(v??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));\n"+await fs.readFile(path.join(base,'web/viewer.js'),'utf8')});
  await page.evaluate(()=>{
   window.viewer=new FullCodeViewer(document.getElementById('code'),(blocks,index)=>{
    window.progress={count:blocks.length,index};document.getElementById('status').textContent=`${index+1}/${blocks.length}`;
   });
   document.getElementById('prev').onclick=()=>viewer.jump(viewer.index-1);
   document.getElementById('next').onclick=()=>viewer.jump(viewer.index+1);
   const before=Array.from({length:400},(_,i)=>'// context '+(i+1));
   const after=[...before.slice(0,5),'const changed = 1;',...before.slice(6,180),...Array.from({length:70},(_,i)=>'const inserted'+i+' = 2;'),...before.slice(180,395),...before.slice(397)];
   const side=(lines,label)=>({lines,label,available:true,exists:true,note:'',htmlLines:lines.map(s=>`<span class="tok-comment">${s}</span>`)});
   window.sample={path:'example.js',before:side(before,'旧版本'),after:side(after,'新版本'),warnings:[],records:[],anchors:[],ops:[['equal',0,5,0,5],['replace',5,6,5,6],['equal',6,180,6,180],['insert',180,180,180,250],['equal',180,395,250,465],['delete',395,397,465,465],['equal',397,400,465,468]]};
   viewer.render(sample,'single');
  });
  const index=async n=>page.waitForFunction(n=>progress.index===n,n);
  const scrollTo=async(selector,offset=0)=>page.locator(selector).first().evaluate((row,offset)=>{
   const scroll=document.querySelector('.viewer-scroll');scroll.scrollTop=row.getBoundingClientRect().top-scroll.getBoundingClientRect().top+scroll.scrollTop-scroll.clientHeight/3+offset;
  },offset);
  const nextFrame=()=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await index(0);
  assert.equal(await page.locator('.source').count(),471,'full file includes unchanged context and deleted lines');
  assert.equal(await page.locator('.focused-change').count(),1,'only a block-start gutter marker');
  assert.equal(await page.locator('.tok-comment').count(),471,'syntax markup retained');
  assert.equal(await page.locator('.source[data-source-line="100"][data-side="after"]').evaluate(e=>e.closest('tr').classList.contains('add')),false);
  assert.equal(await page.locator('.focused-change').evaluate(e=>getComputedStyle(e).outlineStyle),'none');
  await scrollTo('[data-source-line="70"][data-side="after"]');await nextFrame();await index(0);
  await scrollTo('[data-change="1"]',3);await index(1);
  const top=await page.locator('.viewer-scroll').evaluate(e=>e.scrollTop);await nextFrame();
  assert.equal(await page.locator('.viewer-scroll').evaluate(e=>e.scrollTop),top,'tracking never scrolls back');
  await scrollTo('[data-source-line="220"][data-side="after"]',3);await nextFrame();await index(1);
  assert.equal(await page.locator('.focused-change').count(),1,'long addition does not gain per-row focus');
  await scrollTo('[data-source-line="310"][data-side="after"]',3);await nextFrame();await index(1);
  await page.locator('.viewer-scroll').evaluate(e=>e.scrollTop=e.scrollHeight);await index(2);
  await page.locator('#prev').click();await index(1);await nextFrame();await index(1);
  await page.locator('#next').click();await index(2);await page.locator('#next').click();await index(0);
  await page.locator('.viewer-scroll').hover();await page.mouse.wheel(0,4500);await index(1);
  await page.evaluate(()=>viewer.render(sample,'split'));await index(1);
  await page.waitForSelector('.split-table');assert.equal(await page.locator('.focused-change').count(),1);
  await page.locator('.viewer-scroll').evaluate(e=>e.scrollTop=e.scrollHeight);await index(2);
  await page.evaluate(()=>viewer.locate(220,'after'));await index(1);
  await page.setViewportSize({width:650,height:760});await nextFrame();await index(1);
  await page.evaluate(()=>{
   viewer.render({...sample,path:'history.js',before:{...sample.before,available:false,note:'旧侧不可恢复'},ops:[],anchors:[{start:180,end:210},{start:190,end:250},{start:5,end:6}]},'single');
  });
  assert.equal(await page.locator('.add,.del').count(),0,'unavailable baseline never makes full file added');
  assert.equal(await page.evaluate(()=>progress.count),2,'overlapping historical matches coalesce');
  await scrollTo('[data-change="1"]',3);await index(1);
  await page.evaluate(()=>{
   const stale=document.querySelector('.viewer-scroll');stale.scrollTop=0;stale.dispatchEvent(new Event('scroll'));
   viewer.render({...sample,path:'empty.js',before:{...sample.before,lines:[]},after:{...sample.after,lines:[]},ops:[]},'single');
  });
  await nextFrame();assert.deepEqual(await page.evaluate(()=>progress),{count:0,index:-1},'old listener cannot change new file state');
  await page.evaluate(()=>viewer.render({...sample,path:'deleted.js',after:{...sample.after,lines:[],exists:false},ops:[['delete',0,400,0,0]]},'split'));
  await index(0);assert.equal(await page.locator('[data-change="0"]').count(),400);
  await page.locator('.viewer-scroll').evaluate(e=>e.scrollTop=e.scrollHeight);await nextFrame();await index(0);
  await page.evaluate(()=>viewer.render({...sample,path:'added.js',before:{...sample.before,lines:[],exists:false},ops:[['insert',0,0,0,sample.after.lines.length]]},'single'));
  assert.equal(await page.locator('tr.add').count(),468,'actual added file retains added-line shading');
  await page.evaluate(()=>viewer.render(sample,'single'));await scrollTo('[data-change="1"]',3);await index(1);
  const screenshots=path.join(base,'.test-artifacts','screenshots');await fs.mkdir(screenshots,{recursive:true});
  await page.screenshot({path:path.join(screenshots,'scroll-navigation.png')});
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({result:'PASS',checks:['manual wheel and scroll tracking','unchanged gaps','long modifications','next previous wrapping','single split resize','annotation locate','pure deletion and added file','historical baseline missing','overlapping anchors','detached listener isolation','only changed lines shaded','one gutter marker','syntax highlighting retained']}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
