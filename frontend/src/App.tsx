import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent, KeyboardEvent, ReactNode } from 'react';
import {
  ArrowUp, ArrowUpRight, BookOpen, Check, CheckCircle2, ChevronDown, ChevronRight,
  CircleHelp, Clock3, Copy, Download, FileChartColumn, FileText, FolderOpen,
  Layers2, Loader2, Menu, Moon, PanelLeftClose, PanelRight, Paperclip, Plus,
  Search, Settings2, ShieldCheck, Sparkles, Square, Sun, Terminal, TrendingUp,
  TriangleAlert, X,
} from 'lucide-react';
import { isActive, money, readable, request, safeArtifactUrl } from './api';
import type { Artifact, DocumentDetail, DocumentRecord, Learning, Run, RunEvent, Session, Skill, SourceEvidence, Status } from './api';

const STORAGE_KEY = 'finagent.ui.v1';
const LazyMarkdown = lazy(() => import('./Markdown'));
const SKILL_LABELS: Record<string, string> = {
  'financial-ratios': '财务比率', 'source-alignment': '来源对齐',
  'statement-reconciliation': '报表核验', 'comparative-analysis': '比较分析',
  'valuation-dcf': '现金流估值', 'terminal-analysis': '终端分析',
};
const SKILL_DESCRIPTIONS: Record<string, string> = {
  'financial-ratios': '计算增长、占比与利润率，核对分母、百分比和单位。',
  'source-alignment': '使用数据前，先对齐年份、主体、单位与指标定义。',
  'statement-reconciliation': '按照明确口径与容差，核验资产负债、利润和现金流。',
  'comparative-analysis': '对齐指标，比较公司、业务分部或不同时期。',
  'valuation-dcf': '在输入与假设充分时，构建可复核的现金流折现情景。',
  'terminal-analysis': '通过受限终端，以 Python、SQL 和批处理完成可复现分析。',
};
const suggestions = [
  { icon: TrendingUp, title: '看懂营收增长', description: '从上传资料提取两期数据', question: '请根据我上传的资料，提取两期营收，计算增长金额与增长率，标注数据来源并生成图表。' },
  { icon: Layers2, title: '对比两期财务指标', description: '比较变化，核对口径和单位', question: '请根据我上传的资料，对比两期营收与净利润，计算同比变化并标注数据来源。' },
  { icon: ShieldCheck, title: '核验财务计算', description: '让每个结论都有计算依据', question: '请核验资料中的毛利率与净利率，列出原始数值、单位和计算过程。' },
  { icon: FileChartColumn, title: '整理研究报告', description: '把资料、计算和图表放在一起', question: '请基于上传的资料整理一份财务分析报告，标注数据出处，将关键指标制作成图表，并说明信息缺口。' },
];

function savedPreferences(): { session: string | null; theme: 'light' | 'dark' } {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}');
    return { session: typeof saved.session === 'string' ? saved.session : null, theme: saved.theme === 'dark' ? 'dark' : 'light' };
  } catch { return { session: null, theme: 'light' }; }
}
const INITIAL_PREFERENCES = savedPreferences();
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : '请求未完成，请重试。'; }
function wasAborted(error: unknown): boolean { return error instanceof DOMException && error.name === 'AbortError'; }
function elapsed(seconds: number): string { return seconds < 60 ? `${Math.round(seconds)} 秒` : `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`; }
function bytes(value: number): string { return value < 1024 ? `${value} B` : value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`; }
function WaveMark({ large = false }: { large?: boolean }) {
  return <span className={large ? 'wave-mark wave-mark-large' : 'wave-mark'} aria-hidden="true"><svg viewBox="0 0 40 40" fill="none"><path d="M5 25h7l4-13 5 20 4-14h10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg></span>;
}
function IconButton({ label, onClick, children, className = '', disabled = false }: { label: string; onClick: () => void; children: ReactNode; className?: string; disabled?: boolean }) {
  return <button type="button" className={`icon-button ${className}`} aria-label={label} title={label} onClick={onClick} disabled={disabled}>{children}</button>;
}
function Markdown({ children }: { children: string }) {
  return <Suspense fallback={<p className="muted">正在排版…</p>}><LazyMarkdown>{children}</LazyMarkdown></Suspense>;
}
function Modal({ title, children, onClose, wide = false }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    const dialog = dialogRef.current;
    dialog?.querySelector<HTMLElement>('button, input, select, textarea, [tabindex="0"]')?.focus();
    const keyHandler = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); onClose(); }
      if (event.key !== 'Tab' || !dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]'));
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', keyHandler);
    return () => { document.removeEventListener('keydown', keyHandler); previousFocus?.focus(); };
  }, [onClose]);
  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div className={`modal ${wide ? 'modal-wide' : ''}`} ref={dialogRef} role="dialog" aria-modal="true" aria-label={title}>
      <div className="modal-header"><h2>{title}</h2><IconButton label="关闭弹窗" onClick={onClose}><X size={18} /></IconButton></div>
      <div className="modal-content">{children}</div>
    </div>
  </div>;
}
function ActivityList({ events, compact = false }: { events: RunEvent[]; compact?: boolean }) {
  if (!events.length) return <p className="panel-empty">分析启动后，执行过程会出现在这里。</p>;
  return <ol className={`activity-list ${compact ? 'compact' : ''}`}>{events.map((event) => {
    const failed = event.success === false || event.type === 'error';
    const Icon = failed ? TriangleAlert : event.type === 'tool' ? Terminal : event.type === 'skill' ? BookOpen : event.type === 'model' ? Sparkles : Check;
    return <li key={event.id} className={failed ? 'event-failed' : ''}>
      <span className="event-icon"><Icon size={13} /></span>
      <div className="event-body"><div className="event-title">{event.title}</div>{event.detail ? <details><summary>查看详情</summary><pre>{readable(event.detail)}</pre></details> : null}</div>
    </li>;
  })}</ol>;
}
function ArtifactList({ artifacts, onPreview }: { artifacts: Artifact[]; onPreview: (artifact: Artifact) => void }) {
  if (!artifacts.length) return <p className="panel-empty">生成的报告与图表会保存在这里。</p>;
  return <div className="artifact-list">{artifacts.map((artifact) => {
    const url = safeArtifactUrl(artifact.url);
    const chart = artifact.kind === 'svg' || artifact.name.toLowerCase().endsWith('.svg');
    return <div className="artifact-item" key={artifact.id}>
      <button className="artifact-preview-button" onClick={() => onPreview(artifact)} disabled={!url}><span className="file-icon">{chart ? <FileChartColumn size={19} /> : <FileText size={19} />}</span><span><strong>{artifact.name}</strong><small>{chart ? '图表' : '研究报告'}</small></span></button>
      {url ? <a className="icon-button" href={url} download={artifact.name} aria-label={`下载 ${artifact.name}`} title="下载"><Download size={16} /></a> : null}
    </div>;
  })}</div>;
}
function RunMessage({ run, onPreview, onActivity, onSource }: { run: Run; onPreview: (artifact: Artifact) => void; onActivity: () => void; onSource: (source: SourceEvidence) => void }) {
  const [copied, setCopied] = useState(false);
  const active = isActive(run);
  const final = run.final;
  const copy = async () => {
    try { await navigator.clipboard.writeText(final?.explanation ?? ''); setCopied(true); setTimeout(() => setCopied(false), 1800); }
    catch { setCopied(false); }
  };
  return <section className="conversation-turn" aria-label="分析对话">
    <div className="user-message"><p>{run.question}</p></div>
    <div className="assistant-message">
      <div className="assistant-identity"><WaveMark /><span>Fin Agent</span>{run.mode === 'demo' ? <span className="tiny-badge">脚本演示</span> : <span className="tiny-badge live-badge">真实分析</span>}</div>
      {run.mode === 'demo' ? <p className="demo-disclosure">演示数据 · 不调用模型 API</p> : null}
      {active ? <div className="progress-block" role="status"><Loader2 size={17} className="spin" /><div><strong>{run.cancel_requested ? '正在停止，当前操作结束后停止' : run.events.at(-1)?.title ?? '准备分析'}</strong><span>已执行 {run.steps} 步</span></div><button className="text-button" onClick={onActivity}>查看过程 <ChevronRight size={14} /></button></div> : null}
      {final ? <>
        <Markdown>{final.explanation || readable(final.answer)}</Markdown>
        {final.status === 'insufficient_evidence' ? <div className="insufficient-evidence"><TriangleAlert size={14} />资料不足，当前证据无法支持完整结论。</div> : final.calculation_id ? <div className="evidence-strip"><span><CheckCircle2 size={14} />计算结果 <strong>{readable(final.answer)}{final.unit === 'percent' || final.unit === '%' ? '%' : final.unit ? ` ${final.unit}` : ''}</strong></span><span title={final.calculation_id}>计算凭证 <span className="receipt-id">{final.calculation_id}</span></span></div> : final.answer !== null && final.answer !== undefined ? <div className="evidence-strip"><span>分析结论 <strong>{readable(final.answer)}</strong></span></div> : null}
        {final.sources?.length ? <div className="source-references"><span>数据来源</span>{final.sources.map((source, index) => {
          const id = typeof source === 'object' && source !== null && 'id' in source ? String(source.id) : readable(source);
          const evidence = run.source_evidence?.find((item) => item.evidence_id === id);
          return evidence ? <button className="reference-chip reference-button" key={index} onClick={() => onSource(evidence)} title="查看原始证据"><FileText size={11} />{evidence.row_label || evidence.locator || id}<ChevronRight size={10} /></button> : <span className="reference-chip" key={index} title={id}><FileText size={11} />{id}</span>;
        })}</div> : null}
        {run.artifacts.length ? <ArtifactList artifacts={run.artifacts} onPreview={onPreview} /> : null}
      </> : null}
      {run.status === 'failed' ? <div className="inline-error"><TriangleAlert size={16} /><div><strong>这次分析没有完成</strong><p>{run.error || '执行失败，请查看过程后重试。'}</p></div></div> : null}
      {run.status === 'cancelled' ? <p className="cancelled-note"><Square size={12} />分析已停止，已完成的过程和文件仍保留。</p> : null}
      {!active ? <div className="response-footer"><button className="text-button" onClick={onActivity}><Clock3 size={13} />{run.steps} 步 · {elapsed(run.elapsed_seconds)} <ChevronRight size={12} /></button>{run.cost_cny !== null && run.cost_cny !== undefined && run.mode === 'live' ? <span>预估 {money(run.cost_cny)}</span> : null}{final ? <IconButton label={copied ? '已复制' : '复制回答'} onClick={copy}>{copied ? <Check size={14} /> : <Copy size={14} />}</IconButton> : null}</div> : null}
    </div>
  </section>;
}

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [selectedSession, setSelectedSession] = useState<string | null>(INITIAL_PREFERENCES.session);
  const [selectedDocuments, setSelectedDocuments] = useState<string[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [question, setQuestion] = useState('');
  const [learnOnFailure, setLearnOnFailure] = useState(false);
  const [workspace, setWorkspace] = useState<'chat' | 'skills'>('chat');
  const [panel, setPanel] = useState<'sources' | 'reports' | 'activity' | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [theme, setTheme] = useState<'light' | 'dark'>(INITIAL_PREFERENCES.theme);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [connectionError, setConnectionError] = useState('');
  const [actionError, setActionError] = useState('');
  const [reload, setReload] = useState(0);
  const [connecting, setConnecting] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [cancelPendingId, setCancelPendingId] = useState<string | null>(null);
  const [loadingSession, setLoadingSession] = useState(false);
  const [previewDocument, setPreviewDocument] = useState<string | null>(null);
  const [documentDetail, setDocumentDetail] = useState<DocumentDetail | null>(null);
  const [previewError, setPreviewError] = useState('');
  const [artifactPreview, setArtifactPreview] = useState<Artifact | null>(null);
  const [artifactText, setArtifactText] = useState('');
  const [artifactError, setArtifactError] = useState('');
  const [sourcePreview, setSourcePreview] = useState<SourceEvidence | null>(null);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [learning, setLearning] = useState<Learning | null>(null);
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null);
  const [skillDetail, setSkillDetail] = useState<Skill | null>(null);
  const [skillError, setSkillError] = useState('');
  const [skillsLoading, setSkillsLoading] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameTitle, setRenameTitle] = useState('');
  const questionRef = useRef<HTMLTextAreaElement>(null);
  const uploadRef = useRef<HTMLInputElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const createdSessionRef = useRef<string | null>(null);
  const latestRun = runs.at(-1);
  const activeRunId = runs.findLast((run) => isActive(run))?.id;
  const cancelPending = Boolean(activeRunId && (cancelPendingId === activeRunId || latestRun?.cancel_requested));
  const busy = submitting || Boolean(activeRunId);
  const currentSession = sessions.find((session) => session.id === selectedSession);
  const visibleSessions = useMemo(() => sessions.filter((session) => session.title.toLowerCase().includes(search.toLowerCase())), [sessions, search]);
  const allArtifacts = useMemo(() => runs.flatMap((run) => run.artifacts), [runs]);
  const selectedSourceRecords = useMemo(() => documents.filter((document) => selectedDocuments.includes(document.id)), [documents, selectedDocuments]);
  const liveUnavailable = !status?.api_ready || !status.budget.initialized || status.budget.frozen;

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ session: selectedSession, theme })); } catch { /* Storage is optional. */ }
  }, [selectedSession, theme]);

  useEffect(() => {
    const controller = new AbortController();
    setConnecting(true); setConnectionError('');
    void Promise.allSettled([
      request<Status>('/api/status', { signal: controller.signal }),
      request<Session[]>('/api/sessions', { signal: controller.signal }),
      request<DocumentRecord[]>('/api/documents', { signal: controller.signal }),
    ]).then(([statusResult, sessionsResult, docsResult]) => {
      if (controller.signal.aborted) return;
      if (statusResult.status === 'fulfilled') setStatus(statusResult.value);
      else setStatus(null);
      if (sessionsResult.status === 'fulfilled') {
        setSessions(sessionsResult.value);
        setSelectedSession((previous) => sessionsResult.value.some((session) => session.id === previous) ? previous : null);
      }
      if (docsResult.status === 'fulfilled') {
        const realDocuments = docsResult.value.filter((document) => !document.synthetic);
        setDocuments(realDocuments);
        setSelectedDocuments((previous) => previous.filter((id) => realDocuments.some((document) => document.id === id)));
      }
      const failures = [statusResult, sessionsResult, docsResult].filter((result): result is PromiseRejectedResult => result.status === 'rejected');
      if (failures.length) setConnectionError(errorMessage(failures[0].reason));
      setConnecting(false);
    });
    return () => controller.abort();
  }, [reload]);

  useEffect(() => {
    if (!selectedSession) { setRuns([]); setLoadingSession(false); return; }
    if (createdSessionRef.current === selectedSession) { createdSessionRef.current = null; return; }
    const controller = new AbortController();
    setLoadingSession(true); setRuns([]); setActionError('');
    void request<Session>(`/api/sessions/${encodeURIComponent(selectedSession)}`, { signal: controller.signal }).then((session) => {
      if (controller.signal.aborted) return;
      setRuns(session.runs ?? []); setSelectedDocuments(session.document_ids.filter((id) => id !== 'demo-company'));
    }).catch((error: unknown) => { if (!wasAborted(error)) setActionError(errorMessage(error)); }).finally(() => { if (!controller.signal.aborted) setLoadingSession(false); });
    return () => controller.abort();
  }, [selectedSession]);

  useEffect(() => {
    if (!activeRunId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const run = await request<Run>(`/api/runs/${encodeURIComponent(activeRunId)}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setActionError((previous) => previous.startsWith('无法获取分析进度：') ? '' : previous);
        if (isActive(run)) { setRuns((previous) => previous.map((item) => item.id === run.id ? run : item)); timer = setTimeout(poll, 900); }
        else {
          const results = await Promise.allSettled([request<Session[]>('/api/sessions', { signal: controller.signal }), request<Status>('/api/status', { signal: controller.signal })]);
          if (controller.signal.aborted) return;
          if (results[0].status === 'fulfilled') setSessions(results[0].value);
          if (results[1].status === 'fulfilled') setStatus(results[1].value);
          setCancelPendingId(null);
          setRuns((previous) => previous.map((item) => item.id === run.id ? run : item));
        }
      } catch (error) {
        if (wasAborted(error)) return;
        setActionError(`无法获取分析进度：${errorMessage(error)}`);
        timer = setTimeout(poll, 3000);
      }
    };
    timer = setTimeout(poll, 200);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [activeRunId, selectedSession]);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'instant', block: 'end' }); }, [runs.length, latestRun?.status]);

  const newAnalysis = useCallback(() => {
    if (submitting || uploading) return;
    setSelectedSession(null); setRuns([]); setQuestion(''); setActionError(''); setWorkspace('chat'); setSidebarOpen(false); setPanel(null);
    setSelectedDocuments([]);
    requestAnimationFrame(() => questionRef.current?.focus());
  }, [submitting, uploading]);

  useEffect(() => {
    const keyHandler = (event: globalThis.KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'n') { event.preventDefault(); newAnalysis(); }
      if (event.key === '/' && !event.metaKey && !event.ctrlKey && !['INPUT', 'TEXTAREA', 'SELECT'].includes((event.target as HTMLElement)?.tagName)) {
        event.preventDefault(); document.getElementById('session-search')?.focus();
      }
      if (event.key === 'Escape') { setSidebarOpen(false); setPanel(null); }
    };
    window.addEventListener('keydown', keyHandler);
    return () => window.removeEventListener('keydown', keyHandler);
  }, [newAnalysis]);

  useEffect(() => {
    if (!previewDocument || panel !== 'sources') return;
    const controller = new AbortController();
    setDocumentDetail(null); setPreviewError('');
    void request<DocumentDetail>(`/api/documents/${encodeURIComponent(previewDocument)}`, { signal: controller.signal }).then(setDocumentDetail).catch((error: unknown) => { if (!wasAborted(error)) setPreviewError(errorMessage(error)); });
    return () => controller.abort();
  }, [previewDocument, panel]);

  useEffect(() => {
    if (!artifactPreview) return;
    const url = safeArtifactUrl(artifactPreview.url);
    setArtifactText(''); setArtifactError('');
    if (!url) { setArtifactError('文件地址不属于本地服务，无法预览。'); return; }
    if (artifactPreview.name.toLowerCase().endsWith('.svg') || artifactPreview.kind === 'svg') return;
    const controller = new AbortController();
    void fetch(url, { signal: controller.signal, credentials: 'same-origin' }).then(async (response) => {
      if (!response.ok) throw new Error(`文件读取失败（HTTP ${response.status}）`);
      setArtifactText(await response.text());
    }).catch((error: unknown) => { if (!wasAborted(error)) setArtifactError(errorMessage(error)); });
    return () => controller.abort();
  }, [artifactPreview]);

  useEffect(() => {
    if (workspace !== 'skills') return;
    const controller = new AbortController();
    setSkillsLoading(true); setSkillError('');
    void Promise.allSettled([request<Skill[]>('/api/skills', { signal: controller.signal }), request<Learning>('/api/learning', { signal: controller.signal })]).then(([catalog, records]) => {
      if (controller.signal.aborted) return;
      if (catalog.status === 'fulfilled') { setSkills(catalog.value); setSelectedSkill((previous) => previous ?? catalog.value[0]?.name ?? null); }
      if (records.status === 'fulfilled') setLearning(records.value);
      const failure = [catalog, records].find((result): result is PromiseRejectedResult => result.status === 'rejected');
      if (failure) setSkillError(errorMessage(failure.reason));
      setSkillsLoading(false);
    });
    return () => controller.abort();
  }, [workspace, reload]);

  useEffect(() => {
    if (workspace !== 'skills' || !selectedSkill) return;
    const controller = new AbortController(); setSkillDetail(null);
    void request<Skill>(`/api/skills/${encodeURIComponent(selectedSkill)}`, { signal: controller.signal }).then(setSkillDetail).catch((error: unknown) => { if (!wasAborted(error)) setSkillError(errorMessage(error)); });
    return () => controller.abort();
  }, [selectedSkill, workspace]);

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!question.trim() || busy || !status || liveUnavailable || loadingSession || uploading) return;
    const submittedQuestion = question.trim();
    const docIds = selectedDocuments.filter((id) => documents.some((document) => document.id === id && !document.synthetic));
    if (!docIds.length) { setActionError('请先上传并选择用于分析的资料。'); setPanel('sources'); return; }
    setSubmitting(true); setActionError('');
    try {
      let sessionId = selectedSession;
      if (!sessionId) {
        const session = await request<Session>('/api/sessions', { method: 'POST', body: JSON.stringify({ title: submittedQuestion.slice(0, 30) }) });
        sessionId = session.id; createdSessionRef.current = session.id;
        setSelectedSession(session.id); setSessions((previous) => [session, ...previous]);
      }
      await request<Session>(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: 'PATCH', body: JSON.stringify({ document_ids: docIds }) });
      const run = await request<Run>(`/api/sessions/${encodeURIComponent(sessionId)}/runs`, { method: 'POST', body: JSON.stringify({ question: submittedQuestion, mode: 'live', document_ids: docIds, learn_on_failure: learnOnFailure }) });
      setRuns((previous) => [...previous, run]); setSelectedDocuments(docIds); setQuestion('');
    } catch (error) { setActionError(errorMessage(error)); }
    finally { setSubmitting(false); }
  };

  const cancel = async () => {
    if (!activeRunId) return;
    setCancelPendingId(activeRunId); setActionError('');
    try { await request(`/api/runs/${encodeURIComponent(activeRunId)}/cancel`, { method: 'POST' }); }
    catch (error) { setActionError(errorMessage(error)); setCancelPendingId(null); }
  };
  const upload = async (file: File | undefined) => {
    if (!file) return;
    setUploading(true); setActionError('');
    const form = new FormData(); form.append('file', file);
    try {
      const document = await request<DocumentRecord>('/api/documents', { method: 'POST', body: form });
      setDocuments((previous) => [document, ...previous.filter((item) => item.id !== document.id)]);
      setSelectedDocuments((previous) => [...previous.filter((id) => !documents.find((item) => item.id === id)?.synthetic), document.id]);
      setPanel('sources'); setPreviewDocument(document.id);
    } catch (error) { setActionError(errorMessage(error)); }
    finally { setUploading(false); if (uploadRef.current) uploadRef.current.value = ''; }
  };
  const chooseSuggestion = (suggestion: typeof suggestions[number]) => {
    setQuestion(suggestion.question); setActionError(''); questionRef.current?.focus();
    setPanel('sources');
  };
  const keySubmit = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); void submit(); }
  };
  const toggleDocument = (document: DocumentRecord) => {
    if (busy) return;
    setSelectedDocuments((previous) => previous.includes(document.id) ? previous.filter((id) => id !== document.id) : [...previous, document.id]);
  };
  const openRunActivity = (run: Run) => { setPanel('activity'); if (run.id !== latestRun?.id) setActionError('过程面板显示当前会话的全部执行记录。'); };
  const closeSettings = useCallback(() => setSettingsOpen(false), []);
  const closeArtifact = useCallback(() => setArtifactPreview(null), []);
  const closeSource = useCallback(() => setSourcePreview(null), []);
  const closeRename = useCallback(() => setRenaming(false), []);
  const rename = async (event: FormEvent) => {
    event.preventDefault(); if (!selectedSession || !renameTitle.trim()) return;
    try {
      const updated = await request<Session>(`/api/sessions/${encodeURIComponent(selectedSession)}`, { method: 'PATCH', body: JSON.stringify({ title: renameTitle.trim() }) });
      setSessions((previous) => previous.map((item) => item.id === updated.id ? { ...item, ...updated } : item)); setRenaming(false);
    } catch (error) { setActionError(errorMessage(error)); }
  };

  return <div className={`app-shell ${sidebarOpen ? 'sidebar-visible' : ''} ${panel ? 'has-panel' : ''}`}>
    {sidebarOpen ? <button className="sidebar-scrim" aria-label="关闭侧栏" onClick={() => setSidebarOpen(false)} /> : null}
    <aside className="sidebar" aria-label="主导航">
      <div className="brand-row"><button className="brand" onClick={newAnalysis}><WaveMark /><span>Fin Agent</span></button><IconButton label="收起侧栏" className="mobile-only" onClick={() => setSidebarOpen(false)}><PanelLeftClose size={17} /></IconButton><span className="brand-version desktop-only">工作台</span></div>
      <button className="new-analysis" onClick={newAnalysis} disabled={submitting || uploading}><Plus size={17} /><span>新分析</span><kbd>{navigator.platform.includes('Mac') ? '⌘' : 'Ctrl'} N</kbd></button>
      <nav className="primary-nav"><button className={panel === 'sources' && workspace === 'chat' ? 'nav-active' : ''} onClick={() => { setWorkspace('chat'); setPanel(panel === 'sources' ? null : 'sources'); setSidebarOpen(false); }}><FolderOpen size={17} />资料库<span className="nav-count">{documents.length || ''}</span></button><button className={workspace === 'skills' ? 'nav-active' : ''} onClick={() => { setWorkspace('skills'); setPanel(null); setSidebarOpen(false); }}><BookOpen size={17} />Skills<span className="nav-count">能力库</span></button></nav>
      <div className="session-section"><div className="section-label"><span>最近分析</span><Clock3 size={12} /></div><label className="session-search"><Search size={14} /><input id="session-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索会话" aria-label="搜索分析会话" /><kbd>/</kbd></label>
        <div className="session-list">{visibleSessions.length ? visibleSessions.map((session) => <button key={session.id} disabled={submitting || uploading} className={`session-item ${selectedSession === session.id && workspace === 'chat' ? 'selected' : ''}`} onClick={() => { setSelectedSession(session.id); setWorkspace('chat'); setSidebarOpen(false); setActionError(''); }}><span className="session-indicator" /><span>{session.title}</span></button>) : <p className="session-empty">{search ? '没有匹配的会话' : '你的研究，从这里开始。'}</p>}</div>
      </div>
      <div className="sidebar-bottom"><button className="settings-button" onClick={() => setSettingsOpen(true)}><Settings2 size={17} /><span>设置</span></button><div className="local-service"><span className={`status-dot ${status && !connectionError ? 'online' : ''}`} /><span>{connecting ? '正在连接' : status && !connectionError ? '本地服务已连接' : '本地服务未连接'}</span><span className="local-label">Local</span></div></div>
    </aside>
    <main className="workspace">
      <header className="workspace-header"><div className="header-title"><IconButton label="打开侧栏" className="mobile-only" onClick={() => setSidebarOpen(true)}><Menu size={19} /></IconButton>{workspace === 'skills' ? <span>Skills</span> : currentSession ? <button className="conversation-title" title="重命名分析" onClick={() => { setRenameTitle(currentSession.title); setRenaming(true); }}>{currentSession.title}<ChevronDown size={14} /></button> : <span className="new-title">新分析</span>}{workspace === 'chat' ? <span className="header-divider" /> : null}{workspace === 'chat' ? <span className="header-subtitle">金融研究</span> : <span className="header-subtitle">分析能力与经验</span>}</div><div className="header-actions"><button className="model-status" onClick={() => setSettingsOpen(true)}><span className={`status-dot ${status?.api_ready ? 'online' : ''}`} />{status?.model || 'DeepSeek'}<ChevronDown size={12} /></button>{workspace === 'chat' ? <IconButton label={panel ? '关闭资料面板' : '打开资料面板'} className={panel ? 'is-selected' : ''} onClick={() => setPanel(panel ? null : 'sources')}><PanelRight size={17} /></IconButton> : null}</div></header>
      {connectionError ? <div className="connection-banner" role="alert"><TriangleAlert size={16} /><span>{connectionError}</span><button onClick={() => setReload((value) => value + 1)} disabled={connecting}>{connecting ? '连接中…' : '重试连接'}</button></div> : null}
      {workspace === 'skills' ? <div className="skills-workspace"><div className="skills-heading"><BookOpen size={26} /><h1>把方法，沉淀成能力。</h1><p>按需加载分析方法。失败经验先形成候选，经过独立验证后才会启用。</p></div>{skillError ? <div className="inline-error" role="alert"><TriangleAlert size={16} />{skillError}</div> : null}{skillsLoading ? <div className="loading-note"><Loader2 className="spin" size={18} />正在读取能力库</div> : <div className="skill-layout"><div className="skill-catalog">{skills.map((skill) => <button className={`skill-card ${selectedSkill === skill.name ? 'selected' : ''}`} key={skill.name} onClick={() => setSelectedSkill(skill.name)}><div><BookOpen size={16} /><span>{SKILL_LABELS[skill.name] ?? skill.name}</span><small>{skill.origin === 'builtin' ? '内置' : '已学习'}</small></div><p>{SKILL_DESCRIPTIONS[skill.name] ?? skill.description}</p><span className="skill-id">{skill.name}</span></button>)}</div><div className="skill-detail">{skillDetail ? <><div className="skill-detail-header"><h2>{SKILL_LABELS[skillDetail.name] ?? skillDetail.name}</h2><span className="tiny-badge">v{skillDetail.version}</span></div><p className="muted">{SKILL_DESCRIPTIONS[skillDetail.name] ?? skillDetail.description}</p>{skillDetail.triggers.length ? <div className="trigger-list">{skillDetail.triggers.map((trigger) => <span key={trigger}>{trigger}</span>)}</div> : null}<Markdown>{skillDetail.body ?? ''}</Markdown></> : <p className="panel-empty">选择一项能力，查看完整方法。</p>}</div></div>}<div className="learning-section"><div><h2>经验学习</h2><p>候选能力保持隔离。验证至少覆盖 3 组独立资料，并要求有提升、无回退。</p></div><div className="learning-counts"><span><strong>{learning?.experiences.length ?? '—'}</strong>失败经验</span><span><strong>{learning?.candidates.length ?? '—'}</strong>候选 Skill</span><span><strong>{learning?.reports.length ?? '—'}</strong>验证记录</span></div>{learning?.candidates.length ? <details><summary>查看候选记录</summary><pre>{readable(learning.candidates)}</pre></details> : <p className="learning-empty">暂未发现候选记录。失败经验形成候选后，需经过独立验证才能启用。</p>}</div></div> : <div className="chat-layout"><div className="chat-main"><div className={`chat-scroll ${!runs.length ? 'is-empty' : ''}`}>
        {loadingSession ? <div className="loading-note"><Loader2 className="spin" size={18} />正在读取分析会话</div> : !runs.length ? <div className="empty-state"><WaveMark large /><h1>从一份财报，开始研究。</h1><p>读资料，做计算，让结论有据可查。</p><div className="suggestion-grid">{suggestions.map((suggestion) => <button key={suggestion.title} className="suggestion" onClick={() => chooseSuggestion(suggestion)}><suggestion.icon size={19} /><span><strong>{suggestion.title}</strong><small>{suggestion.description}</small></span><ArrowUpRight size={14} /></button>)}</div></div> : <div className="conversation-content">{runs.map((run) => <RunMessage key={run.id} run={run} onPreview={setArtifactPreview} onActivity={() => openRunActivity(run)} onSource={setSourcePreview} />)}<div ref={endRef} /></div>}
        </div><div className="composer-area">{actionError ? <div className="composer-error" role="alert"><TriangleAlert size={15} /><span>{actionError}</span><IconButton label="关闭提示" onClick={() => setActionError('')}><X size={14} /></IconButton></div> : null}<form className="composer" onSubmit={(event) => { void submit(event); }}>
          {selectedSourceRecords.length ? <div className="selected-sources">{selectedSourceRecords.map((document) => <span className="source-chip" key={document.id}><FileText size={12} /><button type="button" onClick={() => { setPreviewDocument(document.id); setPanel('sources'); }}>{document.name}</button><button type="button" className="remove-source" aria-label={`移除 ${document.name}`} disabled={busy} onClick={() => setSelectedDocuments((previous) => previous.filter((id) => id !== document.id))}><X size={11} /></button></span>)}</div> : null}
          <textarea ref={questionRef} value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={keySubmit} rows={2} aria-label="输入分析问题" placeholder="上传 CSV、财报或其他资料，描述你想研究的问题…" disabled={submitting} />
          <div className="composer-controls"><div className="composer-left"><IconButton label="上传分析资料" onClick={() => uploadRef.current?.click()} disabled={uploading || busy || !status}>{uploading ? <Loader2 size={17} className="spin" /> : <Paperclip size={18} />}</IconButton><input ref={uploadRef} className="visually-hidden" type="file" accept=".pdf,.csv,.xlsx,.txt,.md,.json" onChange={(event) => { void upload(event.target.files?.[0]); }} /><span className="control-divider" /><span className="analysis-mode">真实分析</span><label className="learn-option" title="失败时额外调用模型提炼候选，计入共享预算；独立验证后才可启用。"><input type="checkbox" checked={learnOnFailure} onChange={(event) => setLearnOnFailure(event.target.checked)} disabled={busy} />提炼经验</label></div>{busy ? <button className="send-button stop-button" type="button" title="停止分析" aria-label={cancelPending ? '正在停止分析' : '停止分析'} onClick={() => { void cancel(); }} disabled={!activeRunId || cancelPending}>{cancelPending || submitting ? <Loader2 size={17} className="spin" /> : <Square size={13} fill="currentColor" />}</button> : <button className="send-button" type="submit" title="开始分析（Enter）" aria-label="开始分析" disabled={!question.trim() || !status || liveUnavailable || loadingSession || uploading}><ArrowUp size={19} /></button>}</div>
        </form><div className={`composer-footnote ${liveUnavailable ? 'requires-setup' : ''}`}>{liveUnavailable ? <><TriangleAlert size={12} /><span>{!status?.api_ready ? '服务端尚未配置模型 API Key。' : !status.budget.initialized ? '请先初始化共享预算。' : '共享预算已冻结，暂时无法调用模型。'}</span><button onClick={() => setSettingsOpen(true)}>查看设置</button></> : <><span>真实分析使用已配置模型与共享预算。请核验重要结论。</span><span className="keyboard-hint">Shift Enter 换行</span></>}</div>
      </div></div>
      {panel ? <aside className="context-panel" aria-label="分析上下文"><div className="panel-tabs"><div>{(['sources', 'reports', 'activity'] as const).map((tab) => <button key={tab} className={panel === tab ? 'active' : ''} onClick={() => setPanel(tab)}>{tab === 'sources' ? '资料' : tab === 'reports' ? '产物' : '过程'}</button>)}</div><IconButton label="关闭上下文面板" onClick={() => setPanel(null)}><X size={16} /></IconButton></div><div className="panel-content">
        {panel === 'sources' ? <><div className="panel-section-title"><span>分析资料</span><IconButton label="添加资料" onClick={() => uploadRef.current?.click()} disabled={uploading || busy || !status}><Plus size={16} /></IconButton></div><p className="panel-help">选择本次分析使用的资料。</p>{documents.map((document) => <div className={`document-item ${previewDocument === document.id ? 'previewing' : ''}`} key={document.id}><input type="checkbox" checked={selectedDocuments.includes(document.id)} onChange={() => toggleDocument(document)} disabled={busy} aria-label={`选择 ${document.name}`} /><button onClick={() => setPreviewDocument(document.id)}><FileText size={17} /><span><strong>{document.name}</strong><small>{document.kind.toUpperCase()} · {bytes(document.bytes)}</small></span></button></div>)}{!documents.length ? <p className="panel-empty">上传 PDF、表格或文字资料，开始分析。</p> : null}{previewDocument ? <div className="document-preview"><div className="panel-section-title"><span>资料预览</span><IconButton label="关闭资料预览" onClick={() => setPreviewDocument(null)}><X size={14} /></IconButton></div>{previewError ? <p className="error-text">{previewError}</p> : !documentDetail ? <p className="panel-empty">正在读取…</p> : <><h3>{documentDetail.name}</h3>{documentDetail.synthetic ? <p className="synthetic-note">仅供功能演示，非真实公司财报。</p> : null}<p className="preview-text">{Array.isArray(documentDetail.preview.pre_text) ? documentDetail.preview.pre_text.join('\n') : documentDetail.preview.pre_text}</p>{documentDetail.preview.table?.length ? <div className="preview-table"><table><tbody>{documentDetail.preview.table.slice(0, 10).map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => index === 0 ? <th key={cellIndex}>{readable(cell)}</th> : <td key={cellIndex}>{readable(cell)}</td>)}</tr>)}</tbody></table></div> : null}<p className="preview-text">{Array.isArray(documentDetail.preview.post_text) ? documentDetail.preview.post_text.join('\n') : documentDetail.preview.post_text}</p></>}</div> : null}</> : panel === 'reports' ? <><div className="panel-section-title"><span>报告与图表</span><span className="count-tag">{allArtifacts.length}</span></div><ArtifactList artifacts={allArtifacts} onPreview={setArtifactPreview} /><p className="panel-help artifact-help">点击预览，或下载文件。产物仅属于当前会话。</p></> : <><div className="panel-section-title"><span>执行记录</span><span className="count-tag">{runs.reduce((sum, run) => sum + run.events.length, 0)}</span></div>{runs.length ? runs.map((run) => <div className="run-activity-group" key={run.id}><p>{run.question}</p><ActivityList events={run.events} compact /></div>) : <ActivityList events={[]} />}</>}
      </div><div className="panel-bottom"><ShieldCheck size={13} /><span>资料与会话保存在本机</span></div></aside> : null}</div>}
    </main>
    {settingsOpen ? <Modal title="工作台设置" onClose={closeSettings}><div className="settings-intro"><WaveMark /><div><strong>Fin Agent</strong><p>围绕证据与计算的金融研究工作台</p></div></div><div className="settings-group"><h3>运行环境</h3><div className="setting-row"><span>本地服务</span><span className={status && !connectionError ? 'good-text' : 'muted'}>{status && !connectionError ? '已连接' : '未连接'}</span></div><div className="setting-row"><span>模型</span><strong>{status?.model ?? '未读取'}</strong></div><div className="setting-row"><span>API Key</span><span className={status?.api_ready ? 'good-text' : 'muted'}>{status?.api_ready ? '服务端已配置' : '服务端未配置'}</span></div><p className="settings-note">页面不读取或保存密钥。「已配置」表示服务端检测到配置，不代表已验证 API 连通性。</p><div className="setting-row"><span>Docker 引擎</span><span>{status?.docker.available ? '可用' : '不可用'}</span></div><div className="setting-row"><span>终端镜像</span><span>{status?.docker.image_ready ? '已就绪' : '未就绪'}</span></div><p className="settings-note">结构化计算工具可独立运行。涉及终端执行的任务需要 Docker 和终端镜像。</p></div><div className="settings-group"><h3>共享调用预算</h3><div className="budget-summary"><div><small>已使用</small><strong>{money(status?.budget.spent_cny)}</strong></div><div><small>剩余</small><strong>{money(status?.budget.remaining_cny)}</strong></div><div><small>上限</small><strong>{money(status?.budget.ceiling_cny)}</strong></div></div>{status?.budget.frozen ? <p className="error-text">预算已冻结，请在服务端处理未结算调用后继续。</p> : null}<p className="settings-note">金额来自服务端共享账本。分析调用需在服务端配置 DEEPSEEK_API_KEY 并初始化预算。</p></div><div className="setting-row appearance-setting"><span>外观</span><div className="segmented-control"><button className={theme === 'light' ? 'active' : ''} onClick={() => setTheme('light')}><Sun size={14} />浅色</button><button className={theme === 'dark' ? 'active' : ''} onClick={() => setTheme('dark')}><Moon size={14} />深色</button></div></div><button className="secondary-button full-width" onClick={() => setReload((value) => value + 1)}><Loader2 size={14} className={connecting ? 'spin' : ''} />刷新运行状态</button><a className="repo-link" href="https://github.com/William-Li123/fin_agent" target="_blank" rel="noreferrer noopener"><CircleHelp size={14} />项目文档<ArrowUpRight size={12} /></a></Modal> : null}
    {artifactPreview ? <Modal title={artifactPreview.name} onClose={closeArtifact} wide>{artifactError ? <p className="error-text">{artifactError}</p> : artifactPreview.name.toLowerCase().endsWith('.svg') || artifactPreview.kind === 'svg' ? <img className="chart-preview" src={safeArtifactUrl(artifactPreview.url)} alt={artifactPreview.name} onError={() => setArtifactError('图表读取失败，请检查文件是否仍存在。')} /> : artifactText ? <Markdown>{artifactText}</Markdown> : <p className="loading-note">正在读取文件…</p>}{safeArtifactUrl(artifactPreview.url) ? <a className="secondary-button artifact-download" href={safeArtifactUrl(artifactPreview.url)} download={artifactPreview.name}><Download size={14} />下载文件</a> : null}</Modal> : null}
    {sourcePreview ? <Modal title="原始数据证据" onClose={closeSource}><div className="source-evidence-details"><p className="source-evidence-doc"><FileText size={16} />{documents.find((document) => document.id === sourcePreview.document_id)?.name ?? sourcePreview.document_id}</p><dl><div><dt>位置</dt><dd>{sourcePreview.locator}</dd></div>{sourcePreview.row_label ? <div><dt>指标</dt><dd>{sourcePreview.row_label}</dd></div> : null}{sourcePreview.column_header ? <div><dt>列</dt><dd>{sourcePreview.column_header}</dd></div> : null}<div><dt>证据编号</dt><dd>{sourcePreview.evidence_id}</dd></div></dl><blockquote>{sourcePreview.text || sourcePreview.body}</blockquote><p className="settings-note">这是工具读取的原始证据。仍需结合年份、单位和口径判断是否适用于当前问题。</p></div></Modal> : null}
    {renaming ? <Modal title="重命名分析" onClose={closeRename}><form className="rename-form" onSubmit={(event) => { void rename(event); }}><label htmlFor="rename-title">分析名称</label><input id="rename-title" maxLength={120} value={renameTitle} onChange={(event) => setRenameTitle(event.target.value)} /><button className="primary-button" disabled={!renameTitle.trim()} type="submit">保存名称</button></form></Modal> : null}
  </div>;
}
