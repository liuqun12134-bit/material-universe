import type { Edge, Node } from '@xyflow/react';
import type { Asset, GeneralModel, Job, ModelFamily } from './api';

export type CardData = { assetId?: string; text?: string; title?: string; kind?: 'general' | 'full' | 'lipsync' | 'repair'; config?: Record<string, any>; requestId?: string; jobId?: string; warning?: string; actualPrompt?: string; sourceBasePrompt?: string };
export type Card = Node<CardData>;

export function selectForContextMenu(nodes: Card[], id: string): Card[] {
  if (nodes.some(node => node.id === id && node.selected)) return nodes;
  return nodes.map(node => ({ ...node, selected: node.id === id }));
}

// Historical jobs own their submitted snapshot; draft cards must not override it.
export function taskReferenceIds(data: CardData, job?: Job, bound: string[] = []): string[] {
  const config = job?.config || data.config || {};
  const kind = job?.kind || data.kind;
  if (kind === 'general') return job || isFrozenTask({ data } as Card)
    ? [...(config.referenceAssetIds || config.reference_assets || [])] : bound;
  return kind === 'full'
    ? [config.sourceAssetId || config.source, config.imageAssetId || config.image, ...(config.extraImageAssetIds || config.extra_images || [])].filter(Boolean)
    : [config.videoAssetId || config.video, config.audioAssetId || config.audio].filter(Boolean);
}

export function taskPrompts(data: CardData, job?: Job): { label: string; value: string }[] {
  const config = job?.config || data.config || {};
  if ((job?.kind || data.kind) === 'full') {
    const base = job ? editableSwapPrompt(job) : data.sourceBasePrompt ?? config.prompt ?? '';
    const actual = job?.actual_prompt || data.actualPrompt || '';
    return [{ label: '基础提示词', value: base }, ...(actual && actual !== base ? [{ label: '实际提交提示词', value: actual }] : [])];
  }
  return [{ label: '提示词', value: config.prompt ?? data.actualPrompt ?? '' }];
}

export function contextMenuPosition(point: { x: number; y: number }, viewport: { width: number; height: number }, height = 260) {
  return { x: Math.max(8, Math.min(point.x, viewport.width - 268)), y: Math.max(8, Math.min(point.y, viewport.height - height - 8)) };
}
export type CanvasEdge = Edge<{ kind: 'input' | 'origin'; assetId?: string; order?: number }>;

export const isInput = (edge: Edge): boolean => edge.data?.kind === 'input';
export const isFrozenTask = (node?: Card): boolean => Boolean(node?.data.requestId || node?.data.jobId);
export const availableModel = (id: string, models: GeneralModel[]): GeneralModel | undefined => models.find(model => model.id === id);
export function inputAssetIds(taskId: string, edges: Edge[]): string[] {
  const ids = new Set<string>();
  return edges.filter(edge => edge.target === taskId && isInput(edge))
    .sort((a, b) => Number(a.data?.order || 0) - Number(b.data?.order || 0))
    .flatMap(edge => { const id = edge.data?.assetId; if (typeof id !== 'string' || ids.has(id)) return []; ids.add(id); return [id]; });
}
function compatibleParameter(parameter: GeneralModel['parameters'][number], value: any) {
  if (value === undefined || value === null) return parameter.default;
  if (parameter.type === 'boolean') return typeof value === 'boolean' ? value : parameter.default;
  if (parameter.type === 'enum') return parameter.enum?.includes(value) ? value : parameter.default;
  if (parameter.type === 'integer' || parameter.type === 'number') return typeof value === 'number' && Number.isFinite(value) && (parameter.type !== 'integer' || Number.isInteger(value)) && (parameter.min === undefined || value >= parameter.min) && (parameter.max === undefined || value <= parameter.max) ? value : parameter.default;
  return typeof value === 'string' ? value : parameter.default;
}
export function draftForFamily(family: ModelFamily, refs: string[] = [], prompt = '', parameters: Record<string, any> = {}) {
  const route = family.routes.find(route => route.when === (refs.length ? 'has_references' : 'no_references') || route.when === 'always');
  return { familyId: family.id, nodeType: family.nodeType, ...(family.provider ? { provider: family.provider } : {}), model: route?.modelId || '', prompt, referenceAssetIds: refs,
    parameters: Object.fromEntries(family.parameters.map(parameter => [parameter.key, compatibleParameter(parameter, parameters[parameter.key])])) };
}
export function switchFamilyDraft(config: Record<string, any>, family: ModelFamily, refs: string[] = []) {
  const previousProvider = config.provider || 'fal', nextProvider = family.provider || 'fal';
  const saved = config.providerParameters || {};
  const nextSwitches = previousProvider === nextProvider ? {} : Object.fromEntries(family.parameters.filter(parameter => parameter.type === 'boolean' && saved[nextProvider]?.[parameter.key] !== undefined).map(parameter => [parameter.key, saved[nextProvider][parameter.key]]));
  return { ...draftForFamily(family, refs, config.prompt ?? '', { ...config.parameters, ...nextSwitches }),
    providerParameters: { ...saved, [previousProvider]: structuredClone(config.parameters || {}) } };
}
export function switchSwapProvider(config: Record<string, any>, provider: string, model: string) {
  const previousProvider = config.provider || 'fal', saved = config.providerParameters || {};
  const current = { enableThinking: config.enableThinking ?? true, enablePromptExpansion: config.enablePromptExpansion ?? true };
  return { ...config, ...current, ...(previousProvider === provider ? {} : saved[provider]), provider, model,
    providerParameters: { ...saved, [previousProvider]: current } };
}
export function normalizeDraft(config: Record<string, any>, refs: string[], models: GeneralModel[]) {
  const current = models.find(model => model.id === config.model);
  const familyId = config.familyId || current?.familyId;
  if (!familyId) return { ...config, referenceAssetIds: refs };
  const candidates = models.filter(model => model.familyId === familyId && (model.routeWhen === (refs.length ? 'has_references' : 'no_references') || model.routeWhen === 'always'));
  const route = candidates.find(model => model.id === config.model) || candidates[0];
  if (!route) return { ...config, referenceAssetIds: refs };
  return { ...config, familyId, nodeType: route.nodeType, ...(route.provider ? { provider: route.provider } : {}), model: route.id, referenceAssetIds: refs,
    parameters: Object.fromEntries(route.parameters.map(parameter => [parameter.key,
      parameter.type === 'enum' && parameter.enum?.length === 1 ? parameter.enum[0] : config.parameters?.[parameter.key] ?? parameter.default])) };
}
export function syncDraftReferences(nodes: Card[], edges: Edge[], models: GeneralModel[] = []): Card[] {
  return nodes.map(node => node.type === 'task' && node.data.kind === 'general' && !isFrozenTask(node)
    ? { ...node, data: { ...node.data, config: normalizeDraft(node.data.config || {}, inputAssetIds(node.id, edges), models) } } : node);
}
export function markLegacyOrigins(edges: Edge[]): Edge[] {
  return edges.map(edge => {
    if (isInput(edge)) return edge;
    if (edge.data?.kind === 'origin' && edge.style?.strokeDasharray === '5 5' && edge.label === '来源 · 不参与生成') return edge;
    return { ...edge, data: { ...edge.data, kind: 'origin' }, style: { ...edge.style, strokeDasharray: '5 5' }, label: '来源 · 不参与生成' };
  });
}
export function nextInputOrder(taskId: string, edges: Edge[]): number {
  return Math.max(0, ...edges.filter(edge => edge.target === taskId && isInput(edge)).map(edge => Number(edge.data?.order || 0))) + 1;
}
export function checkInput(sourceId: string, targetId: string, nodes: Card[], edges: Edge[], assets: Asset[], models: GeneralModel[], locked: Set<string> = new Set()): string | null {
  const source = nodes.find(node => node.id === sourceId), target = nodes.find(node => node.id === targetId);
  if (source?.type !== 'asset' || target?.type !== 'task' || target.data.kind !== 'general') return '仅能从素材卡连接到通用创作草稿。';
  if (isFrozenTask(target) || locked.has(targetId)) return '任务已提交或正在预检，不能修改输入。';
  const asset = assets.find(item => item.id === source.data.assetId);
  if (!asset) return '素材不在素材库中。';
  const model = models.find(item => item.id === target.data.config?.model);
  if (!model) return '该任务模型已不可用，请先选择可用模型。';
  const limit = (model.familyReferenceLimits || model.referenceLimits)[asset.kind];
  if (!limit?.max) return `当前模型不接受${asset.kind === 'image' ? '图片' : asset.kind === 'video' ? '视频' : '音频'}参考。`;
  const existing = inputAssetIds(targetId, edges);
  if (!existing.includes(asset.id) && existing.filter(id => assets.find(item => item.id === id)?.kind === asset.kind).length >= limit.max) return '该类型参考素材已达到模型上限。';
  return null;
}
export function addInputEdge(sourceId: string, targetId: string, nodes: Card[], edges: Edge[], assets: Asset[], models: GeneralModel[], idFactory: () => string, locked: Set<string> = new Set()) {
  const error = checkInput(sourceId, targetId, nodes, edges, assets, models, locked);
  if (error) return { edges, error };
  const assetId = nodes.find(node => node.id === sourceId)!.data.assetId!;
  if (edges.some(edge => isInput(edge) && edge.source === sourceId && edge.target === targetId)) return { edges, error: null };
  return { edges: [...edges, { id: idFactory(), source: sourceId, target: targetId, type: 'smoothstep', data: { kind: 'input', assetId, order: nextInputOrder(targetId, edges) }, label: '输入' }], error: null };
}
export function removeAssetInputs(taskId: string, assetId: string, edges: Edge[]): Edge[] {
  return edges.filter(edge => !(edge.target === taskId && isInput(edge) && edge.data?.assetId === assetId));
}
export function setReferenceBound(taskId: string, assetId: string, checked: boolean, nodes: Card[], edges: Edge[], assets: Asset[], models: GeneralModel[], idFactory: () => string, locked: Set<string> = new Set()) {
  const task = nodes.find(node => node.id === taskId);
  if (!task || task.type !== 'task' || task.data.kind !== 'general') return { nodes, edges, error: '请选择通用创作草稿。' };
  if (isFrozenTask(task) || locked.has(taskId)) return { nodes, edges, error: '任务已提交或正在预检，不能修改参考。' };
  if (!checked) return { nodes, edges: removeAssetInputs(taskId, assetId, edges), error: null };
  if (!assets.some(asset => asset.id === assetId)) return { nodes, edges, error: '素材不在素材库中。' };
  let source = nodes.find(node => node.type === 'asset' && node.data.assetId === assetId);
  if (!source) {
    const position = worldPosition(task, nodes);
    source = { id: idFactory(), type: 'asset', position: { x: position.x - 380, y: position.y + 100 }, data: { assetId } } as Card;
  }
  const nextNodes = nodes.includes(source) ? nodes : [...nodes, source];
  const added = addInputEdge(source.id, taskId, nextNodes, edges, assets, models, idFactory, locked);
  return added.error ? { nodes, edges, error: added.error } : { nodes: nextNodes, edges: added.edges, error: null };
}
export function withImportedAsset(assets: Asset[], imported: Asset): Asset[] {
  return [imported, ...assets.filter(asset => asset.id !== imported.id)];
}
export function bindImportedResult(taskId: string, imported: Asset, previousAssets: Asset[], nodes: Card[], edges: Edge[], models: GeneralModel[], idFactory: () => string) {
  const assets = withImportedAsset(previousAssets, imported);
  return { assets, ...setReferenceBound(taskId, imported.id, true, nodes, edges, assets, models, idFactory) };
}
export function protectedRemovalError(nodes: Card[], edges: Edge[], removedNodes: Set<string>, removedEdges: Set<string>, locked: Set<string> = new Set()): string | null {
  if ([...removedNodes].some(id => locked.has(id))) return '正在预检，不能移除任务卡。';
  for (const edge of edges.filter(isInput)) {
    if (!removedNodes.has(edge.source) && !removedEdges.has(edge.id)) continue;
    const target = nodes.find(node => node.id === edge.target);
    if (locked.has(edge.target)) return '正在预检，不能修改输入。';
    if (isFrozenTask(target) && !removedNodes.has(edge.target)) return '已提交任务的输入已冻结；需连同任务卡一起移除。';
  }
  return null;
}
export function modelAcceptsReferences(model: GeneralModel, ids: string[], assets: Asset[]): boolean {
  return (['image', 'video', 'audio'] as const).every(kind => ids.filter(id => assets.find(asset => asset.id === id)?.kind === kind).length <= model.referenceLimits[kind].max);
}
export function modelMeetsReferenceMinimums(model: GeneralModel, ids: string[], assets: Asset[]): boolean {
  return modelAcceptsReferences(model, ids, assets) && (['image', 'video', 'audio'] as const).every(kind => ids.filter(id => assets.find(asset => asset.id === id)?.kind === kind).length >= model.referenceLimits[kind].min);
}
export function migrateLegacyDrafts(nodes: Card[], edges: Edge[], assets: Asset[], idFactory: () => string, models: GeneralModel[] = []): { nodes: Card[]; edges: Edge[]; changed: boolean } {
  let nextNodes = nodes, nextEdges = markLegacyOrigins(edges), changed = nextEdges.some((edge, i) => edge !== edges[i]);
  for (const task of nodes.filter(node => node.type === 'task' && node.data.kind === 'general' && !isFrozenTask(node))) {
    const refs = task.data.config?.referenceAssetIds;
    if (!Array.isArray(refs) || !refs.length) continue;
    for (const assetId of refs) {
      if (inputAssetIds(task.id, nextEdges).includes(assetId)) continue;
      if (!assets.some(asset => asset.id === assetId)) {
        nextNodes = nextNodes.map(node => node.id === task.id ? { ...node, data: { ...node.data, warning: `历史参考素材 ${assetId} 已不在素材库，请重新选择。` } } : node);
        changed = true; continue;
      }
      let source = nextNodes.find(node => node.type === 'asset' && node.data.assetId === assetId);
      if (!source) {
        source = { id: idFactory(), type: 'asset', position: { x: task.position.x - 380, y: task.position.y + 100 + nextEdges.length * 30 }, data: { assetId } } as Card;
        nextNodes = [...nextNodes, source];
      }
      nextEdges = [...nextEdges, { id: idFactory(), source: source.id, target: task.id, type: 'smoothstep', data: { kind: 'input', assetId, order: nextInputOrder(task.id, nextEdges) }, label: '输入' }];
      changed = true;
    }
  }
  const synced = syncDraftReferences(nextNodes, nextEdges, models);
  changed ||= synced.some((node, index) => JSON.stringify(node.data) !== JSON.stringify(nextNodes[index].data));
  return { nodes: synced, edges: nextEdges, changed };
}

export function jobFor(data: CardData, jobs: Job[]): Job | undefined {
  return jobs.find(item =>
    (Boolean(data.jobId) && item.id === data.jobId) ||
    (Boolean(data.requestId) && Boolean(item.request_id) && item.request_id === data.requestId));
}

export function canEditAfterSubmitError(error: { code?: string; submitted?: boolean }) {
  return error.code === 'validation_failed' && error.submitted === false;
}

export function canReuseJob(job: Job, assets: Asset[] = []): boolean {
  if (job.kind !== 'general') return true;
  const config = job.config || {};
  if (config.reusable === false || typeof config.prompt !== 'string') return false;
  const ids = config.referenceAssetIds;
  const paths = config.reference_assets;
  if (!Array.isArray(ids) && !Array.isArray(paths)) return false;
  if (Array.isArray(ids) && ids.some((id: string) => !assets.some(asset => asset.id === id))) return false;
  if (Array.isArray(paths) && paths.some((path: string) => !assets.some(asset => asset.path === path || asset.id === path))) return false;
  return true;
}

export function worldPosition(node: Card, nodes: Card[]): { x: number; y: number } {
  const parent = node.parentId && nodes.find(item => item.id === node.parentId);
  if (!parent) return node.position;
  const origin = worldPosition(parent, nodes);
  return { x: origin.x + node.position.x, y: origin.y + node.position.y };
}

export function copyCards(nodes: Card[], edges: Edge[], idFactory: () => string, assets: Asset[] = []) {
  const chosen = new Set(nodes.filter(node => node.selected).map(node => node.id));
  let grew = true;
  while (grew) { grew = false; for (const node of nodes) if (node.parentId && chosen.has(node.parentId) && !chosen.has(node.id)) { chosen.add(node.id); grew = true; } }
  const selected = nodes.filter(node => chosen.has(node.id));
  if (!selected.length) return { nodes, edges, error: null };
  for (const node of selected.filter(item => item.type === 'task' && item.data.kind === 'general')) {
    const refs = node.data.config?.referenceAssetIds;
    if (!Array.isArray(refs) || !refs.length || inputAssetIds(node.id, edges).length) continue;
    const missing = refs.find((id: string) => !assets.some(asset => asset.id === id));
    if (missing) return { nodes, edges, error: `旧任务参考素材 ${missing} 已缺失，无法完整复制配置。` };
  }
  const remap = new Map(selected.map(node => [node.id, idFactory()]));
  const copies = selected.map(node => ({ ...structuredClone(node), id: remap.get(node.id)!, selected: node.selected,
    parentId: node.parentId && remap.has(node.parentId) ? remap.get(node.parentId) : undefined,
    position: node.parentId && remap.has(node.parentId) ? structuredClone(node.position) : { x: worldPosition(node, nodes).x + 45, y: worldPosition(node, nodes).y + 45 },
    data: node.type === 'task' ? { ...structuredClone(node.data), requestId: undefined, jobId: undefined, warning: undefined } : structuredClone(node.data) }));
  const connections = edges.filter(edge => remap.has(edge.target) && (remap.has(edge.source) || isInput(edge)))
    .map(edge => ({ ...structuredClone(edge), id: idFactory(), source: remap.get(edge.source) || edge.source, target: remap.get(edge.target)! }));
  const legacyInputs: Edge[] = [];
  const referenceCards: Card[] = [];
  for (const original of selected.filter(node => node.type === 'task' && node.data.kind === 'general')) {
    const copyId = remap.get(original.id)!;
    if (inputAssetIds(copyId, connections).length) continue;
    const refs: string[] = Array.isArray(original.data.config?.referenceAssetIds) ? [...new Set<string>(original.data.config.referenceAssetIds)] : [];
    for (const [index, assetId] of refs.entries()) {
      const existing = nodes.find(node => node.type === 'asset' && node.data.assetId === assetId);
      let sourceId = existing && (remap.get(existing.id) || existing.id);
      if (!sourceId) {
        sourceId = idFactory();
        const position = worldPosition(original, nodes);
        referenceCards.push({ id: sourceId, type: 'asset', position: { x: position.x - 380, y: position.y + index * 170 }, data: { assetId } } as Card);
      }
      legacyInputs.push({ id: idFactory(), source: sourceId, target: copyId, type: 'smoothstep', data: { kind: 'input', assetId, order: index + 1 }, label: '输入' });
    }
  }
  return { nodes: [...nodes.map(node => ({ ...node, selected: false })), ...copies, ...referenceCards], edges: [...edges, ...connections, ...legacyInputs], error: null };
}

export function deleteCards(nodes: Card[], edges: Edge[]) {
  const ids = new Set(nodes.filter(node => node.selected).map(node => node.id));
  return { nodes: nodes.filter(node => !ids.has(node.id)).map(node => node.parentId && ids.has(node.parentId)
    ? { ...node, parentId: undefined, position: worldPosition(node, nodes) } : node),
    edges: edges.filter(edge => !ids.has(edge.source) && !ids.has(edge.target)) };
}

export function configFromJob(job: Job, assets: Asset[]): Record<string, any> {
  const find = (path?: string) => assets.find(item => item.path === path)?.id || '';
  const config = job.config || {};
  if (job.kind === 'general') return { ...(config.canvas_draft?.providerParameters || config.providerParameters ? { providerParameters: structuredClone(config.canvas_draft?.providerParameters || config.providerParameters) } : {}), model: config.model || '', ...(config.provider ? { provider: config.provider } : {}), ...(config.familyId ? { familyId: config.familyId, nodeType: config.nodeType } : {}), prompt: config.prompt ?? '',
    referenceAssetIds: (config.referenceAssetIds || config.reference_assets || []).map((value: string) => assets.find(item => item.id === value || item.path === value)?.id).filter(Boolean),
    parameters: structuredClone(config.parameters || {}) };
  if (job.kind === 'full') return { ...(config.canvas_draft?.providerParameters || config.providerParameters ? { providerParameters: structuredClone(config.canvas_draft?.providerParameters || config.providerParameters) } : {}), sourceAssetId: find(config.source), imageAssetId: find(config.image),
    extraImageAssetIds: (config.extra_images || []).map(find).filter(Boolean), relation: config.relation || '',
    promptMode: config.prompt_mode === 'reuse' ? 'reuse' : 'analyze',
    prompt: config.base_prompt || (config.prompt_mode === 'reuse' || config.remove_subtitles === true
      ? editableSwapPrompt(job) : config.prompt || ''),
    removeSubtitles: config.remove_subtitles === true,
    enablePromptExpansion: config.enable_prompt_expansion ?? true,
    enableThinking: config.canvas_draft?.enableThinking ?? config.enable_thinking ?? true,
    duration: config.duration, aspectRatio: config.aspect_ratio, resolution: config.resolution,
    model: config.model, ...(config.provider ? { provider: config.provider } : {}), visionModel: config.vision_model };
  if (job.kind === 'lipsync') return { videoAssetId: find(config.video), audioAssetId: find(config.audio) };
  return { videoAssetId: find(config.video), options: structuredClone(config.options || {}) };
}

export const SUBTITLE_PREFIX = '先把参考视频中的字幕去掉，再';

export function editableSwapPrompt(job: Pick<Job, 'config' | 'actual_prompt'>): string {
  const config = job.config || {};
  if (typeof config.base_prompt === 'string' && config.base_prompt) return config.base_prompt;
  const prompt = job.actual_prompt || config.prompt || '';
  return config.remove_subtitles === true && prompt.startsWith(SUBTITLE_PREFIX)
    ? prompt.slice(SUBTITLE_PREFIX.length) : prompt;
}

export function reusableActualPrompt(data: Pick<CardData, 'actualPrompt' | 'sourceBasePrompt'>, job?: Job): string {
  if (job) return editableSwapPrompt(job);
  return data.sourceBasePrompt ?? data.actualPrompt ?? '';
}
