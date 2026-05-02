'use strict';

/**
 * R-EN — Tiny i18n helper for the public bot.
 *
 * Usage:
 *   const { t } = require('./i18n');
 *   t('start.recurring_welcome')                       // "Welcome back 👋"
 *   t('track.saved_with_label', { addr, label })       // interpolates {addr} {label}
 *   t('start.list_total', { count, max })              // numeric vars are stringified
 *
 * Design notes:
 *   • Single supported locale (English). Multi-locale support is intentionally
 *     deferred — the bot's audience is international/English-speaking.
 *   • Returns `[MISSING_STRING: <key>]` if the key is missing so that broken
 *     keys are visible in screenshots, not silent.
 *   • `collectAllStrings()` is exported for tests (Spanish-leak detection,
 *     placeholder consistency).
 */

const en = require('./en');

function t(key, vars) {
  if (!key || typeof key !== 'string') return '';
  const path = key.split('.');
  let cursor = en;
  for (const segment of path) {
    if (cursor && typeof cursor === 'object' && segment in cursor) {
      cursor = cursor[segment];
    } else {
      return `[MISSING_STRING: ${key}]`;
    }
  }
  if (typeof cursor !== 'string') {
    return `[MISSING_STRING: ${key}]`;
  }
  if (!vars || typeof vars !== 'object') return cursor;
  return Object.entries(vars).reduce((s, [k, v]) => {
    const re = new RegExp(`\\{${k}\\}`, 'g');
    return s.replace(re, v == null ? '' : String(v));
  }, cursor);
}

/**
 * Walk the en dictionary and yield every leaf string with its dotted path.
 * Used by tests + sanitizer.
 */
function collectAllStrings(node, prefix) {
  if (node === undefined) node = en;
  if (prefix === undefined) prefix = '';
  const out = [];
  if (typeof node === 'string') {
    out.push({ key: prefix, value: node });
    return out;
  }
  if (node && typeof node === 'object') {
    for (const k of Object.keys(node)) {
      const child = node[k];
      const childPrefix = prefix ? `${prefix}.${k}` : k;
      out.push(...collectAllStrings(child, childPrefix));
    }
  }
  return out;
}

/**
 * Returns the regex used to detect Spanish leaks in a string.
 * Centralized so tests + sanitizer share the same definition.
 */
function spanishLeakRegex() {
  // Accented chars + opening punct + a curated list of common Spanish markers
  // that occur in this codebase. Word boundaries used to avoid false-positives
  // on English words that happen to contain these substrings.
  return /[áéíóúñ¿¡]|\b(Bienvenido|Hola|trackead\w*|configurad\w*|wallets propias|tu zona|próximo|primer|último|gracias|cancelar|continuar|aceptar|rechazar|guardar|eliminar|cambiar|setear|activar|desactivar|silenciar|mensaje|recibir|enviar|copiar|ganar|perder|capital|monto|cantidad|paso|ayuda|tutorial|seleccionar|elegir|disponible|máximo|mínimo|requerido|opcional|obligatorio|automático|manual)\b/i;
}

/**
 * Stricter detector — only the unambiguous Spanish characters + the highly
 * idiomatic markers that won't false-positive on English. Use this for the
 * sanitizer + production guards.
 */
function strictSpanishRegex() {
  return /[áéíóúñ¿¡]|\b(Bienvenido|Bienvenida|Hola|trackead\w*|configurad\w*|wallets propias|próximo|último|usá|tocá|querés|podés|recibís|tenés|escribí|elegí|setear)\b/i;
}

module.exports = { t, collectAllStrings, spanishLeakRegex, strictSpanishRegex };
