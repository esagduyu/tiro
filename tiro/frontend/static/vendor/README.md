# Vendored frontend dependencies

Local-first: no CDN at runtime. Pinned versions:
| File | Package | Version | License | Source |
|------|---------|---------|---------|--------|
| marked.min.js | marked | 15.0.12 | MIT | jsdelivr |
| purify.min.js | dompurify | 3.4.11 | Apache-2.0 OR MPL-2.0 | jsdelivr |
| chart.umd.min.js | chart.js | 4.4.7 | MIT | jsdelivr |
| d3.v7.min.js | d3 | 7.9.0 | ISC | d3js.org |

Upgrade: download the new pinned file, update this table, bump `?v=N` in base.html, re-run the suite + manual smoke.
