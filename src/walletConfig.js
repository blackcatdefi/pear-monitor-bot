'use strict';

/**
 * Round v2 — Multi-wallet awareness with labels.
 *
 * Identifies whether a given wallet is BCD's primary (which controls referral
 * footer behaviour) or a tracked referral wallet. The current canonical BCD
 * wallet for Pear basket is 0xc7AE...1505 — see auto-memory project_real_wallets.
 *
 * BCD_WALLET_ADDRESS env var overrides the default. Comparison is
 * case-insensitive.
 */

const DEFAULT_BCD_WALLET =
  '0xc7AE9550A37e72fed7B40dCC95Bd17e5BB1F1505';

function bcdWalletAddress() {
  return (process.env.BCD_WALLET_ADDRESS || DEFAULT_BCD_WALLET).toLowerCase();
}

function isPrimaryWallet(address) {
  if (!address) return false;
  return String(address).toLowerCase() === bcdWalletAddress();
}

function labelFor(address, fallback) {
  if (isPrimaryWallet(address)) return 'BCD';
  return fallback || (address ? `${address.slice(0, 6)}…${address.slice(-4)}` : '?');
}

function isTrackedReferral(address) {
  // Future: list referrals via env var REFERRAL_WALLETS=0x..,0x..
  const raw = process.env.REFERRAL_WALLETS || '';
  if (!raw) return false;
  return raw
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean)
    .includes(String(address || '').toLowerCase());
}

module.exports = {
  bcdWalletAddress,
  isPrimaryWallet,
  isTrackedReferral,
  labelFor,
  DEFAULT_BCD_WALLET,
};
