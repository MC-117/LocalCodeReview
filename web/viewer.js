'use strict';
/* Complete file views. Every unchanged line remains present; no collapsed context. */
class FullCodeViewer {
  constructor(root,onProgress){this.root=root;this.onProgress=onProgress;this.index=-1;this.path='';this.blocks=[];}
  detach(){
    if(this.frame)cancelAnimationFrame(this.frame);
    this.frame=null;this.observer?.disconnect();this.observer=null;
    if(this.scroll)this.scroll.removeEventListener('scroll',this.onScroll);
    this.scroll=null;this.blockRows=[];this.pinned=null;
  }
  clear(){this.detach();this.path='';this.blocks=[];this.index=-1;this.onProgress(this.blocks,this.index);}
  render(data,mode){
    this.detach();
    const same=this.path===data.path;this.path=data.path;this.data=data;this.mode=mode;
    this.index=same?this.index:-1;this.blocks=[];
    const a=data.before,b=data.after,rows=[];
    const source=(text,kind='',html=null,line=null,side='after')=>`<td class="source ${kind}"${line==null?'':` data-source-line="${line}" data-side="${side}"`}>${(html??esc(text))||' '}</td>`;
    const number=(n,side='after')=>`<td class="ln"${n==null?'':` data-line="${n}" data-side="${side}"`}>${n??''}</td>`;
    const single=(old,next,text,kind='',block=null)=>`<tr class="${kind}"${block===null?'':` data-change="${block}"`}>${number(old,'before')}${number(next)}<td class="sign">${kind==='add'?'+':kind==='del'?'−':kind==='historical'?'·':''}</td>${source(text,'',next==null?a.htmlLines?.[old-1]:b.htmlLines?.[next-1],next??old,next==null?'before':'after')}</tr>`;
    const paired=(old,next,left,right,kind='',block=null)=>`<tr${block===null?'':` data-change="${block}"`}>${number(old,'before')}${source(left,kind==='equal'?'':old==null?'gap':'del',old==null?null:a.htmlLines?.[old-1],old,'before')}${number(next)}${source(right,kind==='equal'?'':next==null?'gap':'add',next==null?null:b.htmlLines?.[next-1],next)}</tr>`;
    if(mode==='current'||!a.available||!b.available){
      const lines=b.lines||[];
      // Historical matches are navigation hints, never evidence of a full-file addition.
      const anchors=[];
      for(const range of (mode==='current'?[]:data.anchors||[]).map(r=>({start:Math.max(0,r.start),end:Math.min(lines.length,r.end)})).filter(r=>r.end>r.start).sort((a,b)=>a.start-b.start)){
        const last=anchors.at(-1);
        if(last&&range.start<=last.end)last.end=Math.max(last.end,range.end);else anchors.push({...range});
      }
      this.blocks=anchors.map((r,i)=>({index:i,label:I18n.t("历史匹配 · 当前 {0}–{1} 行", [r.start+1, r.end])}));
      lines.forEach((line,i)=>{
        const block=anchors.findIndex(r=>i>=r.start&&i<r.end),kind=block>=0?'historical':'';
        rows.push(mode==='split'?`<tr${block<0?'':` data-change="${block}"`}>${number(null)}${source('','gap')}${number(i+1)}${source(line,kind,b.htmlLines?.[i],i+1)}</tr>`:single(null,i+1,line,kind,block<0?null:block));
      });
    }else{
      for(const [tag,a0,a1,b0,b1] of data.ops){
        const block=tag==='equal'?null:this.blocks.length;
        if(block!==null)this.blocks.push({index:block,label:I18n.t("{0}. 旧 {1}–{2} / 新 {3}–{4} 行", [this.blocks.length+1, a0+1, Math.max(a0+1,a1), b0+1, Math.max(b0+1,b1)])});
        if(mode==='split'){
          for(let i=0;i<Math.max(a1-a0,b1-b0);i++)rows.push(paired(a0+i<a1?a0+i+1:null,b0+i<b1?b0+i+1:null,a0+i<a1?a.lines[a0+i]:'',b0+i<b1?b.lines[b0+i]:'',tag,block));
        }else if(tag==='equal'){
          for(let i=0;i<b1-b0;i++)rows.push(single(a0+i+1,b0+i+1,b.lines[b0+i]));
        }else{
          for(let i=a0;i<a1;i++)rows.push(single(i+1,null,a.lines[i],'del',block));
          for(let i=b0;i<b1;i++)rows.push(single(null,i+1,b.lines[i],'add',block));
        }
      }
    }
    const warning=data.warnings.map(I18n.system);
    if(mode!=='current'&&!a.available)warning.push(I18n.t("旧侧不可用：")+I18n.system(a.note));
    if(!b.available)warning.push(I18n.t("当前侧不可预览：")+I18n.system(b.note));
    const label=side=>esc(I18n.system(side.label))+(side.exists?'':I18n.t(" · 文件不存在"));
    const heads=mode==='split'?`<div class="viewer-labels split"><span>${label(a)}</span><span>${label(b)}</span></div>`:I18n.t("<div class=\"viewer-labels\"><span>{0}</span><span>完整文件 · {1} 行</span></div>", [mode==='current'?label(b):a.available?label(a)+' → '+label(b):label(b)+I18n.t(" · 历史匹配定位"), b.lines.length]);
    const history=data.records.length?I18n.t("<details class=\"history-patches\"><summary>原始会话补丁 · {0} 次</summary><div class=\"history-body\">{1}</div></details>", [data.records.length, data.records.map(r=>I18n.t("<section><div class=\"patch-heading\"><span>第 {0} 轮 · {1}{2}</span></div><pre>{3}</pre></section>", [r.turn, esc(r.action), r.confirmed?'':I18n.t(" · 无成功回执"), esc(r.patch)])).join('')]):'';
    this.root.innerHTML=(warning.length?`<div class="comparison-note">${warning.map(esc).join(' ')}</div>`:'')+history+heads+I18n.t("<div class=\"viewer-scroll\" tabindex=\"0\" aria-label=\"完整代码\"><table class=\"diff full-file {0}\"><tbody>{1}</tbody></table>{2}</div>", [mode==='split'?'split-table':'', rows.join(''), rows.length?'':`<div class="empty">${b.available?(b.exists?I18n.t("空文件"):I18n.t("当前侧文件不存在")):I18n.t("二进制或过大文件，仅提供摘要")}</div>`]);
    this.scroll=this.root.querySelector('.viewer-scroll');
    this.blockRows=this.blocks.map((_,i)=>this.root.querySelector(`[data-change="${i}"]`));
    this.onScroll=()=>this.schedulePosition();
    this.scroll.addEventListener('scroll',this.onScroll,{passive:true});
    this.observer=new ResizeObserver(()=>this.schedulePosition());
    this.observer.observe(this.scroll);this.observer.observe(this.scroll.querySelector('table'));
    const previous=this.index;
    this.index=this.blocks.length?Math.max(0,Math.min(previous,this.blocks.length-1)):-1;
    this.markPosition();this.onProgress(this.blocks,this.index);
    if(same&&previous>=0)this.jump(this.index);
  }
  relocalize(){
    const top=this.scroll?.scrollTop||0,left=this.scroll?.scrollLeft||0,index=this.index;
    const anchors=[...this.root.querySelectorAll('.annotation-anchor .source[data-source-line]')].map(e=>[e.dataset.sourceLine,e.dataset.side]);
    const historyOpen=this.root.querySelector('.history-patches')?.open;
    this.render(this.data,this.mode);
    if(this.scroll){this.scroll.scrollTop=top;this.scroll.scrollLeft=left;this.pinned={top:this.scroll.scrollTop};}
    this.index=index;this.markPosition();this.onProgress(this.blocks,this.index);
    for(const [line,side] of anchors)this.root.querySelector(`.source[data-source-line="${line}"][data-side="${side}"]`)?.closest('tr').classList.add('annotation-anchor');
    const history=this.root.querySelector('.history-patches');if(history)history.open=historyOpen;
  }
  markPosition(){
    this.root.querySelector('.focused-change')?.classList.remove('focused-change');
    // A single gutter marker is enough; do not outline every row in the block.
    this.blockRows[this.index]?.classList.add('focused-change');
  }
  schedulePosition(){
    if(this.frame)return;
    this.frame=requestAnimationFrame(()=>{this.frame=null;this.syncPosition();});
  }
  syncPosition(){
    const scroll=this.scroll;
    if(!scroll||!this.blocks.length||!scroll.clientHeight)return;
    // Preserve an explicit jump when alignment is clamped at a viewport edge.
    // The first actual scroll movement releases it; observation never scrolls back.
    if(this.pinned&&Math.abs(this.pinned.top-scroll.scrollTop)<0.5)return;
    this.pinned=null;
    const reference=scroll.getBoundingClientRect().top+scroll.clientHeight/3;
    let low=0,high=this.blockRows.length;
    while(low<high){const mid=(low+high)>>1;if(this.blockRows[mid].getBoundingClientRect().top<=reference)low=mid+1;else high=mid;}
    let index=Math.max(0,low-1);
    if(scroll.scrollTop>0&&scroll.scrollTop+scroll.clientHeight>=scroll.scrollHeight-1)index=this.blocks.length-1;
    if(index!==this.index){this.index=index;this.markPosition();this.onProgress(this.blocks,this.index);}
  }
  jump(index){
    if(!this.scroll||!this.blocks.length)return;
    this.index=(index+this.blocks.length)%this.blocks.length;
    this.markPosition();
    const target=this.blockRows[this.index],scroll=this.scroll;
    if(target){const top=target.getBoundingClientRect().top-scroll.getBoundingClientRect().top+scroll.scrollTop;scroll.scrollTop=Math.max(0,top-scroll.clientHeight/3);}
    this.pinned={top:scroll.scrollTop};
    this.onProgress(this.blocks,this.index);
  }
  locate(line,side='after',end=line){
    this.root.querySelectorAll('.annotation-anchor').forEach(e=>e.classList.remove('annotation-anchor'));
    const source=this.root.querySelector(`.source[data-source-line="${line}"][data-side="${side}"]`),scroll=this.root.querySelector('.viewer-scroll');
    if(!source||!scroll)return false;
    for(const cell of this.root.querySelectorAll(`.source[data-side="${side}"]`)){const n=Number(cell.dataset.sourceLine);if(n>=line&&n<=end)cell.closest('tr').classList.add('annotation-anchor');}
    this.pinned=null;
    scroll.scrollTop=Math.max(0,source.getBoundingClientRect().top-scroll.getBoundingClientRect().top+scroll.scrollTop-scroll.clientHeight/3);
    this.syncPosition();
    return true;
  }
}
