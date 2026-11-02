export const token = decodeURIComponent(location.hash.slice(1));

export type Asset = { id: string; name: string; kind: 'video' | 'image' | 'audio'; path: string; size: number; group_id?: string };
export type AssetGroup = { id: string; name: string; created: number };
export type JobKind = 'general' | 'full' | 'lipsync' | 'repair';
export type ReferenceLimits = Record<Asset['kind'], { min: number; max: number }>;
export type ModelParameter = { key: string; type: 'number' | 'integer' | 'boolean' | 'enum' | 'string'; label: string; default: string | number | boolean; min?: number; max?: number; enum?: (string | number)[]; placement?: 'direct' };
export type GeneralModel = { provider?: string; providerLabel?: string; pricingUnverified?: boolean; id: string; label: string; outputKind: 'video' | 'image'; promptRequired: boolean; referenceLimits: ReferenceLimits; parameters: ModelParameter[]; familyId?: string; nodeType?: string; familyReferenceLimits?: ReferenceLimits; routeWhen?: string };
export type ModelFamily = { provider?: string; providerLabel?: string; id: string; label: string; nodeType: string; promptRequired: boolean; referenceLimits: ReferenceLimits; parameters: ModelParameter[]; routes: { modelId: string; when: 'no_references' | 'has_references' | 'always'; label: string }[] };
export type CostEstimate = { status: 'known' | 'unknown'; reason?: string; note?: string; amount?: number; billableSeconds?: number; outputSeconds?: number; referenceVideoSeconds?: number; unitRate?: number; unit?: string; currency?: string; durationBasis?: string; verifiedAt?: string; sourceUrl?: string };
export type Job = { id: string; kind: JobKind; name: string; status: string; stage: string; request_id?: string; config: Record<string, any>; result?: { output?: string; output_kind?: 'video' | 'image'; video_url?: string; local_downloaded?: boolean }; error?: string; events?: { time: number; stage: string }[]; task_id?: string; actual_prompt?: string; created?: number; started?: number; finished?: number };
export type CanvasSummary = { id: string; name: string; updated: number };
export type CanvasDocument = CanvasSummary & { revision: number; nodes: any[]; edges: any[]; viewport: { x: number; y: number; zoom: number } };
export type Bootstrap = { canvases: CanvasSummary[]; assets: Asset[]; groups: AssetGroup[]; jobs: Job[]; models: { model: string; label: string; adapter: string; provider?: string }[]; generalModels: GeneralModel[]; modelFamilies: ModelFamily[]; demo: boolean };

export class ApiError extends Error {
  constructor(message: string, public code?: string, public submitted?: boolean) { super(message); }
}

export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: { 'X-Canvas-Token': token, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store'
  });
  const content = await response.json();
  if (!response.ok) throw new ApiError(content.error || `操作失败 (${response.status})`, content.code, content.submitted);
  return content as T;
}

export async function upload(file: File, groupId: string | null = null): Promise<Asset> {
  const target = groupId ? `&group_id=${encodeURIComponent(groupId)}` : '';
  const response = await fetch(`/api/assets/upload?name=${encodeURIComponent(file.name)}${target}`, {
    method: 'POST', headers: { 'X-Canvas-Token': token }, body: file
  });
  const content = await response.json();
  if (!response.ok) throw new ApiError(content.error || `导入失败 (${response.status})`, content.code);
  return content as Asset;
}

export const media = (kind: string, id: string) => `/media/${kind}/${encodeURIComponent(id)}?token=${encodeURIComponent(token)}`;

declare global {
  interface Window { pywebview?: { api: { host_action: (action: string) => Promise<unknown>; download_result: (identity: string) => Promise<{ cancelled?: boolean; path?: string; error?: string }> } }; canvasFlush?: () => Promise<void> }
}
