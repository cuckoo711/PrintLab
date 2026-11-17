import React,{useEffect,useState} from 'react';
import './printer-picker.css';

async function json(url,options){const response=await fetch(url,{cache:'no-store',...options});let data={};try{data=await response.json()}catch{}if(!response.ok)throw new Error(data.detail||data.message||`请求失败 (${response.status})`);return data}

// Lists the printers Bambuddy knows and switches which one this workbench is bound to.
// Switching only changes the selected printer id; it never sends or approves anything.
export default function PrinterPicker({onSwitched}){
 const topLevel=window.top===window.self;
 const [data,setData]=useState(null),[error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false);
 async function load(){try{setData(await json('/api/printers'));setError('')}catch(e){setData(null);setError(e.message)}}
 useEffect(()=>{load()},[]);
 const printers=Array.isArray(data?.printers)?data.printers:[];
 if(printers.length<2&&!error)return null; // one printer: nothing to choose
 const selected=printers.find(p=>p.selected);
 const active=Array.isArray(data?.active_tasks)?data.active_tasks:[];
 async function change(event){
  const id=Number(event.target.value);
  const target=printers.find(p=>p.id===id);
  if(!target||target.selected||busy||!topLevel)return;
  const mismatch=target.matches_device_card===false;
  const text=`把工作台切换到「${target.name}」（${target.model}）？\n\n`+
   (mismatch?`设备卡目前记录的是 ${data.device_card_model||'另一台设备'}，里面的供料与喷嘴信息不适用于 ${target.model}，页面上与之相关的内容可能不准确，需要你之后重新核对。\n\n`:'')+
   '已有的切片与批准会失效，需要为这台设备重新生成。这一步只改“当前设备”，不会发送或批准任何打印。';
  event.target.value=String(selected?.id??'');
  if(!window.confirm(text))return;
  setBusy(true);setError('');setNotice('');
  try{
   const session=await json('/api/prepared/session');
   const result=await json('/api/printer/select',{method:'POST',headers:{'Content-Type':'application/json','X-PrintLab-CSRF':session.csrf_token},body:JSON.stringify({printer_id:id})});
   setNotice(result.message||'已切换');
   await load();
   onSwitched&&onSwitched();
  }catch(e){setError(e.message)}finally{setBusy(false)}
 }
 return <div className="printer-picker">
  <label htmlFor="printer-select">当前设备</label>
  <select id="printer-select" value={selected?.id??''} onChange={change} disabled={busy||!topLevel||active.length>0||!printers.length}>
   {printers.map(p=><option key={p.id} value={p.id}>{p.name}（{p.model}）</option>)}
  </select>
  {!topLevel&&<p className="printer-picker-note">请独立打开工作台后再切换设备。</p>}
  {topLevel&&active.length>0&&<p className="printer-picker-note">有进行中或结果不明的打印任务（{active.slice(0,2).join('、')}），先处理完再切换。</p>}
  {selected&&selected.matches_device_card===false&&<p className="printer-picker-warn" role="status">设备卡记录的是 {data.device_card_model}，与这台 {selected.model} 不一致；供料与喷嘴信息不适用，请重新核对。</p>}
  {notice&&<p className="printer-picker-note" role="status">{notice}</p>}
  {error&&<p className="printer-picker-error" role="alert">{error}</p>}
 </div>
}
