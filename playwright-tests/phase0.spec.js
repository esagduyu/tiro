// phase0.spec.js — Phase 0 (Security & Integrity) end-to-end smoke test.
//
// Covers the core loop that Phase 0's auth + delete work must not break:
//   log in (or complete first-run setup) -> save an article -> open it in the
//   reader -> delete it -> confirm it's gone from the inbox.
//
// Run against a live Tiro server:
//   cd playwright-tests && npm install && npx playwright test
//
// Configuration (env vars, both optional):
//   TIRO_URL      base URL of the running server (default http://localhost:8000)
//   TIRO_PASSWORD password to use for login / first-run setup
//                 (default "phase0-spec-test-pass")
//
// The spec handles both states a fresh library can be in when it starts:
//   - First run (no password configured yet): /login shows the setup variant
//     ("Set a password to protect your library"). The spec creates the
//     password and is then logged in automatically.
//   - Already configured: /login shows the normal sign-in variant. The spec
//     logs in with TIRO_PASSWORD.
//
// This is a hand-run smoke test for the 0.2 release pass, not wired into CI
// yet (see docs/plans for the Phase 0 M7 verification report).

const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TIRO_URL || 'http://localhost:8000';
const PASSWORD = process.env.TIRO_PASSWORD || 'phase0-spec-test-pass';
const TEST_URL = 'https://example.com';
const TEST_TITLE = 'Example Domain';

test.describe('Phase 0 core loop', () => {
  test.use({ baseURL: BASE_URL });

  test('login (or first-run setup) -> save -> open -> delete -> confirm gone', async ({ page }) => {
    // --- Auth: handle first-run setup or plain login ---
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

    // --- Save an article via the "+" Save modal ---
    // Use the #sidebar-save-btn id rather than a role/name lookup: the inbox
    // rating buttons ("Like"/"Dislike") also render literal "+"/"−" text, which
    // makes name-based lookups ambiguous once the library has articles.
    await page.locator('#sidebar-save-btn').click();
    const urlInput = page.getByRole('textbox', { name: 'Paste a URL...' });
    await expect(urlInput).toBeVisible();
    await urlInput.fill(TEST_URL);
    await urlInput.press('Enter');

    // Wait for the modal to report success (ingestion involves network fetch +
    // extraction, so give it a generous timeout).
    await expect(page.getByText(`Saved: ${TEST_TITLE}`)).toBeVisible({ timeout: 30000 });

    // Close the modal and confirm the article now appears in the inbox.
    await page.locator('#save-modal-close').click();
    const articleLink = page.getByRole('link', { name: TEST_TITLE }).first();
    await expect(articleLink).toBeVisible();

    // --- Open the article in the reader ---
    await articleLink.click();
    await expect(page).toHaveURL(/\/articles\/\d+/);
    await expect(page.getByRole('heading', { name: TEST_TITLE }).first()).toBeVisible();

    // --- Delete it, confirming the danger dialog ---
    await page.getByRole('button', { name: '🗑' }).click();
    await expect(page.getByRole('heading', { name: 'Delete article' })).toBeVisible();
    await page.getByRole('button', { name: 'Delete', exact: true }).click();

    // --- Confirm we're back in the inbox and the article is gone ---
    await expect(page).toHaveURL(/\/inbox/);
    await expect(page.getByRole('link', { name: TEST_TITLE })).toHaveCount(0);
  });
});
