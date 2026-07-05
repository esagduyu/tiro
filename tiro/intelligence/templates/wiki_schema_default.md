# Wiki Maintenance Instructions

This file is injected verbatim into every wiki page generation prompt. It is yours to
edit — change the rules below to match how you actually want your library's wiki to
read. Nothing else in Tiro depends on this file's exact wording; the wiki maintainer
just follows whatever it says.

## Page kinds

- **entity** pages cover a person, company, or organization: who/what they are, what
  they've done, and how they show up across your library.
- **concept** pages cover a topic or theme (a tag): what it means in context, the
  positions different sources take on it, and how it connects to other concepts.

## When to create a new page vs. edit an existing one

Default to editing. A new page should be rare: create one only when a name or topic
has accumulated enough independent coverage (roughly 3+ articles, or 1 if it's from a
VIP source) that it deserves its own standing synthesis. If the subject is a narrower
facet of something that already has a page, fold it into that page as a subsection
instead of splitting it out — a proliferation of thin, overlapping pages is a worse
outcome than one page with more headings. When in doubt, ask: "would a reader be
better served by one deeper page or two shallow ones?" — almost always the former.

## Tone and length

Write dense, factual prose. No filler, no meta-commentary about the page itself, no
"in conclusion" summaries. Every sentence should say something a skim of the source
articles wouldn't already tell the reader faster. Prefer short paragraphs organized
under `##` subsections over one long undifferentiated block once a page passes a
handful of paragraphs. Length should track the amount of real signal in the sources,
not a target word count — a page with three thin articles should stay short.

## Compression rules

Merge, don't proliferate. When two or more sources say the same thing, write it once
with multiple citations rather than once per source. When regenerating an existing
page, rewrite the affected paragraph in place rather than appending a new one — the
page should read as if it had always said this, not as an append-only log. Drop
claims that newer, better sources have superseded or corrected; don't just add the
correction alongside the outdated claim and leave the reader to reconcile them.

## Citation rules

Every factual claim must carry at least one `[[stem|label]]` wikilink to a source
article, using only the citation stems actually provided for this generation — never
invent one. A claim with no citation is a claim that shouldn't be on the page. When a
single sentence draws on multiple sources, cite all of them.

## Trust-weighting rules

Say the quiet part out loud: when a claim comes from a VIP source, or from an article
you loved, liked, or disliked, name that in the sentence itself rather than treating
all sources as equally authoritative. Where sources disagree, don't silently pick a
side — note the disagreement and let the trust signals (VIP status, your ratings,
relevance decay) inform how it's framed, not which claim gets omitted.
