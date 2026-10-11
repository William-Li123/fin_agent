import { expect, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const realCsv = resolve(process.cwd(), '../examples/real-data/apple_fy2024_fy2025.csv');
const writeHeaders = { 'X-Finagent-Client': 'web' };

test('new research starts without synthetic data and previews an actual financial CSV', async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await expect(page.getByText('本地服务已连接', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: /新分析/ }).first().click();
  await expect(page.getByRole('heading', { name: '从一份财报，开始研究。' })).toBeVisible();
  await expect(page.locator('.source-chip')).toHaveCount(0);
  await expect(page.getByText('演示公司 · 两期财务数据.json', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('combobox', { name: '运行模式' })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('desktop-empty.png') });

  await page.getByRole('button', { name: /看懂营收增长/ }).click();
  await expect(page.getByRole('textbox', { name: '输入分析问题' })).toHaveValue(/根据我上传的资料/);
  await page.locator('input[type=file]').setInputFiles(realCsv);
  await expect(page.locator('.document-preview')).toContainText('apple_fy2024_fy2025.csv');
  await expect(page.locator('.preview-table')).toContainText('416161');
  await expect(page.locator('.preview-table')).toContainText('391035');
  await expect(page.locator('.preview-table')).toContainText('https://');
  await expect(page.locator('.preview-table')).not.toContainText('http[local path]');
  await expect(page.locator('.source-chip')).toHaveCount(1);
  await expect(page.getByRole('button', { name: '开始分析', exact: true })).toBeDisabled();
  await expect(page.locator('.composer-footnote')).toContainText('服务端尚未配置模型 API Key');
  await page.screenshot({ path: testInfo.outputPath('desktop-real-csv.png') });
  await page.getByRole('button', { name: '关闭上下文面板' }).click();
  await page.getByRole('button', { name: /移除 apple_fy2024_fy2025.csv/ }).click();
  await expect(page.locator('.source-chip')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('real document selection and session title survive a refresh', async ({ page, request }) => {
  const documents = await (await request.get('/api/documents')).json();
  expect(documents.every((item: { synthetic: boolean }) => !item.synthetic)).toBe(true);
  const uploaded = await request.post('/api/documents', {
    headers: writeHeaders,
    multipart: { file: { name: 'apple_fy2024_fy2025.csv', mimeType: 'text/csv', buffer: await readFile(realCsv) } },
  });
  expect(uploaded.status()).toBe(201);
  const source = await uploaded.json();
  const created = await request.post('/api/sessions', { headers: writeHeaders, data: { title: 'Apple 真实财报研究' } });
  expect(created.status()).toBe(201);
  const session = await created.json();
  expect(session.document_ids).toEqual([]);
  const selected = await request.patch(`/api/sessions/${session.id}`, { headers: writeHeaders, data: { document_ids: [source.id] } });
  expect(selected.ok()).toBe(true);
  await page.addInitScript((id) => localStorage.setItem('finagent.ui.v1', JSON.stringify({ session: id, theme: 'light' })), session.id);
  await page.goto('/');
  await expect(page.locator('.source-chip')).toContainText('apple_fy2024_fy2025.csv');
  await expect(page.locator('.conversation-title')).toContainText('Apple 真实财报研究');
  await page.reload();
  await expect(page.locator('.source-chip')).toContainText('apple_fy2024_fy2025.csv');
  await expect(page.locator('.conversation-title')).toContainText('Apple 真实财报研究');
});

test('skills and quarantined learning remain available', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('本地服务已连接', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: /Skills/ }).click();
  await expect(page.locator('.skill-card')).toHaveCount(6);
  await page.getByRole('button', { name: /财务比率 内置/ }).click();
  await expect(page.locator('.skill-detail')).toContainText('financial');
  await expect(page.getByRole('heading', { name: '经验学习', exact: true })).toBeVisible();
});

test('mobile workspace and settings stay accessible without horizontal overflow', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await expect(page.getByText('本地服务已连接', { exact: true })).toBeAttached();
  await page.getByRole('button', { name: '打开侧栏' }).click();
  await page.getByRole('button', { name: /新分析/ }).first().click();
  await expect(page.getByRole('heading', { name: '从一份财报，开始研究。' })).toBeVisible();
  await expect(page.locator('.source-chip')).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('mobile-empty.png') });
  await page.getByRole('button', { name: '打开侧栏' }).click();
  await page.getByRole('button', { name: '设置', exact: true }).click();
  const settings = page.getByRole('dialog', { name: '工作台设置' });
  await expect(settings).toContainText('共享调用预算');
  await settings.getByRole('button', { name: '深色', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.screenshot({ path: testInfo.outputPath('mobile-dark-settings.png') });
  await settings.getByRole('button', { name: '浅色', exact: true }).click();
  await page.keyboard.press('Escape');
  await expect(settings).toHaveCount(0);
});


test('upload in progress blocks Enter and session changes', async ({ page }) => {
  const timestamp = '2026-10-11T00:00:00Z';
  const existingDocument = { id: 'existing-document', name: 'existing.csv', kind: 'csv', bytes: 24, created_at: timestamp, synthetic: false, document_count: 1 };
  const uploadedDocument = { ...existingDocument, id: 'uploaded-document', name: 'uploaded.csv' };
  const currentSession = { id: 'upload-session-a', title: '上传前会话', created_at: timestamp, updated_at: timestamp, document_ids: [existingDocument.id], runs: [] };
  const otherSession = { ...currentSession, id: 'upload-session-b', title: '另一个会话', document_ids: [] };
  let releaseUpload!: () => void;
  const uploadReady = new Promise<void>((resolveUpload) => { releaseUpload = resolveUpload; });
  let uploadStarted = false;
  const mutations: string[] = [];

  // Every API call is mocked, including unexpected mutations: this regression
  // cannot submit a real analysis or touch the local service's state/budget.
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (method === 'POST' && path === '/api/documents') {
      uploadStarted = true;
      await uploadReady;
      await route.fulfill({ status: 201, json: uploadedDocument });
      return;
    }
    if (method !== 'GET') {
      mutations.push(`${method} ${path}`);
      await route.fulfill({ status: 409, json: { detail: 'Unexpected request during upload' } });
      return;
    }
    const values: Record<string, unknown> = {
      '/api/status': { model: 'test-model', api_ready: true, budget: { initialized: true, ceiling_cny: 10, spent_cny: 0, remaining_cny: 10, frozen: false }, docker: { available: false, image_ready: false }, document_formats: ['csv'] },
      '/api/documents': [existingDocument],
      '/api/sessions': [currentSession, otherSession],
      [`/api/sessions/${currentSession.id}`]: currentSession,
      [`/api/sessions/${otherSession.id}`]: otherSession,
      [`/api/documents/${uploadedDocument.id}`]: { ...uploadedDocument, preview: { pre_text: [], table: [['metric', 'value'], ['revenue', '100']] } },
    };
    await route.fulfill({ status: path in values ? 200 : 404, json: values[path] ?? { detail: 'Unknown fixture endpoint' } });
  });
  await page.addInitScript((id) => localStorage.setItem('finagent.ui.v1', JSON.stringify({ session: id, theme: 'light' })), currentSession.id);
  await page.goto('/');
  await expect(page.locator('.conversation-title')).toContainText(currentSession.title);
  await expect(page.locator('.source-chip')).toContainText(existingDocument.name);
  await page.getByRole('textbox', { name: '输入分析问题' }).fill('根据两份资料比较收入');
  await expect(page.getByRole('button', { name: '开始分析', exact: true })).toBeEnabled();

  await page.locator('input[type=file]').setInputFiles({ name: uploadedDocument.name, mimeType: 'text/csv', buffer: Buffer.from('metric,value\nrevenue,100\n') });
  await expect.poll(() => uploadStarted).toBe(true);
  await expect(page.getByRole('button', { name: '开始分析', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: /新分析/ }).first()).toBeDisabled();
  await expect(page.locator('.session-list').getByRole('button', { name: otherSession.title, exact: true })).toBeDisabled();
  await page.getByRole('textbox', { name: '输入分析问题' }).press('Enter');
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+n' : 'Control+n');
  await page.locator('.brand').click();
  await expect(page.locator('.conversation-title')).toContainText(currentSession.title);
  await expect(page.locator('.source-chip')).toHaveCount(1);

  releaseUpload();
  await expect(page.locator('.source-chip')).toHaveCount(2);
  await expect(page.locator('.source-chip').filter({ hasText: existingDocument.name })).toHaveCount(1);
  await expect(page.locator('.source-chip').filter({ hasText: uploadedDocument.name })).toHaveCount(1);
  await expect(page.locator('.conversation-title')).toContainText(currentSession.title);
  await expect(page.getByRole('button', { name: '开始分析', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: /新分析/ }).first()).toBeEnabled();
  await expect(page.locator('.session-list').getByRole('button', { name: otherSession.title, exact: true })).toBeEnabled();
  expect(mutations).toEqual([]);
});
