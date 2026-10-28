'use strict';
/* MCP Apps JSON-RPC bridge. All project data stays in the local stdio server. */
window.ReviewHost=(()=>{
  const embedded=document.querySelector('meta[name="review-transport"]')?.content==='mcp';
  const pending=new Map();let serial=0,context=null,hostContext={},hostCapabilities={},input=null;
  let resolveContext;const contextReady=new Promise(resolve=>resolveContext=resolve);
  const preferences=new Map();
  function accept(result){
    const value=result?._meta?.reviewContext;
    if(value&&typeof value.projectRoot==='string'&&typeof value.viewToken==='string'){
      if(context&&context.projectRoot!==value.projectRoot)return;
      context=value;resolveContext(value);
    }
  }
  function applyHost(value){
    hostContext={...hostContext,...value};
    if(['light','dark'].includes(hostContext.theme))document.documentElement.style.colorScheme=hostContext.theme;
    if(hostContext.locale)document.documentElement.lang=hostContext.locale;
    for(const [key,val] of Object.entries(hostContext.styles?.variables||{})){
      if(key.startsWith('--')&&typeof val==='string')document.documentElement.style.setProperty(key,val);
    }
    window.dispatchEvent(new CustomEvent('review-host-context',{detail:hostContext}));
  }
  function notify(method,params={}){window.parent.postMessage({jsonrpc:'2.0',method,params},'*');}
  function request(method,params){
    if(window.parent===window)return Promise.reject(Error('此界面需要由支持 MCP Apps 的宿主打开。'));
    const id=++serial;
    return new Promise((resolve,reject)=>{
      const timeout=setTimeout(()=>{pending.delete(id);reject(Error('应用未响应，请重新打开审查界面；未保存意见仍保留在当前界面。'));},20000);
      pending.set(id,{resolve,reject,timeout});
      window.parent.postMessage({jsonrpc:'2.0',id,method,params},'*');
    });
  }
  if(embedded)window.addEventListener('message',event=>{
    if(event.source!==window.parent)return;
    const msg=event.data;if(!msg||msg.jsonrpc!=='2.0')return;
    const p=pending.get(msg.id);
    if(p){clearTimeout(p.timeout);pending.delete(msg.id);msg.error?p.reject(Error(msg.error.message||'宿主请求失败')):p.resolve(msg.result);return;}
    if(msg.method==='ui/notifications/tool-input')input=msg.params?.arguments||msg.params;
    if(msg.method==='ui/notifications/tool-result')accept(msg.params);
    if(msg.method==='ui/notifications/host-context-changed')applyHost(msg.params||{});
    if(msg.method==='ui/resource-teardown'&&msg.id!==undefined)window.parent.postMessage({jsonrpc:'2.0',id:msg.id,result:{}},'*');
  });
  async function initialize(){
    if(!embedded)return;
    document.body.classList.add('mcp-app');
    const result=await request('ui/initialize',{protocolVersion:'2026-01-26',appInfo:{name:'Local Code Review',version:'3.0.3'},appCapabilities:{}});
    hostCapabilities=result.hostCapabilities||{};applyHost(result.hostContext||{});notify('ui/notifications/initialized');
    notify('ui/notifications/size-changed',{height:720});
    // The host delivers the render tool result, including private _meta, after initialization.
    if(!context){
      let timeout;try{await Promise.race([contextReady,new Promise((_,reject)=>{timeout=setTimeout(()=>reject(Error('宿主未提供审查项目，请重新调用 review_open。')),20000);})]);}finally{clearTimeout(timeout);}
    }
    // Drafts are an instance-local safety net. Durable opinions live on the server.
    if(typeof window.openai?.widgetState==='object'&&window.openai.widgetState){
      for(const [k,v] of Object.entries(window.openai.widgetState.reviewPreferences||{}))if(typeof v==='string')preferences.set(k,v);
    }
  }
  async function call(route,payload){
    if(!context)throw Error('审查项目尚未连接');
    const result=await request('tools/call',{name:'review_ui_request',arguments:{projectRoot:context.projectRoot,viewToken:context.viewToken,route,payload:payload||{}}});
    if(result?.isError)throw Error(result.content?.find(c=>c.type==='text')?.text||'审查操作失败');
    if(!result||!Object.hasOwn(result,'structuredContent'))throw Error('宿主返回了不完整的审查数据');
    return result.structuredContent;
  }
  async function message(text){
    if(!hostCapabilities.message)throw Error('当前宿主未声明支持对话消息，请在聊天中输入：'+text);
    const result=await request('ui/message',{role:'user',content:[{type:'text',text}]});
    if(result?.isError)throw Error('宿主未发送消息，请在聊天中输入：'+text);
  }
  async function fullscreen(){
    if(!hostContext.availableDisplayModes?.includes('fullscreen'))throw Error('当前宿主未声明支持全屏审查，可继续在此面板浏览。');
    const result=await request('ui/request-display-mode',{mode:'fullscreen'});
    if(result?.mode)applyHost({displayMode:result.mode});return result;
  }
  function persist(){
    if(embedded&&typeof window.openai?.setWidgetState==='function'){
      try{window.openai.setWidgetState({...window.openai.widgetState,reviewPreferences:Object.fromEntries(preferences)});}catch{/* The instance still retains the draft. */}
    }
  }
  const storage={
    getItem(key){if(embedded)return preferences.get(key)??null;try{return localStorage.getItem(key);}catch{return preferences.get(key)??null;}},
    setItem(key,value){preferences.set(key,String(value));if(embedded){persist();return;}try{localStorage.setItem(key,value);}catch{}},
    removeItem(key){preferences.delete(key);if(embedded){persist();return;}try{localStorage.removeItem(key);}catch{}}
  };
  return {embedded,storage,initialize,call,message,fullscreen,get input(){return input;},get hostContext(){return hostContext;},get canMessage(){return !!hostCapabilities.message;}};
})();
