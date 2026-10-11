import { defineConfig } from '@playwright/test';

// Tests use a separate, unfunded local state and an empty API key. Their server
// cannot issue paid model requests or modify the research budget ledger.
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:8011',
    viewport: { width: 1440, height: 960 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
      : {},
  },
  webServer: {
    command: 'python -m finagent.web --state ../.finagent/web-e2e-real --port 8011 --frontend dist',
    url: 'http://127.0.0.1:8011/api/status',
    reuseExistingServer: !process.env.CI,
    env: { DEEPSEEK_API_KEY: '' },
    timeout: 30_000,
  },
});
