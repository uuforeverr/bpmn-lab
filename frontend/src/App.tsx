import {useEffect,useMemo,useState} from 'react';
import {Activity, ChevronRight, CirclePlay, Download, FlaskConical, Gauge, Layers3, StepForward} from 'lucide-react';
import {BpmnViewer} from './BpmnViewer';

type Run={id:string;status:string;stage:string;strategy:string;input_text:string;state:any};
const activityNames:Record<string,string>={sentence_splitter:'切句处理中',semantic_resolver:'语义消解模型思考中',planner:'语义分段规划中',generator:'流程图生成中',reviewer:'语义审查中',repair:'局部修订中',bpmn_layout:'BPMN 自动布局中',pipeline:'流水线处理中'};
const sample='客户提交贷款申请。系统登记申请并检查材料；如果材料不完整，则通知客户补充材料。材料完整后，信用审核和风险审核并行进行。两项审核均通过后，经理决定是否批准申请。批准后通知客户并结束流程；拒绝后通知客户并结束流程。';

async function request(path:string, init?:RequestInit){
  const response=await fetch(path,{headers:{'Content-Type':'application/json'},...init});
  const payload=await response.json().catch(()=>null);
  if(!response.ok)throw new Error(payload?.detail??`请求失败（HTTP ${response.status}）`);
  return payload;
}
function splitDraft(value:string){return value.trim().split(/(?<=[。！？.])\s*|\n+/u).map(v=>v.trim()).filter(Boolean)}
function saveFile(name:string,content:string,type:string){const url=URL.createObjectURL(new Blob([content],{type}));const link=document.createElement('a');link.href=url;link.download=name;link.click();URL.revokeObjectURL(url)}

export default function App(){
  const [text,setText]=useState(sample),[strategy,setStrategy]=useState('semantic'),[run,setRun]=useState<Run|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[snapshot,setSnapshot]=useState(0),[elapsed,setElapsed]=useState(0);
  const snapshots=run?.state?.snapshots??[];
  const draftSentences=useMemo(()=>splitDraft(text),[text]);
  const selected=snapshots[snapshot]??snapshots.at(-1);
  const usage=useMemo(()=>{const calls=run?.state?.llmCalls??[];return {calls:calls.length,tokens:calls.reduce((n:number,c:any)=>n+(c.usage?.total_tokens??0),0),latency:calls.reduce((n:number,c:any)=>n+(c.latencyMs??0),0)}},[run]);
  const applyRun=(next:Run)=>{setRun(next);setSnapshot(next.state?.snapshots?.length?next.state.snapshots.length-1:0)};
  useEffect(()=>{if(!busy){setElapsed(0);return}const started=Date.now();const timer=window.setInterval(()=>setElapsed(Math.floor((Date.now()-started)/1000)),500);return()=>window.clearInterval(timer)},[busy]);
  const act=async(kind:'create'|'step'|'continue')=>{setBusy(true);setError('');let pollTimer:number|undefined;try{let next;if(kind==='create')next=await request('/api/runs',{method:'POST',body:JSON.stringify({inputText:text,strategy})});else{pollTimer=window.setInterval(()=>request(`/api/runs/${run!.id}`).then(applyRun).catch(()=>{}),800);next=await request(`/api/runs/${run!.id}/${kind}`,{method:'POST'})}applyRun(next)}catch(e){if(kind!=='create'&&run?.id){try{applyRun(await request(`/api/runs/${run.id}`))}catch{}}setError(e instanceof Error?e.message:String(e))}finally{if(pollTimer)window.clearInterval(pollTimer);setBusy(false)}};
  const statusText=!run?'尚未运行':busy?`${activityNames[run.state?.activeAgent]??activityNames.pipeline} · ${elapsed}s`:`${run.stage} · ${run.status}`;
  return <main>
    <header><div className="brand"><span className="mark"><FlaskConical size={19}/></span><div><h1>BPMN 增量生成实验台</h1><p>语义分段 · Graph-JSON · 多智能体审查</p></div></div><div className={`status ${busy?'running':run?.status??'idle'}`}><i/>{statusText}</div></header>
    <section className="toolbar">
      <label>分段策略<select value={strategy} onChange={e=>setStrategy(e.target.value)}><option value="semantic">语义分段</option><option value="fixed_length">固定三句</option><option value="single_segment">单片段</option></select></label>
      <button className={!run?'primary':undefined} disabled={busy||!text.trim()} onClick={()=>act('create')}><CirclePlay size={18}/>{run?'创建新实验':'创建实验'}</button>
      {run&&<><button disabled={busy||run.stage==='COMPLETED'} onClick={()=>act('step')}><StepForward size={18}/>{run.status==='failed'?'重试当前步骤':'执行下一步'}</button><button className="primary" disabled={busy||run.stage==='COMPLETED'} onClick={()=>act('continue')}><CirclePlay size={18}/>继续全部</button></>}
      {run&&<button disabled={!run.state?.graph} onClick={()=>saveFile(`${run.id}.graph.json`,JSON.stringify(run.state.graph,null,2),'application/json')}><Download size={17}/>Graph-JSON</button>}
      {run?.state?.bpmnXml&&<button onClick={()=>saveFile(`${run.id}.bpmn`,run.state.bpmnXml,'application/xml')}><Download size={17}/>BPMN</button>}
      <div className="metrics"><span><Activity size={15}/>{usage.calls} 次调用</span><span><Layers3 size={15}/>{usage.tokens} tokens</span><span><Gauge size={15}/>{(usage.latency/1000).toFixed(1)} s</span></div>
    </section>
    {error&&<div className="error">{error}</div>}
    <div className="workspace">
      <aside className="input-panel"><h2>{run?'新实验流程描述':'流程描述'}</h2><textarea value={text} onChange={e=>setText(e.target.value)}/><div className="split-heading"><h2>切句预览</h2><span>{draftSentences.length} 句</span></div><div className="sentence-preview">{draftSentences.map((sentence,index)=><label key={index}><b>S{index+1}</b><input value={sentence} onChange={event=>{const next=[...draftSentences];next[index]=event.target.value;setText(next.join('\n'))}}/></label>)}</div><h2>语义分段</h2><div className="segments">{(run?.state?.segments??[]).map((s:any,i:number)=><button key={s.id} className={i===run?.state?.segmentIndex?'active':''} onClick={()=>setSnapshot(Math.min(i,snapshots.length-1))}><b>{s.id}</b><span>{s.title}</span><small>{s.start}—{s.end}</small></button>)}{!run?.state?.segments?.length&&<p className="hint">完成语义消解和分段后显示</p>}</div></aside>
      <section className="diagram-panel"><div className="panel-title"><div><h2>阶段 BPMN</h2><p>{selected?`${selected.segment.id} · ${selected.segment.title}`:'等待首个正式快照'}</p></div><div className="snapshot-tabs">{snapshots.map((s:any,i:number)=><button key={s.segment.id} className={i===snapshot?'active':''} onClick={()=>setSnapshot(i)}>{s.segment.id}</button>)}</div></div><BpmnViewer xml={selected?.bpmnXml}/></section>
      <aside className="detail-panel"><h2>阶段详情</h2><Detail title="当前 Graph-JSON" value={run?.state?.graph}/><Detail title="本轮 Graph Patch" value={run?.state?.subgraph}/><Detail title="Repair 整体计划" value={run?.state?.repairPlan}/><Detail title="本轮编辑函数" value={run?.state?.edits}/><Detail title="结构问题" value={run?.state?.issues}/><Detail title="Reviewer Findings" value={run?.state?.reviewFindings}/><Detail title="Generate 审查决定" value={run?.state?.generatorReview}/><Detail title="已拒绝 Findings" value={run?.state?.rejectedFindings}/><Detail title="模型调用统计" value={run?.state?.llmCalls}/></aside>
    </div>
  </main>
}

function Detail({title,value}:{title:string,value:any}){const [open,setOpen]=useState(false);return <div className="detail"><button onClick={()=>setOpen(!open)}><span>{title}</span><ChevronRight size={16} className={open?'rotate':''}/></button>{open&&<pre>{JSON.stringify(value??null,null,2)}</pre>}</div>}
