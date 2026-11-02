import { test } from 'node:test';
import assert from 'node:assert/strict';
import { addInputEdge, availableModel, bindImportedResult, canEditAfterSubmitError, canReuseJob, configFromJob, copyCards, deleteCards, editableSwapPrompt, inputAssetIds, jobFor, migrateLegacyDrafts, modelAcceptsReferences, modelMeetsReferenceMinimums, protectedRemovalError, removeAssetInputs, reusableActualPrompt, setReferenceBound, syncDraftReferences, SUBTITLE_PREFIX, taskReferenceIds, taskPrompts, contextMenuPosition, normalizeDraft, draftForFamily, switchFamilyDraft, switchSwapProvider, selectForContextMenu } from '../src/logic.ts';

test('已选卡片右键保持多选，未选卡片右键只选中目标', () => {
  const nodes = [{ id: 'a', selected: true }, { id: 'b', selected: true }, { id: 'c', selected: false }];
  assert.equal(selectForContextMenu(nodes, 'b'), nodes);
  const target = selectForContextMenu(nodes, 'c');
  assert.deepEqual(target.filter(node => node.selected).map(node => node.id), ['c']);
  assert.deepEqual(nodes.filter(node => node.selected).map(node => node.id), ['a', 'b']);
});

test('去字幕历史复用只剥除已标记任务的系统前缀', () => {
  const original = '保留原场景。';
  const old = { kind: 'full', config: { remove_subtitles: true, prompt_mode: 'reuse' }, actual_prompt: SUBTITLE_PREFIX + original };
  assert.equal(editableSwapPrompt(old), original);
  assert.equal(configFromJob(old, []).prompt, original);
  assert.equal(configFromJob(old, []).removeSubtitles, true);
  assert.equal(editableSwapPrompt({ ...old, config: { ...old.config, base_prompt: original } }), original);
  assert.equal(editableSwapPrompt({ ...old, config: { remove_subtitles: false } }), SUBTITLE_PREFIX + original);
});

test('复用去字幕任务取消勾选后点上次实际提示词仍恢复原始提示词', () => {
  const base = '保留原场景。';
  const job = { kind: 'full', config: { remove_subtitles: true, prompt_mode: 'analyze', base_prompt: base },
    actual_prompt: SUBTITLE_PREFIX + base };
  const draft = { config: configFromJob(job, []), actualPrompt: job.actual_prompt,
    sourceBasePrompt: editableSwapPrompt(job) };
  draft.config.removeSubtitles = false;
  draft.config.promptMode = 'reuse';
  draft.config.prompt = reusableActualPrompt(draft);
  assert.equal(draft.config.prompt, base);
  assert.equal(draft.config.removeSubtitles, false);
  const legacy = { ...job, config: { remove_subtitles: true, prompt_mode: 'reuse' } };
  assert.equal(reusableActualPrompt({ actualPrompt: legacy.actual_prompt,
    sourceBasePrompt: editableSwapPrompt(legacy) }), base);
  const userAuthored = SUBTITLE_PREFIX + base;
  const unmarked = { ...job, config: { remove_subtitles: false, prompt_mode: 'reuse', base_prompt: userAuthored } };
  assert.equal(reusableActualPrompt({ actualPrompt: unmarked.actual_prompt,
    sourceBasePrompt: editableSwapPrompt(unmarked) }), userAuthored);
});

const asset = (id, kind = 'image') => ({ id, name: id, kind, path: `/${id}`, size: 1 });
const model = (id, limits) => ({ id, promptRequired: id !== 'sync_lipsync', referenceLimits: {
  image: { min: 0, max: 0 }, video: { min: 0, max: 0 }, audio: { min: 0, max: 0 }, ...limits
}, parameters: [] });

test('历史任务缺少 request_id 时不会被空草稿误认', () => {
  const legacy = { id: 'old', kind: 'full', config: {}, status: 'success', stage: '' };
  const current = { id: 'new', request_id: 'req-1', kind: 'full', config: {}, status: 'queued', stage: '' };
  assert.equal(jobFor({}, [legacy, current]), undefined);
  assert.equal(jobFor({ requestId: 'req-1' }, [legacy, current]), current);
  assert.equal(jobFor({ jobId: 'old' }, [legacy, current]), legacy);
});

test('仅确认未提交的校验错误允许草稿恢复编辑', () => {
  assert.equal(canEditAfterSubmitError({ code: 'validation_failed', submitted: false }), true);
  assert.equal(canEditAfterSubmitError({ code: 'conflict', submitted: false }), false);
  assert.equal(canEditAfterSubmitError({ code: 'validation_failed' }), false);
  assert.equal(canEditAfterSubmitError({ code: 'validation_failed', submitted: true }), false);
});

test('复制分组包含子卡片并重映射父子和连线，已提交任务变成新草稿', () => {
  const nodes = [
    { id: 'group', type: 'group', position: { x: 100, y: 200 }, data: {}, selected: true },
    { id: 'task', type: 'task', parentId: 'group', position: { x: 20, y: 40 }, data: { requestId: 'paid', jobId: 'job' } },
    { id: 'outside', type: 'note', position: { x: 700, y: 100 }, data: {} },
  ];
  const edges = [{ id: 'edge', source: 'group', target: 'task' }];
  let next = 0;
  const copied = copyCards(nodes, edges, () => `copy-${++next}`);
  const groupCopy = copied.nodes.find(node => node.id === 'copy-1');
  const taskCopy = copied.nodes.find(node => node.id === 'copy-2');
  assert.deepEqual(groupCopy.position, { x: 145, y: 245 });
  assert.equal(taskCopy.parentId, groupCopy.id);
  assert.deepEqual(taskCopy.position, { x: 20, y: 40 });
  assert.equal(taskCopy.data.requestId, undefined);
  assert.equal(taskCopy.data.jobId, undefined);
  assert.equal(copied.edges[1].source, groupCopy.id);
  assert.equal(copied.edges[1].target, taskCopy.id);
  assert.equal(copied.nodes.find(node => node.id === 'task').data.requestId, 'paid');
});

test('删除分组保留子卡片的画布坐标', () => {
  const nodes = [
    { id: 'group', type: 'group', position: { x: 100, y: 200 }, data: {}, selected: true },
    { id: 'child', type: 'asset', parentId: 'group', position: { x: 20, y: 40 }, data: {} },
  ];
  const next = deleteCards(nodes, [{ id: 'edge', source: 'group', target: 'child' }]);
  assert.deepEqual(next.nodes, [{ id: 'child', type: 'asset', parentId: undefined, position: { x: 120, y: 240 }, data: {} }]);
  assert.equal(next.edges.length, 0);
});

test('复用配置保留原提示词模式和素材，实际生成提示词需另行选择', () => {
  const assets = [
    { id: 'video', path: '/source.mp4' }, { id: 'main', path: '/main.png' }, { id: 'extra', path: '/extra.png' },
  ];
  const job = { kind: 'full', actual_prompt: '生成时分析出的词', config: {
    source: '/source.mp4', image: '/main.png', extra_images: ['/extra.png'], relation: '小一号',
    prompt_mode: 'analyze', prompt: '原始输入', duration: 4, aspect_ratio: '16:9'
  } };
  const reused = configFromJob(job, assets);
  assert.equal(reused.promptMode, 'analyze');
  assert.equal(reused.prompt, '原始输入');
  assert.deepEqual([reused.sourceAssetId, reused.imageAssetId, ...reused.extraImageAssetIds], ['video', 'main', 'extra']);
  assert.equal(reused.duration, 4);
  assert.equal(reused.aspectRatio, '16:9');
  assert.equal(reused.enablePromptExpansion, true);
  assert.equal(reused.enableThinking, true);
  const disabled = configFromJob({ ...job, config: {
    ...job.config, enable_prompt_expansion: false, enable_thinking: false,
  } }, assets);
  assert.equal(disabled.enablePromptExpansion, false);
  assert.equal(disabled.enableThinking, false);
});

test('通用任务复用保留原文提示词、参考顺序和显式参数', () => {
  const assets = [{ id: 'one', path: '/one.png' }, { id: 'two', path: '/two.mp4' }];
  const job = { kind: 'general', config: { model: 'wan3.0-video', prompt: '  原样提示词\n第二行  ', referenceAssetIds: ['two', 'one'], parameters: { duration: 5, aspect_ratio: '9:16' } } };
  const config = configFromJob(job, assets);
  assert.equal(config.prompt, '  原样提示词\n第二行  ');
  assert.deepEqual(config.referenceAssetIds, ['two', 'one']);
  assert.deepEqual(config.parameters, { duration: 5, aspect_ratio: '9:16' });
});

test('旧通用任务缺少配置快照时不提供误导性的复用草稿', () => {
  assert.equal(canReuseJob({ kind: 'general', config: { model: 'wan3.0-t2v' } }), false);
  assert.equal(canReuseJob({ kind: 'general', config: { reusable: false, prompt: '旧记录', referenceAssetIds: [] } }), false);
  assert.equal(canReuseJob({ kind: 'general', config: { prompt: '  原文  ', referenceAssetIds: [] } }), true);
  assert.equal(canReuseJob({ kind: 'general', config: { prompt: '原文', reference_assets: ['/missing.png'] } }, []), false);
  assert.equal(canReuseJob({ kind: 'general', config: { prompt: '原文', reference_assets: ['/one.png'] } }, [{ id: 'one', path: '/one.png' }]), true);
  assert.equal(canReuseJob({ kind: 'full', config: {} }), true);
});

test('输入线按order派生去重参考，同素材多卡只提交一次，删除最后一条才移除', () => {
  const nodes = [
    { id: 'a1', type: 'asset', data: { assetId: 'a' } }, { id: 'a2', type: 'asset', data: { assetId: 'a' } },
    { id: 'v', type: 'asset', data: { assetId: 'v' } }, { id: 't', type: 'task', data: { kind: 'general', config: { model: 'wan' } } }
  ];
  const assets = [asset('a'), asset('v', 'video')];
  const models = [model('wan', { image: { min: 0, max: 1 }, video: { min: 0, max: 2 } })];
  let n = 0, edges = [];
  for (const source of ['v', 'a1', 'a2']) edges = addInputEdge(source, 't', nodes, edges, assets, models, () => `e${++n}`).edges;
  assert.deepEqual(inputAssetIds('t', edges), ['v', 'a']);
  assert.deepEqual(syncDraftReferences(nodes, edges).at(-1).data.config.referenceAssetIds, ['v', 'a']);
  edges = edges.filter(edge => edge.source !== 'a1');
  assert.deepEqual(inputAssetIds('t', edges), ['v', 'a']);
  edges = removeAssetInputs('t', 'a', edges);
  assert.deepEqual(inputAssetIds('t', edges), ['v']);
});

test('参考勾选自动放置素材卡、重复勾选复用、取消移除输入线', () => {
  const nodes = [{ id: 't', type: 'task', position: { x: 500, y: 100 }, data: { kind: 'general', config: { model: 'wan' } } }];
  const assets = [asset('a')], models = [model('wan', { image: { min: 0, max: 2 } })];
  let n = 0;
  const first = setReferenceBound('t', 'a', true, nodes, [], assets, models, () => `new${++n}`);
  assert.equal(first.nodes.length, 2);
  assert.equal(first.edges.length, 1);
  assert.deepEqual(inputAssetIds('t', first.edges), ['a']);
  const again = setReferenceBound('t', 'a', true, first.nodes, first.edges, assets, models, () => `new${++n}`);
  assert.equal(again.nodes.length, 2);
  assert.equal(again.edges.length, 1);
  const removed = setReferenceBound('t', 'a', false, again.nodes, again.edges, assets, models, () => 'unused');
  assert.equal(removed.nodes.length, 2);
  assert.deepEqual(inputAssetIds('t', removed.edges), []);
});

test('首次导入全新结果时用更新后的素材快照绑定真实输入线', () => {
  const imported = asset('fresh-result', 'video');
  const staleAssets = [asset('old', 'image')];
  const nodes = [
    { id: 'result-card', type: 'asset', position: { x: 0, y: 0 }, data: { assetId: imported.id } },
    { id: 'new-draft', type: 'task', position: { x: 400, y: 0 }, data: { kind: 'general', config: { model: 'wan-ref', prompt: '', referenceAssetIds: [], parameters: {} } } }
  ];
  const models = [model('wan-ref', { image: { min: 1, max: 10 }, video: { min: 0, max: 5 } })];
  const bound = bindImportedResult('new-draft', imported, staleAssets, nodes, [], models, () => 'input-1');
  assert.equal(bound.error, null);
  assert.equal(bound.assets[0].id, imported.id);
  assert.equal(bound.edges[0].source, 'result-card');
  assert.deepEqual(bound.edges[0].data, { kind: 'input', assetId: imported.id, order: 1 });
  assert.deepEqual(syncDraftReferences(bound.nodes, bound.edges)[1].data.config.referenceAssetIds, [imported.id]);
});

test('不兼容、超限、已提交和预检中的输入连接被拒绝', () => {
  const nodes = [{ id: 'v', type: 'asset', data: { assetId: 'v' } }, { id: 'a', type: 'asset', data: { assetId: 'a' } }, { id: 't', type: 'task', data: { kind: 'general', config: { model: 'sync_lipsync' } } }];
  const assets = [asset('v', 'video'), asset('v2', 'video'), asset('a')];
  const models = [model('sync_lipsync', { video: { min: 1, max: 1 }, audio: { min: 1, max: 1 } })];
  assert.match(addInputEdge('a', 't', nodes, [], assets, models, () => 'x').error, /不接受/);
  const first = addInputEdge('v', 't', nodes, [], assets, models, () => 'e');
  assert.equal(first.error, null);
  assert.match(addInputEdge('v2', 't', [{ ...nodes[0], id: 'v2', data: { assetId: 'v2' } }, ...nodes], first.edges, assets, models, () => 'x').error, /上限/);
  assert.match(addInputEdge('v', 't', nodes, [], assets, models, () => 'x', new Set(['t'])).error, /预检/);
  assert.match(addInputEdge('v', 't', [nodes[0], nodes[1], { ...nodes[2], data: { ...nodes[2].data, requestId: 'paid' } }], [], assets, models, () => 'x').error, /已提交/);
});

test('模型选择按完整输入约束优先Sync，模型切换不兼容时可判定拒绝', () => {
  const assets = [asset('v', 'video'), asset('a', 'audio'), asset('i')];
  const wan = model('wan', { image: { min: 1, max: 3 }, video: { min: 0, max: 5 }, audio: { min: 0, max: 1 } });
  const sync = model('sync_lipsync', { video: { min: 1, max: 1 }, audio: { min: 1, max: 1 } });
  assert.equal(modelAcceptsReferences(wan, ['v', 'a'], assets), true);
  assert.equal(modelMeetsReferenceMinimums(wan, ['v', 'a'], assets), false);
  assert.equal(modelMeetsReferenceMinimums(sync, ['v', 'a'], assets), true);
  assert.equal(modelAcceptsReferences(sync, ['i'], assets), false);
  assert.equal(sync.promptRequired, false);
  assert.equal(availableModel('flux-schnell', [wan, sync]), undefined);
  assert.equal(availableModel('sync_lipsync', [wan, sync]), sync);
});

test('旧来源线不被误认输入，旧草稿参考转成真实输入线并保留原文', () => {
  const nodes = [{ id: 'a', type: 'asset', position: { x: 0, y: 0 }, data: { assetId: 'a' } }, { id: 't', type: 'task', position: { x: 400, y: 0 }, data: { kind: 'general', config: { model: 'wan', prompt: ' 原文 ', referenceAssetIds: ['a'] } } }];
  const legacy = [{ id: 'old', source: 'a', target: 't' }];
  let n = 0;
  const result = migrateLegacyDrafts(nodes, legacy, [asset('a')], () => `new${++n}`);
  assert.equal(result.edges[0].data.kind, 'origin');
  assert.equal(result.edges[0].style.strokeDasharray, '5 5');
  assert.deepEqual(inputAssetIds('t', result.edges), ['a']);
  assert.equal(result.nodes[1].data.config.prompt, ' 原文 ');
  assert.equal(migrateLegacyDrafts(result.nodes, result.edges, [asset('a')], () => 'unused').changed, false);
});

test('已绑定jobId的旧任务不再当作草稿重派生参考', () => {
  const nodes = [{ id: 't', type: 'task', position: { x: 0, y: 0 }, data: { kind: 'general', jobId: 'old-job', config: { model: 'wan', referenceAssetIds: ['old'] } } }];
  const migrated = migrateLegacyDrafts(nodes, [], [asset('old')], () => 'unexpected');
  assert.deepEqual(migrated.nodes[0].data.config.referenceAssetIds, ['old']);
  assert.equal(migrated.edges.length, 0);
});

test('已提交输入冻结：单删素材卡或输入线拒绝，连同任务卡移除允许', () => {
  const nodes = [{ id: 'a', type: 'asset', data: { assetId: 'a' } }, { id: 't', type: 'task', data: { kind: 'general', requestId: 'paid' } }];
  const edges = [{ id: 'e', source: 'a', target: 't', data: { kind: 'input', assetId: 'a', order: 1 } }];
  assert.match(protectedRemovalError(nodes, edges, new Set(['a']), new Set()), /冻结/);
  assert.match(protectedRemovalError(nodes, edges, new Set(), new Set(['e'])), /冻结/);
  assert.equal(protectedRemovalError(nodes, edges, new Set(['a', 't']), new Set()), null);
  assert.match(protectedRemovalError(nodes, edges, new Set(), new Set(['e']), new Set(['t'])), /预检/);
});

test('复制含参考任务而未选素材卡时，复用原素材卡的真实输入线', () => {
  const nodes = [{ id: 'a', type: 'asset', position: { x: 0, y: 0 }, data: { assetId: 'a' } }, { id: 't', type: 'task', position: { x: 400, y: 0 }, selected: true, data: { kind: 'general', requestId: 'paid', config: { model: 'wan', referenceAssetIds: ['a'] } } }];
  const edges = [{ id: 'e', source: 'a', target: 't', data: { kind: 'input', assetId: 'a', order: 1 } }];
  let n = 0;
  const copied = copyCards(nodes, edges, () => `copy${++n}`);
  const clone = copied.nodes.at(-1);
  assert.equal(clone.data.requestId, undefined);
  assert.deepEqual(inputAssetIds(clone.id, copied.edges), ['a']);
  assert.deepEqual(syncDraftReferences(copied.nodes, copied.edges).at(-1).data.config.referenceAssetIds, ['a']);
});

test('复制旧冻结通用卡且没有输入线时，按原配置顺序补建真正输入线', () => {
  const nodes = [
    { id: 'old-image', type: 'asset', position: { x: 0, y: 0 }, data: { assetId: 'image' } },
    { id: 'task', type: 'task', position: { x: 420, y: 0 }, selected: true, data: { kind: 'general', requestId: 'paid', jobId: 'job', config: { model: 'wan3.0-video', prompt: '原文', referenceAssetIds: ['video', 'image'] } } }
  ];
  const oldOrigin = [{ id: 'old-origin', source: 'old-image', target: 'task', data: { kind: 'origin' } }];
  const assets = [asset('image'), asset('video', 'video')];
  let n = 0;
  const copied = copyCards(nodes, oldOrigin, () => `copy-${++n}`, assets);
  assert.equal(copied.error, null);
  const draft = copied.nodes.find(node => node.id === 'copy-1');
  assert.equal(draft.data.requestId, undefined);
  assert.equal(draft.data.jobId, undefined);
  assert.deepEqual(inputAssetIds(draft.id, copied.edges), ['video', 'image']);
  assert.deepEqual(syncDraftReferences(copied.nodes, copied.edges).find(node => node.id === draft.id).data.config.referenceAssetIds, ['video', 'image']);
  assert.equal(oldOrigin[0].data.kind, 'origin');
  assert.equal(copied.edges[0].data.kind, 'origin');
  assert.equal(copied.nodes.some(node => node.type === 'asset' && node.data.assetId === 'video'), true);
  const missing = copyCards(nodes, oldOrigin, () => 'unused', [asset('image')]);
  assert.match(missing.error, /缺失/);
  assert.equal(missing.nodes, nodes);
});


test('历史卡展示冻结素材快照并保留缺失项，草稿仍按有序输入线展示', () => {
  const data = { kind: 'general', config: { referenceAssetIds: ['edited'], prompt: '修改后' } };
  const job = { kind: 'general', config: { reference_assets: ['D:/旧素材.png', 'missing'] } };
  assert.deepEqual(taskReferenceIds(data, job, ['connected']), ['D:/旧素材.png', 'missing']);
  assert.deepEqual(taskReferenceIds(data, undefined, ['connected']), ['connected']);
  assert.deepEqual(taskReferenceIds({ ...data, requestId: 'pending' }, undefined, ['connected']), ['edited']);
  assert.deepEqual(taskReferenceIds({ kind: 'full' }, { kind: 'full', config: { source: 'original.mp4', image: 'product.png', extra_images: ['detail.png'] } }), ['original.mp4', 'product.png', 'detail.png']);
});

test('历史提示词从任务原快照读取，基础词和实际提交词可分别复制且不会改记录', () => {
  const job = { kind: 'full', config: { base_prompt: '原文', remove_subtitles: true }, actual_prompt: SUBTITLE_PREFIX + '原文' };
  const before = structuredClone(job);
  assert.deepEqual(taskPrompts({ kind: 'full', config: { prompt: '卡片上修改过的词' } }, job), [{ label: '基础提示词', value: '原文' }, { label: '实际提交提示词', value: SUBTITLE_PREFIX + '原文' }]);
  assert.deepEqual(job, before);
  assert.deepEqual(taskPrompts({ kind: 'general', config: { prompt: '草稿' } }, { kind: 'general', config: { model: '已下架模型', prompt: '历史原文' } }), [{ label: '提示词', value: '历史原文' }]);
});

test('右键菜单在窗口边缘保持可见且不改写用于创建的画布落点', () => {
  const click = { x: 990, y: 790 };
  assert.deepEqual(contextMenuPosition(click, { width: 1000, height: 800 }), { x: 732, y: 532 });
  assert.deepEqual(click, { x: 990, y: 790 });
  assert.deepEqual(contextMenuPosition({ x: -30, y: -10 }, { width: 1000, height: 800 }), { x: 8, y: 8 });
});


test('新视频家族草稿首次接参考自动切路由，删完回文生，两个false始终保留且冻结记录不迁移', () => {
  const refs = { image: { min: 0, max: 10 }, video: { min: 0, max: 5 }, audio: { min: 0, max: 5 } };
  const parameters = [{ key: 'duration', type: 'integer', default: 5, min: 2, max: 30 }, { key: 'enable_thinking', type: 'boolean', default: true }, { key: 'enable_prompt_expansion', type: 'boolean', default: true }];
  const models = [{ id: 'text', familyId: 'family', nodeType: 'video', routeWhen: 'no_references', familyReferenceLimits: refs, referenceLimits: { image: {max:0}, video: {max:0}, audio: {max:0} }, parameters }, { id: 'ref', familyId: 'family', nodeType: 'video', routeWhen: 'has_references', familyReferenceLimits: refs, referenceLimits: refs, parameters }];
  const family = { id: 'family', nodeType: 'video', parameters, routes: [{ modelId: 'text', when: 'no_references' }, { modelId: 'ref', when: 'has_references' }] };
  const config = draftForFamily(family, [], '原文', { enable_thinking: false, enable_prompt_expansion: false });
  const nodes = [{ id:'asset', type:'asset', data:{assetId:'v'} }, { id:'task', type:'task', data:{kind:'general', config} }];
  const assets = [{ id:'v', kind:'video' }];
  const added = addInputEdge('asset', 'task', nodes, [], assets, models, () => 'edge');
  assert.equal(added.error, null);
  const bound = syncDraftReferences(nodes, added.edges, models);
  assert.equal(bound[1].data.config.model, 'ref');
  assert.deepEqual(bound[1].data.config.parameters, { duration:5, enable_thinking:false, enable_prompt_expansion:false });
  assert.equal(syncDraftReferences(bound, [], models)[1].data.config.model, 'text');
  const frozen = { ...bound[1], data:{ ...bound[1].data, requestId:'old-request' } };
  assert.strictEqual(syncDraftReferences([frozen], [], models)[0], frozen);
  assert.equal(normalizeDraft({model:'ref', parameters:{enable_thinking:false,enable_prompt_expansion:false}}, [], models).model, 'text');
});


test('官方服务商切换保留兼容参数、清除思考参数、历史复用保留 provider', () => {
  const parameters = [{ key: 'duration', type: 'integer', default: 5, min: 2, max: 30 }, { key: 'resolution', type: 'enum', default: '480p', enum: ['480p', '720p', '1080p'] }, { key: 'enable_prompt_expansion', type: 'boolean', default: true }];
  const family = { id: 'wan3-dashscope', nodeType: 'video', provider: 'dashscope', parameters, routes: [{ modelId: 'official', when: 'always' }, { modelId: 'prime', when: 'always' }] };
  const draft = draftForFamily(family, ['image'], '  prompt  ', { duration: 7, resolution: '720p', enable_prompt_expansion: false, enable_thinking: true });
  assert.equal(draft.provider, 'dashscope');
  assert.deepEqual(draft.parameters, { duration: 7, resolution: '720p', enable_prompt_expansion: false });
  const invalid = draftForFamily(family, [], 'same', { duration: 99, resolution: '4k' });
  assert.deepEqual(invalid.parameters, { duration: 5, resolution: '480p', enable_prompt_expansion: true });
  const job = { kind: 'general', config: { ...draft, model: 'prime' } };
  assert.equal(configFromJob(job, [{ id: 'image' }]).provider, 'dashscope');
  assert.equal(configFromJob(job, [{ id: 'image' }]).model, 'prime');
  const full = { kind: 'full', config: { provider: 'dashscope', model: 'prime', source: 'v', image: 'i' } };
  assert.equal(configFromJob(full, []).provider, 'dashscope');
});

test('官方 Prime 草稿同步素材保持型号，已提交草稿不随当前服务商变化', () => {
  const limits = { image: { min: 0, max: 10 }, video: { min: 0, max: 5 }, audio: { min: 0, max: 5 } };
  const models = ['official', 'prime'].map(id => ({ id, provider: 'dashscope', familyId: 'wan3-dashscope', nodeType: 'video', routeWhen: 'always', referenceLimits: limits, parameters: [] }));
  const config = { model: 'prime', provider: 'dashscope', familyId: 'wan3-dashscope', nodeType: 'video', parameters: {}, referenceAssetIds: [] };
  assert.equal(normalizeDraft(config, ['image'], models).model, 'prime');
  const frozen = { id: 'frozen', type: 'task', data: { kind: 'general', jobId: 'original', config } };
  const synced = syncDraftReferences([frozen], [], [{ ...models[0], provider: 'fal' }]);
  assert.equal(synced[0], frozen);
  assert.equal(synced[0].data.config.provider, 'dashscope');
});


test('通用草稿 fal→官方→fal 恢复各服务商显式开关，官方实际参数不带thinking', () => {
  const shared = [{ key: 'duration', type: 'integer', default: 5, min: 2, max: 30 }, { key: 'enable_prompt_expansion', type: 'boolean', default: true }];
  const fal = { id: 'wan3', provider: 'fal', nodeType: 'video', parameters: [...shared, { key: 'enable_thinking', type: 'boolean', default: true }], routes: [{ modelId: 'fal-ref', when: 'always' }] };
  const official = { id: 'wan3-dashscope', provider: 'dashscope', nodeType: 'video', parameters: shared, routes: [{ modelId: 'official', when: 'always' }] };
  const original = draftForFamily(fal, [], 'original', { duration: 7, enable_thinking: false, enable_prompt_expansion: false });
  const ali = switchFamilyDraft(original, official);
  assert.equal(ali.parameters.enable_prompt_expansion, false);
  assert.ok(!('enable_thinking' in ali.parameters));
  ali.parameters.enable_prompt_expansion = true;
  const restored = switchFamilyDraft(ali, fal);
  assert.equal(restored.parameters.enable_thinking, false);
  assert.equal(restored.parameters.enable_prompt_expansion, false);
  const again = switchFamilyDraft(restored, official);
  assert.equal(again.parameters.enable_prompt_expansion, true);
  assert.ok(!('enable_thinking' in again.parameters));
  const reused = configFromJob({ kind: 'general', config: { ...again, canvas_draft: again } }, []);
  assert.equal(switchFamilyDraft(reused, fal).parameters.enable_thinking, false);
});

test('换品草稿往返服务商保留两个false并分别恢复官方扩写选择', () => {
  const original = { provider: 'fal', model: 'fal-ref', enableThinking: false, enablePromptExpansion: false, prompt: 'same' };
  const ali = switchSwapProvider(original, 'dashscope', 'official');
  assert.equal(ali.enableThinking, false);
  assert.equal(ali.enablePromptExpansion, false);
  ali.enablePromptExpansion = true;
  const restored = switchSwapProvider(ali, 'fal', 'fal-ref');
  assert.equal(restored.enableThinking, false);
  assert.equal(restored.enablePromptExpansion, false);
  assert.equal(switchSwapProvider(restored, 'dashscope', 'official').enablePromptExpansion, true);
  const snapshot = { kind: 'full', config: { provider: 'dashscope', model: 'official', enable_thinking: false, canvas_draft: ali } };
  assert.equal(switchSwapProvider(configFromJob(snapshot, []), 'fal', 'fal-ref').enableThinking, false);
});


test('TJ 多参切换保留输入和提示词，旧草稿固定720p但已提交快照不变', () => {
  const parameters = [{ key: 'resolution', type: 'enum', default: '720p', enum: ['720p'] }, { key: 'duration', type: 'enum', default: 4, enum: [4, 5, 6, 8, 10, 12, 15, 20, 25, 30] }];
  const limits = { image: {min:0,max:30}, video: {min:0,max:10}, audio: {min:0,max:10} };
  const family = { id: 'seedance-kaiyuncode', provider: 'kaiyuncode', nodeType: 'video', parameters, referenceLimits: limits, routes: [{modelId:'kaiyuncode-seedance-2.5-tj',when:'always'}] };
  const refs = ['image', 'video', 'audio'];
  const switched = switchFamilyDraft({provider:'kaiyuncode', prompt:'  用户原文  ', parameters:{resolution:'1080p',duration:10,enable_prompt_expansion:true}}, family, refs);
  assert.deepEqual(switched.referenceAssetIds, refs);
  assert.equal(switched.prompt, '  用户原文  ');
  assert.deepEqual(switched.parameters, {resolution:'720p',duration:10});
  const model = {id:switched.model, familyId:family.id, provider:family.provider, nodeType:'video', routeWhen:'always', parameters, referenceLimits:limits};
  const old = {...switched, parameters:{resolution:'480p',duration:10}};
  assert.equal(normalizeDraft(old, refs, [model]).parameters.resolution, '720p');
  const frozen = {id:'frozen',type:'task',data:{kind:'general',jobId:'submitted',config:old}};
  assert.equal(syncDraftReferences([frozen], [], [model])[0], frozen);
  assert.equal(frozen.data.config.parameters.resolution, '480p');
});
