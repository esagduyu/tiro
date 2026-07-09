// Canonical Tiro icon set — 24x24 grid, 1.6px stroke, round caps/joins,
// currentColor. Fills reserved for star/heart and fixed dots (spec §4).
// SYNC INVARIANT: templates/_icons.html mirrors these bodies verbatim;
// js/tests/icons.test.mjs enforces it. Never edit one without the other.
export const ICONS = {
  bookmark: { body: '<path d="M6 4.5A1.5 1.5 0 0 1 7.5 3h9A1.5 1.5 0 0 1 18 4.5V21l-6-4-6 4z"/>' },
  inbox: { body: '<path d="M4 13h4l1.5 3h5L16 13h4"/><path d="M6.5 5h11l2.5 8v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-4z"/>' },
  layers: { body: '<path d="M12 3 3 8l9 5 9-5z"/><path d="M3 13l9 5 9-5"/>' },
  graph: { body: '<circle cx="6" cy="7" r="2.2"/><circle cx="18" cy="7" r="2.2"/><circle cx="12" cy="18" r="2.2"/><path d="m7.7 8.7 3 7.6"/><path d="m16.3 8.7-3 7.6"/><path d="M8 7h8"/>' },
  "bar-chart": { body: '<path d="M3 21h18"/><path d="M7 21v-6"/><path d="M12 21V7"/><path d="M17 21v-9"/>' },
  settings: { body: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3"/><path d="M12 19v3"/><path d="M2 12h3"/><path d="M19 12h3"/><path d="m4.9 4.9 2.1 2.1"/><path d="m17 17 2.1 2.1"/><path d="m19.1 4.9-2.1 2.1"/><path d="m7 17-2.1 2.1"/>' },
  "book-open": { body: '<path d="M12 7v13"/><path d="M3 5.5A1.5 1.5 0 0 1 4.5 4H9a3 3 0 0 1 3 3 3 3 0 0 1 3-3h4.5A1.5 1.5 0 0 1 21 5.5v11a1.5 1.5 0 0 1-1.5 1.5H14a2 2 0 0 0-2 2 2 2 0 0 0-2-2H4.5A1.5 1.5 0 0 1 3 16.5z"/>' },
  library: { body: '<path d="M4 4v16"/><path d="M8.5 4v16"/><path d="m13 5 4.2 14.8"/><path d="M3 20h18"/>' },
  highlighter: { body: '<path d="m10.5 7.5 6 6"/><path d="M6 12 14.5 3.5a2.1 2.1 0 0 1 3 0l3 3a2.1 2.1 0 0 1 0 3L12 18l-5.5 1.5L8 14z"/><path d="M3 21h6"/>' },
  star: { fill: true, body: '<path d="m12 2.5 2.9 6.1 6.6.6-5 4.4 1.5 6.5L12 17.2 5.5 20.6 7 14.1l-5-4.4 6.6-.6z"/>' },
  heart: { fill: true, body: '<path d="M12 20.5S4.5 15.3 4.5 9.8A4.3 4.3 0 0 1 12 7a4.3 4.3 0 0 1 7.5 2.8c0 5.5-7.5 10.7-7.5 10.7Z"/>' },
  "thumb-up": { body: '<path d="M6 21H4a1 1 0 0 1-1-1v-8a1 1 0 0 1 1-1h2"/><path d="M6 11 10 3a1.5 1.5 0 0 1 2.1 1.4V8.5h4.9a1.8 1.8 0 0 1 1.8 2.2l-1.3 6.5a1.8 1.8 0 0 1-1.8 1.4H6z"/>' },
  "thumb-down": { body: '<path d="M18 3h2a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-2"/><path d="M18 13 14 21a1.5 1.5 0 0 1-2.1-1.4V15.5H7a1.8 1.8 0 0 1-1.8-2.2l1.3-6.5A1.8 1.8 0 0 1 8.3 5H18z"/>' },
  info: { body: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>' },
  trash: { body: '<path d="M4 6h16"/><path d="M8 6V4.5A1.5 1.5 0 0 1 9.5 3h5A1.5 1.5 0 0 1 16 4.5V6"/><path d="M18.5 6 17.5 20a2 2 0 0 1-2 1.8h-7a2 2 0 0 1-2-1.8L5.5 6"/><path d="M10 10.5v6"/><path d="M14 10.5v6"/>' },
  refresh: { body: '<path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/>' },
  external: { body: '<path d="M14 4h6v6"/><path d="M20 4 11 13"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>' },
  close: { body: '<path d="M6 6 18 18"/><path d="M18 6 6 18"/>' },
  "file-text": { body: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6"/><path d="M9 17h4"/>' },
  tag: { body: '<path d="M12.6 3.2 20 10.6a2 2 0 0 1 0 2.8l-6.6 6.6a2 2 0 0 1-2.8 0L3.2 12.6a2 2 0 0 1-.6-1.4V4.5A1.5 1.5 0 0 1 4.1 3h6.1a2 2 0 0 1 1.4.6Z"/><circle cx="7.7" cy="7.7" r="1.2" fill="currentColor" stroke="none"/>' },
  copy: { body: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M4 16a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2"/>' },
  message: { body: '<path d="M20 11.5a7.5 7.5 0 0 1-10.8 6.7L4 19.5l1.3-4.1A7.5 7.5 0 1 1 20 11.5Z"/>' },
  zap: { body: '<path d="M13 2 4.5 13.5H12l-1 8.5L19.5 10.5H12z"/>' },
  basket: { body: '<rect x="3" y="13" width="18" height="7" rx="2"/><path d="M6.5 13 4 5.8A2 2 0 0 1 5.9 3h12.2A2 2 0 0 1 20 5.8L17.5 13"/><path d="M7 16.5h.01"/><path d="M11 16.5h4"/>' },
  download: { body: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 19h16"/>' },
  sun: { body: '<circle cx="12" cy="12" r="4.4"/><path d="M12 2.5V5"/><path d="M12 19v2.5"/><path d="M2.5 12H5"/><path d="M19 12h2.5"/><path d="m5.2 5.2 1.8 1.8"/><path d="m17 17 1.8 1.8"/><path d="m18.8 5.2-1.8 1.8"/><path d="m7 17-1.8 1.8"/>' },
  moon: { body: '<path d="M20.5 14.5A8.5 8.5 0 1 1 9.5 3.5a7 7 0 0 0 11 11Z"/>' },
  search: { body: '<circle cx="11" cy="11" r="6.5"/><path d="m15.8 15.8 5.2 5.2"/>' },
  logout: { body: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>' },
  clock: { body: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>' },
  archive: { body: '<rect x="3" y="4" width="18" height="5" rx="1.2"/><path d="M5 9v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9"/><path d="M10 13.5h4"/>' },
  undo: { body: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>' },
  play: { body: '<path d="M7.5 4.5v15l12-7.5z"/>' },
  pause: { body: '<path d="M9 5v14"/><path d="M15 5v14"/>' },
  pencil: { body: '<path d="M16.7 3.3a2.1 2.1 0 0 1 3 3L8 18l-4.5 1.5L5 15z"/><path d="m13.5 6.5 4 4"/>' },
  note: { body: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="m9.5 16.5 2-.5 4-4a1.4 1.4 0 0 0-2-2l-4 4z"/>' },
  check: { body: '<path d="m4.5 12.5 5 5 10-11"/>' },
  "chevron-left": { body: '<path d="m14.5 6-6 6 6 6"/>' },
  "chevron-right": { body: '<path d="m9.5 6 6 6-6 6"/>' },
  "chevron-up": { body: '<path d="m6 14.5 6-6 6 6"/>' },
  "chevron-down": { body: '<path d="m6 9.5 6 6 6-6"/>' },
  kebab: { body: '<circle cx="12" cy="5.5" r="1.3" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="12" cy="18.5" r="1.3" fill="currentColor" stroke="none"/>' },
  ellipsis: { body: '<circle cx="5.5" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="18.5" cy="12" r="1.3" fill="currentColor" stroke="none"/>' },
  filter: { body: '<path d="M3 5h18l-7 8v5.2l-4 2.3V13z"/>' },
  grid: { body: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/>' },
  plus: { body: '<path d="M12 5v14"/><path d="M5 12h14"/>' },
  mail: { body: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 7 8.5 6 8.5-6"/>' },
  rss: { body: '<path d="M4 11a9 9 0 0 1 9 9"/><path d="M4 4a16 16 0 0 1 16 16"/><circle cx="5.2" cy="18.8" r="1.4" fill="currentColor" stroke="none"/>' },
  globe: { body: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a13.5 13.5 0 0 1 0 18 13.5 13.5 0 0 1 0-18"/>' },
  alert: { body: '<path d="M10.3 4 2.6 18a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 4a2 2 0 0 0-3.4 0Z"/><path d="M12 9.5V14"/><path d="M12 17.2h.01"/>' },
  calendar: { body: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4"/><path d="M16 3v4"/><path d="M3 10.5h18"/>' },
  send: { body: '<path d="m21 3-9.5 9.5"/><path d="M21 3 14 21l-2.5-8.5L3 10z"/>' },
  eye: { body: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/>' },
  headphones: { body: '<path d="M4 14v-1a8 8 0 0 1 16 0v1"/><rect x="3" y="14" width="4" height="6" rx="1.5"/><rect x="17" y="14" width="4" height="6" rx="1.5"/>' },
  phone: { body: '<rect x="7" y="2.5" width="10" height="19" rx="2.2"/><path d="M11 18.5h2"/>' },
};

export function icon(name, opts = {}) {
  const def = ICONS[name];
  if (!def) throw new Error(`unknown icon: ${name}`);
  const size = opts.size ?? 17;
  const sw = opts.strokeWidth ?? 1.6;
  const cls = opts.cls ? `ti ti-${name} ${opts.cls}` : `ti ti-${name}`;
  const fill = def.fill ? "currentColor" : "none";
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="${fill}" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round" class="${cls}" aria-hidden="true">${def.body}</svg>`;
}
