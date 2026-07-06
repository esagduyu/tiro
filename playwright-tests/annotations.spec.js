// annotations.spec.js — M2.2 Task 2 (reader selection -> highlight -> paint)
// end-to-end check, mirroring phase0.spec.js's bootstrap (first-run setup or
// login, save an article via the "+" modal, open the reader).
//
// Run against a SCRATCH Tiro server only (never the real library) — see
// .superpowers/sdd/task-2-report.md (M2.2) for the exact invocation used
// during development:
//
//   TIRO_CONFIG=/path/to/scratch/config.yaml \
//   TIRO_LIBRARY_PATH=/path/to/scratch/library \
//   uv run python run.py &
//   cd playwright-tests && TIRO_PASSWORD=<pw> npx playwright test annotations.spec.js
//
// Configuration (env vars, both optional, same defaults as phase0.spec.js):
//   TIRO_URL      base URL of the running server (default http://localhost:8000)
//   TIRO_PASSWORD password to use for login / first-run setup

const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TIRO_URL || 'http://localhost:8000';
const PASSWORD = process.env.TIRO_PASSWORD || 'annotations-spec-test-pass';
const TEST_URL = 'https://example.com';
const TEST_TITLE = 'Example Domain';

async function loginOrSetup(page) {
  await page.goto('/login');

  const setupHeading = page.getByText('Set a password to protect your library');
  const isFirstRun = await setupHeading.isVisible().catch(() => false);

  if (isFirstRun) {
    await page.getByRole('textbox', { name: 'Password', exact: true }).fill(PASSWORD);
    await page.getByRole('textbox', { name: 'Confirm password' }).fill(PASSWORD);
    await page.getByRole('button', { name: 'Create password' }).click();
  } else {
    await page.getByRole('textbox', { name: 'Password' }).fill(PASSWORD);
    await page.getByRole('button', { name: 'Sign in' }).click();
  }

  await expect(page).toHaveURL(/\/inbox/);
}

async function saveAndOpenTestArticle(page) {
  // If a prior run left the article in place, open it directly rather than
  // re-ingesting (POST /api/ingest/url 409s on a duplicate URL). Give the
  // inbox's initial GET /api/articles a moment to settle first so this
  // check isn't racing an empty, not-yet-rendered list.
  await page.waitForLoadState('networkidle').catch(() => {});
  const existingLink = page.getByRole('link', { name: TEST_TITLE }).first();
  if (await existingLink.isVisible({ timeout: 3000 }).catch(() => false)) {
    await existingLink.click();
  } else {
    await page.locator('#save-btn').click();
    const urlInput = page.getByRole('textbox', { name: 'Paste a URL...' });
    await expect(urlInput).toBeVisible();
    await urlInput.fill(TEST_URL);
    await urlInput.press('Enter');
    await expect(page.getByText(`Saved: ${TEST_TITLE}`)).toBeVisible({ timeout: 30000 });
    await page.locator('#save-modal-close').click();
    const articleLink = page.getByRole('link', { name: TEST_TITLE }).first();
    await expect(articleLink).toBeVisible();
    await articleLink.click();
  }

  await expect(page).toHaveURL(/\/articles\/\d+/);
  await expect(page.getByRole('heading', { name: TEST_TITLE }).first()).toBeVisible();
  // Reader body renders async (fetch + renderMarkdown) — wait for at least
  // one paragraph before driving a selection over it.
  await page.waitForSelector('#reader-body p', { timeout: 10000 });
}

test.describe('M2.2 reader annotation UI', () => {
  test.use({ baseURL: BASE_URL });

  test('selection -> toolbar -> highlight -> paint -> survives reload', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    page.on('pageerror', (err) => consoleErrors.push(String(err)));

    await loginOrSetup(page);
    await saveAndOpenTestArticle(page);

    // --- Negative case: collapsed selection shows no toolbar ---
    await page.evaluate(() => {
      const p = document.querySelector('#reader-body p');
      const range = document.createRange();
      range.selectNodeContents(p);
      range.collapse(true);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      document.getElementById('reader-body').dispatchEvent(
        new MouseEvent('mouseup', { bubbles: true })
      );
    });
    await expect(page.locator('#annotate-toolbar.open')).toHaveCount(0);

    // --- Positive case: select the whole first paragraph, dispatch mouseup ---
    await page.evaluate(() => {
      const p = document.querySelector('#reader-body p');
      const range = document.createRange();
      range.selectNodeContents(p);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      document.getElementById('reader-body').dispatchEvent(
        new MouseEvent('mouseup', { bubbles: true })
      );
    });

    const toolbar = page.locator('#annotate-toolbar.open');
    await expect(toolbar).toHaveCount(1, { timeout: 5000 });

    const yellowBtn = page.locator('.annotate-color-btn[data-color="yellow"]');
    await expect(yellowBtn).toBeVisible();

    const [response] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes('/highlights') && res.request().method() === 'POST'
      ),
      yellowBtn.click(),
    ]);
    expect(response.status()).toBe(200);
    const created = await response.json();
    expect(created.success).toBe(true);
    expect(created.data.color).toBe('yellow');

    // Toolbar hides after a successful create.
    await expect(page.locator('#annotate-toolbar.open')).toHaveCount(0);

    // Painted via CSS Custom Highlight API right after creation (no reload).
    const paintedSize = await page.evaluate(
      () => (typeof CSS !== 'undefined' && CSS.highlights && CSS.highlights.get('tiro-hl-yellow')
        ? CSS.highlights.get('tiro-hl-yellow').size
        : 0)
    );
    expect(paintedSize).toBeGreaterThanOrEqual(1);

    // --- Reload: highlight is fetched from GET /annotations and repainted ---
    await page.reload();
    await page.waitForSelector('#reader-body p', { timeout: 10000 });
    // Give the async GET /api/articles/{id}/annotations fetch a moment to
    // resolve and paint before asserting.
    await page.waitForFunction(
      () => {
        const hl = typeof CSS !== 'undefined' && CSS.highlights && CSS.highlights.get('tiro-hl-yellow');
        return !!hl && hl.size >= 1;
      },
      { timeout: 10000 }
    );
    const paintedAfterReload = await page.evaluate(
      () => CSS.highlights.get('tiro-hl-yellow').size
    );
    expect(paintedAfterReload).toBeGreaterThanOrEqual(1);

    // --- Manual console sweep: zero JS errors across the whole flow ---
    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });
});
