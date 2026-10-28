"use strict";
const $ = s => document.querySelector(s);
let cfg = null, latestLogs = [], active = false, timer = null;
const titles = {overview:'运行概览',config:'配置管理',tools:'常用工具',logs:'运行日志'};
const formatTime = s => s ? new Date(s*1000).toLocaleString('zh-CN',{hour12:false}) : '尚未扫描';
function toast(message){$('#toast').textContent=message;$('#toast').hidden=false;clearTimeout(timer);timer=setTimeout(()=>$('#toast').hidden=true,6000);}
async function api(path,body){
 const options={credentials:'same-origin',headers:{'X-Media-Bridge':'1'}};
 if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
 const response=await fetch(path,options);const data=await response.json();
 if(!response.ok){if(response.status===401&&path!='/api/login'){active=false;$('#shell').hidden=true;$('#login').hidden=false;}throw Error(data.error||'请求失败');}
 return data;
}
function view(name){Object.keys(titles).forEach(x=>{$('#view-'+x).hidden=x!==name;document.querySelector(`[data-view="${x}"]`).classList.toggle('active',x===name);});$('#page-title').textContent=titles[name];}
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>view(b.dataset.view)));
document.querySelectorAll('[data-go]').forEach(b=>b.addEventListener('click',()=>view(b.dataset.go)));
$('#login-form').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/login',{key:$('#login-key').value});$('#login-key').value='';await enter();}catch(e){$('#login-error').textContent=e.message;}});
$('#logout').addEventListener('click',async()=>{try{await api('/api/logout',{});active=false;cfg=null;$('#config-json').value='';$('#shell').hidden=true;$('#login').hidden=false;}catch(e){toast(e.message);}});
async function enter(){await loadConfig();$('#login').hidden=true;$('#shell').hidden=false;$('#login-error').textContent='';active=true;await refresh();}
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
function field(label,value,type='text'){const l=el('label','',label),i=el('input');i.type=type;if(type==='checkbox'){l.className='check';i.checked=value;l.prepend(i);}else{i.value=value??'';l.append(i);}return{label:l,input:i};}
async function loadConfig(){cfg=await api('/api/config');renderConfig();}
function renderConfig(){
 $('#cloud-url').value=cfg.cloud.url;$('#cloud-token').value=cfg.cloud.token;$('#cloud-enabled').checked=cfg.cloud.enabled;
 $('#config-json').value=JSON.stringify(cfg,null,2);
 $('#sync-config').replaceChildren();
 for(const [key,label,min,max] of [['interval_seconds','定时全量扫描间隔（秒）',10,86400],['scan_workers','目录扫描线程数',1,32],['file_workers','文件处理线程数',1,32]]){
  const f=field(label,cfg.sync[key],'number');f.input.min=String(min);f.input.max=String(max);f.input.step='1';f.input.required=true;
  f.input.addEventListener('input',()=>{cfg.sync[key]=Number(f.input.value);});$('#sync-config').append(f.label);
 }
 for(const [key,label] of [['scan_on_startup','启动或应用配置后全量扫描'],['scan_on_reconnect','文件通知重连后全量补扫']]){
  const f=field(label,cfg.sync[key],'checkbox');f.input.addEventListener('input',()=>{cfg.sync[key]=f.input.checked;});$('#sync-config').append(f.label);
 }
 $('#library-config').replaceChildren();$('#server-config').replaceChildren();
 for(const lib of cfg.libraries){const p=el('article','panel'),grid=el('div','form-grid');p.append(el('h3','',lib.name+' / '+lib.id));
  for(const [key,label,type] of [['source','视频源目录','text'],['output','软链接输出目录','text'],['cloud_path','CloudDrive 虚拟路径（可留空自动识别）','text'],['enabled','启用此媒体库','checkbox']]){const f=field(label,lib[key],type);f.input.addEventListener('input',()=>{lib[key]=type==='checkbox'?f.input.checked:f.input.value;});grid.append(f.label);}
  p.append(grid,el('p','fine','保留现有目录和命名。Linux Plex/Jellyfin 可使用 symlink；Windows Plex 文件软链接存在兼容性限制。'));$('#library-config').append(p);
 }
 for(const server of cfg.servers){const p=el('article','panel'),grid=el('div','form-grid');p.append(el('h3','',server.kind.toUpperCase()+' / '+server.id));
  const fields=[['url','服务器地址','text'],['token','API Token','password'],['enabled','启用媒体扫描通知','checkbox']];
  if(server.kind==='plex')fields.splice(2,0,['section_id','媒体库 Section ID','text']);
  for(const [key,label,type]of fields){const f=field(label,server[key],type);f.input.addEventListener('input',()=>{server[key]=type==='checkbox'?f.input.checked:f.input.value;});grid.append(f.label);}p.append(grid);$('#server-config').append(p);
 }
 for(const selector of ['#tool-library','#cover-library']){const s=$(selector),old=s.value;s.replaceChildren();cfg.libraries.forEach(l=>{const o=el('option','',l.name);o.value=l.id;s.append(o);});if([...s.options].some(o=>o.value===old))s.value=old;}
 renderTransfers();
}
async function save(config){
 const buttons=[$('#save-basic'),$('#save-json')],message=$('#config-save-status');
 buttons.forEach(b=>b.disabled=true);message.hidden=false;message.textContent='正在提交配置…';
 try{
  const job=await api('/api/config',config);
  if(job.id){
   message.textContent='配置已提交，等待当前任务结束后应用。完成前尚未写入配置文件，请勿重启服务。';
   toast('配置已提交，正在等待应用');
   while(true){
    await new Promise(resolve=>setTimeout(resolve,1000));
    if(!active)return;
    const result=await api('/api/jobs/'+job.id);
    if(result.status==='失败')throw Error(result.error||'配置应用失败');
    if(result.status==='完成')break;
    if(result.status==='运行中')message.textContent='正在保存配置并重新连接文件通知…';
   }
  }
  if(!active)return;
  await loadConfig();message.textContent='配置已保存并应用；监听端口变化需重启服务。';toast('配置已保存并应用');await refresh();
 }catch(error){message.textContent='配置保存未完成：'+error.message;throw error;}
 finally{buttons.forEach(b=>b.disabled=false);}
}
$('#save-basic').addEventListener('click',async()=>{try{cfg.cloud.url=$('#cloud-url').value;cfg.cloud.token=$('#cloud-token').value;cfg.cloud.enabled=$('#cloud-enabled').checked;await save(cfg);}catch(e){toast(e.message);}});
$('#save-json').addEventListener('click',async()=>{try{await save(JSON.parse($('#config-json').value));}catch(e){toast(e.message);}});
async function action(name,library,confirm=false){const job=await api('/api/actions',{action:name,library,confirm});toast('任务已加入队列');
 const poll=async()=>{if(!active)return;try{const j=await api('/api/jobs/'+job.id);if(j.status==='完成'||j.status==='失败'){$('#tool-result').textContent=JSON.stringify(j.result||{error:j.error},null,2);toast(j.status==='失败'?j.error:'任务已完成');await refresh();}else setTimeout(poll,1000);}catch(e){toast(e.message);}};setTimeout(poll,600);return job;
}
function bindAction(selector,name,confirmation){$(selector).addEventListener('click',async()=>{try{if(confirmation&&!window.confirm(confirmation))return;await action(name,$('#tool-library').value,!!confirmation);}catch(e){toast(e.message);}});}
$('#scan-all').addEventListener('click',()=>action('scan').catch(e=>toast(e.message)));
bindAction('#preview','preview');bindAction('#cleanup','cleanup','按当前清理策略清理托管文件？建议先预览。');bindAction('#initialize','initialize','将在已有的源目录中创建健康标记文件。确认初始化？');
function renderStatus(s){
 let media=0,metadata=0;for(const r of s.managed){if(r.kind==='metadata')metadata+=r.count;else media+=r.count;}
 $('#media-count').textContent=media.toLocaleString();$('#metadata-count').textContent=metadata.toLocaleString();$('#cloud-state').textContent=s.cloud;$('#notice-count').textContent=s.pending_notifications;
 $('#worker-state').textContent=s.busy?'● '+(s.activity?.stage||'正在处理'):'● 服务就绪';$('#worker-state').title=s.activity?.path||'';$('#queue-count').textContent=s.pending_jobs+' 个等待';$('#libraries').replaceChildren();
 for(const lib of s.libraries){const n=$('#library-template').content.firstElementChild.cloneNode(true);n.querySelector('.name').textContent=lib.name;n.querySelector('.source').textContent=lib.source;n.querySelector('.output').textContent=lib.output;n.querySelector('.mode').textContent=(lib.enabled?lib.mode.toUpperCase():'已停用');
  const scan=lib.scan?.latest,full=lib.scan?.last_full_success;
  const labels={running:'扫描中',complete:'完成',partial:'有待处理项',failed:'失败',interrupted:'中断后等待补扫'};
  const live=scan?.status==='running'&&lib.progress?.scan_id===scan.id?lib.progress:null;
  let detail='';
  if(live){
   detail=live.stage+' · 已发现 '+live.seen.toLocaleString()+' 个文件';
   if(live.total!==null)detail+=' · 已处理 '+live.processed.toLocaleString()+'/'+live.total.toLocaleString()+' · 变更 '+live.changes.toLocaleString();
   if(live.errors)detail+=' · 问题 '+live.errors;
   if(live.deferred)detail+=' · 等待稳定 '+live.deferred;
  }else if(scan?.status==='running'){
   detail=scan.seen?'扫描中 · 已发现 '+scan.seen.toLocaleString()+' 个文件':'扫描中 · 正在读取目录，等待进度';
  }else if(scan){detail=(labels[scan.status]||scan.status)+(scan.status==='partial'?'（查看日志）':'')+' · 本次发现 '+scan.seen.toLocaleString()+' 个文件';}
  n.querySelector('.last').textContent=(full?'全量成功：'+formatTime(full):'首次全量尚未完成')+(detail?' · '+detail:'');
  n.querySelector('.last').title='本次扫描的媒体和元数据文件；顶部数字为累计托管数。'+(live?.path?' 当前路径：'+live.path:'')+(scan?.error?' 扫描问题：'+scan.error:'');
  n.querySelector('.scan').disabled=!lib.enabled;n.querySelector('.scan').addEventListener('click',()=>action('scan',lib.id).catch(e=>toast(e.message)));n.querySelector('.preview').addEventListener('click',()=>{view('tools');$('#tool-library').value=lib.id;action('preview',lib.id).catch(e=>toast(e.message));});$('#libraries').append(n);
 }
 $('#jobs').replaceChildren();for(const j of [...s.jobs].reverse().slice(0,5)){const item=el('div','list-item'),c=el('div','list-content');c.append(el('span','list-title',({scan:'同步媒体库',cleanup:'清理托管文件',preview:'预览同步与清理',initialize:'初始化源标记',transfer_initialize:'初始化转存标记'})[j.action]||j.action),el('span','list-detail',formatTime(j.created)+(j.error?' · '+j.error:'')));item.append(el('span','badge',j.status),c);$('#jobs').append(item);}if(!s.jobs.length)$('#jobs').append(el('div','empty','还没有手动任务。后台会按配置自动同步。'));
}
function renderLogs(){
 $('#logs').replaceChildren();for(const r of latestLogs.filter(x=>!$('#log-filter').value||x.level===$('#log-filter').value)){const tr=el('tr');const level=el('td');level.append(el('span','badge '+r.level,r.level));tr.append(el('td','',formatTime(r.at)),level,el('td','',r.message));$('#logs').append(tr);}
 $('#recent').replaceChildren();for(const r of latestLogs.slice(0,4)){const item=el('div','list-item'),c=el('div','list-content');c.append(el('span','list-title',r.message),el('span','list-detail',formatTime(r.at)));item.append(el('span','badge '+r.level,r.level),c);$('#recent').append(item);}if(!latestLogs.length)$('#recent').append(el('div','empty','暂无运行记录。'));
}
$('#log-filter').addEventListener('change',renderLogs);
async function refresh(){if(!active)return;try{const [s,logs]=await Promise.all([api('/api/status'),api('/api/logs')]);renderStatus(s);latestLogs=logs;renderLogs();$('#refresh-time').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});}catch(e){toast(e.message);}}
$('#refresh').addEventListener('click',refresh);
$('#cover-form').addEventListener('submit',async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{const result=await api('/api/covers',{library:$('#cover-library').value,title:$('#cover-title').value,subtitle:$('#cover-subtitle').value,theme:$('#cover-theme').value});$('#cover-preview').src=result.url+'?v='+Date.now();$('#cover-download').href=result.url;$('#cover-result').hidden=false;toast('封面已生成');}catch(e){toast(e.message);}finally{button.disabled=false;}});
function renderTransfers(){$('#transfers').replaceChildren();for(const t of cfg.transfers){const item=el('div','list-item'),c=el('div','list-content');c.append(el('span','list-title',t.id+' · '+(t.enabled?'监控中':'已停用')),el('span','list-detail',t.source+' → '+t.destination+' · 稳定 '+t.stable_seconds+' 秒'));const b=el('button','secondary','初始化标记');b.addEventListener('click',()=>{if(window.confirm('在转存源目录中创建健康标记文件？'))action('transfer_initialize',t.id,true).catch(e=>toast(e.message));});item.append(c,b);$('#transfers').append(item);}if(!cfg.transfers.length)$('#transfers').append(el('div','empty','尚未配置转存任务。'));}
$('#transfer-form').addEventListener('submit',async e=>{e.preventDefault();try{const next=structuredClone(cfg);next.transfers.push({id:$('#transfer-id').value,enabled:false,source:$('#transfer-source').value,destination:$('#transfer-destination').value,stable_seconds:Number($('#transfer-stable').value)});await save(next);}catch(e){toast(e.message);}});
setInterval(()=>{if(active&&!document.hidden)refresh();},5000);
api('/api/status').then(enter).catch(()=>{});
