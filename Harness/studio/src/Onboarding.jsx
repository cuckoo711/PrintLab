import React, {useEffect, useState} from 'react';
import {ArrowRight} from 'lucide-react';
import './onboarding.css';

export default function Onboarding({onNavigate, onDocument}) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch('/api/onboarding', {cache: 'no-store', signal: controller.signal});
        if (!response.ok) throw new Error(`配置读取失败（${response.status}）`);
        setData(await response.json());
        setError('');
      } catch (failure) {
        if (failure.name !== 'AbortError') setError(failure.message);
      }
    }
    load();
    window.addEventListener('printlab:refresh', load);
    return () => {
      controller.abort();
      window.removeEventListener('printlab:refresh', load);
    };
  }, []);

  // The Sarah and Mike steps describe external agent chats. This workspace uses its own assistant, so those steps are not shown.
  const nextActions = (data?.next_actions || []).filter(action => action.kind !== 'role');

  function openDocument(row) {
    onDocument({title: row.name, path: row.path, url: '/api/documents/' + row.path});
  }

  return <div className="onboarding-page">
    <header className="page-heading">
      <div><h1>配置与队伍</h1><p>共同设备、使用习惯和项目进度，供每个会话接续。</p></div>
    </header>
    {error && <div role="alert" className="inline-error">{error}。{data ? '保留上次读取的资料。' : '请刷新重试。'}</div>}
    {!data && !error && <p className="muted">正在读取共同配置…</p>}
    {data && <>
      <section className="onboarding-panel">
        <h2>接下来</h2>
        {nextActions.length > 0 ? <ul>{nextActions.map((action, index) =>
          <li key={`${action.kind}-${index}`}>{action.text}</li>
        )}</ul> : <p>基础配置已记录，可以继续自己的作品。</p>}
        {data.questions.length > 0 && <details>
          <summary>首次会话需要补齐的信息</summary>
          <ul>{data.questions.map(question => <li key={question.id}>{question.text}</li>)}</ul>
        </details>}
      </section>
      <section className="onboarding-panel">
        <h2>我的设备与材料</h2>
        <p>{data.device_card?.model || '机型待确认'} · {data.device?.message || '实时连接待检查'}</p>
        <p>{data.inventory?.state === 'online' ? `库存已读取，当前有 ${data.inventory.items.length} 条料卷记录。` : '库存尚未读取。'}</p>
        <button className="text-link" onClick={() => onNavigate('device')}>查看设备、已装材料与库存<ArrowRight size={16}/></button>
      </section>
      <section className="onboarding-panel">
        <h2>共同资料</h2>
        <div className="onboarding-documents">{data.shared_documents.filter(row => row.sha256).map(row =>
          <button className="text-link" key={row.name} onClick={() => openDocument(row)}>{row.path}<ArrowRight size={16}/></button>
        )}</div>
        <p className="muted onboarding-path">{data.workspace}</p>
      </section>
    </>}
  </div>;
}
