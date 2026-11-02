// Creates one isolated, unpaid 100-card canvas in a demo server only.
// Usage: node tests/seed-performance.mjs "http://127.0.0.1:PORT/#TOKEN"
const target = process.argv[2];
if (!target) throw new Error('请提供本地演示画布 URL（包含 # 令牌）。');
const base = new URL(target);
if (base.hostname !== '127.0.0.1' && base.hostname !== 'localhost') throw new Error('只允许本机演示服务。');
const token = decodeURIComponent(base.hash.slice(1));
if (!token) throw new Error('URL 缺少画布令牌。');

async function api(path, method = 'GET', body) {
  const response = await fetch(new URL(path, base), { method,
    headers: { 'X-Canvas-Token': token, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const content = await response.json();
  if (!response.ok) throw new Error(content.error || `请求失败 ${response.status}`);
  return content;
}

const bootstrap = await api('/api/bootstrap');
if (bootstrap.demo !== true) throw new Error('当前并非演示服务，未写入任何画布。');
const name = `性能验收 · 100卡 · ${new Date().toLocaleString('zh-CN')}`;
const canvas = await api('/api/canvases', 'POST', { name });
const nodes = [], edges = [];
for (let groupIndex = 0; groupIndex < 5; groupIndex++) {
  const groupId = `perf-group-${groupIndex}`;
  nodes.push({ id: groupId, type: 'group', position: { x: groupIndex * 1380, y: 40 },
    style: { width: 1300, height: 820 }, data: { title: `素材组 ${groupIndex + 1}` } });
  for (let itemIndex = 0; itemIndex < 11; itemIndex++) {
    const id = `perf-note-${groupIndex}-${itemIndex}`;
    nodes.push({ id, type: 'note', parentId: groupId,
      position: { x: 35 + (itemIndex % 4) * 310, y: 65 + Math.floor(itemIndex / 4) * 240 },
      data: { text: `验收备注 ${groupIndex + 1}-${itemIndex + 1}` } });
    if (itemIndex > 0) edges.push({ id: `perf-edge-note-${groupIndex}-${itemIndex}`, source: `perf-note-${groupIndex}-${itemIndex - 1}`, target: id, type: 'smoothstep' });
  }
}
for (let i = 0; i < 20; i++) {
  const id = `perf-task-${i}`;
  nodes.push({ id, type: 'task', position: { x: (i % 10) * 580, y: 1020 + Math.floor(i / 10) * 450 },
    data: { kind: i % 3 === 0 ? 'repair' : i % 3 === 1 ? 'lipsync' : 'full', config: {} } });
  if (i > 0) edges.push({ id: `perf-edge-task-${i}`, source: `perf-task-${i - 1}`, target: id, type: 'smoothstep' });
}
const assets = bootstrap.assets || [];
for (let i = 0; i < 20; i++) {
  const id = `perf-asset-${i}`;
  nodes.push(assets.length ? { id, type: 'asset', position: { x: (i % 10) * 580, y: 2070 + Math.floor(i / 10) * 350 },
    data: { assetId: assets[i % assets.length].id } } :
    { id, type: 'note', position: { x: (i % 10) * 580, y: 2070 + Math.floor(i / 10) * 350 }, data: { text: `无素材时的占位卡 ${i + 1}` } });
  edges.push({ id: `perf-edge-asset-${i}`, source: id, target: `perf-task-${i}`, type: 'smoothstep' });
}
if (nodes.length !== 100) throw new Error(`内部错误：创建了 ${nodes.length} 张卡片。`);
await api(`/api/canvases/${canvas.id}`, 'PUT', { revision: canvas.revision, nodes, edges, viewport: { x: 100, y: 100, zoom: 0.65 } });
console.log(`已创建演示画布“${name}”，100 张卡片、${edges.length} 条连线；画布 ID：${canvas.id}`);
