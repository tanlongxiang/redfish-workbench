<script setup lang="ts">
import { computed,nextTick,onMounted,onBeforeUnmount,ref,watch } from 'vue'
import { ElMessage,ElMessageBox } from 'element-plus'
import { api } from './api'
const props=defineProps<{busy:boolean}>()
const emit=defineEmits<{job:[job:any]}>()
const fixtureMode=ref(false)
const operations=ref<any[]>([]),module=ref('Systems'),operationId=ref('3.3.1'),search=ref('')
const source=ref(''),targets=ref<any[]>([]),loaded=ref(''),selected=ref<any[]>([])
const biosOld=ref(''),biosNew=ref(''),biosRepeat=ref('')
const canary=ref(false)
const username=ref(''),password=ref(''),configured=ref(false),insecure=ref(false),workers=ref(2)
const expectedModel=ref(''),expectedVersion=ref(''),params=ref<Record<string,string>>({}),payload=ref('{}')
const image=ref<any>(null),uploading=ref(false),pending=ref(false),plan=ref<any>(null),phrase=ref(''),dialog=ref(false),confirming=ref(false)
const job=ref<any>(null),catalogError=ref(''),now=ref(Date.now())
let timer:ReturnType<typeof setInterval>|undefined,clock:ReturnType<typeof setInterval>|undefined
const operation=computed(()=>operations.value.find(x=>x.id===operationId.value))
const choices=computed(()=>operations.value.filter(x=>x.module===module.value&&(!search.value||`${x.id} ${x.title} ${x.path}`.toLowerCase().includes(search.value.toLowerCase()))))
const dirty=computed(()=>source.value.trim()!==loaded.value.trim())
const busy=computed(()=>props.busy||pending.value||['running','queued'].includes(job.value?.state))
const firmware=computed(()=>['3.7.5','3.7.6'].includes(operationId.value))
const payloadObject=computed(()=>{try{const v=JSON.parse(payload.value);return v&&typeof v==='object'&&!Array.isArray(v)?v:{}}catch{return {}}})
const expired=computed(()=>plan.value&&now.value/1000>plan.value.expires)
const cleanup:Record<string,string>={deleted:'已确认注销',already_absent:'会话已不存在',unconfirmed:'注销未确认',not_created:'未创建会话'}
function error(e:unknown){ElMessage.error(e instanceof Error?e.message:'请求失败')}
async function cancelPlan(){const id=plan.value?.id;plan.value=null;phrase.value='';if(id)try{await api('/api/redfish/plans/'+id,'DELETE')}catch{} }
function changed(){if(plan.value){dialog.value=false;void cancelPlan()}}
watch([biosOld,biosNew,biosRepeat,canary,operationId,source,expectedModel,expectedVersion,insecure,workers,payload,configured,username,password,selected,params,image],changed,{deep:true})
watch(module,()=>{search.value='';operationId.value=operations.value.find(x=>x.module===module.value)?.id||''})
watch(operationId,()=>{biosOld.value='';biosNew.value='';biosRepeat.value='';params.value={};payload.value=JSON.stringify(operation.value?.sample||{},null,2);image.value=null})
async function load(){try{const r=await api('/api/targets/parse','POST',{text:source.value});targets.value=r.targets;selected.value=[];loaded.value=source.value;changed();ElMessage.success('清单已加载；请勾选本次节点，尚未连接BMC')}catch(e){error(e)}}
async function importFile(file:any){try{const f=new FormData();f.append('file',file.raw);const r=await api('/api/targets/import','POST',f);targets.value=r.targets;selected.value=[];source.value=r.targets.map((x:any)=>`${x.name} ${x.address}`).join('\n');loaded.value=source.value;ElMessage.success('已导入，默认不选节点')}catch(e){error(e)}}
async function upload(file:any){if(file.size>64*1024*1024){ElMessage.error('镜像不能超过64MiB');return}uploading.value=true;try{const f=new FormData();f.append('file',file.raw);image.value=await api('/api/redfish/images','POST',f);ElMessage.success('仅导入本机，尚未上传到BMC')}catch(e){error(e)}finally{uploading.value=false}}
function body(){
 if(dirty.value)throw new Error('清单已修改，请重新加载并勾选')
 if(!selected.value.length)throw new Error('请勾选本次节点')
 if(operation.value?.blocked)throw new Error(operation.value.blocked)
 if(!configured.value&&(!username.value||!password.value))throw new Error('请输入本次账号密码，或显式选择后端凭据')
 if(operationId.value==='3.3.22'&&(!biosNew.value||biosNew.value!==biosRepeat.value))throw new Error('新BIOS密码不能为空且两次输入必须一致')
 if(firmware.value&&!image.value)throw new Error('请导入本次G50镜像')
 const resourceParams=Object.fromEntries(Object.entries(params.value).filter(([,v])=>v.trim()).map(([k,v])=>[k,v.trim()]))
 return {...(operationId.value==='3.3.22'?{bios_passwords:{old:biosOld.value,new:biosNew.value}}:{}),operation:operationId.value,targets:selected.value.map(({name,address})=>({name,address})),params:resourceParams,payload:JSON.parse(payload.value),credentials:{source:configured.value?'configured':'manual',username:username.value,password:password.value},canary_confirmed:canary.value,insecure:insecure.value,workers:workers.value,expected_model:expectedModel.value,expected_version:expectedVersion.value,image_id:image.value?.id||''}
}
async function submit(){
 try{
  const b=body()
  if(operation.value.write){pending.value=true;const result=await api('/api/redfish/plans','POST',b);password.value='';biosOld.value='';biosNew.value='';biosRepeat.value='';await nextTick();plan.value=result;phrase.value='';dialog.value=true;return}
  await ElMessageBox.confirm(`只对以下 ${b.targets.length} 台通过Session读取「${operation.value.title}」：\n${b.targets.map(x=>`${x.name} (${x.address})`).join('\n')}\n\n任务结束注销本次会话；不执行设备配置变更。`,'确认本次只读范围',{confirmButtonText:'确认只读连接',type:'info'})
  pending.value=true;job.value=await api('/api/redfish/jobs','POST',b);password.value='';emit('job',job.value)
 }catch(e){if(e!=='cancel'&&e!=='close')error(e)}finally{pending.value=false}
}
async function confirm(){if(!plan.value||phrase.value!==plan.value.phrase||expired.value)return;confirming.value=true;try{job.value=await api(`/api/redfish/plans/${plan.value.id}/confirm`,'POST',{phrase:phrase.value});plan.value=null;phrase.value='';dialog.value=false;emit('job',job.value)}catch(e){error(e)}finally{confirming.value=false}}
async function refresh(){
 if(!job.value)return
 try{
  job.value=await api('/api/jobs/'+job.value.id)
  if(!['queued','running'].includes(job.value.state)&&timer){clearInterval(timer);timer=undefined}
 }catch{}
}
async function stop(){if(job.value)try{const r=await api(`/api/jobs/${job.value.id}/stop`,'POST',{});ElMessage.warning(r.message)}catch(e){error(e)}}
function download(kind:string){window.open(`/api/jobs/${job.value.id}/files/${kind}`,'_blank','noopener,noreferrer')}
onMounted(async()=>{
 try{const c=await api('/api/redfish/catalog');operations.value=c.operations;fixtureMode.value=!!c.test_fixture}catch(e){catalogError.value=e instanceof Error?e.message:'目录加载失败'}
 timer=setInterval(refresh,1500);clock=setInterval(()=>now.value=Date.now(),1000)
})
onBeforeUnmount(()=>{if(timer)clearInterval(timer);if(clock)clearInterval(clock);password.value='';biosOld.value='';biosNew.value='';biosRepeat.value='';void cancelPlan()})
</script>
<template>
 <div class="rf-workbench" data-testid="redfish-workbench">
  <el-alert v-if="fixtureMode" title="本机HTTPS模拟BMC验证环境，不是现场设备" type="warning" :closable="false"/><el-alert v-if="catalogError" :title="catalogError" type="error" :closable="false"/>
  <el-alert title="任务级 Session：账号密码只用于创建会话；Token不回传页面，结束后主动注销" type="info" :closable="false" description="三模块基于文档适配，尚未真实设备写验证。不会自动降级Basic、自动切电或重发未知写请求。"/>
  <section class="panel rf-section"><div class="panel-heading"><div><span class="section-number">01</span><h2>本次节点范围</h2><span class="subtle">{{selected.length}} / {{targets.length}} 台已选 · 不扫描网段</span></div><el-upload :auto-upload="false" :show-file-list="false" accept=".xlsx,.csv,.txt" :disabled="busy" :on-change="importFile"><el-button :disabled="busy">导入清单</el-button></el-upload></div>
   <div class="rf-padded"><el-input v-model="source" type="textarea" :rows="3" placeholder="节点名 IP，每行一台；也支持从原表BMCIP列导入" :disabled="busy" data-testid="rf-targets"/><el-button class="rf-load" :disabled="busy||!source.trim()" @click="load" data-testid="rf-load">加载节点（不连接）</el-button><el-text v-if="dirty" type="warning"> 清单尚未加载</el-text>
   <el-table :data="targets" max-height="280" @selection-change="selected=$event" :inert="busy||undefined" empty-text="加载后请显式勾选本次节点"><el-table-column type="selection" width="44"/><el-table-column prop="name" label="节点名"/><el-table-column prop="address" label="BMC HTTPS地址"/></el-table></div>
  </section>
  <section class="panel rf-section"><div class="panel-heading"><div><span class="section-number">02</span><h2>资源与操作</h2></div><el-tag effect="plain">文档适配 · 非现场验证</el-tag></div>
   <div class="rf-padded"><el-radio-group v-model="module" :disabled="busy" aria-label="Redfish模块"><el-radio-button label="Systems"/><el-radio-button label="Chassis"/><el-radio-button label="UpdateService"/></el-radio-group>
    <div class="rf-grid"><div><el-input v-model="search" placeholder="筛选编号、名称或路径" clearable :disabled="busy"/><el-select data-testid="rf-operation" v-model="operationId" filterable :disabled="busy" class="rf-select" aria-label="资源操作"><el-option v-for="o in choices" :key="o.id" :value="o.id" :label="`${o.id} ${o.title}${o.blocked?'（待适配）':''}`"/></el-select><div v-if="operation" class="rf-contract"><el-tag :type="operation.write?'warning':'success'">{{operation.method}}</el-tag><code>{{operation.path}}</code><p>{{operation.impact}}</p><small>PDF 第{{operation.page}}页 · {{operation.verification}}</small></div></div>
    <el-form label-position="top" v-if="operation"><el-form-item v-for="key in operation.parameters" :key="key" :label="key+(['system_id','chassis_id'].includes(key)?'（可留空，仅唯一成员时自动发现）':'（必填）')"><el-input v-model="params[key]" :disabled="busy" placeholder="填写集合查询得到的资源ID，不填完整URL"/></el-form-item><el-alert v-if="operation.blocked" :title="operation.blocked" type="warning" :closable="false"/><template v-if="operation.write"><el-form-item label="准确机型（执行前从设备再次核对）"><el-input data-testid="rf-model" v-model="expectedModel" :disabled="busy" placeholder="R6250A0 或 X7450A0；不适用于原四组DCU适配"/></el-form-item><template v-if="operationId==='3.3.22'"><el-alert title="BIOS旧/新密码使用专用秘密字段，不填入JSON；提交后清空，不进入计划或报告" type="warning" :closable="false"/><el-form-item label="当前BIOS密码（仅按平台情况允许留空）"><el-input data-testid="rf-bios-old" v-model="biosOld" type="password" autocomplete="new-password" :disabled="busy"/></el-form-item><el-form-item label="本次新BIOS密码"><el-input data-testid="rf-bios-new" v-model="biosNew" type="password" autocomplete="new-password" :disabled="busy"/></el-form-item><el-form-item label="再次输入新BIOS密码"><el-input data-testid="rf-bios-repeat" v-model="biosRepeat" type="password" autocomplete="new-password" :disabled="busy"/></el-form-item></template><el-form-item v-if="operationId==='3.3.23'" label="本次电源动作"><el-select :model-value="payloadObject.ResetType" @update:model-value="payload=JSON.stringify({ResetType:$event},null,2)" :disabled="busy"><el-option v-for="v in ['On','GracefulShutdown','ForceOff','ForceRestart','ForcePowerCycle','Nmi']" :key="v" :value="v" :label="v"/></el-select></el-form-item><el-form-item v-if="operationId==='3.7.6'" label="本次固件组件"><el-select :model-value="payloadObject.Targets?.[0]" @update:model-value="payload=JSON.stringify({Targets:[$event],SaveConfig:true},null,2)" :disabled="busy"><el-option v-for="v in ['ActiveBMC','BackupBMC','Bios','Cpld','PSU','PSU1','PSU2']" :key="v" :value="'/redfish/v1/UpdateService/FirmwareInventory/'+v" :label="v"/></el-select></el-form-item><el-form-item v-if="operation.body_keys.length" label="业务参数 JSON（只接受此操作声明的字段）"><el-input data-testid="rf-payload" v-model="payload" type="textarea" :rows="8" :disabled="busy" spellcheck="false"/></el-form-item></template></el-form>
    </div>
    <div v-if="firmware" class="rf-image"><el-upload :auto-upload="false" :show-file-list="false" accept=".ima,.bin,.rom,.jed" :disabled="busy||uploading" :on-change="upload"><el-button :loading="uploading" :disabled="busy">导入本次镜像到本机</el-button></el-upload><div v-if="image"><strong>{{image.name}}</strong><small>SHA256：{{image.sha256}}</small></div><p>上传/升级前必须读到主机Off；本工具不自动关机。SimpleUpdate计划包含上传、激活、任务跟踪及版本读回。</p><el-input v-if="operationId==='3.7.6'" v-model="expectedVersion" :disabled="busy" placeholder="预期版本（可选；留空不判定目标版本一致）"/></div>
   </div>
  </section>
  <section class="panel rf-section"><div class="panel-heading"><div><span class="section-number">03</span><h2>认证与执行</h2></div><el-tag>Session only</el-tag></div><div class="rf-padded"><div class="rf-grid"><el-form label-position="top"><el-checkbox v-model="configured" :disabled="busy">本次使用后端已配置凭据（不回传浏览器）</el-checkbox><template v-if="!configured"><el-form-item label="本次账号"><el-input data-testid="rf-username" v-model="username" autocomplete="off" :disabled="busy"/></el-form-item><el-form-item label="本次密码（提交后清空，不写入任务记录）"><el-input data-testid="rf-password" v-model="password" type="password" autocomplete="new-password" :disabled="busy"/></el-form-item></template></el-form><div><el-checkbox v-model="insecure" :disabled="busy">跳过TLS证书校验（降低设备身份保障）</el-checkbox><p class="subtle">默认校验证书；内网CA可通过系统信任配置。</p><label>节点并发 <el-input-number v-model="workers" :min="1" :max="4" :disabled="busy"/></label><p class="subtle">每台任务内复用一个Session；不会每个接口重新登录。</p><el-checkbox v-if="operation?.write" v-model="canary" :disabled="busy">已完成同机型、同参数单台验证（多台变更必选）</el-checkbox></div></div><el-button type="primary" :loading="pending" :disabled="busy||dirty||!selected.length||!operation||!!operation.blocked" @click="submit" data-testid="rf-submit">{{operation?.write?'生成变更计划（此步不连接）':'确认范围并只读执行'}}</el-button></div></section>
  <section v-if="job" class="panel rf-section"><div class="panel-heading"><div><h2>本次任务</h2><span class="subtle">{{job.id}} · {{job.state}}</span></div><div><el-button v-if="['queued','running'].includes(job.state)" @click="stop">停止后续步骤</el-button><el-button v-if="job.has_report" @click="download('results')">JSON结果</el-button><el-button v-if="job.has_report" @click="download('report')">Excel报告</el-button></div></div><div class="rf-padded"><el-alert v-if="job.error" :title="job.error" type="error" :closable="false"/><el-table :data="job.results||[]" empty-text="后台排队或执行中，关闭页面不会中断已开始的升级"><el-table-column prop="node" label="节点"/><el-table-column prop="status" label="结果" min-width="240"/><el-table-column label="Session清理" min-width="140"><template #default="{row}"><el-tag :type="row.session_cleanup==='unconfirmed'?'danger':'info'">{{cleanup[row.session_cleanup]||row.session_cleanup}}</el-tag></template></el-table-column><el-table-column prop="error" label="说明" min-width="200"/><el-table-column type="expand"><template #default="{row}"><pre class="rf-json">{{JSON.stringify(row,null,2)}}</pre><a v-if="row.attachment" :href="`/api/redfish/jobs/${job.id}/attachments/${row.attachment}`">下载本次附件（内部资料）</a></template></el-table-column></el-table></div></section>
  <el-dialog v-model="dialog" title="核对本次变更计划" width="min(820px,94vw)" :close-on-click-modal="false" :close-on-press-escape="!confirming" :show-close="!confirming" @closed="cancelPlan"><el-alert title="确认只授权下面列出的节点、参数、镜像和动作；不会自动执行额外电源操作" type="warning" :closable="false"/><pre class="rf-json" v-if="plan">{{JSON.stringify(plan.plan,null,2)}}</pre><p v-if="plan">120秒内输入：<strong>{{plan.phrase}}</strong></p><el-input data-testid="rf-confirm-input" v-model="phrase" autocomplete="off" :disabled="confirming"/><el-text v-if="expired" type="danger">计划已过期，请重新生成</el-text><template #footer><el-button :disabled="confirming" @click="dialog=false">取消并清除计划</el-button><el-button data-testid="rf-confirm" type="danger" :loading="confirming" :disabled="!plan||phrase!==plan.phrase||expired" @click="confirm">确认本次范围并执行</el-button></template></el-dialog>
 </div>
</template>
<style scoped>
.rf-workbench{display:grid;gap:20px;min-width:0}.rf-section{min-width:0}.rf-padded{padding:20px}.rf-grid{display:grid;grid-template-columns:1fr 1fr;gap:28px;margin-top:20px;min-width:0}.rf-load{margin:12px 0}.rf-select{width:100%;margin:12px 0}.rf-contract{padding:16px;background:var(--el-fill-color-light);border-radius:8px;line-height:1.7}.rf-contract code{display:block;overflow-wrap:anywhere;margin-top:10px}.rf-contract small{color:var(--el-text-color-secondary)}.rf-image{padding:18px;border:1px solid var(--el-border-color);border-radius:8px}.rf-image small{display:block;overflow-wrap:anywhere}.rf-json{white-space:pre-wrap;overflow-wrap:anywhere;max-height:440px;overflow:auto;padding:16px;font-size:12px;background:var(--el-fill-color-light);border-radius:8px}.rf-workbench :deep(.el-checkbox){height:auto;align-items:flex-start;max-width:100%}.rf-workbench :deep(.el-checkbox__label){white-space:normal;overflow-wrap:anywhere;line-height:1.6;min-width:0}.rf-workbench :deep(.el-radio-group){flex-wrap:wrap}.rf-workbench :deep(.panel-heading){flex-wrap:wrap;gap:12px}@media(max-width:800px){.rf-grid{grid-template-columns:1fr}.rf-padded{padding:16px}}
</style>
