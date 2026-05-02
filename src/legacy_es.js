'use strict';

/**
 * R-PUBLIC — i18n.
 * Public bot speaks Spanish. Technical tokens (PnL, TP, SL, TWAP, basket) stay
 * in English because that's how Pear Protocol surfaces them.
 */

const MESSAGES = {
  es: {
    POSITION_CLOSED: 'Posición cerrada',
    POSITION_OPENED: 'Nueva posición abierta',
    NEW_BASKET: 'NUEVA BASKET ABIERTA',
    BASKET_CLOSED: 'BASKET CERRADA',
    TAKE_PROFIT_HIT: 'TAKE PROFIT alcanzado',
    STOP_LOSS_TRIGGERED: 'STOP LOSS activado',
    TRAILING_STOP_TRIGGERED: 'TRAILING STOP activado',
    MANUAL_CLOSE: 'Cierre manual',
    PARTIAL_CLOSE: 'Cierre parcial',
    COMPOUND_DETECTED: 'COMPOUNDING DETECTADO',
    WALLET: 'Wallet',
    ENTRY: 'Entrada',
    CLOSE: 'Cierre',
    PNL: 'PnL',
    LEVERAGE: 'Apalancamiento',
    NOTIONAL: 'Notional',
    POSITIONS: 'Posiciones',
    POSITIONS_LIST: 'Composición',
    TOTAL_PNL: 'PnL Total',
    DURATION: 'Duración',
    NOTIONAL_BEFORE: 'Notional anterior',
    NOTIONAL_NOW: 'Notional actual',
    GROWTH: 'Crecimiento',
    REFERRAL_CTA:
      'Usá este código para 10% off en fees de Pear Protocol',
    AMBASSADOR_TAGLINE: 'Pear Protocol Alerts · Community Bot',
    WEEKLY_SUMMARY_TITLE: 'RESUMEN SEMANAL — Performance',
    WEEKLY_WEEK: 'Semana',
    WEEKLY_PNL_NET: 'PnL Neto',
    WEEKLY_TRADES: 'Trades',
    WEEKLY_WIN_RATE: 'Win Rate',
    WEEKLY_VOLUME: 'Volumen',
    WEEKLY_FEES: 'Fees',
    WEEKLY_BEST: 'Mejor',
    WEEKLY_WORST: 'Peor',
    WEEKLY_FOLLOW_CTA:
      'Querés copiar este estilo? Usá el código para 10% off en Pear.',
    HEARTBEAT_OK: 'Pear Alerts Bot operativo',
    UPTIME: 'Uptime',
    ERRORS_24H: 'Errores 24h',
    LAST_POLL: 'Último poll',
    HISTORY_HEADER: 'ÚLTIMOS CIERRES',
    HISTORY_EMPTY: 'Sin cierres registrados.',
    PNL_PERIOD_HEADER: 'PnL',
    EXPORT_CAPTION: 'Export de cierres',
    STATUS_OK: 'Bot operativo',
    SUMMARY_FORCED: 'Forzando weekly summary...',
    PNL_DISCREPANCY: 'PnL DISCREPANCY DETECTADA',
  },
};

function t(key, lang) {
  const code = (lang || process.env.LANGUAGE || 'es').toLowerCase();
  const dict = MESSAGES[code] || MESSAGES.es;
  return dict[key] || key;
}

function isSpanish() {
  return (process.env.LANGUAGE || 'es').toLowerCase() === 'es';
}

module.exports = { t, MESSAGES, isSpanish };
