<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Monitor, List, Collection, DocumentChecked, Lock, Connection, Upload, Refresh, Search, ArrowRight, SwitchButton, WarningFilled, CircleCheckFilled, VideoPause, Download, Setting, CircleCloseFilled } from '@element-plus/icons-vue'
import { api, setCsrf } from './api'
import AssetManager from './AssetManager.vue'
import RedfishWorkbench from './RedfishWorkbench.vue'
import { COMPONENTS, type NodeRow, type ImageMeta, type Group, type Draft, type Job, type EventRow, type Mode } from './types'

const loggedIn=ref(false), loginKey=ref(''), loginBusy=ref(false), page=ref('workbench')
const assetManager=ref<InstanceType<typeof AssetManager>>()
const config=ref<any>({credentials_available:false})
const mode=ref<Mode>('live'), auth=ref('basic'), selfSigned=ref(false), workers=ref(2), canary=ref(false)
const revision=ref(crypto.randomUUID()), source=ref(''), loadedSource=ref(''), sourceOpen=ref(false), settingsOpen=ref(false)
const rows=ref<NodeRow[]>([]), selected=ref<NodeRow[]>([]), nodeTable=ref<any>(), images=ref<ImageMeta[]>([])
const groups=ref<Group[]>(COMPONENTS.map(component=>({component,enabled:false,imageId:'',expected:''})))
const readiness=ref({ready:false,reason:'请先加载节点、读取状态并配置所需组件'})
const jobs=ref<Job[]>([]), activeJob=ref<Job|null>(null), inspectedJob=ref<Job|null>(null), events=ref<EventRow[]>([])
const assetPage=ref(1)
const assets=ref<any[]>([]), audits=ref<any[]>([]), query=ref(''), currentPage=ref(1)
const plan=ref<any>(null), planInput=ref(''), planOpen=ref(false), confirming=ref(false), jobDrawer=ref(false)
const uploading=ref<string|null>(null), serverBusy=ref(false), now=ref(Date.now())
let syncingSelection=false
let eventStream:EventSource|null=null, eventJob='', readySerial=0, readyTimer:ReturnType<typeof setTimeout>|null=null
let tickTimer:ReturnType<typeof setInterval>, pollTimer:ReturnType<typeof setInterval>
const dirty=computed(()=>source.value.trim()!==loadedSource.value.trim())
const busy=computed(()=>serverBusy.value||jobs.value.some(j=>['queued','running'].includes(j.state))||Boolean(activeJob.value&&['queued','running'].includes(activeJob.value.state)))
const selectedGroups=computed(()=>groups.value.filter(g=>g.enabled))
const selectedNames=computed(()=>selectedGroups.value.map(g=>g.component).join('、')||'尚未勾选')
const filteredRows=computed(()=>rows.value.filter(r=>!query.value||`${r.name} ${r.address}`.toLowerCase().includes(query.value.toLowerCase())))
const pagedAssets=computed(()=>assets.value.slice((assetPage.value-1)*50,assetPage.value*50))
const pagedRows=computed(()=>filteredRows.value.slice((currentPage.value-1)*20,currentPage.value*20))
const planExpired=computed(()=>Boolean(plan.value&&now.value/1000>plan.value.expires))
const canExecute=computed(()=>readiness.value.ready&&!dirty.value&&!busy.value&&selected.value.length>0)
const pageTitles:Record<string,string>={redfish:'Redfish 资源操作',workbench:'维护工作台',jobs:'任务中心',assets:'资产统计管理',audit:'操作审计'}
const actionLabels:Record<string,string>={redfish:'Redfish 资源操作','power-read':'读取电源','asset-collect':'资产采集',read:'读取状态',precheck:'升级预检查',verify:'版本读回',upgrade:'固件升级',GracefulShutdown:'正常关机',ForceOff:'强制关机',On:'上电'}
const stateLabels:Record<string,string>={queued:'排队中',running:'执行中',completed:'已完成',failed:'检查未通过',stopped:'已停止后续任务',needs_review:'结果待核验'}
const eventProgress=computed(()=>activeJob.value?.id===eventJob?[...events.value].reverse().find(e=>typeof e.data?.progress==='number'):undefined)
const progressLabel=computed(()=>eventProgress.value?`${eventProgress.value.node} · ${eventProgress.value.data.component} · 当前组件进度（非整批完成率）`:'')

function errorMessage(error:unknown){ElMessage.error(error instanceof Error?error.message:'请求失败')}
function formatTime(value?:number){return value?new Date(value*1000).toLocaleString('zh-CN',{hour12:false}):'—'}
function bytes(value:number){return value>1048576?`${(value/1048576).toFixed(1)} MiB`:`${Math.ceil(value/1024)} KiB`}
function stateType(state:string):'success'|'warning'|'info'|'danger'{return state==='completed'?'success':['failed','needs_review'].includes(state)?'danger':state==='running'?'warning':'info'}
function invalidate(){revision.value=crypto.randomUUID();readiness.value={ready:false,reason:'配置或范围已变化，请重新执行升级预检查'};canary.value=false;queueReady()}
function queueReady(){if(readyTimer)clearTimeout(readyTimer);readyTimer=setTimeout(refreshReady,250)}
function buildDraft(withImages=true):Draft{
  if(dirty.value)throw new Error('清单输入已修改，请先点击“加载节点”')
  if(!selected.value.length)throw new Error('请先在表格中选择节点')
  const choices=withImages?selectedGroups.value.map(g=>{
    if(!g.imageId)throw new Error(`请为 ${g.component} 选择镜像`)
    return {component:g.component,image_id:g.imageId,expected:''}
  }):[]
  return {revision:revision.value,mode:mode.value,targets:selected.value.map(({name,address})=>({name,address})),images:choices,
    auth:auth.value,self_signed:selfSigned.value,workers:workers.value,canary_confirmed:canary.value}
}
async function refreshReady(){
  if(!loggedIn.value)return
  const serial=++readySerial
  if(busy.value){readiness.value={ready:false,reason:'后台有任务执行中；任务结束后请核对结果'};return}
  try{
    const draft=buildDraft(), result=await api('/api/readiness','POST',draft)
    if(serial===readySerial)readiness.value=result
  }catch(error){if(serial===readySerial)readiness.value={ready:false,reason:error instanceof Error?error.message:'尚未满足升级条件'}}
}
function imageMeta(id:string){return images.value.find(i=>i.id===id)}
function currentVersions(component:string){
  const values=[...new Set(selected.value.map(n=>n.snapshot?.inventory?.[component]?.version).filter(Boolean))]
  if(!values.length)return '未读取'
  return values.join(' / ')+(selected.value.some(n=>!n.snapshot?.inventory?.[component]?.version)?'（部分未读）':'')
}
function selectionChanged(selection:NodeRow[]){
  if(syncingSelection)return
  if(JSON.stringify(selection.map(r=>r.address))===JSON.stringify(selected.value.map(r=>r.address)))return
  selected.value=[...selection];invalidate()
}
async function restoreTableSelection(){
  // v-if navigation remounts ElTable; reflect the retained scope in its new store.
  const addresses=new Set(selected.value.map(row=>row.address))
  await nextTick()
  syncingSelection=true
  try{
    nodeTable.value?.clearSelection()
    for(const row of rows.value){if(addresses.has(row.address))nodeTable.value?.toggleRowSelection(row,true)}
  }finally{syncingSelection=false}
}
async function installRows(targets:NodeRow[]){
  rows.value=targets;selected.value=[];query.value='';currentPage.value=1
  loadedSource.value=targets.map(n=>`${n.name} ${n.address}`).join('\n');source.value=loadedSource.value
  invalidate();await nextTick();nodeTable.value?.clearSelection()
  if(targets[0])nodeTable.value?.toggleRowSelection(rows.value[0],true)
}
async function loadSource(){try{const result=await api('/api/targets/parse','POST',{text:source.value});await installRows(result.targets);sourceOpen.value=false;await refreshAssets();ElMessage.success('清单已加载，默认只选择首台；尚未连接 BMC')}catch(error){errorMessage(error)}}
async function importNodes(file:any){
  try{const data=new FormData();data.append('file',file.raw);const result=await api('/api/targets/import','POST',data);await installRows(result.targets);sourceOpen.value=false;await refreshAssets();ElMessage.success(`已导入 ${result.targets.length} 台，默认只选首台`)}catch(error){errorMessage(error)}
}
async function uploadImage(file:any,group:Group){
  if(file.size>64*1024*1024){ElMessage.error('单个镜像不能超过 64 MiB');return}
  try{uploading.value=group.component;const body=new FormData();body.append('file',file.raw)
    const result:ImageMeta=await api('/api/images','POST',body);images.value=[result,...images.value.filter(i=>i.id!==result.id)]
    group.imageId=result.id;ElMessage.success('镜像已导入本机服务器，并未上传到 BMC；请显式勾选组件')
  }catch(error){errorMessage(error)}finally{uploading.value=null}
}
async function refreshAssets(){
  if(!loggedIn.value)return
  assets.value=await api('/api/assets?mode='+mode.value)
  const index=new Map(assets.value.map(a=>[a.address,a]))
  for(const row of rows.value){const asset=index.get(row.address);row.snapshot=asset?.snapshot;row.observed=asset?.observed}
}
async function refreshJobs(){if(loggedIn.value)jobs.value=await api('/api/jobs')}
async function refreshAudit(){if(loggedIn.value)audits.value=await api('/api/audit')}
async function readOnly(action:'read'|'power-read'|'precheck'|'verify'){
  try{
    const needsImages=action==='precheck'||action==='verify'
    const draft=buildDraft(needsImages)
    if(needsImages&&!draft.images.length)throw new Error('请先勾选并配置需要处理的 DCU 组')
    if(mode.value==='live')await ElMessageBox.confirm(`仅对以下 ${draft.targets.length} 台执行${actionLabels[action]}：\n${draft.targets.map(t=>`${t.name} (${t.address})`).join('\n')}\n\n只读操作，不关机、不上传固件、不激活。`,'确认只读连接',{confirmButtonText:'确认只读连接',cancelButtonText:'取消',type:'info'})
    const job:Job=await api('/api/jobs','POST',{action,draft});await trackJob(job);ElMessage.success('只读任务已提交到后台')
  }catch(error){if(error!=='cancel'&&error!=='close')errorMessage(error)}
}
async function preparePlan(action:string){
  try{
    const draft=buildDraft(action==='upgrade')
    if(action==='upgrade'&&!canExecute.value)throw new Error(readiness.value.reason)
    plan.value=await api('/api/plans','POST',{action,draft});planInput.value='';planOpen.value=true
  }catch(error){errorMessage(error)}
}
async function confirmPlan(){
  if(!plan.value||planInput.value!==plan.value.phrase||planExpired.value)return
  try{confirming.value=true;const job:Job=await api(`/api/plans/${plan.value.id}/confirm`,'POST',{phrase:planInput.value})
    planOpen.value=false;planInput.value='';readiness.value={ready:false,reason:'本次确认已消费；操作后必须重新预检查'}
    await trackJob(job);ElMessage.success('已确认本次范围，后台开始执行；不会自动执行下一步')
  }catch(error){errorMessage(error)}finally{confirming.value=false}
}
async function trackJob(job:Job){
  activeJob.value=job;inspectedJob.value=job;events.value=[]
  await refreshJobs();subscribe(job.id)
}
function subscribe(id:string,trackActive=true){
  eventStream?.close();eventJob=id
  const stream=new EventSource(`/api/jobs/${id}/events`);eventStream=stream
  stream.addEventListener('record',(event:MessageEvent)=>{
    if(eventJob!==id)return
    const row:EventRow=JSON.parse(event.data);events.value.push(row);if(events.value.length>1000)events.value.shift()
    const node=rows.value.find(n=>n.address===row.address)
    if(node&&trackActive&&activeJob.value?.mode===mode.value){node.status=row.data.status||row.event;if(row.data.snapshot){node.snapshot=row.data.snapshot;node.observed=row.at}
      if(row.data.power&&node.snapshot)node.snapshot.power=row.data.power
      if(typeof row.data.progress==='number')node.progress=row.data.progress}
  })
  stream.addEventListener('done',async()=>{
    if(eventJob!==id)return
    stream.close()
    try{const job:Job=await api('/api/jobs/'+id);if(trackActive)activeJob.value=job;if(inspectedJob.value?.id===id)inspectedJob.value=job;await refreshJobs();await refreshAssets();await refreshReady();await refreshAudit();await assetManager.value?.refresh()}catch(error){errorMessage(error)}
  })
  // Native EventSource reconnects using Last-Event-ID; closing the page never stops a worker.
}
async function inspectJob(job:Job){
  try{inspectedJob.value=await api('/api/jobs/'+job.id);jobDrawer.value=true;events.value=[];subscribe(job.id,job.id===activeJob.value?.id)}catch(error){errorMessage(error)}
}
async function stopJob(job?:Job|null){
  const target=job||activeJob.value
  if(!target)return
  try{const result=await api(`/api/jobs/${target.id}/stop`,'POST',{});ElMessage.warning(result.message);await refreshJobs()}catch(error){errorMessage(error)}
}
function download(job:Job,kind='report'){window.open(`/api/jobs/${job.id}/files/${kind}`,'_blank','noopener,noreferrer')}
function exportJson(value:any,name:string){const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
async function saveWorkspace(){try{const value=await api('/api/workspaces/export','POST',buildDraft());exportJson(value,'BMC维护配置.json')}catch(error){errorMessage(error)}}
async function restoreWorkspace(file:any){
  try{
    if(file.size>1024*1024)throw new Error('配置文件不能超过 1 MiB')
    const result=await api('/api/workspaces/restore','POST',JSON.parse(await file.raw.text()))
    const draft:Draft=result.draft;mode.value=draft.mode;auth.value=draft.auth;selfSigned.value=draft.self_signed;workers.value=draft.workers
    groups.value=COMPONENTS.map(component=>{const choice=draft.images.find(i=>i.component===component);return {component,enabled:Boolean(choice),imageId:choice?.image_id||'',expected:choice?.expected||''}})
    await installRows(draft.targets);canary.value=false;await refreshAssets();ElMessage.success('配置已恢复，默认只选首台；未恢复预检查或执行许可')
  }catch(error){errorMessage(error)}
}
async function maintainAssets(nodes:NodeRow[]){
  page.value='workbench';await nextTick();await installRows(nodes);await refreshAssets()
  ElMessage.success('所选资产已载入维护工作台，默认仅选首台；未连接或操作设备')
}
async function bootstrap(){
  try{
    const token=new URLSearchParams(location.hash.slice(1)).get('bootstrap')
    if(token){history.replaceState(null,'',location.pathname);const result=await api('/api/session','POST',{key:token,bootstrap:true});setCsrf(result.csrf)}
    else {const result=await api('/api/session');setCsrf(result.csrf)}
    loggedIn.value=true;await afterLogin()
  }catch{loggedIn.value=false}
}
async function login(){try{loginBusy.value=true;const result=await api('/api/session','POST',{key:loginKey.value});setCsrf(result.csrf);loggedIn.value=true;loginKey.value='';await afterLogin()}catch(error){errorMessage(error)}finally{loginBusy.value=false;loginKey.value=''}}
async function afterLogin(){
  config.value=await api('/api/config');mode.value=config.value.mode==='demo'?'demo':'live';images.value=await api('/api/images');await refreshJobs();await refreshAssets();await refreshAudit()
  const running=jobs.value.find(j=>['queued','running'].includes(j.state))
  if(running){const job:Job=await api('/api/jobs/'+running.id);await trackJob(job)}
}
async function logout(){
  try{await ElMessageBox.confirm('退出登录不会停止后台任务。是否继续？','退出本机工作台',{type:'info'});await api('/api/session','DELETE');eventStream?.close();setCsrf('');loggedIn.value=false}catch(error){if(error!=='cancel'&&error!=='close')errorMessage(error)}
}
watch([mode,auth,selfSigned,workers],()=>invalidate())
watch(groups,()=>invalidate(),{deep:true})
watch(source,()=>invalidate())
watch(canary,()=>queueReady())
watch(mode,async()=>{for(const row of rows.value){row.snapshot=undefined;row.status='数据上下文已更新，需重新读取'};await refreshAssets()})
watch(page,async value=>{try{if(value==='workbench')await restoreTableSelection();if(value==='jobs')await refreshJobs();if(value==='assets')await refreshAssets();if(value==='audit')await refreshAudit()}catch(error){errorMessage(error)}})
onMounted(()=>{bootstrap();tickTimer=setInterval(()=>{now.value=Date.now();if(loggedIn.value&&!busy.value)queueReady()},1500);pollTimer=setInterval(()=>{if(loggedIn.value)refreshJobs().catch(()=>{})},4000)})
onBeforeUnmount(()=>{clearInterval(tickTimer);clearInterval(pollTimer);if(readyTimer)clearTimeout(readyTimer);eventStream?.close()})
</script>

<template>
  <div v-if="!loggedIn" class="login-shell">
    <div class="login-brand"><span class="brand-mark">N</span><span>NODE OPS <small>LOCAL WORKBENCH</small></span></div>
    <div class="login-card">
      <span class="eyebrow">本机访问验证</span><h1>节点运维工作台</h1><p>资产状态、固件计划与执行记录，保留在你的运维机器上。</p>
      <el-form @submit.prevent="login"><el-form-item label="本地访问口令"><el-input v-model="loginKey" type="password" show-password placeholder="不是 BMC 密码" autocomplete="off" data-testid="access-key" /></el-form-item><el-button type="primary" native-type="submit" :loading="loginBusy" class="login-submit">进入工作台</el-button></el-form>
      <div class="login-note"><el-icon><Lock /></el-icon><span>通常由启动器自动验证。手动访问时，口令位于本机数据目录的 <code>access-key</code> 文件。服务仅监听回环地址。</span></div>
    </div>
  </div>
  <div v-else class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">N</span><div>NODE OPS<small>节点运维工作台</small></div></div>
      <div class="nav-caption">工作空间</div>
      <button v-for="item in [{id:'workbench',name:'维护工作台',icon:Monitor},{id:'redfish',name:'Redfish 资源操作',icon:Connection},{id:'jobs',name:'任务中心',icon:List},{id:'assets',name:'资产统计管理',icon:Collection},{id:'audit',name:'操作审计',icon:DocumentChecked}]" :key="item.id" class="nav-item" :aria-label="item.name" :class="{active:page===item.id}" @click="page=item.id"><el-icon><component :is="item.icon" /></el-icon><span>{{ item.id==='assets'?'资产管理':item.name }}</span><el-icon class="nav-arrow"><ArrowRight /></el-icon></button>
      <div class="sidebar-bottom"><span class="local-indicator"></span>LOCAL ONLY<small>127.0.0.1 · 数据不上传云端</small><span class="side-version">Web 0.6.0 / Python Engine</span></div>
    </aside>
    <div class="main-shell">
      <header class="topbar"><div class="breadcrumb">工作空间 <span>/</span> {{ pageTitles[page] }}</div><div class="topbar-right"><el-tag effect="plain" type="info"><el-icon><Lock /></el-icon> 本机操作员</el-tag><el-button text :icon="SwitchButton" @click="logout">退出</el-button></div></header>
      <main class="content">
        <div class="page-heading"><div><span class="eyebrow">{{ page==='workbench'?'MAINTENANCE WORKBENCH':'OPERATIONS RECORDS' }}</span><h1>{{ pageTitles[page] }}</h1><p>{{ page==='workbench'?'节点、电源、版本与镜像同屏核对。每次执行都有明确范围。':page==='redfish'?'Systems、Chassis与UpdateService，任务级Session与逐次范围确认。':page==='assets'?'资产台账、硬件信息与固件分布。采集只在所选节点上执行。':'只查看本机保存的记录，不会自动连接或操作 BMC。' }}</p></div><div class="heading-actions"><el-tag type="info" effect="plain">操作逐次确认</el-tag></div></div>

        <template v-if="page==='workbench'">
          <div class="workspace-toolbar"><div class="connection-summary"><el-icon><Connection /></el-icon><span>{{ auth }}</span><span class="dot">·</span><span>{{ workers }} 台并发</span><el-button link type="primary" :disabled="busy" @click="settingsOpen=true" data-testid="settings-button">连接设置</el-button></div><div class="toolbar-spacer"></div><el-button :disabled="busy" @click="saveWorkspace">保存配置</el-button><el-upload :show-file-list="false" :auto-upload="false" accept=".json" :disabled="busy" :on-change="restoreWorkspace"><el-button :disabled="busy">加载配置</el-button></el-upload></div>
          <div class="context-note"><el-icon><Connection /></el-icon><span>读取仅针对所选节点；关机、上电与固件升级分别确认，不会自动执行下一步。</span></div>
          <section class="panel nodes-panel">
            <div class="panel-heading"><div><span class="section-number">01</span><h2>节点与当前状态</h2><span class="subtle">已加载 {{ rows.length }} 台 · 已选 {{ selected.length }} 台</span></div><div class="panel-tools"><el-input v-model="query" placeholder="搜索节点 / IP" :prefix-icon="Search" clearable class="node-search"/><el-button :disabled="busy" @click="sourceOpen=!sourceOpen">{{ sourceOpen?'收起清单':'导入 / 粘贴清单' }}</el-button><el-button :icon="Refresh" :disabled="busy||!selected.length||dirty" @click="readOnly('read')" data-testid="read-state">读取状态</el-button></div></div>
            <div v-if="sourceOpen||!rows.length" class="source-box"><el-input v-model="source" type="textarea" :rows="3" :disabled="busy" placeholder="每行：节点名、BMC IP，或“节点名 IP”。加载清单不会连接设备。" data-testid="source-text"/><div class="source-actions"><el-button type="primary" plain :disabled="busy" @click="loadSource" data-testid="load-source">加载节点</el-button><el-upload :show-file-list="false" :auto-upload="false" accept=".csv,.xlsx" :disabled="busy" :on-change="importNodes"><el-button :disabled="busy" :icon="Upload">导入 Excel / CSV</el-button></el-upload><span v-if="dirty" class="dirty-hint">输入已修改，需重新加载才能执行任务</span><span v-else class="subtle">默认只选首台；信息读取和写操作是分开的。</span></div></div>
            <el-table ref="nodeTable" :data="pagedRows" :inert="busy || undefined" :aria-busy="busy" row-key="address" @selection-change="selectionChanged" empty-text="导入需要维护的节点清单" class="node-table" data-testid="node-table">
              <el-table-column type="selection" :reserve-selection="true" width="44"/>
              <el-table-column prop="name" label="节点" min-width="120"><template #default="{row}"><span class="node-name">{{ row.name }}</span><small class="node-ip">{{ row.address.replace('https://','') }}</small></template></el-table-column>
              <el-table-column label="机型" min-width="110"><template #default="{row}">{{ row.snapshot?.model||'—' }}</template></el-table-column>
              <el-table-column label="电源" width="92"><template #default="{row}"><span class="power-badge" :class="row.snapshot?.power==='Off'?'off':'on'"><i></i>{{ row.snapshot?.power||'未知' }}</span></template></el-table-column>
              <el-table-column v-for="component in COMPONENTS" :key="component" :label="component" min-width="100"><template #default="{row}"><span class="version-text">{{ row.snapshot?.inventory?.[component]?.version||'—' }}</span></template></el-table-column>
              <el-table-column label="结果 / 最近读取" min-width="180"><template #default="{row}"><span class="row-status">{{ row.status||'未读取' }}</span><small class="subtle">{{ formatTime(row.observed) }}</small></template></el-table-column>
            </el-table>
            <div class="table-footer"><span>状态来自最近一次手动读取，不是实时监控。</span><el-pagination v-if="rows.length>20" v-model:current-page="currentPage" :page-size="20" :total="filteredRows.length" layout="prev, pager, next" small/></div>
          </section>
          <section class="panel independent-power" data-testid="power-panel">
            <div class="panel-heading"><div><span class="section-number">02</span><h2>主机电源 · 独立控制</h2><span class="subtle">不需要 CPLD 镜像或组选择</span></div></div>
            <div class="independent-power-actions"><el-button :disabled="busy||!selected.length||dirty" @click="readOnly('power-read')" data-testid="power-read">读取电源 / 机型 · 只读</el-button><el-button :disabled="busy||!selected.length||dirty" @click="preparePlan('GracefulShutdown')" data-testid="normal-off">正常关机</el-button><el-button :disabled="busy||!selected.length||dirty" @click="preparePlan('ForceOff')" data-testid="force-off">强制关机</el-button><el-button :disabled="busy||!selected.length||dirty" @click="preparePlan('On')" data-testid="power-on">上电</el-button></div>
            <p class="panel-footnote">每个电源动作单独确认，不自动升级；仅访问 System 电源路径，不依赖 CPLD 接口。升级前仍须重新预检查并确认全部节点为 Off。</p>
          </section>
          <section class="panel image-panel">
            <div class="panel-heading"><div><span class="section-number">03</span><h2>升级范围与镜像</h2><span class="subtle">只处理勾选的组，节点内串行</span></div><span class="scope-summary">本批每台：{{ selectedNames }}</span></div>
            <div class="image-grid image-grid-head"><span>勾选升级组</span><span>所选节点当前版本</span><span>镜像文件 / SHA256</span></div>
            <div v-for="group in groups" :key="group.component" class="image-grid image-row" :class="{selected:group.enabled}" :data-testid="'group-'+group.component">
              <el-checkbox v-model="group.enabled" :disabled="busy" :data-testid="'check-'+group.component"><strong>{{ group.component }}</strong></el-checkbox>
              <span class="current-version">{{ currentVersions(group.component) }}</span>
              <div class="image-cell"><div class="image-controls"><el-select v-model="group.imageId" :disabled="busy" clearable placeholder="选择已导入镜像" class="image-select"><el-option v-for="image in images" :key="image.id" :label="image.name" :value="image.id" :disabled="mode==='live'&&Boolean(image.demo)"><span>{{ image.name }}</span><small class="option-meta">{{ bytes(image.size) }}{{ image.demo?' · 演示':'' }}</small></el-option></el-select><el-upload :show-file-list="false" :auto-upload="false" accept=".svf,.tar" :disabled="busy||uploading!==null" :on-change="(file:any)=>uploadImage(file,group)"><el-button :disabled="busy" :loading="uploading===group.component" :icon="Upload">导入镜像</el-button></el-upload></div><el-tooltip v-if="imageMeta(group.imageId)" :content="imageMeta(group.imageId)?.sha256" placement="top"><span class="hash-line"><span v-if="imageMeta(group.imageId)?.demo" class="demo-file">演示文件 · </span>SHA256 {{ imageMeta(group.imageId)?.sha256.slice(0,20) }}… <span>{{ bytes(imageMeta(group.imageId)?.size||0) }}</span></span></el-tooltip><span v-else class="hash-line">导入仅保存到本机服务器，不会上传到 BMC。</span></div>
            </div>
            <div class="panel-footnote"><el-icon><WarningFilled /></el-icon><span>当前仅支持 Lattice DCU CPLD。无需填写目标版本。版本由镜像决定；不会自动判定同版本跳过。文件名与扩展名不能代替兼容性核对，不接受 BIOS / BMC 镜像。</span></div>
          </section>
          <section class="execution-panel" :class="{ready:canExecute}" data-testid="execution-gate">
            <div class="gate-message"><el-icon :size="22"><CircleCheckFilled v-if="canExecute"/><Lock v-else/></el-icon><div><strong>{{ canExecute?'预检查条件已满足':'升级保持锁定' }}</strong><p data-testid="gate-reason">{{ dirty?'清单已修改，请先重新加载':readiness.reason }}</p></div><span class="gate-expiry">120s 状态有效期 · 执行前再次复核</span></div>
            <div class="execution-actions"><div class="primary-actions"><el-button :disabled="busy||!selected.length||!selectedGroups.length||dirty" @click="readOnly('precheck')" data-testid="precheck">升级预检查 · 只读</el-button><el-button type="primary" :disabled="!canExecute" @click="preparePlan('upgrade')" data-testid="upgrade">核对计划并升级 <el-icon class="el-icon--right"><ArrowRight/></el-icon></el-button><el-button :disabled="busy||!selected.length||!selectedGroups.length||dirty" @click="readOnly('verify')" data-testid="verify">读回版本</el-button></div></div>
          </section>
          <section v-if="activeJob" class="panel current-task"><div class="panel-heading"><div><span class="section-number">04</span><h2>后台任务</h2><el-tag :type="stateType(activeJob.state)">{{ stateLabels[activeJob.state]||activeJob.state }}</el-tag><span class="subtle">{{ actionLabels[activeJob.action] }} · {{ activeJob.mode==='demo'?'模拟':'真实' }} · {{ activeJob.id.slice(0,8) }}</span></div><div class="panel-tools"><el-button v-if="['running','queued'].includes(activeJob.state)" :icon="VideoPause" @click="stopJob(activeJob)">停止后续任务</el-button><el-button v-if="activeJob.has_report" :icon="Download" @click="download(activeJob)">下载报告</el-button><el-button @click="inspectJob(activeJob)">查看记录</el-button></div></div><div v-if="activeJob.state==='running'&&eventProgress" class="component-progress"><span>{{ progressLabel }}</span><el-progress :percentage="Math.max(0,Math.min(100,eventProgress.data.progress))" :stroke-width="5" color="#4266e8"/></div><p v-if="activeJob.action==='upgrade'&&activeJob.state==='completed'" class="task-note">写入流程已结束，不等于已生效。仍须完成厂商要求的生效过程，并核验版本与硬件状态。</p><p class="task-note">关闭或刷新浏览器不停止已确认的后台任务。“停止”只影响后续步骤，不打断已开始的烧写。</p><div v-if="activeJob.id===eventJob" class="recent-events"><div v-for="event in events.slice(-4)" :key="event.id"><time>{{ new Date(event.at*1000).toLocaleTimeString('zh-CN',{hour12:false}) }}</time><span>{{ event.node||'后台执行器' }}</span><strong>{{ event.event }}</strong><span class="subtle">{{ event.data.reason||event.data.component||'' }}</span></div></div></section>
        </template>

        <section v-else-if="page==='jobs'" class="panel records-panel"><div class="panel-heading"><div><h2>任务记录</h2><span class="subtle">服务中断后标记待核验，不自动续刷</span></div><el-button :icon="Refresh" @click="refreshJobs">刷新记录</el-button></div><el-table :data="jobs" @row-click="inspectJob" empty-text="还没有执行任务"><el-table-column label="任务" min-width="200"><template #default="{row}"><strong>{{ actionLabels[row.action] }}</strong><small class="node-ip">{{ row.id.slice(0,12) }}</small></template></el-table-column><el-table-column label="模式" width="110"><template #default="{row}">{{ row.mode==='demo'?'离线模拟':'真实设备' }}</template></el-table-column><el-table-column label="状态" width="150"><template #default="{row}"><el-tag :type="stateType(row.state)">{{ stateLabels[row.state] }}</el-tag></template></el-table-column><el-table-column label="创建时间" min-width="180"><template #default="{row}">{{ formatTime(row.created) }}</template></el-table-column><el-table-column prop="error" label="说明" min-width="200"/><el-table-column width="100"><template #default="{row}"><el-button link type="primary" @click.stop="inspectJob(row)">详情</el-button></template></el-table-column></el-table></section>
        <RedfishWorkbench v-else-if="page==='redfish'" :busy="busy" @job="trackJob"/>
        <AssetManager ref="assetManager" v-else-if="page==='assets'" :mode="mode" :busy="busy" :auth="auth" :self-signed="selfSigned" :workers="workers" :workbench-nodes="rows" @job="trackJob" @maintain="maintainAssets" @settings="settingsOpen=true"/>
        <section v-else class="panel records-panel"><div class="panel-heading"><div><h2>操作审计</h2><span class="subtle">记录本机登录、导入、计划确认与任务执行，不记录凭据</span></div><el-button :icon="Refresh" @click="refreshAudit">刷新记录</el-button></div><el-table :data="audits" empty-text="暂无审计记录"><el-table-column label="时间" width="190"><template #default="{row}">{{ formatTime(row.at) }}</template></el-table-column><el-table-column prop="event" label="事件" min-width="210"/><el-table-column label="会话标识" width="140"><template #default="{row}"><code>{{ row.actor.slice(0,10) }}</code></template></el-table-column><el-table-column label="范围 / 关联记录" min-width="360"><template #default="{row}"><span class="audit-data">{{ JSON.stringify(row.data) }}</span></template></el-table-column></el-table></section>
        <footer class="page-footer"><span>本机运行 · 无云端依赖 · 默认不连接设备</span><span>审批范围不可复用 · 不自动重试写请求 · 不自动上电</span></footer>
      </main>
    </div>

    <el-drawer v-model="settingsOpen" title="连接与维护设置" size="min(440px,95vw)"><el-alert title="BMC 凭据仅由后端读取" type="info" :closable="false" description="来源：仓库根目录.env或环境变量。密码不会发送到浏览器或保存到任务数据库。"/><el-form label-position="top" class="settings-form"><el-form-item label="认证方式"><el-select v-model="auth" :disabled="busy"><el-option label="Basic · 每次 HTTPS 请求携带凭据" value="basic"/><el-option label="Session · 创建并注销本次 Redfish 会话" value="session"/></el-select></el-form-item><el-form-item><el-checkbox v-model="selfSigned" :disabled="busy">允许自签名证书（跳过 TLS 证书校验）</el-checkbox><small class="subtle">HTTPS 仍加密，但不能验证设备身份。企业 CA 证书有效时不要勾选。</small></el-form-item><el-form-item label="节点并发（同一台内始终串行）"><el-input-number v-model="workers" :min="1" :max="4" :disabled="busy"/></el-form-item><el-form-item><el-checkbox v-model="canary" :disabled="busy" class="wrapped-check">已完成同机型、同镜像的单台现场验证</el-checkbox><small class="subtle">真实多台升级必选；离线测试不能替代现场验证。</small></el-form-item></el-form><div class="credential-state"><el-icon><Lock/></el-icon>{{ config.credentials_available?'本机 BMC 凭据已配置':'本机 BMC 凭据未配置（演示仍可用）' }}</div></el-drawer>
    <el-dialog v-model="planOpen" :title="(plan?.mode==='demo'?'离线模拟 · ':'真实设备写操作 · ')+(plan?.label||'执行确认')" width="min(800px,94vw)" :close-on-click-modal="false" :close-on-press-escape="!confirming" :show-close="!confirming">
      <template v-if="plan"><el-alert :type="plan.mode==='demo'?'info':'warning'" :closable="false" :title="plan.mode==='demo'?'以下操作仅作用于模拟器，不会连接真实设备':'请确认维护窗口、业务状态及下面的精确范围'"/><div class="plan-summary"><div><span>动作</span><strong>{{ plan.label }}</strong></div><div><span>节点数量</span><strong>{{ plan.targets.length }} 台</strong></div><div><span>确认有效期</span><strong>{{ Math.max(0,Math.floor(plan.expires-now/1000)) }} 秒</strong></div></div><div class="plan-nodes"><strong>节点 / IP</strong><p v-for="target in plan.targets" :key="target.address">{{ target.name }} <code>{{ target.address }}</code><small class="subtle"> · 已读取机型：{{ target.model||'未读取' }}</small></p></div><div v-if="plan.images.length" class="plan-images"><div v-for="image in plan.images" :key="image.component"><strong>{{ image.component }}{{ image.expected?' → '+image.expected:' · 版本由镜像决定' }}</strong><p>{{ image.filename }}</p><code>SHA256 {{ image.sha256 }}</code></div><p class="subtle">不升级：{{ plan.excluded.join('、')||'无' }}</p></div><p class="plan-policy">{{ plan.power_policy }}</p><el-form label-position="top"><el-form-item :label="'核对以上范围后，手动输入：'+plan.phrase"><el-input v-model="planInput" autocomplete="off" :disabled="confirming||planExpired" data-testid="confirm-phrase"/></el-form-item></el-form><el-alert v-if="planExpired" type="error" :closable="false" title="计划已过期，请关闭后重新预检查和创建计划"/></template>
      <template #footer><el-button :disabled="confirming" @click="planOpen=false">取消，不执行</el-button><el-button type="primary" :disabled="!plan||planInput!==plan.phrase||planExpired" :loading="confirming" @click="confirmPlan" data-testid="confirm-execute">确认本次范围并执行</el-button></template>
    </el-dialog>
    <el-drawer v-model="jobDrawer" title="任务范围与操作记录" size="min(1000px,95vw)"><template v-if="inspectedJob"><div class="job-detail-heading"><div><h2>{{ actionLabels[inspectedJob.action] }}</h2><code>{{ inspectedJob.id }}</code></div><el-tag :type="stateType(inspectedJob.state)">{{ stateLabels[inspectedJob.state] }}</el-tag></div><el-alert v-if="inspectedJob.mode==='demo'" type="info" :closable="false" title="离线模拟记录，不能作为现场升级成功依据"/><el-alert v-if="inspectedJob.error" type="error" :closable="false" :title="inspectedJob.error"/><div class="job-scope"><strong>已固定的执行范围</strong><p>{{ inspectedJob.draft?.targets.map(t=>`${t.name} (${t.address})`).join('、') }}</p><template v-if="inspectedJob.action==='redfish'"><p>{{ inspectedJob.draft?.operation }} · {{ inspectedJob.draft?.title }}</p><p>{{ inspectedJob.draft?.image_name||'本次无镜像' }} {{ inspectedJob.draft?.image_sha256||'' }}</p><pre class="audit-data">{{ JSON.stringify(inspectedJob.draft,null,2) }}</pre></template><p v-else>{{ inspectedJob.draft?.images?.map(i=>i.expected?`${i.component} → ${i.expected}`:i.component).join('、')||'无固件写入组件' }}</p></div><div class="detail-actions"><el-button v-if="inspectedJob.has_report" :icon="Download" @click="download(inspectedJob)">Excel 报告</el-button><el-button v-if="inspectedJob.has_report" @click="download(inspectedJob,'evidence')">原始操作记录</el-button><el-button v-if="['running','queued'].includes(inspectedJob.state)" @click="stopJob(inspectedJob)">停止后续任务</el-button></div><p class="subtle">显示最近 1000 条事件，完整记录可下载。</p><el-table :data="events" max-height="520" empty-text="等待任务事件"><el-table-column label="时间" width="110"><template #default="{row}">{{ new Date(row.at*1000).toLocaleTimeString('zh-CN',{hour12:false}) }}</template></el-table-column><el-table-column prop="node" label="节点" width="130"/><el-table-column prop="event" label="事件" min-width="170"/><el-table-column label="说明" min-width="240"><template #default="{row}">{{ row.data.reason||row.data.component||row.data.state||'' }}{{ row.data.progress!==undefined?' '+row.data.progress+'%':'' }}</template></el-table-column></el-table></template></el-drawer>
  </div>
</template>
