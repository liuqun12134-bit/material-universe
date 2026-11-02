import { createContext, useContext, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { blurActiveEditor, ImeTextarea, isImeKey } from './ImeTextarea';
import {
  addEdge, applyEdgeChanges, applyNodeChanges, Background, Controls, Handle, MiniMap,
  NodeResizer, Position, ReactFlow, ReactFlowProvider, useReactFlow, type Connection, type Edge,
  type EdgeChange, type NodeChange, type OnConnectEnd, type Viewport
} from '@xyflow/react';
import { api, media, upload, ApiError, type Asset, type AssetGroup, type Bootstrap, type CanvasDocument, type CanvasSummary, type Job, type GeneralModel, type ModelFamily, type CostEstimate } from './api';
import { addInputEdge, availableModel, bindImportedResult, canEditAfterSubmitError, canReuseJob, checkInput, contextMenuPosition, copyCards, deleteCards, configFromJob, draftForFamily, switchFamilyDraft, switchSwapProvider, editableSwapPrompt, inputAssetIds, isFrozenTask, migrateLegacyDrafts,  protectedRemovalError, reusableActualPrompt, selectForContextMenu, setReferenceBound, syncDraftReferences, taskPrompts, taskReferenceIds, withImportedAsset, jobFor, worldPosition as nodeWorldPosition, type Card, type CardData } from './logic';

type Snapshot = { nodes: Card[]; edges: Edge[]; viewport: Viewport };
type CanvasActions = {
  assets: Asset[]; jobs: Job[]; edges: Edge[]; pendingIds: Set<string>; models: Bootstrap['models']; generalModels: GeneralModel[]; modelFamilies: ModelFamily[];
  update: (id: string, patch: Partial<CardData>, history?: boolean) => void;
  submit: (id: string) => Promise<void>; recover: (id: string) => Promise<void>;
  cancel: (id: string) => Promise<void>; reuse: (id: string) => void;
  continueFrom: (id: string) => Promise<void>;
  toggleReference: (taskId: string, assetId: string, checked: boolean) => void;
  report: (message: string) => void;
};
const Context = createContext<CanvasActions | null>(null);
const useActions = () => useContext(Context)!;
const uid = () => crypto.randomUUID().replaceAll('-', '');
const labels = { general: '自由创作', full: 'AI 视频换品', lipsync: 'AI 换口型', repair: 'AI 视频修复画质' };
const statusNames: Record<string, string> = { queued: '排队中', running: '执行中', success: '已完成', failed: '失败', attention: '需处理', paused: '待启动', cancelled: '已取消' };
const clone = <T,>(value: T): T => structuredClone(value);
const pauseOtherVideos = (active: HTMLVideoElement) => document.querySelectorAll('video').forEach(video => { if (video !== active) video.pause(); });
const timeLabel = (stamp?: number) => stamp ? new Date(stamp * 1000).toLocaleString() : '';
const elapsedLabel = (start?: number, finish?: number) => {
  if (!start) return '';
  const seconds = Math.max(0, Math.floor((finish || Date.now() / 1000) - start));
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
};

function ResultDownloadButton({ job }: { job: Job }) {
  const { report } = useActions();
  const [busy, setBusy] = useState(false);
  const download = async () => {
    if (busy) return;
    setBusy(true);
    try {
      if (window.pywebview?.api.download_result) {
        const result = await window.pywebview.api.download_result(job.id);
        if (result.error) throw new Error(result.error);
        if (!result.cancelled) report(`已保存到 ${result.path}`);
      } else {
        const info = await api<{ name: string }>(`/api/jobs/${job.id}/download-info`);
        const link = document.createElement('a');
        link.href = `${media('job', job.id)}&download=1`; link.download = info.name;
        document.body.appendChild(link); link.click(); link.remove();
        report('已开始下载生成结果。');
      }
    } catch (reason) { report(reason instanceof Error ? reason.message : '保存失败，请重试。'); }
    finally { setBusy(false); }
  };
  return <button type="button" className="secondary nodrag" disabled={busy} onClick={() => void download()}>{busy ? '正在保存…' : '下载'}</button>;
}

function AssetPicker({ value, kind, onChange, empty = '请选择素材' }: { value?: string; kind: Asset['kind']; onChange: (value: string) => void; empty?: string }) {
  const { assets } = useActions();
  const [open, setOpen] = useState(false);
  return <div className="asset-slot"><ReferenceStrip ids={value ? [value] : []} onRemove={() => onChange('')} /><button type="button" className="secondary" onClick={() => setOpen(true)}>{value ? '更换素材' : `＋ ${empty}`}</button>
    {open && <ReferencePicker title={empty} assets={assets.filter(asset => asset.kind === kind)} selected={value ? [value] : []} onChoose={asset => { onChange(asset.id); setOpen(false); }} onClose={() => setOpen(false)} />}</div>;
}

const assetKindName = (kind: Asset['kind']) => kind === 'image' ? '图片' : kind === 'video' ? '视频' : '音频';
function AssetThumb({ asset }: { asset: Asset }) {
  return asset.kind === 'audio' ? <span className="thumb-audio">♫</span> : <img src={media('thumb', asset.id)} alt={asset.name} loading="lazy" />;
}
function ImageResult({ job }: { job: Job }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false); };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [open]);
  return <><button type="button" className="image-result nodrag nopan" title="放大查看图片" onClick={() => setOpen(true)}><img className="poster" src={media('job', job.id)} alt="生成图片，点击放大" /></button>
    {open && createPortal(<div className="dialog-backdrop nodrag nopan" onMouseDown={event => { if (event.target === event.currentTarget) setOpen(false); }}><div className="dialog media-preview" role="dialog" aria-label="生成图片预览"><div className="picker-heading"><strong>生成图片</strong><button onClick={() => setOpen(false)}>关闭</button></div><img src={media('job', job.id)} alt="生成图片" /></div></div>, document.body)}</>;
}

function AssetPreview({ asset, onClose }: { asset: Asset; onClose: () => void }) {
  useEffect(() => { const close = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); }; window.addEventListener('keydown', close); return () => window.removeEventListener('keydown', close); }, [onClose]);
  return createPortal(<div className="dialog-backdrop nodrag nopan" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><div className="dialog media-preview"><div className="picker-heading"><strong>{asset.name}</strong><button onClick={onClose}>关闭</button></div>{asset.kind === 'image' ? <img src={media('asset', asset.id)} alt={asset.name} /> : asset.kind === 'video' ? <video src={media('asset', asset.id)} controls controlsList="nodownload" autoPlay onPlay={event => pauseOtherVideos(event.currentTarget)} /> : <audio src={media('asset', asset.id)} controls autoPlay />}</div></div>, document.body);
}
function ReferenceStrip({ ids, onRemove }: { ids: string[]; onRemove?: (id: string) => void }) {
  const { assets } = useActions();
  const [preview, setPreview] = useState<Asset | null>(null);
  return <div className="reference-strip nodrag nopan nowheel">{ids.map((value, index) => {
    const asset = assets.find(item => item.id === value || item.path === value);
    const typeIndex = asset ? ids.slice(0, index + 1).filter(id => assets.find(item => item.id === id || item.path === id)?.kind === asset.kind).length : index + 1;
    return <div className="reference-tile" key={`${value}-${index}`} title={asset?.name || `素材缺失：${value}`}><button className="reference-preview" disabled={!asset} onClick={() => asset && setPreview(asset)}>{asset ? <AssetThumb asset={asset} /> : <span className="missing-thumb">素材缺失</span>}<span className="reference-badge">{asset ? assetKindName(asset.kind) : '参考'}{typeIndex} · {index + 1}</span></button><span className="reference-name">{asset?.name || value}</span>{onRemove && <button className="reference-remove" aria-label={`移除参考 ${index + 1}`} onClick={() => onRemove(value)}>×</button>}</div>;
  })}{preview && <AssetPreview asset={preview} onClose={() => setPreview(null)} />}</div>;
}
function ReferencePicker({ title = '添加参考素材', assets, selected, disabled, onChoose, onClose }: { title?: string; assets: Asset[]; selected: string[]; disabled?: (asset: Asset) => boolean; onChoose: (asset: Asset) => void; onClose: () => void }) {
  const [query, setQuery] = useState('');
  useEffect(() => { const close = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.stopPropagation(); onClose(); } }; window.addEventListener('keydown', close); return () => window.removeEventListener('keydown', close); }, [onClose]);
  return createPortal(<div className="dialog-backdrop nodrag nopan" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><div className="dialog reference-picker"><div className="picker-heading"><h2>{title}</h2><button onClick={onClose}>完成</button></div><input autoFocus placeholder="搜索素材" value={query} onChange={event => setQuery(event.target.value)} /><div className="picker-grid">{assets.filter(asset => asset.name.toLocaleLowerCase().includes(query.toLocaleLowerCase())).map(asset => <button className={`picker-asset ${selected.includes(asset.id) ? 'chosen' : ''}`} key={asset.id} disabled={disabled?.(asset)} onClick={() => onChoose(asset)}><AssetThumb asset={asset} /><span>{asset.name}</span><small>{assetKindName(asset.kind)}{selected.includes(asset.id) ? ' · 已绑定' : ''}</small></button>)}</div>{!assets.length && <p>暂无可用素材，请先从左侧导入。</p>}</div></div>, document.body);
}

function CardShell({ children, selected, accent = 'blue' }: { children: React.ReactNode; selected: boolean; accent?: string }) {
  return <div className={`card nowheel ${selected ? 'selected' : ''} ${accent}`}><Handle className="connection-handle" type="target" position={Position.Left} />{children}<Handle className="connection-handle" type="source" position={Position.Right} /></div>;
}

function AssetCard({ id, data, selected }: { id: string; data: CardData; selected: boolean }) {
  const { assets } = useActions();
  const asset = assets.find(item => item.id === data.assetId);
  if (!asset) return <CardShell selected={selected} accent="amber"><div className="card-title">素材已删除</div><div className="muted">请移除此卡片</div></CardShell>;
  return <CardShell selected={selected} accent="amber">
    <div className="eyebrow">{asset.kind === 'image' ? '图片素材' : asset.kind === 'audio' ? '音频素材' : '视频素材'}</div>
    <div className="card-title" title={asset.name}>{asset.name}</div>
    {asset.kind === 'image' && <img className="poster" src={media('thumb', id === '' ? '' : asset.id)} alt={asset.name} />}
    {asset.kind === 'video' && (selected ? <video className="poster nodrag" src={media('asset', asset.id)} controls controlsList="nodownload" preload="metadata" poster={media('thumb', asset.id)} onPlay={event => pauseOtherVideos(event.currentTarget)} /> : <img className="poster" src={media('thumb', asset.id)} alt={asset.name} />)}
    {asset.kind === 'audio' && (selected ? <audio className="audio nodrag" src={media('asset', asset.id)} controls preload="metadata" /> : <div className="audio-placeholder">♫</div>)}
    <div className="meta">{(asset.size / 1024 / 1024).toFixed(1)} MB · 素材库副本</div>
  </CardShell>;
}

function TaskCard({ id, data, selected }: { id: string; data: CardData; selected: boolean }) {
  const actions = useActions();
  const [referencePicker, setReferencePicker] = useState<Asset['kind'] | 'all' | null>(null);
  const config = data.config || {};
  const kind = data.kind || 'full';
  const retired = ['full', 'lipsync', 'repair'].includes(kind) || ['sync_lipsync', 'kaiyuncode-omni_video_edit', 'flashvsr_upscale'].includes(config.model);
  const job = jobFor(data, actions.jobs);
  const frozen = Boolean(data.requestId || data.jobId || job), pending = actions.pendingIds.has(id);
  const field = (name: string, value: any) => actions.update(id, { config: { ...config, [name]: value } });
  const model = availableModel(config.model, actions.generalModels);
  const referenceIds = inputAssetIds(id, actions.edges);
  const family = actions.modelFamilies.find(item => item.id === (config.familyId || model?.familyId));
  const familyChoices = actions.modelFamilies.filter(item => item.nodeType === (config.nodeType || family?.nodeType));
  const referenceLimits = family?.referenceLimits || model?.referenceLimits;
  const familyProvider = family?.provider || model?.provider || 'fal';
  const providerChoices = [...new Map(familyChoices.map(item => [item.provider || 'fal', item.providerLabel || item.provider || 'fal'])).entries()];
  const providerFamilies = familyChoices.filter(item => (item.provider || 'fal') === familyProvider);
  const swapModels = actions.models.filter(item => ['fal_wan3', 'dashscope_wan3', 'kaiyuncode_wan3'].includes(item.adapter));
  const swapProvider = config.provider || swapModels.find(item => item.model === config.model)?.provider || 'fal';
  const swapProviderChoices = [...new Set(swapModels.map(item => item.provider || 'fal'))];
  const chooseSwapProvider = (provider: string) => {
    const next = swapModels.find(item => item.provider === provider);
    if (next) actions.update(id, { config: switchSwapProvider({ ...config, provider: swapProvider }, provider, next.model) });
  };
  const chooseFamily = (next: ModelFamily) => {
    if ((['image', 'video', 'audio'] as const).some(kind => referenceIds.filter(value => actions.assets.find(asset => asset.id === value)?.kind === kind).length > next.referenceLimits[kind].max)) { actions.report('当前参考素材与所选模型不兼容。请先移除对应参考。'); return; }
    actions.update(id, { config: switchFamilyDraft({ ...config, provider: familyProvider }, next, referenceIds) });
  };
  const estimateKey = JSON.stringify({ model: config.model, provider: config.provider, familyId: config.familyId, referenceAssetIds: referenceIds, parameters: config.parameters });
  const [estimate, setEstimate] = useState<{ key: string; value: CostEstimate } | null>(null);
  useEffect(() => {
    if (kind !== 'general' || frozen || !selected) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      api<CostEstimate>('/api/jobs/estimate', 'POST', { config: { ...config, referenceAssetIds: referenceIds } }).then(value => {
        if (!cancelled) setEstimate({ key: estimateKey, value });
      }).catch(() => { if (!cancelled) setEstimate({ key: estimateKey, value: { status: 'unknown', reason: '暂时无法获取费用。' } }); });
    }, 250);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [estimateKey, selected, frozen, kind]);
  const shownEstimate = estimate?.key === estimateKey ? estimate.value : null;
  const priceCurrency = shownEstimate?.currency || 'USD';
  const priceSymbol = priceCurrency === 'CNY' ? '¥' : priceCurrency === 'USD' ? '$' : priceCurrency + ' ';
  const toggleReference = (assetId: string, checked: boolean) => actions.toggleReference(id, assetId, checked);
  const shownReferences = taskReferenceIds(data, job, referenceIds);
  const copyPrompt = async (value: string) => { try { await navigator.clipboard.writeText(value); actions.report('提示词已复制。'); } catch { actions.report('请选中提示词，按 Ctrl+C 复制。'); } };
  const parameterField = (parameter: GeneralModel['parameters'][number]) => <label key={parameter.key} className={parameter.type === 'string' ? 'parameter-text' : undefined}>{parameter.label || parameter.key}
    {parameter.type === 'string' ? <ImeTextarea value={String(config.parameters?.[parameter.key] ?? parameter.default)} onCommit={value => field('parameters', { ...config.parameters, [parameter.key]: value })} placeholder="https://…" />
      : parameter.type === 'enum' ? <select value={String(config.parameters?.[parameter.key] ?? parameter.default)} onChange={event => field('parameters', { ...config.parameters, [parameter.key]: parameter.enum?.find(value => String(value) === event.target.value) ?? event.target.value })}>{(parameter.enum || []).map(value => <option key={value} value={value}>{value}</option>)}</select>
      : parameter.type === 'boolean' ? <input type="checkbox" checked={Boolean(config.parameters?.[parameter.key] ?? parameter.default)} onChange={event => field('parameters', { ...config.parameters, [parameter.key]: event.target.checked })} />
        : <input type={parameter.type === 'number' || parameter.type === 'integer' ? 'number' : 'text'} min={parameter.min} max={parameter.max} step={parameter.type === 'integer' ? 1 : 'any'} value={String(config.parameters?.[parameter.key] ?? parameter.default)} onChange={event => field('parameters', { ...config.parameters, [parameter.key]: parameter.type === 'number' || parameter.type === 'integer' ? Number(event.target.value) : event.target.value })} />}
  </label>;
  const select = (name: string, assetKind: Asset['kind'], label: string) => <label>{label}<AssetPicker kind={assetKind} value={config[name]} onChange={value => field(name, value)} /></label>;
  const output = job?.result?.output && job.status === 'success';
  const selectedAssets = (kind === 'general' ? referenceIds : Object.values(config).flat().filter((value): value is string => typeof value === 'string'))
    .map(value => actions.assets.find(asset => asset.id === value)?.name).filter(Boolean);
  return <CardShell selected={selected} accent="violet">
    <div className="eyebrow">生成任务 <span className={`pill ${job?.status || 'draft'}`}>{job ? statusNames[job.status] || job.status : frozen ? '等待确认' : '草稿'}</span></div>
    <div className="card-title">{kind === 'general' ? family ? `${family.label} ${family.nodeType === 'lipsync' ? '对口型' : family.nodeType === 'image' ? '图片创作' : '视频创作'}` : model?.label || labels.general : labels[kind]}</div>
    {job && <div className="stage"><strong>{job.stage || statusNames[job.status] || job.status}</strong><small>{job.started ? `已运行 ${elapsedLabel(job.started, job.finished)}` : job.created ? `创建于 ${timeLabel(job.created)}` : ''}{job.events?.length ? ` · 最近更新 ${timeLabel(job.events[job.events.length - 1].time)}` : ''}</small></div>}
    {!selected && !job && selectedAssets.length > 0 && <div className="task-summary" title={selectedAssets.join('、')}>{selectedAssets.join(' · ')}</div>}
    {data.warning && <div className="error-text">{data.warning}</div>}
    {frozen && !job && <details className="events nodrag"><summary>提交确认中 · 查看冻结参数</summary><div className="frozen-config">{Object.entries(config).map(([key, value]) => <div key={key}><b>{key}</b>　{typeof value === 'string' ? value : JSON.stringify(value)}</div>)}</div></details>}
    {!frozen && selected && <div className={`task-fields nodrag nopan nowheel ${kind === 'general' ? 'creation-fields' : ''}`} style={pending ? { pointerEvents: 'none', opacity: .65 } : undefined}>
      {kind === 'general' && <>
        <div className="creation-references"><div className="reference-heading"><strong>参考素材</strong><small>{referenceIds.length ? `${referenceIds.length} 个已绑定` : Object.values(referenceLimits || {}).some(limit => limit.max > 0) ? '连接素材，或点击添加参考' : '此模型使用文字生成'}</small></div>
          <ReferenceStrip ids={referenceIds} onRemove={assetId => toggleReference(assetId, false)} />
          {model && Object.values(referenceLimits || {}).some(limit => limit.max > 0) && <div className="reference-add-row">{referenceLimits?.video?.max === 1 && referenceLimits?.audio?.max === 1 && !model.promptRequired
            ? (['video', 'audio'] as const).map(assetKind => <button type="button" className="reference-add" key={assetKind} onClick={() => setReferencePicker(assetKind)}>＋ {assetKindName(assetKind)}参考</button>)
            : <button type="button" className="reference-add" onClick={() => setReferencePicker('all')}>＋ 添加参考</button>}</div>}
          {model && <small className="reference-limits">{(['image', 'video', 'audio'] as const).filter(assetKind => referenceLimits![assetKind]?.max > 0).map(assetKind => `${assetKindName(assetKind)} ${referenceLimits![assetKind].min}–${referenceLimits![assetKind].max} 个`).join(' · ') || '文生视频无需参考素材'}</small>}
        </div>
        {model?.promptRequired !== false && <label className="creation-prompt">提示词<ImeTextarea value={config.prompt ?? ''} onCommit={value => field('prompt', value)} placeholder="描述想生成的画面、动作与风格…" /></label>}
        <div className="creation-controls">{providerChoices.length > 1 && <label>服务商<select value={familyProvider} onChange={event => { const next = familyChoices.find(item => (item.provider || 'fal') === event.target.value); if (next) chooseFamily(next); }}>{providerChoices.map(([provider, label]) => <option key={provider} value={provider}>{label}</option>)}</select></label>}<label className="model-field">创作模型{providerFamilies.length > 1 ? <select value={family?.id || ''} onChange={event => { const next = providerFamilies.find(item => item.id === event.target.value); if (next) chooseFamily(next); }}>{providerFamilies.map(item => <option value={item.id} key={item.id}>{item.label}</option>)}</select> : <span className="fixed-model">{family?.label || model?.label || '模型不可用'}</span>}</label>
          {(family?.routes.filter(route => route.when === 'always').length || 0) > 1 && <label>型号<select value={config.model} onChange={event => field('model', event.target.value)}>{family!.routes.map(route => <option key={route.modelId} value={route.modelId}>{route.label}</option>)}</select></label>}
          {(family?.parameters || model?.parameters)?.filter(parameter => parameter.type !== 'boolean' && !parameter.placement).map(parameterField)}
        </div>
        <small className="route-hint">{family?.providerLabel || familyProvider} · {model?.label}{family?.routes.every(route => route.when === 'always') ? '' : ' · 按参考素材自动选择'}</small>
        {model?.id === 'kaiyuncode-seedance-2.5-tj' && <small className="route-hint">特价多参考模型，仅支持 720p。</small>}
        {model?.id === 'kaiyuncode-omni_video_edit' && <small className="route-hint">原视频须为 MP4，最长 30 秒；输出时长跟随原视频。点击生成后，本地参考图会上传至 Litterbox 并公开保留 24 小时，再将链接交给 Omni。</small>}
        <div className="direct-switches">{(family?.parameters || model?.parameters)?.filter(parameter => parameter.type === 'boolean' || parameter.placement === 'direct').map(parameterField)}</div>
        <div className="cost-estimate" title={shownEstimate?.sourceUrl ? `价格核对 ${shownEstimate.verifiedAt} · ${shownEstimate.sourceUrl}` : undefined}>{!shownEstimate ? '正在估算费用…' : shownEstimate.status === 'unknown' ? `费用未知 · ${shownEstimate.reason}` : <><strong>预估费用 ≈ {priceSymbol}{shownEstimate.amount?.toFixed(2)} {priceCurrency}</strong><small>{shownEstimate.durationBasis === 'shorter_media' ? `按较短素材 ${shownEstimate.billableSeconds?.toFixed(2)} 秒 · ${priceSymbol}${shownEstimate.unitRate}/分钟` : `输出 ${shownEstimate.outputSeconds} 秒${shownEstimate.referenceVideoSeconds ? ` + 参考视频 ${shownEstimate.referenceVideoSeconds.toFixed(2)} 秒` : ''} · ${priceSymbol}${shownEstimate.unitRate}/秒`}</small>{shownEstimate.note && <small>{shownEstimate.note}</small>}</>}</div>
        {!model && <div className="error-text">此模型已不可用于新任务。请选择可用模型；现有结果仍可查看。</div>}
      </>}
      {kind === 'full' && <>
        {select('sourceAssetId', 'video', '原视频')}
        {select('imageAssetId', 'image', '产品参考图')}
        <div className="reference-field"><span>其他参考图</span><ReferenceStrip ids={config.extraImageAssetIds || []} onRemove={assetId => field('extraImageAssetIds', (config.extraImageAssetIds || []).filter((value: string) => value !== assetId))} /><button className="secondary" type="button" onClick={() => setReferencePicker('image')}>＋ 添加参考图</button></div>
        <label>提示词方式<select value={config.promptMode || 'analyze'} onChange={event => field('promptMode', event.target.value)}><option value="analyze">重新分析视频</option><option value="reuse">使用已有提示词</option></select></label>
        {data.actualPrompt && <button className="secondary" onClick={() => actions.update(id, { config: { ...config, promptMode: 'reuse', prompt: reusableActualPrompt(data, job) } })}>改用上次实际提示词</button>}
        {(config.promptMode || 'analyze') === 'analyze' ? <label>产品尺寸关系<ImeTextarea value={config.relation || ''} onCommit={value => field('relation', value)} placeholder="描述参考图产品与原视频产品的尺寸、外形关系" /></label> : <label>已有提示词<ImeTextarea value={config.prompt || ''} onCommit={value => field('prompt', value)} placeholder="原样使用这段提示词" /></label>}
        <label><input type="checkbox" checked={config.removeSubtitles === true} onChange={event => field('removeSubtitles', event.target.checked)} />去字幕模式（先去掉参考视频中的字幕）</label>
        <label><input type="checkbox" checked={config.enablePromptExpansion ?? true} onChange={event => field('enablePromptExpansion', event.target.checked)} />提示词扩写（优化生成提示词）</label>
        {swapProvider === 'fal' && <label><input type="checkbox" checked={config.enableThinking ?? true} onChange={event => field('enableThinking', event.target.checked)} />思考模式（生成前增强推理）</label>}
        <div className="field-row"><label>时长<input type="number" min="2" max="30" value={config.duration ?? 5} onChange={event => field('duration', Number(event.target.value))} /></label><label>比例<select value={config.aspectRatio || '9:16'} onChange={event => field('aspectRatio', event.target.value)}>{['9:16', '16:9', '1:1', '4:3', '3:4'].map(x => <option key={x}>{x}</option>)}</select></label></div>
        <label>服务商<select value={swapProvider} onChange={event => chooseSwapProvider(event.target.value)}>{swapProviderChoices.map(provider => <option key={provider} value={provider}>{provider === 'dashscope' ? '阿里云官方' : provider === 'kaiyuncode' ? 'KaiyunCode' : provider}</option>)}</select></label>
        <div className="field-row"><label>清晰度<select value={config.resolution || '480p'} onChange={event => field('resolution', event.target.value)}>{['480p', '720p', '1080p'].map(x => <option key={x}>{x}</option>)}</select></label><label>模型<select value={config.model || 'wan3.0-video'} onChange={event => actions.update(id, { config: { ...config, model: event.target.value, provider: swapProvider } })}>{swapModels.filter(m => (m.provider || 'fal') === swapProvider).map(m => <option value={m.model} key={m.model}>{m.label || m.model}</option>)}</select></label></div>
      </>}
      {kind === 'lipsync' && <>{select('videoAssetId', 'video', '视频')}{select('audioAssetId', 'audio', '音频')}</>}
    {kind === 'repair' && <>{select('videoAssetId', 'video', '视频')}<div className="field-row"><label>放大倍数<input type="number" min="1" max="4" step="0.1" value={config.options?.upscale_factor ?? 2} onChange={event => field('options', { ...config.options, upscale_factor: Number(event.target.value) })} /></label><label>速度<select value={config.options?.acceleration || 'regular'} onChange={event => field('options', { ...config.options, acceleration: event.target.value })}><option value="regular">最佳质量</option><option value="high">更快</option><option value="full">最快</option></select></label></div><div className="checks"><label><input type="checkbox" checked={config.options?.color_fix ?? true} onChange={event => field('options', { ...config.options, color_fix: event.target.checked })} />颜色修复</label><label><input type="checkbox" checked={config.options?.preserve_audio ?? true} onChange={event => field('options', { ...config.options, preserve_audio: event.target.checked })} />保留音频</label></div></>}
    </div>}
    {selected && frozen && <div className="historical-fields task-fields nodrag nopan nowheel"><div className="reference-heading"><strong>已提交参考素材</strong></div><ReferenceStrip ids={shownReferences} />
      {taskPrompts(data, job).filter(prompt => prompt.value).map(prompt => <label className="creation-prompt" key={prompt.label}><span>{prompt.label}<button type="button" className="copy-prompt secondary" onClick={() => void copyPrompt(prompt.value)}>复制</button></span><textarea readOnly value={prompt.value} onFocus={event => event.currentTarget.select()} /></label>)}
    </div>}
    {selected && referencePicker && !frozen && kind === 'full' && <ReferencePicker title="其他参考图" assets={actions.assets.filter(asset => asset.kind === 'image' && asset.id !== config.imageAssetId)} selected={config.extraImageAssetIds || []} onChoose={asset => field('extraImageAssetIds', (config.extraImageAssetIds || []).includes(asset.id) ? config.extraImageAssetIds.filter((value: string) => value !== asset.id) : [...(config.extraImageAssetIds || []), asset.id])} onClose={() => setReferencePicker(null)} />}
    {selected && referencePicker && !frozen && kind === 'general' && model && <ReferencePicker assets={actions.assets.filter(asset => referenceLimits![asset.kind]?.max > 0 && (referencePicker === 'all' || asset.kind === referencePicker))} selected={referenceIds} disabled={asset => pending || (!referenceIds.includes(asset.id) && referenceIds.filter(value => actions.assets.find(item => item.id === value)?.kind === asset.kind).length >= referenceLimits![asset.kind].max)} onChoose={asset => toggleReference(asset.id, !referenceIds.includes(asset.id))} onClose={() => setReferencePicker(null)} />}
    {job && <>
      <div className="job-info">任务 {job.id.slice(0, 8)}{job.task_id ? ` · 服务商 ${job.task_id}` : ''}</div>
      {job.error && <div className="error-text">{job.error}</div>}
      <details className="events nodrag"><summary>查看冻结配置与执行过程</summary><div className="frozen-config">{Object.entries(job.config || {}).filter(([key]) => key !== 'output').map(([key, value]) => <div key={key}><b>{key}</b>　{typeof value === 'string' ? value : JSON.stringify(value)}</div>)}</div>{job.events?.slice(-12).map((e, i) => <div key={i}>{timeLabel(e.time)}　{e.stage}</div>)}</details>
      {output && (job.result?.output_kind === 'image' ? <ImageResult job={job} /> : selected ? <video className="poster nodrag" src={media('job', job.id)} controls controlsList="nodownload" preload="metadata" poster={media('job-thumb', job.id)} onPlay={event => pauseOtherVideos(event.currentTarget)} /> : <img className="poster" src={media('job-thumb', job.id)} alt="成片封面" />)}
      {job.status === 'attention' && <button className="secondary nodrag" onClick={() => actions.recover(job.id)}>继续查询 / 下载原任务</button>}
      {job.status === 'paused' && <button className="secondary nodrag" onClick={() => actions.recover(job.id)}>检查原任务能否继续</button>}
      {job.status === 'queued' && <button className="secondary nodrag" onClick={() => actions.cancel(job.id)}>取消排队</button>}
      {!retired && canReuseJob(job, actions.assets) && (job.kind !== 'general' || availableModel(job.config.model, actions.generalModels)) ? <button className="secondary nodrag" onClick={() => actions.reuse(id)}>复用配置 · 新草稿</button>
        : <div className="error-text">{job.kind === 'general' && !availableModel(job.config.model, actions.generalModels) ? '此模型已不可用于新任务，历史结果仍可查看或恢复。' : job.config?.reuse_warning || '旧任务配置或参考素材不完整，无法直接复用；请新建草稿重新填写。'}</div>}
      {output && <button className="secondary nodrag" onClick={() => actions.continueFrom(id)}>作为参考继续创作</button>}
      {output && <ResultDownloadButton job={job} />}
    </>}
    {!job && (retired ? <div className="meta">此功能已下线。历史结果可查看或恢复，请使用图音生视频创建新任务。</div> : selected ? <button className="primary nodrag" disabled={pending || (kind === 'general' && (!model || (!frozen && !model.pricingUnverified && shownEstimate?.status !== 'known')))} onClick={() => actions.submit(id)}>{pending ? '正在预检…' : frozen ? '查询 / 确认同一任务' : '开始生成'}</button> : <div className="meta">点击卡片查看配置与开始任务</div>)}
  </CardShell>;
}

function NoteCard({ id, data, selected }: { id: string; data: CardData; selected: boolean }) {
  const { update } = useActions();
  return <CardShell selected={selected} accent="yellow"><div className="eyebrow">文字备注</div><ImeTextarea className="note-input nodrag" value={data.text || ''} onCommit={value => update(id, { text: value })} placeholder="记录想法、提示词或待办…" /></CardShell>;
}

function GroupCard({ id, data, selected }: { id: string; data: CardData; selected: boolean }) {
  const { update } = useActions();
  return <div className={`group-card ${selected ? 'selected' : ''}`}><NodeResizer isVisible={selected} minWidth={300} minHeight={200} /><input className="nodrag" value={data.title || '分组'} onChange={event => update(id, { title: event.target.value })} /><Handle type="target" position={Position.Left} /><Handle type="source" position={Position.Right} /></div>;
}

function ResultCard({ data, selected }: { data: CardData; selected: boolean }) {
  const { jobs } = useActions();
  const job = jobs.find(item => item.id === data.jobId);
  return <CardShell selected={selected} accent="green"><div className="eyebrow">生成结果</div><div className="card-title">{data.title || job?.name || '成片'}</div>
    {job?.result?.output && (job.result.output_kind === 'image' ? <ImageResult job={job} /> : selected ? <video className="poster nodrag" src={media('job', job.id)} controls controlsList="nodownload" preload="metadata" poster={media('job-thumb', job.id)} onPlay={event => pauseOtherVideos(event.currentTarget)} /> : <img className="poster" src={media('job-thumb', job.id)} alt="成片封面" />)}
    {job?.status === 'success' && job.result?.output && <ResultDownloadButton job={job} />}
    <div className="meta">来源任务 {job?.id.slice(0, 8)}</div></CardShell>;
}

const nodeTypes = { asset: AssetCard, task: TaskCard, note: NoteCard, group: GroupCard, result: ResultCard };

function Workspace() {
  const flow = useReactFlow<Card, Edge>();
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [canvases, setCanvases] = useState<CanvasSummary[]>([]);
  const [current, setCurrent] = useState<CanvasDocument | null>(null);
  const [nodes, setNodes] = useState<Card[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [viewport, setViewport] = useState<Viewport>({ x: 0, y: 0, zoom: 1 });
  const [assets, setAssets] = useState<Asset[]>([]);
  const [groups, setGroups] = useState<AssetGroup[]>([]);
  const [libraryGroup, setLibraryGroup] = useState('all');
  const [libraryKind, setLibraryKind] = useState('all');
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('就绪');
  const [saveState, setSaveState] = useState('已保存');
  const [switching, setSwitching] = useState(false);
  const [pendingIds, setPendingIds] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState('');
  const [dialog, setDialog] = useState<'new' | 'rename' | 'delete' | null>(null);
  const [dialogValue, setDialogValue] = useState('');
  const [contextMenu, setContextMenu] = useState<{ mode: 'create' | 'selection'; x: number; y: number; flowPosition: { x: number; y: number } } | null>(null);
  const contextMenuRef = useRef<HTMLDivElement | null>(null);
  const pendingConnectionRef = useRef<Connection | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const importTargetRef = useRef<{ groupId: string | null; canvasId?: string }>({ groupId: null });
  const docRef = useRef<CanvasDocument | null>(null);
  const nodesRef = useRef<Card[]>([]);
  const edgesRef = useRef<Edge[]>([]);
  const viewportRef = useRef<Viewport>({ x: 0, y: 0, zoom: 1 });
  const dirtyRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const chainRef = useRef<Promise<void>>(Promise.resolve());
  const undoRef = useRef<Snapshot[]>([]);
  const redoRef = useRef<Snapshot[]>([]);
  const submittingRef = useRef(new Set<string>());
  const assetsRef = useRef<Asset[]>([]);
  const catalogRef = useRef<GeneralModel[]>([]);
  const switchingRef = useRef(false);
  const savingRef = useRef(false);

  const report = (value: string) => { setMessage(value); setError(''); };
  const showError = (reason: unknown) => { const value = reason instanceof Error ? reason.message : String(reason); setError(value); setMessage(value); };
  const snapshot = (): Snapshot => ({ nodes: clone(nodesRef.current), edges: clone(edgesRef.current), viewport: clone(viewportRef.current) });
  const pushHistory = () => { undoRef.current.push(snapshot()); if (undoRef.current.length > 50) undoRef.current.shift(); redoRef.current = []; };
  const schedule = () => {
    dirtyRef.current = true; setSaveState('待保存');
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => { void saveNow().catch(showError); }, 650);
  };
  const replace = (nextNodes: Card[], nextEdges = edgesRef.current, nextViewport = viewportRef.current) => {
    const synced = syncDraftReferences(nextNodes, nextEdges, catalogRef.current);
    nodesRef.current = synced; edgesRef.current = nextEdges; viewportRef.current = nextViewport;
    setNodes(synced); setEdges(nextEdges); setViewport(nextViewport); schedule();
  };
  const saveNow = (): Promise<void> => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = null;
    chainRef.current = chainRef.current.catch(() => {}).then(async () => {
      if (!dirtyRef.current || !docRef.current) return;
      const source = docRef.current;
      const body = { revision: source.revision, nodes: clone(nodesRef.current), edges: clone(edgesRef.current), viewport: clone(viewportRef.current) };
      dirtyRef.current = false;
      savingRef.current = true;
      setSaveState('保存中');
      try {
        const saved = await api<CanvasDocument>(`/api/canvases/${source.id}`, 'PUT', body);
        if (docRef.current?.id === source.id) {
          docRef.current = { ...source, revision: saved.revision };
          setCurrent(docRef.current);
          setSaveState(dirtyRef.current ? '待保存' : '已保存');
        }
      } catch (reason) { dirtyRef.current = true; setSaveState('保存失败'); throw reason; }
      finally { savingRef.current = false; }
    });
    return chainRef.current;
  };

  const flushAll = async () => {
    do { await saveNow(); } while (dirtyRef.current);
  };

  const loadCanvas = async (id: string, alreadyLocked = false) => {
    if (!alreadyLocked && switchingRef.current) return;
    if (!alreadyLocked) { switchingRef.current = true; setSwitching(true); }
    try {
      await flushAll();
      const doc = await api<CanvasDocument>(`/api/canvases/${id}`);
      const migrated = migrateLegacyDrafts(doc.nodes, doc.edges, assetsRef.current, uid, catalogRef.current);
      docRef.current = doc; nodesRef.current = migrated.nodes; edgesRef.current = migrated.edges; viewportRef.current = doc.viewport;
      dirtyRef.current = false; undoRef.current = []; redoRef.current = [];
      setCurrent(doc); setNodes(migrated.nodes); setEdges(migrated.edges); setViewport(doc.viewport); setSaveState('已保存');
      if (migrated.changed) schedule();
      localStorage.setItem('material-universe-last-canvas', id);
    } finally { if (!alreadyLocked) { switchingRef.current = false; setSwitching(false); } }
  };

  useEffect(() => {
    let alive = true;
    window.canvasFlush = async () => {
      blurActiveEditor();
      switchingRef.current = true; setSwitching(true);
      try { await flushAll(); }
      catch (reason) { switchingRef.current = false; setSwitching(false); throw reason; }
    };
    const beforeUnload = (event: BeforeUnloadEvent) => {
      blurActiveEditor();
      if (dirtyRef.current || savingRef.current) { void flushAll(); event.preventDefault(); event.returnValue = ''; }
    };
    window.addEventListener('beforeunload', beforeUnload);
    api<Bootstrap>('/api/bootstrap').then(async result => {
      if (!alive) return;
      setBootstrap(result); catalogRef.current = result.generalModels || []; assetsRef.current = result.assets; setAssets(result.assets); setGroups(result.groups || []); setJobs(result.jobs); setCanvases(result.canvases);
      const last = localStorage.getItem('material-universe-last-canvas');
      await loadCanvas(result.canvases.find(item => item.id === last)?.id || result.canvases[0].id);
    }).catch(showError);
    const poll = setInterval(() => api<{ assets: Asset[]; groups: AssetGroup[]; jobs: Job[]; canvases: CanvasSummary[] }>('/api/state').then(state => {
      if (!alive) return;
      assetsRef.current = state.assets; setAssets(state.assets); setGroups(state.groups || []); setJobs(state.jobs); setCanvases(state.canvases);
      setLibraryGroup(previous => previous === 'all' || previous === 'ungrouped' || state.groups?.some(group => group.id === previous) ? previous : 'ungrouped');
    }).catch(() => {}), 1200);
    return () => { alive = false; clearInterval(poll); window.removeEventListener('beforeunload', beforeUnload); if (timerRef.current) clearTimeout(timerRef.current); void flushAll(); delete window.canvasFlush; };
  }, []);

  const update = (id: string, patch: Partial<CardData>, history = true) => {
    if (switchingRef.current) return;
    const target = nodesRef.current.find(node => node.id === id);
    if (history && target?.type === 'task' && (isFrozenTask(target) || submittingRef.current.has(id))) { report('任务已提交或正在预检，不能修改配置。'); return; }
    if (history) pushHistory();
    replace(nodesRef.current.map(node => node.id === id ? { ...node, data: { ...node.data, ...patch } } : node));
  };
  const worldPosition = (node: Card) => nodeWorldPosition(node, nodesRef.current);
  const focusCard = (id: string, type?: string) => {
    requestAnimationFrame(() => { setTimeout(() => { void flow.fitView({ nodes: [{ id }], padding: 0.12, minZoom: 0.3, maxZoom: type === 'task' ? 1 : 1.25, duration: 300 }); }, 80); });
  };
  const addCard = (type: Card['type'], data: CardData, near?: Card, sourceId?: string, at?: { x: number; y: number }) => {
    if (switchingRef.current) return '';
    pushHistory();
    const id = uid();
    const view = flow.getViewport();
    const pane = document.querySelector('.flow-wrap')?.getBoundingClientRect();
    const center = { x: ((pane?.width || 900) / 2 - view.x) / view.zoom, y: ((pane?.height || 600) / 2 - view.y) / view.zoom };
    const origin = near ? worldPosition(near) : { x: center.x - 165, y: center.y - 90 };
    let position = at || (near ? { x: origin.x + 430, y: origin.y + 30 } : origin);
    while (!at && nodesRef.current.some(node => { const existing = worldPosition(node); return Math.abs(existing.x - position.x) < 300 && Math.abs(existing.y - position.y) < 180; })) {
      position = { x: position.x, y: position.y + 240 };
    }
    const next = { id, type, position, data, selected: true } as Card;
    const nextEdges = sourceId ? [...edgesRef.current, { id: uid(), source: sourceId, target: id, type: 'smoothstep', data: { kind: 'origin' }, style: { strokeDasharray: '5 5' }, label: '来源 · 不参与生成' }] : edgesRef.current;
    replace([...nodesRef.current.map(node => ({ ...node, selected: false })), next], nextEdges);
    if (!at) focusCard(id, type);
    return id;
  };
  const addTask = (nodeType = 'video', at?: { x: number; y: number }) => {
    const family = bootstrap?.modelFamilies?.find(item => item.nodeType === nodeType);
    if (!family) { report('该类型暂未配置可用模型。'); return; }
    addCard('task', { kind: 'general', config: draftForFamily(family) }, undefined, undefined, at);
  };
  const toggleReference = (taskId: string, assetId: string, checked: boolean) => {
    if (switchingRef.current) return;
    const next = setReferenceBound(taskId, assetId, checked, nodesRef.current, edgesRef.current, assets, bootstrap?.generalModels || [], uid, submittingRef.current);
    if (next.error) { report(next.error); return; }
    if (next.nodes !== nodesRef.current || next.edges !== edgesRef.current) { pushHistory(); replace(next.nodes, next.edges); }
  };
  const submit = async (id: string) => {
    if (submittingRef.current.has(id)) return;
    submittingRef.current.add(id);
    setPendingIds(new Set(submittingRef.current));
    try {
      const node = nodesRef.current.find(item => item.id === id);
      if (!node || !docRef.current) return;
      const canvasId = docRef.current.id;
      const requestId = node.data.requestId || uid();
      if (!node.data.requestId) {
        await flushAll();
        if (docRef.current?.id !== canvasId || switchingRef.current) return;
        await api('/api/jobs/validate', 'POST', { canvasId, nodeId: id, requestId, kind: node.data.kind, config: node.data.config || {} });
        if (docRef.current?.id !== canvasId || switchingRef.current) return;
        update(id, { requestId, warning: undefined }, false);
        undoRef.current = []; redoRef.current = [];
      }
      await saveNow();
      const job = await api<Job>('/api/jobs', 'POST', { canvasId, nodeId: id, requestId });
      setJobs(previous => [job, ...previous.filter(item => item.id !== job.id)]);
      if (docRef.current?.id === canvasId) update(id, { jobId: job.id, warning: undefined }, false);
      report(`任务 ${job.id.slice(0, 8)} 已创建，画布关闭后仍会执行。`);
    } catch (reason) {
      if (reason instanceof ApiError && canEditAfterSubmitError(reason)) {
        const node = nodesRef.current.find(item => item.id === id);
        if (node?.data.requestId && !jobFor(node.data, jobs)) update(id, { requestId: undefined, warning: reason.message }, false);
      }
      showError(reason);
    } finally { submittingRef.current.delete(id); setPendingIds(new Set(submittingRef.current)); }
  };
  const recover = async (id: string) => { try { const job = await api<Job>(`/api/jobs/${id}/recover`, 'POST', {}); setJobs(previous => [job, ...previous.filter(item => item.id !== id)]); report('正在继续原任务查询或下载。'); } catch (reason) { showError(reason); } };
  const cancel = async (id: string) => { try { await api(`/api/jobs/${id}/cancel`, 'POST', {}); report('已取消本地排队。'); } catch (reason) { showError(reason); } };
  const reuse = (id: string) => {
    const source = nodesRef.current.find(node => node.id === id);
    const job = source && jobFor(source.data, jobs);
    if (!source || !job) return;
    if (!canReuseJob(job, assets)) { report('旧任务配置或参考素材不完整，无法直接复用；请新建草稿重新填写。'); return; }
    if (job.kind === 'general' && !availableModel(job.config.model, bootstrap?.generalModels || [])) { report('此模型已不可用于新任务。'); return; }
    const config = configFromJob(job, assets);
    const referenceIds: string[] = job.kind === 'general' ? config.referenceAssetIds : [];
    const taskId = addCard('task', { kind: job.kind, config: { ...config, ...(job.kind === 'general' ? { referenceAssetIds: [] } : {}) },
      actualPrompt: job.actual_prompt, sourceBasePrompt: job.kind === 'full' ? editableSwapPrompt(job) : undefined }, source, id);
    for (const assetId of referenceIds) toggleReference(taskId, assetId, true);
    report('已复制成新草稿。检查参数后点击开始，才会创建新付费任务。');
  };
  const continueFrom = async (id: string) => {
    try {
      const canvasId = docRef.current?.id;
      const source = nodesRef.current.find(node => node.id === id);
      const job = source && jobFor(source.data, jobs);
      if (!source || !job) return;
      const asset = await api<Asset>(`/api/jobs/${job.id}/asset`, 'POST', {});
      const nextAssets = withImportedAsset(assetsRef.current, asset);
      assetsRef.current = nextAssets;
      setAssets(nextAssets);
      if (docRef.current?.id !== canvasId || switchingRef.current) { report('结果已入素材库。请在当前画布从素材库加入。'); return; }
      const currentSource = nodesRef.current.find(item => item.id === id);
      if (!currentSource) { report('结果已入素材库。原任务卡片已移除，请从素材库加入。'); return; }
      const assetNodeId = addCard('asset', { assetId: asset.id }, currentSource, id);
      const assetNode = nodesRef.current.find(item => item.id === assetNodeId);
      const model = bootstrap?.generalModels?.find(item => item.id === job.config.model && item.referenceLimits[asset.kind]?.max > 0)
        || bootstrap?.generalModels?.find(item => item.referenceLimits[asset.kind]?.max > 0);
      const taskId = addCard('task', { kind: 'general', config: { model: model?.id || '', prompt: '', referenceAssetIds: [], parameters: Object.fromEntries((model?.parameters || []).map(parameter => [parameter.key, parameter.default])) } }, assetNode);
      if (model) {
        const bound = bindImportedResult(taskId, asset, nextAssets, nodesRef.current, edgesRef.current, bootstrap?.generalModels || [], uid);
        if (bound.error) { report(`结果已入素材库，但绑定参考失败：${bound.error}`); return; }
        if (bound.edges !== edgesRef.current) { pushHistory(); replace(bound.nodes, bound.edges); }
      }
      report(model ? '结果已作为参考加入新草稿。请检查参考素材和参数后手动开始。' : '结果已加入素材库和画布；当前没有兼容的创作模型，请待模型可用后选择。');
    } catch (reason) { showError(reason); }
  };
  const restore = (from: React.MutableRefObject<Snapshot[]>, to: React.MutableRefObject<Snapshot[]>) => {
    if (submittingRef.current.size) { report('正在预检，完成后再撤销或重做。'); return; }
    const previous = from.current.pop(); if (!previous) return;
    to.current.push(snapshot()); replace(previous.nodes, previous.edges, previous.viewport);
  };
  const copySelected = () => {
    if (submittingRef.current.size) { report('正在预检，完成后再复制。'); return; }
    if (!nodesRef.current.some(node => node.selected)) return;
    pushHistory();
    const next = copyCards(nodesRef.current, edgesRef.current, uid, assets);
    if (next.error) { undoRef.current.pop(); report(next.error); return; }
    replace(next.nodes, next.edges);
  };
  const deleteSelected = () => {
    if (submittingRef.current.size) { report('正在预检，完成后再删除。'); return; }
    const selected = nodesRef.current.filter(node => node.selected);
    const selectedEdges = edgesRef.current.filter(edge => edge.selected);
    if (!selected.length && !selectedEdges.length) return;
    const candidate = deleteCards(nodesRef.current, edgesRef.current);
    const selectedIds = new Set(nodesRef.current.filter(node => !candidate.nodes.some(remaining => remaining.id === node.id)).map(node => node.id));
    const error = protectedRemovalError(nodesRef.current, edgesRef.current, selectedIds, new Set(selectedEdges.map(edge => edge.id)), submittingRef.current);
    if (error) { report(error); return; }
    pushHistory();
    replace(candidate.nodes, candidate.edges.filter(edge => !edge.selected));
  };
  const groupSelected = () => {
    if (submittingRef.current.size) { report('正在预检，完成后再分组。'); return; }
    const selected = nodesRef.current.filter(node => node.selected && node.type !== 'group');
    if (selected.length < 2) { report('请先框选至少两张卡片。'); return; }
    pushHistory();
    const minX = Math.min(...selected.map(n => worldPosition(n).x)) - 25, minY = Math.min(...selected.map(n => worldPosition(n).y)) - 55;
    const maxX = Math.max(...selected.map(n => worldPosition(n).x + (n.width || 330))) + 25;
    const maxY = Math.max(...selected.map(n => worldPosition(n).y + (n.height || 180))) + 25;
    const id = uid();
    const group = { id, type: 'group', position: { x: minX, y: minY }, data: { title: '新分组' }, style: { width: maxX - minX, height: maxY - minY } } as Card;
    replace([group, ...nodesRef.current.map(node => selected.some(s => s.id === node.id) ? { ...node, parentId: id, position: { x: worldPosition(node).x - minX, y: worldPosition(node).y - minY }, selected: false } : node)]);
  };
  const requestDelete = () => {
    if (nodesRef.current.some(node => node.selected && node.type === 'task' && isFrozenTask(node))) setDialog('delete');
    else deleteSelected();
  };
  const openSelectionMenu = (event: React.MouseEvent, nodeId?: string) => {
    if ((event.target as HTMLElement).closest('input, textarea, select, [contenteditable="true"]')) return;
    event.preventDefault();
    if (switchingRef.current) return;
    if (nodeId) {
      const selected = selectForContextMenu(nodesRef.current, nodeId);
      nodesRef.current = selected; setNodes(selected);
    }
    setContextMenu({ mode: 'selection', ...contextMenuPosition({ x: event.clientX, y: event.clientY }, { width: window.innerWidth, height: window.innerHeight }, 165), flowPosition: flow.screenToFlowPosition({ x: event.clientX, y: event.clientY }) });
  };
  const selectedImportGroup = () => libraryGroup === 'all' || libraryGroup === 'ungrouped' ? null : libraryGroup;
  const importFiles = () => {
    importTargetRef.current = { groupId: selectedImportGroup(), canvasId: docRef.current?.id };
    fileInputRef.current?.click();
  };
  const importBrowserFiles = async (files: FileList | File[], target: { groupId: string | null; canvasId?: string } = { groupId: selectedImportGroup(), canvasId: docRef.current?.id }) => {
    const list = Array.from(files).slice(0, 30);
    if (!list.length) return;
    const { groupId, canvasId } = target;
    const known = new Set(assetsRef.current.map(asset => asset.id));
    const imported: Asset[] = [];
    const errors: string[] = [];
    let duplicates = 0;
    setMessage(`正在导入 ${list.length} 个素材…`);
    for (const file of list) {
      try {
        const asset = await upload(file, groupId);
        if (known.has(asset.id)) duplicates += 1;
        else known.add(asset.id);
        imported.push(asset);
      } catch (reason) { errors.push(`${file.name}：${reason instanceof Error ? reason.message : String(reason)}`); }
    }
    const nextAssets = [...new Map([...assetsRef.current, ...imported].map(asset => [asset.id, asset])).values()];
    assetsRef.current = nextAssets;
    setAssets(nextAssets);
    const summary = `新增 ${imported.length - duplicates} 个素材${duplicates ? `，${duplicates} 个已存在并保留原分组，可在主窗口批量移动` : ''}${errors.length ? `；${errors.length} 个导入失败：${errors.join('；')}` : ''}`;
    if (docRef.current?.id !== canvasId || switchingRef.current) { report(`${summary}。请从素材库加入当前画布。`); return; }
    for (const asset of imported) addCard('asset', { assetId: asset.id });
    if (errors.length) showError(summary); else report(summary);
  };
  const onDropFiles = (event: React.DragEvent) => {
    const assetId = event.dataTransfer.getData('application/x-canvas-asset');
    if (assetId && assets.some(asset => asset.id === assetId)) {
      event.preventDefault();
      addCard('asset', { assetId }, undefined, undefined, flow.screenToFlowPosition({ x: event.clientX, y: event.clientY }));
      return;
    }
    if (!event.dataTransfer.files.length) return;
    event.preventDefault();
    void importBrowserFiles(event.dataTransfer.files, { groupId: selectedImportGroup(), canvasId: docRef.current?.id });
  };
  const newCanvas = async () => {
    if (switchingRef.current) return;
    switchingRef.current = true; setSwitching(true);
    try { await flushAll();
      const doc = await api<CanvasDocument>('/api/canvases', 'POST', { name: dialogValue.trim() || '新画布' });
      setCanvases(previous => [{ id: doc.id, name: doc.name, updated: doc.updated }, ...previous]);
      await loadCanvas(doc.id, true); setDialog(null); report('新画布已创建。'); } catch (reason) { showError(reason); }
    finally { switchingRef.current = false; setSwitching(false); }
  };
  const renameCanvas = async () => {
    if (!current || switchingRef.current) return;
    switchingRef.current = true; setSwitching(true);
    try { await flushAll(); const renamed = await api<CanvasDocument>(`/api/canvases/${current.id}/rename`, 'POST', { name: dialogValue.trim() });
      docRef.current = { ...docRef.current!, name: renamed.name }; setCurrent(docRef.current);
      setCanvases(previous => previous.map(item => item.id === current.id ? { ...item, name: renamed.name } : item)); setDialog(null); } catch (reason) { showError(reason); }
    finally { switchingRef.current = false; setSwitching(false); }
  };

  const onNodesChange = (changes: NodeChange<Card>[]) => {
    if (switchingRef.current) return;
    if (changes.some(change => change.type === 'remove') && submittingRef.current.size) { report('正在预检，不能删除卡片。'); return; }
    const removed = new Set(changes.filter(change => change.type === 'remove').map(change => change.id));
    if (edgesRef.current.some(edge => removed.has(edge.id) && (submittingRef.current.has(edge.target) || submittingRef.current.has(edge.source)))) { report('正在预检，不能修改连线。'); return; }
    const error = protectedRemovalError(nodesRef.current, edgesRef.current, removed, new Set(), submittingRef.current);
    if (error) { report(error); return; }
    if (changes.some(change => change.type === 'position' && submittingRef.current.has(change.id))) return;
    const next = applyNodeChanges(changes, nodesRef.current) as Card[];
    if (removed.size) { replace(next, edgesRef.current.filter(edge => !removed.has(edge.source) && !removed.has(edge.target))); return; }
    nodesRef.current = next; setNodes(next);
    if (changes.some(change => change.type !== 'select' && change.type !== 'position')) schedule();
  };
  const onEdgesChange = (changes: EdgeChange[]) => {
    if (switchingRef.current) return;
    const removed = new Set(changes.filter(change => change.type === 'remove').map(change => change.id));
    const error = protectedRemovalError(nodesRef.current, edgesRef.current, new Set(), removed, submittingRef.current);
    if (error) { report(error); return; }
    if (changes.every(change => change.type === 'select')) { const next = applyEdgeChanges(changes, edgesRef.current); edgesRef.current = next; setEdges(next); return; }
    pushHistory(); replace(nodesRef.current, applyEdgeChanges(changes, edgesRef.current));
  };
  const commitConnection = (connection: Connection) => {
    if (switchingRef.current || !connection.source || !connection.target) return;
    if (submittingRef.current.has(connection.source) || submittingRef.current.has(connection.target)) { report('正在预检，不能修改连线。'); return; }
    const target = nodesRef.current.find(node => node.id === connection.target);
    if (target?.type === 'task' && target.data.kind === 'general') {
      const added = addInputEdge(connection.source, connection.target, nodesRef.current, edgesRef.current, assets, bootstrap?.generalModels || [], uid, submittingRef.current);
      if (added.error) { report(added.error); return; }
      if (added.edges !== edgesRef.current) { pushHistory(); replace(nodesRef.current.map(node => ({ ...node, selected: node.id === target.id })), added.edges); }
      return;
    }
    if (target?.type === 'task' && target.data.kind === 'general' && (isFrozenTask(target) || submittingRef.current.has(target.id))) { report('任务已提交或正在预检，不能修改连线。'); return; }
    pushHistory(); replace(nodesRef.current, addEdge({ ...connection, id: uid(), type: 'smoothstep', data: { kind: 'origin' }, style: { strokeDasharray: '5 5' }, label: '来源 · 不参与生成' }, edgesRef.current));
  };

  // React Flow may snap to a handle on a covered card. Commit only after checking
  // the actual release point, so one gesture creates exactly one history entry.
  const onConnect = (connection: Connection) => { pendingConnectionRef.current = connection; };
  const onConnectEnd: OnConnectEnd = (event, state) => {
    const snapped = pendingConnectionRef.current;
    pendingConnectionRef.current = null;
    const point = 'changedTouches' in event ? event.changedTouches[0] : event;
    if (point && state.fromNode && state.fromHandle) {
      const element = document.elementFromPoint(point.clientX, point.clientY)?.closest('.react-flow__node');
      const nodeId = element?.getAttribute('data-id');
      if (nodeId && nodeId !== state.fromNode.id) {
        const connection: Connection = state.fromHandle.type === 'source'
          ? { source: state.fromNode.id, target: nodeId, sourceHandle: state.fromHandle.id ?? null, targetHandle: null }
          : { source: nodeId, target: state.fromNode.id, sourceHandle: null, targetHandle: state.fromHandle.id ?? null };
        const target = nodesRef.current.find(node => node.id === connection.target);
        if (target?.type === 'task' && target.data.kind === 'general') {
          // A rejected foreground input must not fall back to an origin behind it.
          commitConnection(connection);
          return;
        }
      }
    }
    if (snapped) commitConnection(snapped);
  };
  const validConnection = (connection: Connection | Edge) => {
    const target = nodesRef.current.find(node => node.id === connection.target);
    return target?.type === 'task' && target.data.kind === 'general'
      ? checkInput(connection.source, connection.target, nodesRef.current, edgesRef.current, assets, bootstrap?.generalModels || [], submittingRef.current) === null : true;
  };
  useEffect(() => {
    if (!contextMenu) return;
    const close = (event: PointerEvent) => { if (!contextMenuRef.current?.contains(event.target as Node)) setContextMenu(null); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setContextMenu(null); };
    window.addEventListener('pointerdown', close); window.addEventListener('keydown', escape);
    return () => { window.removeEventListener('pointerdown', close); window.removeEventListener('keydown', escape); };
  }, [contextMenu]);
  const createAtMenu = (nodeType: string) => {
    if (!contextMenu) return;
    if (nodeType === 'note') addCard('note', { text: '' }, undefined, undefined, contextMenu.flowPosition);
    else addTask(nodeType, contextMenu.flowPosition);
    setContextMenu(null);
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (isImeKey(event)) return;
      if (window.pywebview?.api.host_action && (event.key === 'F11' || event.key === 'Escape')) {
        if (event.key === 'F11') event.preventDefault();
        void window.pywebview.api.host_action(event.key === 'F11' ? 'fullscreen' : 'leave_fullscreen').catch(() => {});
      }
      const target = event.target as HTMLElement | null;
      if (target?.closest('input, textarea, select, .dialog, [contenteditable="true"]') || dialog || switchingRef.current) return;
      const key = event.key.toLowerCase();
      if ((event.ctrlKey || event.metaKey) && ['z', 'y', 'd', 'g'].includes(key)) {
        event.preventDefault();
        if (key === 'z') restore(event.shiftKey ? redoRef : undoRef, event.shiftKey ? undoRef : redoRef);
        if (key === 'y') restore(redoRef, undoRef);
        if (key === 'd') copySelected();
        if (key === 'g') groupSelected();
      } else if (key === 'delete' || key === 'backspace') {
        event.preventDefault();
        requestDelete();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  if (!bootstrap || !current) return <div className="loading">{error || '正在打开创作画布…'}</div>;
  const groupName = groups.find(group => group.id === libraryGroup)?.name || '未分组';
  const visibleAssets = assets.filter(asset => (libraryGroup === 'all' || (libraryGroup === 'ungrouped' ? !asset.group_id : asset.group_id === libraryGroup))
    && (libraryKind === 'all' || asset.kind === libraryKind)
    && asset.name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  return <Context.Provider value={{ assets, jobs, edges, pendingIds, models: bootstrap.models, generalModels: bootstrap.generalModels || [], modelFamilies: bootstrap.modelFamilies || [], update, submit, recover, cancel, reuse, continueFrom, toggleReference, report }}>
    <div className={`app-shell ${switching ? 'switching' : ''}`}>
      <header className="topbar"><div className="brand canvas-brand"><div><strong>创作画布</strong>{bootstrap.demo && <small>本地演示</small>}</div></div>
        <div className="canvas-select"><span>当前画布</span><select value={current.id} onChange={event => void loadCanvas(event.target.value).catch(showError)}>{canvases.map(canvas => <option key={canvas.id} value={canvas.id}>{canvas.name}</option>)}</select><button onClick={() => { setDialogValue('新画布'); setDialog('new'); }}>新建</button><button onClick={() => { setDialogValue(current.name); setDialog('rename'); }}>重命名</button></div>
        <div className="save-state"><i className={saveState === '已保存' ? 'saved' : ''} />{saveState}</div>
      </header>
      <div className="toolbar"><button className="primary" onClick={importFiles}>＋ 导入素材</button><span className="toolbar-target" title={`新素材导入到：${groupName}`}>导入到：{groupName}</span><input ref={fileInputRef} type="file" multiple accept="image/*,video/*,audio/*" hidden onChange={event => { if (event.target.files) void importBrowserFiles(event.target.files, importTargetRef.current); event.target.value = ''; }} /><div className="history-controls"><button title="撤销 Ctrl+Z" aria-label="撤销" onClick={() => restore(undoRef, redoRef)}>↶</button><button title="重做 Ctrl+Shift+Z" aria-label="重做" onClick={() => restore(redoRef, undoRef)}>↷</button></div></div>
      <div className="main"><aside className="library"><div className="aside-title">素材库 <span>{visibleAssets.length}/{assets.length}</span></div><select className="library-select" value={libraryGroup} onChange={event => setLibraryGroup(event.target.value)}><option value="all">全部分组</option><option value="ungrouped">未分组</option>{groups.map(group => <option key={group.id} value={group.id}>{group.name}</option>)}</select><select className="library-select" value={libraryKind} onChange={event => setLibraryKind(event.target.value)}><option value="all">全部素材</option><option value="video">视频</option><option value="image">图片</option><option value="audio">音频</option></select><input className="search" placeholder="搜索素材" value={search} onChange={event => setSearch(event.target.value)} /><div className="library-items">{visibleAssets.map(asset => <button key={asset.id} className="library-item" draggable onDragStart={event => event.dataTransfer.setData('application/x-canvas-asset', asset.id)} onClick={() => addCard('asset', { assetId: asset.id })}><span className={`asset-icon ${asset.kind}`}>{asset.kind === 'image' ? '▧' : asset.kind === 'audio' ? '♫' : '▶'}</span><span title={asset.name}>{asset.name}<small>{asset.kind === 'image' ? '图片' : asset.kind === 'audio' ? '音频' : '视频'} · {(asset.size / 1024 / 1024).toFixed(1)} MB</small></span><b>＋</b></button>)}</div><div className="aside-help">新素材导入到：{groupName}。分组可在主窗口管理。点击或拖入素材到画布，再连到创作草稿作为输入。</div></aside>
        <div className="flow-wrap" onDragOver={event => { if (event.dataTransfer.types.includes('Files') || event.dataTransfer.types.includes('application/x-canvas-asset')) event.preventDefault(); }} onDrop={onDropFiles}><ReactFlow key={current.id} nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect} onConnectStart={() => { pendingConnectionRef.current = null; setContextMenu(null); }} onConnectEnd={onConnectEnd} onClickConnectStart={() => { pendingConnectionRef.current = null; setContextMenu(null); }} onClickConnectEnd={onConnectEnd} isValidConnection={validConnection} connectionRadius={70} connectionDragThreshold={1} onPaneContextMenu={event => { event.preventDefault(); if (switchingRef.current) return; setContextMenu({ mode: 'create', ...contextMenuPosition({ x: event.clientX, y: event.clientY }, { width: window.innerWidth, height: window.innerHeight }, 180), flowPosition: flow.screenToFlowPosition({ x: event.clientX, y: event.clientY }) }); }} onPaneClick={() => setContextMenu(null)} onNodeContextMenu={(event, node) => openSelectionMenu(event, node.id)} onSelectionContextMenu={event => openSelectionMenu(event)} onNodeDragStart={pushHistory} onNodeDragStop={() => schedule()} onNodeDoubleClick={(_, node) => focusCard(node.id, node.type)} onMoveEnd={(_, next) => { if (docRef.current?.id !== current.id || switchingRef.current) return; viewportRef.current = next; setViewport(next); schedule(); }} defaultViewport={current.viewport} fitView={false} selectionOnDrag panOnDrag={[1]} colorMode="dark" multiSelectionKeyCode="Shift" deleteKeyCode={null} minZoom={0.2} maxZoom={2} proOptions={{ hideAttribution: true }}><Background color="#40424a" gap={24} size={1} /><Controls />{!nodes.some(node => node.type === 'task' && node.selected) && <MiniMap pannable zoomable />}</ReactFlow><div className="canvas-hint">滚轮缩放 · 中键平移 · 空白右键新建 · 从素材右侧圆点连到创作卡</div></div>
      </div>
      <footer className={error ? 'footer error' : 'footer'}><span>{message}</span><span>{nodes.length} 张卡片 · {edges.length} 条连线 · {Math.round(viewport.zoom * 100)}%</span></footer>
      {contextMenu && createPortal(<div className="canvas-context-menu nodrag nopan" ref={contextMenuRef} role="menu" style={{ left: contextMenu.x, top: contextMenu.y }} onContextMenu={event => event.preventDefault()}>{contextMenu.mode === 'create' ? <><strong>在此处新建</strong>{[...new Set((bootstrap.modelFamilies || []).map(family => family.nodeType))].map(nodeType => <button role="menuitem" key={nodeType} onClick={() => createAtMenu(nodeType)}><span>{nodeType === 'lipsync' ? '新增对口型节点' : nodeType === 'image' ? '新增生图节点' : '新增视频节点'}</span></button>)}<button role="menuitem" onClick={() => createAtMenu('note')}><span>文字备注</span></button></> : <><strong>已选 {nodes.filter(node => node.selected).length} 张卡片</strong><button role="menuitem" disabled={pendingIds.size > 0} onClick={() => { copySelected(); setContextMenu(null); }}>复制<small>Ctrl+D</small></button><button role="menuitem" disabled={pendingIds.size > 0 || nodes.filter(node => node.selected && node.type !== 'group').length < 2} onClick={() => { groupSelected(); setContextMenu(null); }}>分组<small>Ctrl+G</small></button><button role="menuitem" disabled={pendingIds.size > 0} onClick={() => { requestDelete(); setContextMenu(null); }}>移除卡片<small>Delete</small></button></>}</div>, document.body)}
      {dialog && <div className="dialog-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setDialog(null); }}><form className="dialog" onSubmit={event => { event.preventDefault(); if (dialog === 'new') void newCanvas(); else if (dialog === 'rename') void renameCanvas(); else { deleteSelected(); setDialog(null); } }}><h2>{dialog === 'new' ? '新建画布' : dialog === 'rename' ? '重命名画布' : '移除任务卡片'}</h2>{dialog === 'delete' ? <p>仅移除画布上的卡片。已提交的任务会继续执行，任务记录和成片仍保留。</p> : <input autoFocus value={dialogValue} onChange={event => setDialogValue(event.target.value)} maxLength={60} placeholder="画布名称" />}<div className="dialog-actions"><button type="button" onClick={() => setDialog(null)}>取消</button><button className="primary" type="submit">{dialog === 'delete' ? '移除卡片' : '保存'}</button></div></form></div>}
    </div>
  </Context.Provider>;
}

export default function App() { return <ReactFlowProvider><Workspace /></ReactFlowProvider>; }
