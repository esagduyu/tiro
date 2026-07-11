// First-run onboarding wizard (Phase 5 M5.1, spec D6). Page module (M2.0
// pattern): client-side step state only — no server-side wizard state. The
// password step posts to the existing POST /api/auth/setup unchanged, which
// mints the session every step after it rides. Every step is skippable except
// the password step, which gates progression (Phase 0 made a password
// mandatory).
import { showToast } from "./core.js";

const $ = (id) => document.getElementById(id);
const card = document.querySelector(".onb-card");
const stepEls = Array.from(document.querySelectorAll(".onb-step"));
const ORDER = stepEls.map((el) => el.dataset.step);
const errorEl = $("onb-error");

const state = {
  configured: false,      // does the server already have a password?
  index: 0,
};

function showError(msg) {
  errorEl.textContent = msg || "";
}

function renderProgress() {
  const ol = $("onb-progress");
  ol.innerHTML = ORDER.map(
    (_, i) =>
      `<li class="onb-dot${i === state.index ? " active" : ""}${i < state.index ? " done" : ""}"></li>`
  ).join("");
}

function showStep(index) {
  state.index = Math.max(0, Math.min(index, ORDER.length - 1));
  stepEls.forEach((el, i) => {
    el.hidden = i !== state.index;
  });
  showError("");
  renderProgress();
  // Focus the first focusable control for keyboard flow.
  const active = stepEls[state.index];
  const focusable = active.querySelector("input:not([type=hidden]), select, button");
  if (focusable) focusable.focus();
}

function finish() {
  window.location.href = "/inbox";
}

function advance() {
  if (state.index >= ORDER.length - 1) {
    finish();
    return;
  }
  showStep(state.index + 1);
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  let data = {};
  try {
    data = await r.json();
  } catch {
    data = {};
  }
  return { ok: r.ok, status: r.status, data };
}

function detailOf(data, fallback) {
  const d = data && data.detail;
  if (typeof d === "string") return d;
  if (d && typeof d === "object" && d.message) return d.message;
  return fallback;
}

// --- Per-step submit handlers. Return true to advance, false to stay. --------

const submitters = {
  async library() {
    const input = $("library-path-input");
    const wanted = input.value.trim();
    const current = card.dataset.libraryPath || "";
    if (!wanted || wanted === current) return true; // unchanged -> no-op
    const { ok, data } = await postJSON("/api/setup/library-path", { path: wanted });
    if (!ok) {
      showError(detailOf(data, "Couldn't use that folder."));
      return false;
    }
    card.dataset.libraryPath = (data.data && data.data.library_path) || wanted;
    showToast("Library location saved", "success");
    return true;
  },

  async password() {
    if (state.configured) return true; // already set (revisit) — nothing to do
    const pw = $("onb-password").value;
    const confirm = $("onb-confirm").value;
    if (pw.length < 8) {
      showError("Password must be at least 8 characters.");
      return false;
    }
    if (pw !== confirm) {
      showError("Passwords do not match.");
      return false;
    }
    const { ok, data } = await postJSON("/api/auth/setup", { password: pw });
    if (!ok) {
      showError(detailOf(data, "Couldn't set the password."));
      return false;
    }
    state.configured = true;
    showToast("Password set", "success");
    return true;
  },

  async ai() {
    const provider = $("ai-provider-select").value;
    if (provider === "skip") return true;
    const key = $("ai-key-input").value.trim();
    const { ok, data } = await postJSON("/api/setup/ai", { provider, api_key: key || null });
    if (!ok) {
      showError(detailOf(data, "Couldn't save the AI provider."));
      return false;
    }
    showToast("AI provider saved", "success");
    return true;
  },

  async email() {
    const address = $("email-address").value.trim();
    if (!address) return true; // empty -> treat as skip
    const appPassword = $("email-app-password").value;
    const { ok, data } = await postJSON("/api/settings/email", {
      gmail_address: address,
      app_password: appPassword,
      enable_receive: true,
    });
    if (!ok) {
      showError(detailOf(data, "Couldn't connect email."));
      return false;
    }
    showToast("Email connected", "success");
    return true;
  },

  async samples() {
    const { ok, data } = await postJSON("/api/setup/samples", {});
    if (!ok) {
      showError(detailOf(data, "Couldn't add samples."));
      return false;
    }
    const n = (data.data && data.data.created) || 0;
    $("samples-done").hidden = false;
    showToast(n > 0 ? `Added ${n} sample article${n === 1 ? "" : "s"}` : "Samples already present", "success");
    return true;
  },
};

// --- Wiring ------------------------------------------------------------------

async function handleNext(btn) {
  const stepName = ORDER[state.index];
  const submit = submitters[stepName];
  if (!submit) {
    advance();
    return;
  }
  btn.disabled = true;
  try {
    const advanceOk = await submit();
    if (advanceOk) advance();
  } catch (e) {
    showError("Network error — please try again.");
  } finally {
    btn.disabled = false;
  }
}

card.addEventListener("click", (e) => {
  const nextBtn = e.target.closest("[data-next]");
  if (nextBtn) {
    e.preventDefault();
    handleNext(nextBtn);
    return;
  }
  const skipBtn = e.target.closest("[data-skip]");
  if (skipBtn) {
    e.preventDefault();
    advance();
  }
});

// Enter key submits the current step's primary action.
card.addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  if (e.target.tagName === "SELECT" || e.target.tagName === "BUTTON") return;
  const active = stepEls[state.index];
  const primary = active.querySelector("[data-next]");
  if (primary) {
    e.preventDefault();
    handleNext(primary);
  }
});

// AI provider select: reveal the key field only for key-taking providers.
$("ai-provider-select").addEventListener("change", (e) => {
  const needsKey = e.target.value === "anthropic" || e.target.value === "openai-compatible";
  $("ai-key-wrap").hidden = !needsKey;
});

// Prefill the library path and reflect whether a password already exists.
$("library-path-input").value = card.dataset.libraryPath || "";

fetch("/api/auth/status")
  .then((r) => r.json())
  .then(({ data }) => {
    if (data && data.configured) {
      state.configured = true;
      // Password step already done: hide the fields, show a confirmation, and
      // relabel the button so a revisiting user just moves on.
      $("password-fields").hidden = true;
      $("password-done").hidden = false;
      const btn = $("password-next-btn");
      if (btn) btn.textContent = "Continue";
    }
  })
  .catch(() => {});

showStep(0);
