// swipe-triage.spec.js — M3.2 Task 3 (swipe gestures + undo binder).
//
// Covers the pointer wiring of js/swipe.js + js/undo.js into the inbox:
//   - mobile emulation: swipe-right -> archived (mark-read) + undo toast ->
//     Undo -> card restored unread (server-side too, via the new
//     PATCH read {"is_read": false} capability)
//   - mobile emulation: swipe-left -> snooze preset sheet -> Tonight ->
//     card gone + undo toast -> Undo -> card back
//   - mobile emulation: a mostly-VERTICAL drag over a card triggers
//     nothing (no toast, no sheet, no transform residue) — the
//     direction-lock/scroll-protection invariant
//   - desktop: keyboard `2` (like) -> undo toast -> `u` -> rating restored
//
// Pointer synthesis note: gestures are driven through page.mouse (trusted
// CDP input -> real PointerEvents in Chromium). The gesture handlers are
// delegated on #article-list and unify mouse/touch via pointer events, so a
// mouse drag exercises exactly the same code path a finger does. Drags
// start in the card's bottom padding (the card element itself) so the
// pointerdown target is never an interactive descendant (which correctly
// refuses to engage the gesture).
//
// Run against a SCRATCH Tiro server only (never the real library):
//
//   TIRO_CONFIG=/path/to/scratch/config.yaml \
//   TIRO_LIBRARY_PATH=/path/to/scratch/library \
//   uv run python run.py &
//   cd playwright-tests && TIRO_PASSWORD=<pw> npx playwright test swipe-triage.spec.js --workers=1

const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TIRO_URL || 'http://localhost:8000';
const PASSWORD = process.env.TIRO_PASSWORD || 'swipe-triage-spec-test-pass';

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

// Saves a fresh example.com URL straight through the API from page context
// (same-origin fetch, session cookie + Sec-Fetch-Site pass auth/CSRF) —
// avoids driving the save modal on a mobile viewport. Returns the id.
async function saveArticleViaApi(page, uniqueSuffix) {
  const result = await page.evaluate(async (suffix) => {
    const res = await fetch('/api/ingest/url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: `https://example.com/?swipe-spec=${suffix}` }),
    });
    return await res.json();
  }, uniqueSuffix);
  if (!result || !result.data || !result.data.id) {
    throw new Error(`saveArticleViaApi failed: ${JSON.stringify(result)}`);
  }
  return result.data.id;
}

async function getArticle(page, id) {
  return await page.evaluate(async (articleId) => {
    const res = await fetch(`/api/articles/${articleId}`);
    const json = await res.json();
    return json.data;
  }, id);
}

// Drag horizontally across a card, starting in its bottom padding (the card
// element itself, never a child button/link). dxFraction is relative to the
// card's own width (the state machine's act threshold is 35%).
async function dragCard(page, card, dxFraction, dyPx = 0) {
  const box = await card.boundingBox();
  const startX = box.x + box.width * 0.45;
  const startY = box.y + box.height - 6;
  const dx = box.width * dxFraction;

  await page.mouse.move(startX, startY);
  await page.mouse.down();
  const steps = 8;
  for (let i = 1; i <= steps; i++) {
    await page.mouse.move(startX + (dx * i) / steps, startY + (dyPx * i) / steps);
    await page.waitForTimeout(25);
  }
  await page.mouse.up();
}

function collectConsoleErrors(page) {
  const errors = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  page.on('pageerror', (err) => errors.push(String(err)));
  return errors;
}

test.describe('M3.2 swipe triage (mobile emulation)', () => {
  test.use({
    baseURL: BASE_URL,
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    isMobile: true,
  });

  test.beforeEach(async ({ page }) => {
    // Keep the A2HS hint from ever appearing near the toast area.
    await page.addInitScript(() => {
      window.localStorage.setItem('tiro-a2hs-hint-dismissed', '1');
    });
  });

  test('swipe right -> archived + undo toast -> Undo -> restored unread', async ({ page }) => {
    const consoleErrors = collectConsoleErrors(page);
    await loginOrSetup(page);

    const id = await saveArticleViaApi(page, `${Date.now()}-archive`);
    await page.goto('/inbox');
    const card = page.locator(`.article-card[data-id="${id}"]`);
    await expect(card).toBeVisible();

    const [readResp] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/articles/${id}/read`) && res.request().method() === 'PATCH'
      ),
      dragCard(page, card, 0.6),
    ]);
    expect(readResp.status()).toBe(200);

    // Card leaves the list, undo toast appears.
    await expect(page.locator(`.article-card[data-id="${id}"]`)).toHaveCount(0);
    const toast = page.locator('#undo-toast');
    await expect(toast).toBeVisible();
    await expect(toast).toContainText('Archived');

    // Server really marked it read.
    expect((await getArticle(page, id)).is_read).toBe(1);

    // Undo -> card restored, unread on the server AND in the list.
    const [unreadResp] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/articles/${id}/read`) && res.request().method() === 'PATCH'
      ),
      toast.locator('.undo-toast-btn').click(),
    ]);
    expect(unreadResp.status()).toBe(200);

    const restored = page.locator(`.article-card[data-id="${id}"]`);
    await expect(restored).toBeVisible({ timeout: 10000 });
    await expect(restored).not.toHaveClass(/is-read/);
    expect((await getArticle(page, id)).is_read).toBe(0);
    await expect(page.locator('#undo-toast')).toHaveCount(0);

    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });

  test('swipe left -> preset sheet -> Tonight -> gone + undo toast -> Undo -> back', async ({ page }) => {
    const consoleErrors = collectConsoleErrors(page);
    await loginOrSetup(page);

    const id = await saveArticleViaApi(page, `${Date.now()}-snooze`);
    await page.goto('/inbox');
    const card = page.locator(`.article-card[data-id="${id}"]`);
    await expect(card).toBeVisible();

    await dragCard(page, card, -0.6);

    const sheet = page.locator('#snooze-sheet-overlay');
    await expect(sheet).toBeVisible();

    const [snoozeResp] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/articles/${id}/snooze`) && res.request().method() === 'PATCH'
      ),
      sheet.locator('.snooze-preset-btn[data-preset="tonight"]').click(),
    ]);
    expect(snoozeResp.status()).toBe(200);

    await expect(page.locator(`.article-card[data-id="${id}"]`)).toHaveCount(0);
    const toast = page.locator('#undo-toast');
    await expect(toast).toBeVisible();
    await expect(toast).toContainText('Snoozed until');

    // Undo -> unsnoozed, card returns to the default view.
    const [unsnoozeResp] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/articles/${id}/snooze`) && res.request().method() === 'PATCH'
      ),
      toast.locator('.undo-toast-btn').click(),
    ]);
    expect(unsnoozeResp.status()).toBe(200);
    await expect(page.locator(`.article-card[data-id="${id}"]`)).toBeVisible({ timeout: 10000 });

    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });

  test('mostly-vertical drag over a card triggers nothing', async ({ page }) => {
    const consoleErrors = collectConsoleErrors(page);
    await loginOrSetup(page);

    const id = await saveArticleViaApi(page, `${Date.now()}-vertical`);
    await page.goto('/inbox');
    const card = page.locator(`.article-card[data-id="${id}"]`);
    await expect(card).toBeVisible();

    // Mostly vertical (dy 160px, dx ~6px): the state machine must lock
    // "scrolling" and never act, transform, or hint.
    const box = await card.boundingBox();
    const startX = box.x + box.width * 0.45;
    const startY = box.y + box.height - 6;
    await page.mouse.move(startX, startY);
    await page.mouse.down();
    for (let i = 1; i <= 8; i++) {
      await page.mouse.move(startX + (6 * i) / 8, startY + (160 * i) / 8);
      await page.waitForTimeout(25);
    }
    await page.mouse.up();
    await page.waitForTimeout(300);

    await expect(page.locator('#undo-toast')).toHaveCount(0);
    await expect(page.locator('.settings-toast')).toHaveCount(0);
    await expect(page.locator('#snooze-sheet-overlay')).toHaveCount(0);
    await expect(card).toBeVisible();
    // No transform residue and no swipe classes left behind.
    const residue = await card.evaluate((el) => ({
      transform: el.style.transform || '',
      classes: el.className,
    }));
    expect(residue.transform).toBe('');
    expect(residue.classes).not.toContain('swiping');
    expect(residue.classes).not.toContain('swipe-right-hint');
    expect(residue.classes).not.toContain('swipe-left-hint');
    expect((await getArticle(page, id)).is_read).toBe(0);

    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });
});

test.describe('M3.2 keyboard rate undo (desktop)', () => {
  test.use({ baseURL: BASE_URL, viewport: { width: 1280, height: 800 } });

  test('keyboard 2 (like) -> undo toast -> u -> rating restored', async ({ page }) => {
    const consoleErrors = collectConsoleErrors(page);
    await loginOrSetup(page);

    await saveArticleViaApi(page, `${Date.now()}-rate`);
    await page.goto('/inbox');
    await expect(page.locator('.article-card').first()).toBeVisible();

    // j selects a card; read WHICH one so the assertions target it exactly
    // (the shared scratch library holds articles from earlier tests too).
    await page.keyboard.press('j');
    const selected = page.locator('.article-card.kb-selected');
    await expect(selected).toBeVisible();
    const targetId = Number(await selected.getAttribute('data-id'));
    const priorRating = (await getArticle(page, targetId)).rating; // may be null

    const [rateResp] = await Promise.all([
      page.waitForResponse(
        (res) =>
          res.url().includes(`/api/articles/${targetId}/rate`) && res.request().method() === 'PATCH'
      ),
      page.keyboard.press('2'),
    ]);
    expect(rateResp.status()).toBe(200);

    const toast = page.locator('#undo-toast');
    await expect(toast).toBeVisible();
    await expect(toast).toContainText('Rated: like');
    expect((await getArticle(page, targetId)).rating).toBe(1);
    await expect(selected.locator('.rate-btn.like')).toHaveClass(/active/);

    // u -> rating restored to the pre-action value (null clears).
    const [undoResp] = await Promise.all([
      page.waitForResponse(
        (res) =>
          res.url().includes(`/api/articles/${targetId}/rate`) && res.request().method() === 'PATCH'
      ),
      page.keyboard.press('u'),
    ]);
    expect(undoResp.status()).toBe(200);
    await expect(page.locator('#undo-toast')).toHaveCount(0);
    expect((await getArticle(page, targetId)).rating).toBe(priorRating);
    if (priorRating !== 1) {
      await expect(selected.locator('.rate-btn.like')).not.toHaveClass(/active/);
    }

    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });

  // M3.2 Task 3 review fix (wave 1): rateSelected() used to capture
  // `priorRating` from cachedArticles synchronously but only mutate the
  // cache AFTER the awaited PATCH resolved. Two rapid rating keypresses (the
  // second fired before the first's round-trip lands) meant the second
  // capture read the ORIGINAL pre-action value, not the first action's
  // result — so undoing the second action restored past the first one
  // entirely. Fixed by moving the cache mutation to immediately after the
  // capture, before the await.
  //
  // Made deterministic (not a real timing race) via route interception that
  // holds the FIRST rate PATCH for 1.5s — same delay pattern as
  // annotations.spec.js's fetchToken regression test. Key `1` (dislike, -1)
  // fires first and is held; key `2` (like, 1) fires immediately after
  // (before the held response lands) and is NOT delayed, so it completes
  // first. Undo is triggered while the first request is STILL held, so it
  // exercises exactly the single active undo entry (the second action's) —
  // whether a late-arriving, now-stale response clobbering that entry is a
  // separate, pre-existing, out-of-scope race (last-network-response-wins
  // for which offerUndo call stays active) is NOT what this test is
  // pinning; it is confirmed accepted/unchanged, see the report.
  test('rapid keyboard 1 then 2 -> undo restores to the FIRST action\'s rating, not the original', async ({ page }) => {
    const consoleErrors = collectConsoleErrors(page);
    await loginOrSetup(page);

    const id = await saveArticleViaApi(page, `${Date.now()}-rapid-rate`);
    await page.goto('/inbox');
    const card = page.locator(`.article-card[data-id="${id}"]`);
    await expect(card).toBeVisible();

    let rateCallCount = 0;
    await page.route('**/api/articles/*/rate', async (route) => {
      rateCallCount += 1;
      if (rateCallCount === 1) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
      await route.continue();
    });

    await page.keyboard.press('j');
    const selected = page.locator('.article-card.kb-selected');
    await expect(selected).toHaveAttribute('data-id', String(id));
    expect((await getArticle(page, id)).rating).toBe(null);

    // Fire dislike (held) then IMMEDIATELY like (fast) — the race.
    await page.keyboard.press('1');
    await page.keyboard.press('2');

    // The fast (second) action's PATCH lands first.
    const secondResp = await page.waitForResponse(
      (res) =>
        res.url().includes(`/api/articles/${id}/rate`) &&
        res.request().method() === 'PATCH' &&
        JSON.parse(res.request().postData()).rating === 1
    );
    expect(secondResp.status()).toBe(200);
    expect((await getArticle(page, id)).rating).toBe(1);
    await expect(page.locator('#undo-toast')).toContainText('Rated: like');

    // Undo NOW, while the first (dislike) PATCH is still held in flight —
    // this is the single active undo entry, and per the fix it must restore
    // to -1 (what the FIRST action set), not null (the original, pre-race
    // value a buggy capture would have stored).
    const [undoResp] = await Promise.all([
      page.waitForResponse(
        (res) =>
          res.url().includes(`/api/articles/${id}/rate`) &&
          res.request().method() === 'PATCH' &&
          JSON.parse(res.request().postData()).rating === -1
      ),
      page.keyboard.press('u'),
    ]);
    expect(undoResp.status()).toBe(200);
    expect((await getArticle(page, id)).rating).toBe(-1);
    await expect(page.locator('#undo-toast')).toHaveCount(0);

    // Drain the held first request so it doesn't leak past the test.
    await page.waitForTimeout(1600);

    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });

  // M3.2 Task 3 review fix (wave 1), rollback half: the optimistic cache
  // mutation added above must be rolled back on PATCH failure, with the
  // existing "Failed to rate article" error toast surfaced (new — the path
  // was previously silent) so the rollback is coherent with what the user
  // sees.
  test('rating PATCH failure rolls back the optimistic cache mutation', async ({ page }) => {
    const consoleErrors = collectConsoleErrors(page);
    await loginOrSetup(page);

    const id = await saveArticleViaApi(page, `${Date.now()}-rate-fail`);
    await page.goto('/inbox');
    const card = page.locator(`.article-card[data-id="${id}"]`);
    await expect(card).toBeVisible();

    await page.route('**/api/articles/*/rate', async (route) => {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ success: false }),
      });
    });

    await page.keyboard.press('j');
    const selected = page.locator('.article-card.kb-selected');
    await expect(selected).toHaveAttribute('data-id', String(id));

    const [rateResp] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/articles/${id}/rate`) && res.request().method() === 'PATCH'
      ),
      page.keyboard.press('2'),
    ]);
    expect(rateResp.status()).toBe(500);

    // Error toast surfaced, no undo toast (nothing succeeded to undo).
    await expect(page.locator('.settings-toast')).toContainText('Failed to rate article');
    await expect(page.locator('#undo-toast')).toHaveCount(0);

    // Server was never actually touched (fulfilled locally) — still
    // unrated — and the optimistic mutation was rolled back, so the button
    // does not show active.
    expect((await getArticle(page, id)).rating).toBe(null);
    await expect(selected.locator('.rate-btn.like')).not.toHaveClass(/active/);

    // Chromium logs the fulfilled 500 itself as a console error ("Failed to
    // load resource: ... 500") -- that's the expected side effect of the
    // route.fulfill() above, not an application bug (same filtering
    // rationale as save-queue.spec.js's net::ERR_FAILED case).
    const unexpectedErrors = consoleErrors.filter((e) => !e.includes('500 (Internal Server Error)'));
    expect(unexpectedErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });
});
