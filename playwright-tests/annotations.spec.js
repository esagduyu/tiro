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

  // M2.2 Task 3 addition: the soft-fail case folded in from T2's review —
  // a selection whose text CANNOT be anchored must toast and post nothing.
  test('unanchorable selection shows a toast and posts nothing', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    page.on('pageerror', (err) => consoleErrors.push(String(err)));

    const highlightPosts = [];
    page.on('request', (req) => {
      if (req.url().includes('/highlights') && req.method() === 'POST') {
        highlightPosts.push(req.url());
      }
    });

    await loginOrSetup(page);
    await saveAndOpenTestArticle(page);

    // Mutate the rendered DOM so the paragraph's text no longer exists
    // anywhere in the article's underlying markdown. `annotationProjection`
    // was already built from the ORIGINAL fetched content at load time and
    // never re-reads the DOM, so a selection over this replaced text is
    // genuinely unanchorable (not just prefix/suffix noise) — this is the
    // real soft-fail path, not a contrived one.
    await page.evaluate(() => {
      const p = document.querySelector('#reader-body p');
      p.textContent = 'zzqxvville floobernaut unanchorable gibberish 837291';
    });

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
    await yellowBtn.click();

    // The toast is shown SYNCHRONOUSLY on the same call path that decides
    // not to POST (no `await` precedes it in createHighlightFromSelection's
    // failure branch) — waiting for it is a non-flaky synchronization point
    // rather than a fixed sleep: by the time it's visible, the decision not
    // to fetch has already been made and won't un-make itself later.
    await expect(page.locator('.settings-toast')).toContainText("Couldn't anchor", { timeout: 5000 });

    expect(highlightPosts).toEqual([]);
    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });

  // M2.2 Task 3: highlight note round-trip through the highlights panel.
  test('highlight note round-trip: add via panel, reload, note visible', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    page.on('pageerror', (err) => consoleErrors.push(String(err)));

    await loginOrSetup(page);
    await saveAndOpenTestArticle(page);

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

    const greenBtn = page.locator('.annotate-color-btn[data-color="green"]');
    const [createResponse] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes('/highlights') && res.request().method() === 'POST'
      ),
      greenBtn.click(),
    ]);
    const created = await createResponse.json();
    const uid = created.data.uid;

    // Open the highlights panel and add a note through it.
    await page.locator('#highlights-btn').click();
    await expect(page.locator('#highlights-panel.open')).toHaveCount(1);

    const row = page.locator(`.highlight-row[data-uid="${uid}"]`);
    await expect(row).toBeVisible();
    await row.locator('.highlight-note-btn').click();

    const noteText = 'A note added via the panel, round-tripped across reload.';
    const textarea = row.locator('.highlight-note-textarea');
    await textarea.fill(noteText);

    const [patchResponse] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/highlights/${uid}`) && res.request().method() === 'PATCH'
      ),
      row.locator('.highlight-note-save-btn').click(),
    ]);
    expect(patchResponse.status()).toBe(200);

    // Reload: GET /annotations re-fetches and the panel re-renders with the
    // saved note, from a completely fresh page load (no in-memory carryover).
    await page.reload();
    await page.waitForSelector('#reader-body p', { timeout: 10000 });
    await page.locator('#highlights-btn').click();

    const rowAfterReload = page.locator(`.highlight-row[data-uid="${uid}"]`);
    await expect(rowAfterReload).toBeVisible({ timeout: 10000 });
    await expect(rowAfterReload.locator('.highlight-note-indicator')).toHaveCount(1);
    await rowAfterReload.locator('.highlight-note-btn').click();
    await expect(rowAfterReload.locator('.highlight-note-textarea')).toHaveValue(noteText);

    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });

  // M2.2 Task 4: /highlights review view — renders a created highlight,
  // server-side color filtering, and click-through back to the reader.
  test('/highlights: shows created highlight, color filter narrows via server, click-through to reader', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    page.on('pageerror', (err) => consoleErrors.push(String(err)));

    await loginOrSetup(page);
    await saveAndOpenTestArticle(page);

    // Create a blue highlight (distinct from the yellow/green ones the
    // earlier tests in this file create) so this test's filter assertions
    // aren't confused by highlights left over from a prior run against the
    // same scratch article.
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

    const blueBtn = page.locator('.annotate-color-btn[data-color="blue"]');
    const [createResponse] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes('/highlights') && res.request().method() === 'POST'
      ),
      blueBtn.click(),
    ]);
    const created = await createResponse.json();
    const uid = created.data.uid;
    const articleId = page.url().match(/\/articles\/(\d+)/)[1];

    // Navigate via the sidebar "Highlights" link (also exercises the nav
    // wiring, not just the route directly).
    await page.locator('a[href="/highlights"]').first().click();
    await expect(page).toHaveURL(/\/highlights/);

    const row = page.locator(`.hl-list-row[data-uid="${uid}"]`);
    await expect(row).toBeVisible({ timeout: 10000 });
    await expect(row.locator('.highlight-quote')).toContainText('This domain is for use in documentation examples');

    // --- Color filter is server-side: assert the actual request params,
    // not just the resulting DOM (a client-side-only filter would also
    // pass a DOM-only check). ---
    const [blueRes] = await Promise.all([
      page.waitForResponse((res) => res.url().includes('/api/highlights?') && res.url().includes('color=blue')),
      page.locator('.hl-filter-chip[data-color="blue"]').click(),
    ]);
    expect(blueRes.status()).toBe(200);
    await expect(page.locator(`.hl-list-row[data-uid="${uid}"]`)).toBeVisible();

    const [pinkRes] = await Promise.all([
      page.waitForResponse((res) => res.url().includes('/api/highlights?') && res.url().includes('color=pink')),
      page.locator('.hl-filter-chip[data-color="pink"]').click(),
    ]);
    expect(pinkRes.status()).toBe(200);
    await expect(page.locator(`.hl-list-row[data-uid="${uid}"]`)).toHaveCount(0);

    // Clearing filters brings it back.
    const [clearRes] = await Promise.all([
      page.waitForResponse((res) => res.url().includes('/api/highlights?') && !res.url().includes('color=')),
      page.locator('#highlights-filter-clear').click(),
    ]);
    expect(clearRes.status()).toBe(200);
    await expect(page.locator(`.hl-list-row[data-uid="${uid}"]`)).toBeVisible();

    // --- Click-through lands in the reader ---
    await page.locator(`.hl-list-row[data-uid="${uid}"]`).click();
    await expect(page).toHaveURL(new RegExp(`/articles/${articleId}`));
    await page.waitForSelector('#reader-body p', { timeout: 10000 });

    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });

  // M2.2 Task 4 review fix (wave 1): latest-wins request token in
  // highlights.js's fetchHighlights(). A fast filter flip (blue -> pink,
  // second click fired before the first's response lands) must render
  // pink's result; a stale, later-arriving blue response must not clobber
  // it once it finally shows up. Made deterministic (not a real timing
  // race) by intercepting the color=blue request and injecting an
  // artificial delay, so blue is GUARANTEED to resolve after pink — without
  // the fetchToken guard, that delayed response would repaint the blue row
  // back into view after the fact.
  test('/highlights: rapid filter flip renders the latest filter, not a stale delayed response', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    page.on('pageerror', (err) => consoleErrors.push(String(err)));

    await loginOrSetup(page);
    await saveAndOpenTestArticle(page);

    // Create a blue highlight so /highlights?color=blue has a real row to
    // filter to (and to later assert stays HIDDEN once color=pink wins).
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
    const blueBtn = page.locator('.annotate-color-btn[data-color="blue"]');
    const [createResponse] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes('/highlights') && res.request().method() === 'POST'
      ),
      blueBtn.click(),
    ]);
    const created = await createResponse.json();
    const uid = created.data.uid;

    await page.locator('a[href="/highlights"]').first().click();
    await expect(page).toHaveURL(/\/highlights/);
    await expect(page.locator(`.hl-list-row[data-uid="${uid}"]`)).toBeVisible({ timeout: 10000 });

    // Delay ONLY the color=blue request (added after the page already
    // loaded, so it can't affect the initial/unfiltered fetch above).
    await page.route(
      (url) => url.pathname === '/api/highlights',
      async (route) => {
        if (route.request().url().includes('color=blue')) {
          await new Promise((resolve) => setTimeout(resolve, 1500));
        }
        await route.continue();
      }
    );

    // Fire blue, then IMMEDIATELY fire pink — without awaiting blue's
    // (deliberately slow) response in between. This is the race.
    await page.locator('.hl-filter-chip[data-color="blue"]').click();
    const [pinkRes] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes('/api/highlights?') && res.url().includes('color=pink')
      ),
      page.locator('.hl-filter-chip[data-color="pink"]').click(),
    ]);
    expect(pinkRes.status()).toBe(200);

    // Pink's (fast, un-delayed) response has landed and rendered. No pink
    // highlight exists, so the blue row must NOT be visible.
    await expect(page.locator(`.hl-list-row[data-uid="${uid}"]`)).toHaveCount(0);
    await expect(page.locator('.hl-filter-chip[data-color="pink"]')).toHaveClass(/active/);

    // Now wait out the deliberately delayed blue response and confirm it
    // did NOT clobber the pink view once it finally arrives — this is the
    // actual assertion the fetchToken guard exists for.
    await page.waitForResponse(
      (res) => res.url().includes('/api/highlights?') && res.url().includes('color=blue'),
      { timeout: 5000 }
    );
    // Give the resolved promise's .then chain (json parse + render) a beat
    // to run before re-checking — this is bounded by the already-awaited
    // network response, not an arbitrary sleep-and-hope.
    await page.waitForTimeout(200);
    await expect(page.locator(`.hl-list-row[data-uid="${uid}"]`)).toHaveCount(0);
    await expect(page.locator('.hl-filter-chip[data-color="pink"]')).toHaveClass(/active/);

    await page.unroute((url) => url.pathname === '/api/highlights');
    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });
});
