'use strict';

/**
 * FIX 1 — Weekly realized-PnL aggregator (single source of truth).
 *
 * The old weekly summary read PnL from internal eventLog CLOSE rows, which
 * carry `pnl: 0` (monitor.js never back-filled realized PnL into the log).
 * With a churned tournament week that produced 4117 fills and $44.6M volume,
 * the summary collapsed to Net PnL +$0.00 / 0W-0L / 0.0% win rate — a set of
 * fabricated zeros that contradict the (correct) volume.
 *
 * The authoritative source of realized PnL on Hyperliquid is the per-fill
 * `closedPnl`: opening fills carry 0, closing/reducing fills carry the real
 * number; `fee` is per fill. This module aggregates DIRECTLY from HL fills so
 * the figure equals real, verified data computed once.
 *
 *   net_pnl   = Σ closedPnl − Σ fee   (over the week window)
 *   wins      = count(closedPnl > 0)        losses = count(closedPnl < 0)
 *   breakeven = count(closing fill, closedPnl == 0)
 *   win_rate  = wins / (wins + losses)   → null ("n/d") when denom is 0
 *   best/worst= symbol with max/min aggregated closedPnl
 *   volume    = Σ |px · sz|              (notional, all fills)
 *   fills     = raw fill count (labelled honestly, not as W/L-able "trades")
 */

const PNL_EPSILON = 1e-9; // treat |closedPnl| below this as a flat (0) close

function _num(v) {
  const n = typeof v === 'number' ? v : parseFloat(v);
  return Number.isFinite(n) ? n : 0;
}

function _isClosingFill(f) {
  // HL `dir` is human text: "Close Long", "Close Short", "Long > Short", …
  const dir = String(f && f.dir ? f.dir : '').toLowerCase();
  return dir.includes('close') || dir.includes('>');
}

/**
 * Pure aggregator over a pre-windowed fill array.
 *
 * @param {Array} fills HL fills [{coin, px, sz, time, closedPnl, fee, dir}]
 * @returns summary object (all numbers real; win_rate null when undefined)
 */
function aggregateFills(fills) {
  const list = Array.isArray(fills) ? fills : [];

  let grossPnl = 0;
  let totalFees = 0;
  let volume = 0;
  let wins = 0;
  let losses = 0;
  let breakeven = 0;
  const byCoin = new Map(); // coin -> aggregated closedPnl

  for (const f of list) {
    const cp = _num(f.closedPnl);
    const fee = _num(f.fee);
    const px = _num(f.px);
    const sz = Math.abs(_num(f.sz));

    grossPnl += cp;
    totalFees += fee;
    volume += px * sz;

    if (cp > PNL_EPSILON) wins++;
    else if (cp < -PNL_EPSILON) losses++;
    else if (_isClosingFill(f)) breakeven++;

    if (Math.abs(cp) > PNL_EPSILON) {
      byCoin.set(f.coin, (byCoin.get(f.coin) || 0) + cp);
    }
  }

  const realizedCloses = wins + losses + breakeven;
  const netPnl = grossPnl - totalFees;

  let best = null;
  let worst = null;
  for (const [coin, pnl] of byCoin.entries()) {
    if (best === null || pnl > best.pnl) best = { coin, pnl };
    if (worst === null || pnl < worst.pnl) worst = { coin, pnl };
  }

  const decided = wins + losses;
  const winRate = decided > 0 ? (wins / decided) * 100 : null; // null => n/d

  // FIX 3 — detect a calculation failure masquerading as a flat week: real
  // activity (fills + volume) but zero realized PnL AND zero decided closes.
  const calcFailure =
    list.length > 0 &&
    volume > 0 &&
    Math.abs(netPnl) < PNL_EPSILON &&
    decided === 0;

  return {
    fills: list.length,
    realized_closes: realizedCloses,
    wins,
    losses,
    breakeven,
    win_rate_pct: winRate, // number | null
    gross_pnl: grossPnl,
    total_fees: totalFees,
    net_pnl: netPnl,
    volume,
    best, // {coin, pnl} | null
    worst, // {coin, pnl} | null
    calc_failure: calcFailure,
  };
}

/** Monday-00:00-UTC of the week containing `date`. */
function startOfWeekUTC(date) {
  const d = new Date(date);
  const dow = d.getUTCDay();
  const monday = new Date(
    Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() - ((dow + 6) % 7))
  );
  monday.setUTCHours(0, 0, 0, 0);
  return monday;
}

/** ISO-8601 week number of `date`. */
function weekNumber(date) {
  const target = new Date(date.valueOf());
  const dayNr = (target.getUTCDay() + 6) % 7;
  target.setUTCDate(target.getUTCDate() - dayNr + 3);
  const firstThursday = new Date(Date.UTC(target.getUTCFullYear(), 0, 4));
  return (
    1 +
    Math.round(
      ((target - firstThursday) / 86400000 -
        3 +
        ((firstThursday.getUTCDay() + 6) % 7)) /
        7
    )
  );
}

/** Wallets whose fills compose the fund weekly. Env-configurable. */
function weeklyWalletAddresses() {
  const raw =
    process.env.WEEKLY_SUMMARY_WALLETS ||
    process.env.PRIMARY_WALLET_ADDRESS ||
    process.env.BCD_WALLET_ADDRESS ||
    process.env.BCD_WALLET ||
    '0xc7ae23316b47f7e75f455f53ad37873a18351505';
  return raw
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
}

/**
 * Fetch + aggregate the week's realized PnL across the fund wallets.
 *
 * @returns {Promise<{summary, startMs, endMs, fetchError:boolean}|null>}
 *   null when there were genuinely zero fills this week (skip the message);
 *   fetchError=true when the HL fetch hard-failed (render "fetch error",
 *   never a fabricated zero).
 */
async function buildWeekly(hlApi, { now = new Date() } = {}) {
  const start = startOfWeekUTC(now);
  const startMs = start.getTime();
  const endMs = now.getTime();
  const wallets = weeklyWalletAddresses();

  if (!hlApi || typeof hlApi.getUserFillsByTime !== 'function') {
    return { summary: null, startMs, endMs, fetchError: true };
  }

  let anyFetchFailed = false;
  let anyFetchOk = false;
  const all = [];
  for (const w of wallets) {
    const fills = await hlApi.getUserFillsByTime(w, startMs, endMs);
    if (fills === null) {
      anyFetchFailed = true;
      continue;
    }
    anyFetchOk = true;
    for (const f of fills) {
      const t = _num(f.time);
      if (t >= startMs && t <= endMs) all.push(f);
    }
  }

  // Every wallet failed to fetch → cannot compute, do not fabricate.
  if (anyFetchFailed && !anyFetchOk) {
    return { summary: null, startMs, endMs, fetchError: true };
  }

  if (all.length === 0) {
    return null; // genuinely no activity this week
  }

  return {
    summary: aggregateFills(all),
    startMs,
    endMs,
    fetchError: false,
    partial: anyFetchFailed, // some wallets fetched, some failed
  };
}

module.exports = {
  aggregateFills,
  startOfWeekUTC,
  weekNumber,
  weeklyWalletAddresses,
  buildWeekly,
  PNL_EPSILON,
};
