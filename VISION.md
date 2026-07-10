# Tiro — Vision & Core Principles

Codified: 2026-07-10 (from the owner's vision check-in session, with two principles added during that review).

This is the durable statement of what Tiro is for. It changes rarely and deliberately.
`PRODUCT_ROADMAP.md` says *what to build next*; this document says *why*, and is the
standard everything is judged against: **every feature, phase, and design decision must
be justified by its adherence to the five principles below.** A feature that serves none
of them does not belong in Tiro, no matter how impressive it is.

Named after Cicero's freedman who preserved and organized his master's works for
posterity. *"...without you the oracle was dumb." — Cicero to Tiro, 53 BC*

---

## The Five Principles

### 1. Tiro is my reading library

Everything I read online is ephemeral: websites go down, articles get paywalled,
ad-tech makes pages unreadable, links rot. Tiro is the place where the things I read
become **mine** — saved as clean, readable documents I control, in a cohesive and
consistent reading surface, kept forever.

- Save once, read everywhere. The library is device-agnostic: laptop, phone, tablet —
  the same library, the same highlights, the same reading position in spirit.
- The reading experience is Tiro's, not the source website's: one typography, one
  theme system, no ads, no popups, no consent banners.
- "Forever" is literal. An article saved today must be readable in twenty years,
  which is why the storage format is markdown files on disk, not rows in a
  proprietary database.

**The test:** does this make the library more permanent, more readable, or more
reachable? Does it degrade gracefully when the original source disappears?

### 2. Tiro is my inbox management for reads

Reading material arrives from every direction — newsletters inbound by email, feeds
filling overnight, articles found while browsing — across many unrelated domains.
Tiro manages them as **one stack**: fast to scan, fast to triage, fast to catch up on.

- Triage must be fast and safe: swipe, snooze, rate, archive, undo — inbox zero as a
  reachable state, not an aspiration.
- The stack meets me where I am: read it, have it read to me (TTS), or have the
  intelligence layer summarize what I don't have time for.
- **The goal is not to replace the act of reading. The goal is to focus it.**
  Summaries and tiers exist to route my limited attention to what deserves full
  reading — never to substitute for it. "Summary-enough" is a routing decision,
  not a value judgment on reading.

**The test:** does this help me decide *what* to read and *when* — or does it quietly
start reading for me? The former belongs; the latter doesn't.

### 3. Tiro is my tracking & intelligence layer

I track what I do (sleep, training, health) because measured things can be understood
and improved. Reading deserves the same: what I read, how deeply, what I keep coming
back to — recorded so that a system can **learn from it automatically**, without me
spending my time extracting insights by hand.

- Tracking serves **me** — not a company, and god forbid, not an advertiser. All
  telemetry is local, opt-in, and inspectable (the audit log answers "what has Tiro
  sent to whom?" at any moment).
- Intelligence runs where I choose: inside Tiro (digests, analysis, scheduled
  insights at a cadence I set) or through my own assistant via MCP. Tiro's library
  is a substrate any AI I trust can work over.
- The end state is agentic: a runtime of inspectable agents that watch the library,
  learn my preferences from behavior rather than questionnaires, and surface
  insights proactively — with citations, traces, and replayable runs, never as an
  oracle I'm asked to trust blindly.

**The test:** does the signal flow toward the user or away from them? Would this
feature still make sense if no third party could ever see the data? (If not, it's
surveillance wearing a product costume.)

### 4. Tiro is data I own

The substrate under the other four principles: the library is **mine in the strongest
technical sense**, not as a policy promise but as an architectural fact.

- Files first: articles are markdown on disk, annotations are sidecar files, the
  databases are derived indexes. Truth lives in files a human can read without Tiro.
- Everything exports, everything imports, everything backs up and restores. The
  escape hatch is a core feature, not an afterthought.
- Original sources are never mutated to store personal data — highlights, notes, and
  AI outputs live adjacent, so the saved article stays clean and portable.
- Bring-your-own everything: AI keys, local models, sync storage, hosting. Anything
  paid is convenience, never a gate — **a user who never pays Tiro a cent gets every
  feature.**
- Open source (AGPL), open formats, Obsidian-friendly on disk. Interoperability with
  neighboring tools is a differentiator, not a leak.

**The test:** if Tiro-the-project died tomorrow, does the user keep everything and
lose only convenience? Any feature that makes that answer worse is regression, not
progress.

### 5. Tiro compounds my knowledge

Per-item summaries are a commodity; any tool can summarize one article. Tiro's
distinct bet is **cross-document synthesis that compounds**: the library should get
smarter the longer I use it, because the system connects what I read across months
and domains into knowledge I can query.

- The digest answers *"what should I read today?"* The wiki and knowledge graph
  answer *"what do I know?"* Both matter; the second is the moat.
- Entity and concept pages grow richer with each save; connections, contradictions,
  and shifts in coverage surface across documents I'd never manually cross-reference.
- Synthesis is trust-weighted by my own signals — ratings, engagement, VIP sources
  and authors — so the compiled knowledge reflects what I've vetted, not what I've
  hoarded.
- Compounding demands trust discipline: every synthesized claim carries citations
  back to saved articles, synthesis never feeds on synthesis unchecked, and
  regenerate-from-scratch is always available. A subtly wrong knowledge base is
  worse than none.

**The test:** does this feature's value *grow* with the size and age of the library?
Features that compound beat features that merely function.

---

## How the principles map to the product

The roadmap decomposes Tiro into three components; the principles overlay them, with
two principles as cross-cutting substrate:

| Principle | Product component |
|---|---|
| 1. Reading library | The **Reader** — the context layer the user thinks in |
| 2. Inbox management | The **Management layer** — the inbox-zero control surface |
| 3. Tracking & intelligence | The **Agentic layer** — the intelligence that works the library |
| 4. Data ownership | Substrate — the trust architecture under all three |
| 5. Compounding knowledge | The strategic bet the agentic layer exists to deliver |

## What the principles rule out

The roadmap's "Out-Of-Scope" section holds the full list with revisit triggers; the
principles explain *why* those are out:

- **No default-on telemetry, ever** — violates principle 3's direction-of-signal test.
- **No feature-gated cloud** — violates principle 4; paid tiers sell convenience only.
- **Not a generic note-taking app, not a browser, not a chatbot** — principles 1 and 2
  scope Tiro downstream of save events; notes serve articles.
- **No social/sharing features while single-user daily use is unproven** — principles
  serve one reader's attention and knowledge first; scale triggers are recorded in
  the roadmap.

## Using this document

When planning or reviewing work, name the principle(s) a change serves. If a proposal
serves none, or strengthens one by weakening another (most commonly: a convenience
that erodes ownership, or an automation that erodes trust in synthesis), that tension
must be surfaced and decided explicitly — not resolved silently in code.
