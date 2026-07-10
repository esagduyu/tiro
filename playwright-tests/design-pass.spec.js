// design-pass.spec.js — Frontend design pass (Tasks 1–11), Task 12 verification.
//
// The repeatable verification matrix for the icon-system + component-primitive
// + three-tier-responsive-chrome redesign. For every (viewport × page) combo it
// asserts (a) the right chrome shows for that tier and (b) NO legacy emoji/entity
// glyph survives anywhere in the rendered DOM (the real regression guard for the
// icon migration — every ♥ ★ ⋮ ☰ ▶ 📜 🖍 📚 ⚙ should now be an inline SVG icon).
// A final dark-theme pass toggles the real sidebar control and re-scans /inbox.
// Screenshots of every combo land in playwright-tests/screenshots/ (gitignored)
// for owner eyeballing. Structure mirrors snooze-ui.spec.js's login/seed helpers.
//
// Run against a SCRATCH Tiro server only (never the real library):
//
//   TIRO_CONFIG=/path/to/scratch/config.yaml \
//   TIRO_LIBRARY_PATH=/path/to/scratch/library \
//   uv run python run.py &
//   cd playwright-tests && TIRO_PASSWORD=<pw> npx playwright test design-pass.spec.js
//
// Configuration (env vars, both optional, same defaults as phase0.spec.js):
//   TIRO_URL      base URL of the running server (default http://localhost:8000)
//   TIRO_PASSWORD password to use for login / first-run setup
//
// VIEWPORT NOTE (deliberate, documented deviation from the brief's literal list):
// spec §10 lists 375×812 / 768×1024 / 1440×900 and expects "icon rail on tablet
// only". But spec §D3 (and the shipped, 11-task-reviewed CSS) defines the tiers as
// Phone ≤768px / Tablet 769–1199px / Desktop ≥1200px — so a 768px-wide viewport is
// PHONE (tab bar), not the tablet rail. That is a consistent off-by-one in the
// verification wording, not in the implementation. To exercise all three tiers
// truthfully while still covering the literal 768×1024 viewport the spec names,
// this matrix keeps 768×1024 as a PHONE-tier viewport AND adds 1024×768 to hit the
// 64px rail. See task-12-report.md for the full rationale.

const path = require('path');
const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TIRO_URL || 'http://localhost:8000';
const PASSWORD = process.env.TIRO_PASSWORD || 'design-pass-spec-test-pass';
const SHOTS_DIR = path.join(__dirname, 'screenshots');

// The legacy-glyph blacklist from spec §10 / the Task 12 brief, verbatim.
const GLYPH_RE = /[♥★⋮☰▶📜🖍📚⚙]/;

const VIEWPORTS = [
  { label: 'phone-375', w: 375, h: 812, tier: 'phone' },
  { label: 'phone-768', w: 768, h: 1024, tier: 'phone' }, // ≤768 ⇒ phone per §D3
  { label: 'tablet-1024', w: 1024, h: 768, tier: 'tablet' }, // 769–1199 rail band
  { label: 'desktop-1440', w: 1440, h: 900, tier: 'desktop' },
];

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

// Saves a fresh example.com URL straight through the API (no save-modal driving,
// so it works at any viewport) and returns the new article's id. Mirrors
// swipe-triage.spec.js's saveArticleViaApi — the same-origin fetch rides the
// session cookie and passes the Sec-Fetch-Site / Origin CSRF checks.
async function saveArticleViaApi(page, suffix) {
  const result = await page.evaluate(async (s) => {
    const res = await fetch('/api/ingest/url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: `https://example.com/?design-pass=${s}` }),
    });
    return { status: res.status, body: await res.json().catch(() => null) };
  }, suffix);
  if (result.status !== 200 || !result.body || !result.body.data || !result.body.data.id) {
    throw new Error(`saveArticleViaApi failed: ${JSON.stringify(result)}`);
  }
  return result.body.data.id;
}

// Reads the live chrome state (sidebar/tab-bar/reader-action-bar) off the page.
async function readChrome(page) {
  return page.evaluate(() => {
    const q = (s) => document.querySelector(s);
    const shown = (el) => (el ? getComputedStyle(el).display !== 'none' : false);
    const width = (el) => (el ? Math.round(el.getBoundingClientRect().width) : null);
    const sidebar = q('.sidebar');
    const tabbar = q('#tab-bar');
    const readerBar = q('#reader-action-bar');
    return {
      sidebarShown: shown(sidebar),
      sidebarWidth: shown(sidebar) ? width(sidebar) : 0,
      tabbarShown: shown(tabbar),
      readerBarShown: shown(readerBar),
    };
  });
}

// Asserts the chrome matches the tier. The reader page swaps its bottom tab bar
// for the reader-action-bar on phones (CSS: body.reader-page #tab-bar{display:none}),
// so phone-tier reader is special-cased; tablet/desktop reader keep the sidebar.
async function assertChrome(page, tier, isReader, where) {
  const c = await readChrome(page);
  const msg = (label) => `[${where}] ${label} — chrome=${JSON.stringify(c)}`;
  if (tier === 'phone') {
    expect(c.sidebarShown, msg('sidebar must be hidden on phone')).toBe(false);
    if (isReader) {
      expect(c.readerBarShown, msg('reader action bar must show on phone reader')).toBe(true);
      expect(c.tabbarShown, msg('tab bar must yield to reader action bar')).toBe(false);
    } else {
      expect(c.tabbarShown, msg('tab bar must show on phone')).toBe(true);
    }
  } else if (tier === 'tablet') {
    expect(c.tabbarShown, msg('tab bar must be hidden on tablet')).toBe(false);
    expect(c.sidebarWidth, msg('sidebar must collapse to a 64px rail on tablet')).toBe(64);
  } else {
    expect(c.tabbarShown, msg('tab bar must be hidden on desktop')).toBe(false);
    expect(c.sidebarWidth, msg('full sidebar must show on desktop')).toBeGreaterThanOrEqual(200);
  }
  return c;
}

async function assertNoLegacyGlyphs(page, where) {
  const hit = await page.evaluate((src) => {
    const re = new RegExp(src);
    const m = document.body.innerText.match(re);
    if (!m) return null;
    return {
      ch: m[0],
      ctx: document.body.innerText.slice(Math.max(0, m.index - 40), m.index + 40),
    };
  }, GLYPH_RE.source);
  expect(hit, hit ? `[${where}] legacy glyph "${hit.ch}" survived near: …${hit.ctx}…` : undefined).toBeNull();
}

test.describe('Design pass — responsive chrome + icon-migration matrix', () => {
  test.use({ baseURL: BASE_URL });

  test('matrix: every viewport × page shows the right chrome and zero legacy glyphs', async ({ page }) => {
    test.setTimeout(180000);
    await loginOrSetup(page);
    const seededId = await saveArticleViaApi(page, `matrix-${Date.now()}`);

    const pages = [
      { label: 'inbox', path: '/inbox', reader: false },
      { label: 'digest', path: '/digest', reader: false },
      { label: 'reader', path: `/articles/${seededId}`, reader: true },
      { label: 'stats', path: '/stats', reader: false },
      { label: 'settings', path: '/settings', reader: false },
    ];

    for (const vp of VIEWPORTS) {
      await page.setViewportSize({ width: vp.w, height: vp.h });
      for (const p of pages) {
        const where = `${vp.label} ${p.label}`;
        await page.goto(p.path);
        // Let charts (stats), markdown (reader), and media queries settle.
        await page.waitForTimeout(600);
        await assertChrome(page, vp.tier, p.reader, where);
        await assertNoLegacyGlyphs(page, where);
        await page.screenshot({
          path: path.join(SHOTS_DIR, `${vp.label}-${p.label}.png`),
        });
      }
    }
  });

  test('dark theme pass: sidebar toggle flips theme, /inbox stays glyph-clean', async ({ page }) => {
    await loginOrSetup(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/inbox');

    // Toggle via the real sidebar control (visible at desktop width).
    await expect(page.locator('#theme-toggle')).toBeVisible();
    await page.locator('#theme-toggle').click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');

    // Re-navigate so the server re-renders with the persisted dark mode, then scan.
    await page.goto('/inbox');
    await page.waitForTimeout(400);
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    await assertChrome(page, 'desktop', false, 'dark /inbox');
    await assertNoLegacyGlyphs(page, 'dark /inbox');
    await page.screenshot({ path: path.join(SHOTS_DIR, 'dark-inbox.png') });
  });
});
