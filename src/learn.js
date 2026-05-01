'use strict';

/**
 * R-AUTOCOPY — Tutorials. Pure data + a pagination helper.
 *
 * Each lesson is a Markdown body. Pagination buttons are wired by
 * commandsLearn.js — this module is content-only so tests don't need
 * a bot instance.
 */

const LESSONS = [
  {
    id: 1,
    title: '📘 Cómo trackear una wallet (30s)',
    body: [
      '*Cómo trackear una wallet*',
      '',
      '1️⃣ Tocá /track o el botón *🎯 Trackear wallet* en /start.',
      '2️⃣ Pegá la dirección de la wallet (formato `0x...` con 40 hex).',
      '3️⃣ Opcional: ponele un alias (ej. "Whale 1") o tocá /skip.',
      '4️⃣ Listo — recibís alertas cuando esa wallet abra/cierre baskets.',
      '',
      '_Podés trackear hasta 10 wallets (25 si activás Premium con 3 referidos)._',
    ].join('\n'),
  },
  {
    id: 2,
    title: '📗 Cómo configurar copy auto (1min)',
    body: [
      '*Cómo configurar copy auto*',
      '',
      '1️⃣ Tocá /copy_auto.',
      '2️⃣ Setea tu *capital por signal* con `/capital 500` (entre $10 y $50K).',
      '3️⃣ Elegí modo:',
      '  • *MANUAL* — recibís alert con botón "Copiar en Pear"',
      '  • *AUTO* — alert pre-armado listo para 1-tap firma',
      '4️⃣ Activá con el toggle *🚦 ON / OFF*.',
      '',
      'Cuando llega una signal a @BlackCatDeFiSignals, recibís el link directo a Pear con tu capital pre-cargado y la basket exacta.',
      '',
      '⚠️ *Vos firmás siempre desde tu wallet* — Pear no expone API pública de execution. Esa es la única forma legítima.',
    ].join('\n'),
  },
  {
    id: 3,
    title: '📕 Qué es una basket en Pear (2min)',
    body: [
      '*Qué es una basket*',
      '',
      'Una basket en Pear es un conjunto de posiciones del mismo lado (todas SHORT o todas LONG) sobre varios tokens de un mismo "tipo" (ej. memecoins, L2s, AI, etc.).',
      '',
      '*Ventajas vs. trades sueltos:*',
      '  • Diversificación intra-tema',
      '  • Reduce riesgo idiosincrático de un solo token',
      '  • Stops y trailing aplican al basket completo',
      '',
      '*Ejemplo:* basket SHORT de L2s = SHORT en ARB+OP+DYDX+PYTH+ENA. Si la narrative se rompe, todos caen juntos → ganás más; si no, el SL te protege.',
    ].join('\n'),
  },
  {
    id: 4,
    title: '📙 Risk management 101 (3min)',
    body: [
      '*Risk management 101*',
      '',
      '🎯 *SL (Stop Loss)*: % máximo que estás dispuesto a perder. Default 50% del capital de la basket.',
      '',
      '📈 *Trailing Stop*: SL que se mueve a tu favor cuando estás en ganancia.',
      '  • *Activación*: % de profit a partir del cual el trailing empieza (default 30%).',
      '  • *Distancia*: cuánto deja correr antes de cerrar (default 10%).',
      '',
      '⚡ *Leverage*: multiplica ganancia y pérdida por igual. Default 4x = ganás/perdés 4x el movimiento del precio.',
      '',
      '⏱️ *TWAP* (Time-Weighted Avg Price): partir tu entrada en N "bullets" durante X horas → mejor precio promedio que un market order de un golpe.',
      '',
      '_Regla: nunca riesgues más del 1-2% de tu capital total en un solo trade._',
    ].join('\n'),
  },
  {
    id: 5,
    title: '📒 Cómo leer signals oficiales (1min)',
    body: [
      '*Cómo leer signals oficiales*',
      '',
      'Cuando @BlackCatDeFiSignals publica una signal, vas a ver:',
      '',
      '🚀 *SIGNAL OFICIAL #N*',
      '📊 *Basket*: lista de tokens con su lado (LONG/SHORT)',
      '⚡ *Leverage*: cuántos x apalancar',
      '🎯 *SL / Trailing*: configuración de risk',
      '⏱️ *TWAP*: cómo entrar (horas + bullets)',
      '',
      'Si tenés copy auto ON, recibís la signal personalizada con tu capital y el botón directo a Pear.',
      '',
      'Si no, tocá /signals para suscribirte al canal manualmente.',
    ].join('\n'),
  },
];

function getLessonCount() {
  return LESSONS.length;
}

function getLesson(idx) {
  if (idx < 0 || idx >= LESSONS.length) return null;
  return LESSONS[idx];
}

function getAllTitles() {
  return LESSONS.map((l, i) => ({ idx: i, id: l.id, title: l.title }));
}

function buildKeyboard(idx) {
  const total = LESSONS.length;
  const row = [];
  if (idx > 0) row.push({ text: '◀️ Anterior', callback_data: `learn:nav:${idx - 1}` });
  if (idx < total - 1) row.push({ text: 'Siguiente ▶️', callback_data: `learn:nav:${idx + 1}` });
  const rows = [];
  if (row.length > 0) rows.push(row);
  rows.push([{ text: '✖️ Salir', callback_data: 'learn:exit' }]);
  return { inline_keyboard: rows };
}

function formatLesson(idx) {
  const l = getLesson(idx);
  if (!l) return '⚠️ Lección no encontrada.';
  return `${l.body}\n\n_Lección ${idx + 1} de ${LESSONS.length}_`;
}

function formatIndex() {
  const lines = [
    '📚 *Aprendé el bot*',
    '',
    'Tutoriales cortos — tocá uno para empezar:',
    '',
  ];
  LESSONS.forEach((l, idx) => {
    lines.push(`${idx + 1}. ${l.title}`);
  });
  return lines.join('\n');
}

function buildIndexKeyboard() {
  const rows = LESSONS.map((l, idx) => [
    { text: l.title, callback_data: `learn:nav:${idx}` },
  ]);
  return { inline_keyboard: rows };
}

module.exports = {
  LESSONS,
  getLessonCount,
  getLesson,
  getAllTitles,
  buildKeyboard,
  formatLesson,
  formatIndex,
  buildIndexKeyboard,
};
