export interface Status {
  model: string;
  api_ready: boolean;
  budget: { initialized: boolean; ceiling_cny: number | string | null; spent_cny: number | string | null; remaining_cny: number | string | null; frozen: boolean };
  docker: { available: boolean; image_ready: boolean };
  default_mode?: 'live';
  document_formats: string[];
}
export interface DocumentRecord {
  id: string; name: string; kind: string; bytes: number; created_at: string; synthetic: boolean; document_count: number;
}
export interface DocumentDetail extends DocumentRecord {
  preview: { pre_text?: string[] | string; post_text?: string[] | string; table?: unknown[][] };
}
export interface RunEvent {
  id: string; type: 'model' | 'skill' | 'tool' | 'error' | 'status'; title: string; detail?: unknown; success?: boolean; timestamp: string;
}
export interface Artifact { id: string; name: string; kind: string; url: string }
export interface SourceEvidence {
  evidence_id: string; document_id: string; locator: string; text: string; body?: string; source_sha256?: string;
  section?: string; row?: number; column?: number; column_header?: string; row_label?: string;
}
export interface Run {
  id: string; session_id: string; question: string; mode: 'demo' | 'live';
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'; created_at: string;
  events: RunEvent[]; final: null | { answer: unknown; unit?: string; explanation: string; sources: unknown[]; calculation_id?: string; status?: string };
  artifacts: Artifact[]; error?: string | null; steps: number; elapsed_seconds: number; cost_cny?: number | string | null; cancel_requested?: boolean;
  source_evidence?: SourceEvidence[];
}
export interface Session { id: string; title: string; created_at: string; updated_at: string; document_ids: string[]; runs?: Run[] }
export interface Skill {
  name: string; description: string; triggers: string[]; version: number | string; version_id: string; origin: 'builtin' | 'learned'; sha256: string; body?: string;
}
export interface Learning { experiences: unknown[]; candidates: unknown[]; reports: unknown[] }

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (options.method && options.method !== 'GET') headers.set('X-Finagent-Client', 'web');
  let response: Response;
  try { response = await fetch(path, { ...options, headers, credentials: 'same-origin' }); }
  catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new Error('无法连接本地服务。请启动 finagent web 后重试。');
  }
  const contentType = response.headers.get('content-type') ?? '';
  if (!contentType.includes('application/json')) {
    throw new Error(response.ok ? '服务返回了非 API 内容，请检查后端地址。' : `本地服务未响应（HTTP ${response.status}），请检查后端是否启动。`);
  }
  const value = await response.json();
  if (!response.ok) {
    const detail = value.detail ?? value.error;
    throw new Error(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `请求失败（HTTP ${response.status}）`);
  }
  return value as T;
}

export function readable(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === null || value === undefined) return '';
  return typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
}
export function safeArtifactUrl(value: string): string | undefined {
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin && url.pathname.startsWith('/api/') ? url.href : undefined;
  } catch { return undefined; }
}
export function money(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '未设置';
  const normalized = Number(value);
  return Number.isFinite(normalized) ? `¥${normalized.toFixed(2)}` : '未设置';
}
export function isActive(run: Run | undefined): boolean { return run?.status === 'queued' || run?.status === 'running'; }
