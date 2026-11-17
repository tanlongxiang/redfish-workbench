<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from './api'
import type { Job, Mode, NodeRow, Draft } from './types'
const props=defineProps<{mode:Mode;busy:boolean;auth:string;selfSigned:boolean;workers:number;workbenchNodes:NodeRow[]}>()
const emit=defineEmits<{(e:'job',value:Job):void;(e:'maintain',value:NodeRow[]):void;(e:'settings'):void}>()
const filters=ref({q:'',model:'',power:'',status:'',sn:'',lifecycle:'',firmware_field:'BMC_Active',firmware_version:'',version_source:'observed'})
const page=ref(1), pageSize=ref(50), loading=ref(false), selection=ref<any[]>([]), table=ref<any>()
const result=ref<any>({items:[],total:0,filtered:0,groups:{},model_options:[],firmware_fields:[]})
const details=ref<any>(null), detailOpen=ref(false), saving=ref(false), importing=ref(false)
const metadata=ref<any>({})
const collectionLabels:Record<string,string>={uncollected:'未采集',complete:'完整采集',partial:'部分采集',failed:'读取失败',stopped:'已停止'}
const snLabels:Record<string,string>={missing:'现场 SN 缺失',match:'一致',conflict:'不一致',uncompared:'无参考 SN'}
const lifecycleLabels:Record<string,string>={active:'在用',spare:'备用',repair:'维修',retired:'退役'}
let serial=0,timer:ReturnType<typeof setTimeout>|undefined
function message(error:unknown){ElMessage.error(error instanceof Error?error.message:'请求失败')}
function count(key:string,name:string){return result.value.groups[key]?.find((r:any)=>r.name===name)?.count||0}
function time(value?:number){return value?new Date(value*1000).toLocaleString('zh-CN',{hour12:false}):'—'}
function query(){const p=new URLSearchParams({mode:props.mode,page:String(page.value),page_size:String(pageSize.value),...filters.value});return p.toString()}
async function refresh(){
  const id=++serial;loading.value=true
  try{const value=await api('/api/asset-records?'+query());if(id===serial)result.value=value}catch(error){message(error)}finally{if(id===serial)loading.value=false}
}
function schedule(){if(timer)clearTimeout(timer);timer=setTimeout(refresh,250)}
function clearSelection(){selection.value=[];table.value?.clearSelection()}
watch(filters,()=>{page.value=1;clearSelection();schedule()},{deep:true})
watch(page,schedule)
watch(()=>props.mode,()=>{page.value=1;filters.value.model='';filters.value.firmware_version='';clearSelection();schedule()})
watch(()=>props.busy,value=>{if(!value)schedule()})
watch(()=>props.workbenchNodes,()=>schedule())
const summary=computed(()=>[
  {label:'台账总数',value:result.value.total,note:'不随筛选变化'},
  {label:'当前筛选',value:result.value.filtered,note:'统计及导出均按此范围'},
  {label:'最近采集完整',value:count('status','complete'),note:'最近一次取得 38 项有效固件'},
  {label:'现场 SN 缺失',value:count('sn','missing'),note:'包含未采集；不以 Chassis SN 替代'}
])
async function importLedger(file:any){
  if(props.busy)return
  try{importing.value=true;const form=new FormData();form.append('file',file.raw);const value=await api('/api/asset-records/import?mode='+props.mode,'POST',form);await refresh();ElMessage.success(`已导入 ${value.count} 台台账；未连接设备，也未改写原表`)}catch(error){message(error)}finally{importing.value=false}
}
async function registerWorkbench(){
  try{const value=await api('/api/asset-records/register','POST',{mode:props.mode,targets:props.workbenchNodes.map(({name,address})=>({name,address}))});await refresh();ElMessage.success(`已加入 ${value.count} 台；只写入本机台账`)}catch(error){message(error)}
}
async function collect(){
  if(props.busy||!selection.value.length)return
  const draft:Draft={revision:crypto.randomUUID(),mode:props.mode,targets:selection.value.map(({name,address})=>({name,address})),images:[],auth:props.auth,self_signed:props.selfSigned,workers:props.workers,canary_confirmed:false}
  try{
    if(props.mode==='live')await ElMessageBox.confirm(`仅对所选 ${draft.targets.length} 台读取资产和 38 项固件：\n${draft.targets.map(t=>`${t.name} (${t.address})`).join('\n')}\n\n不会关机、上传、激活或回写 SN。`,'确认资产只读采集',{confirmButtonText:'确认本次只读采集',cancelButtonText:'取消',type:'info'})
    const job:Job=await api('/api/jobs','POST',{action:'asset-collect',draft});emit('job',job);ElMessage.success('只读资产采集已提交到后台，可在任务中心跟踪')
  }catch(error){if(error!=='cancel'&&error!=='close')message(error)}
}
function show(row:any){details.value=row;metadata.value={...row.metadata};detailOpen.value=true}
async function save(){
  if(!details.value)return
  try{saving.value=true;details.value=await api('/api/asset-records/metadata','PUT',{mode:details.value.mode,address:details.value.address,...metadata.value});await refresh();ElMessage.success('本机台账信息已保存；未修改设备或原表')}catch(error){message(error)}finally{saving.value=false}
}
function exportLedger(){window.open('/api/asset-records/export?'+query(),'_blank','noopener,noreferrer')}
function maintain(){emit('maintain',selection.value.map(({name,address})=>({name,address})))}
defineExpose({refresh})
onMounted(refresh)
onBeforeUnmount(()=>{serial++;if(timer)clearTimeout(timer)})
</script>

<template>
  <div class="asset-manager" data-testid="asset-manager">
    <div class="asset-toolbar">

      <el-button :disabled="busy" @click="emit('settings')">采集连接设置</el-button>
      <div class="toolbar-spacer"></div>
      <el-button :disabled="busy||!workbenchNodes.length" @click="registerWorkbench">加入工作台节点</el-button>
      <el-upload :show-file-list="false" :auto-upload="false" accept=".csv,.xlsx" :on-change="importLedger" :disabled="busy||importing"><el-button :disabled="busy" :loading="importing">导入资产台账</el-button></el-upload>
      <el-button @click="exportLedger" data-testid="asset-export">导出筛选结果</el-button>
    </div>
    <el-alert :closable="false" type="info" title="本机台账；导入和筛选不连接设备。采集仅针对所选节点。"/>
    <div class="asset-stat-grid"><div v-for="stat in summary" :key="stat.label" class="asset-stat"><span>{{ stat.label }}</span><strong>{{ stat.value.toLocaleString() }}</strong><small>{{ stat.note }}</small></div></div>
    <section class="panel">
      <div class="panel-heading"><div><h2>资产台账</h2><span class="subtle">已选 {{ selection.length }} 台 · 参考 SN 与现场 SN 分开保留</span></div><div class="panel-tools"><el-button :disabled="busy||!selection.length" @click="maintain" data-testid="asset-maintain">载入维护工作台</el-button><el-button type="primary" :disabled="busy||!selection.length" @click="collect" data-testid="asset-collect">采集所选资产 · 只读</el-button><el-button :loading="loading" @click="refresh">刷新台账</el-button></div></div>
      <div class="asset-filters">
        <el-input v-model="filters.q" clearable placeholder="节点 / IP / SN / 位置 / 标签" aria-label="资产搜索" data-testid="asset-search"/>
        <el-select v-model="filters.model" clearable placeholder="全部机型" aria-label="机型筛选"><el-option v-for="item in result.model_options" :key="item" :label="item" :value="item"/></el-select>
        <el-select v-model="filters.power" clearable placeholder="全部电源" aria-label="电源筛选"><el-option v-for="item in ['On','Off','未知']" :key="item" :label="item" :value="item"/></el-select>
        <el-select v-model="filters.status" clearable placeholder="采集状态" aria-label="采集状态筛选"><el-option v-for="(label,value) in collectionLabels" :key="value" :label="label" :value="value"/></el-select>
        <el-select v-model="filters.sn" clearable placeholder="SN 核对" aria-label="SN筛选"><el-option v-for="(label,value) in snLabels" :key="value" :label="label" :value="value"/></el-select>
        <el-select v-model="filters.lifecycle" clearable placeholder="资产状态" aria-label="资产状态筛选"><el-option v-for="(label,value) in lifecycleLabels" :key="value" :label="label" :value="value"/></el-select>
      </div>
      <el-table ref="table" :data="result.items" row-key="address" :inert="busy||undefined" @selection-change="(value:any[])=>selection=[...value]" data-testid="asset-table" empty-text="导入资产台账或加入工作台节点；不会自动读取业务总表">
        <el-table-column type="selection" :reserve-selection="true" width="44"/>
        <el-table-column label="节点 / BMC IP" min-width="185"><template #default="{row}"><strong class="node-name">{{ row.name }}</strong><small class="node-ip">{{ row.address.replace('https://','') }}</small><el-button link type="primary" class="asset-detail-link" @click="show(row)">详情 / 编辑</el-button></template></el-table-column>
        <el-table-column prop="model" label="机型" min-width="115"/>
        <el-table-column prop="power" label="电源" width="80"/>
        <el-table-column label="参考 SN / 现场 System SN" min-width="220"><template #default="{row}"><span>{{ row.metadata.declared_sn||'无参考 SN' }}</span><small class="node-ip">{{ row.data.system_sn||'未取得有效现场 SN' }}</small><el-tag size="small" :type="row.sn_state==='conflict'?'danger':row.sn_state==='match'?'success':'info'">{{ snLabels[row.sn_state] }}</el-tag></template></el-table-column>
        <el-table-column label="位置 / 标签" min-width="160"><template #default="{row}">{{ row.metadata.location||'—' }}<small class="node-ip">{{ row.metadata.tags }}</small></template></el-table-column>
        <el-table-column label="BIOS / BMC" min-width="175"><template #default="{row}"><span class="version-text">{{ row.data.firmware?.Bios||row.data.reference_firmware?.Bios||'—' }}</span><small class="node-ip">{{ row.data.firmware?.BMC_Active||row.data.reference_firmware?.BMC_Active||'—' }}{{ !row.data.firmware?.BMC_Active&&row.data.reference_firmware?.BMC_Active?'（导入参考）':'' }}</small></template></el-table-column>
        <el-table-column label="采集 / 完整度" min-width="175"><template #default="{row}"><el-tag :type="row.collection_state==='failed'?'danger':row.collection_state==='complete'?'success':'info'">{{ collectionLabels[row.collection_state] }}</el-tag><small class="node-ip">本次 {{ row.latest_count }}/38 · 历史 {{ row.firmware_count }}/38 · 参考 {{ row.reference_count }}/38</small></template></el-table-column>
        <el-table-column label="最近尝试 / 资产状态" min-width="190"><template #default="{row}">{{ time(row.data.last_attempt) }}<small class="node-ip">{{ lifecycleLabels[row.metadata.lifecycle] }}</small></template></el-table-column>
      </el-table>
      <div class="table-footer"><span>筛选变化会清空选择；导出当前全部筛选结果，不只本页。</span><el-pagination v-model:current-page="page" :page-size="pageSize" :total="result.filtered" layout="total, prev, pager, next" small/></div>
    </section>
    <div class="asset-distributions">
      <section class="panel"><div class="panel-heading"><h2>机型与采集统计</h2><span class="subtle">当前筛选范围</span></div><el-table :data="result.groups.model||[]" max-height="240"><el-table-column prop="name" label="机型"/><el-table-column prop="count" label="台数" width="85"/><el-table-column width="80"><template #default="{row}"><el-button link @click="filters.model=row.name">筛选</el-button></template></el-table-column></el-table><div class="asset-counts"><span v-for="item in result.groups.status" :key="item.name">{{ collectionLabels[item.name] }}：{{ item.count }}</span></div></section>
      <section class="panel"><div class="panel-heading"><h2>固件版本分布</h2><el-select v-model="filters.firmware_field" aria-label="统计固件组件" class="asset-version-select" @change="filters.firmware_version='' "><el-option v-for="field in result.firmware_fields" :key="field" :value="field" :label="field"/></el-select></div><div class="asset-counts"><el-segmented v-model="filters.version_source" :options="[{label:'现场历史值',value:'observed'},{label:'导入参考值',value:'reference'}]" @change="filters.firmware_version=''"/></div><el-table :data="result.groups.versions||[]" max-height="240"><el-table-column prop="name" label="版本（按所选来源，非实时）"/><el-table-column prop="count" label="台数" width="85"/><el-table-column width="80"><template #default="{row}"><el-button link @click="filters.firmware_version=row.name">筛选</el-button></template></el-table-column></el-table><div class="asset-counts"><span v-if="filters.firmware_version">当前版本筛选：{{ filters.firmware_version }} <el-button link @click="filters.firmware_version=''">清除</el-button></span><span v-else>失败、NA、不在位与异常版本不会覆盖历史有效值。</span></div></section>
    </div>
    <el-drawer v-model="detailOpen" title="资产详情与台账管理" size="min(740px,96vw)"><template v-if="details">
      <h2>{{ details.name }}</h2><p class="asset-detail-address">{{ details.address }}</p>
      <el-alert :closable="false" type="info" title="只修改本机台账；参考 SN 不会被现场 SN 自动覆盖，也不回写原总表。"/>
      <el-descriptions :column="2" border class="asset-descriptions"><el-descriptions-item label="现场机型">{{ details.data.model||'—' }}</el-descriptions-item><el-descriptions-item label="现场 System SN">{{ details.data.system_sn||'—' }}</el-descriptions-item><el-descriptions-item label="CPU">{{ details.data.cpu_count??'—' }} · {{ details.data.cpu_model||'—' }}</el-descriptions-item><el-descriptions-item label="内存">{{ details.data.memory_gib??'—' }} GiB</el-descriptions-item><el-descriptions-item label="Chassis SN">{{ details.data.chassis_sn||'—' }}</el-descriptions-item><el-descriptions-item label="最近采集">{{ time(details.data.last_attempt) }}</el-descriptions-item></el-descriptions>
      <el-form label-position="top" class="asset-edit-form"><el-form-item label="参考 SN（导入来源 / 人工维护）"><el-input v-model="metadata.declared_sn" maxlength="200"/></el-form-item><el-form-item label="参考机型"><el-input v-model="metadata.declared_model" maxlength="200"/></el-form-item><el-form-item label="位置 / 机柜"><el-input v-model="metadata.location" maxlength="200" data-testid="asset-location"/></el-form-item><el-form-item label="标签"><el-input v-model="metadata.tags" maxlength="300"/></el-form-item><el-form-item label="资产状态"><el-select v-model="metadata.lifecycle"><el-option v-for="(label,value) in lifecycleLabels" :key="value" :label="label" :value="value"/></el-select></el-form-item><el-form-item label="备注"><el-input type="textarea" v-model="metadata.notes" maxlength="1000"/></el-form-item></el-form>
      <el-button type="primary" :loading="saving" @click="save" data-testid="asset-save">保存台账信息</el-button>
      <h3 class="asset-firmware-title">38 项固件 · 历史有效值</h3><p class="subtle">每项分别保留采集时间；不代表所有数据都来自最近一次尝试。参考导入时间：{{ time(details.data.reference_imported_at) }}（非现场采集时间）。</p>
      <el-table :data="result.firmware_fields.map((field:string)=>({field,version:details.data.firmware?.[field]||'未采集',reference:details.data.reference_firmware?.[field]||'未导入',at:details.data.firmware_observed?.[field]}))"><el-table-column prop="field" label="组件" min-width="120"/><el-table-column prop="version" label="现场历史版本" min-width="160"/><el-table-column prop="reference" label="导入参考版本" min-width="160"/><el-table-column label="该项读取时间" min-width="180"><template #default="{row}">{{ time(row.at) }}</template></el-table-column></el-table>
      <p class="asset-notes">{{ details.data.last_error||details.data.notes?.join('；')||'无额外说明' }}</p>
    </template></el-drawer>
  </div>
</template>
