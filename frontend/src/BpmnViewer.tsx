import { useEffect, useRef } from 'react';
import NavigatedViewer from 'bpmn-js/lib/NavigatedViewer';

export function BpmnViewer({xml}:{xml?:string|null}) {
  const ref=useRef<HTMLDivElement>(null);
  useEffect(()=>{
    if(!ref.current||!xml) return;
    const viewer=new NavigatedViewer({container:ref.current});
    viewer.importXML(xml).then(()=>viewer.get('canvas').zoom('fit-viewport')).catch(()=>{});
    return ()=>viewer.destroy();
  },[xml]);
  return <div className="bpmn-canvas" ref={ref}>{!xml&&<div className="empty">完成一个语义片段后，这里会出现 BPMN 快照</div>}</div>;
}
