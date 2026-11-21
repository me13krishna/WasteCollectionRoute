/** Small shared helpers. */

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/** Escape a value for safe interpolation into HTML (OSM / CSV names are untrusted). */
export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ESC[c]);
}

/** Read a CSS custom property from :root (follows the active theme). */
export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function fmt(n, digits = 1) {
  const v = Number(n) || 0;
  return v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
