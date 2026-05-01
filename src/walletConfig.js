'use strict';

/**
 * Wallet configuration utility.
 *
 * Identifies whether a given wallet is the operator's primary wallet (which
 * controls some optional branding/footer behavior). The address is set via
 * the env var PRIMARY_WALLET_ADDRESS. Without it, no wallet is treated as
 * primary and all messages render the standard public footer.
 *
 * Legacy env var BCD_WALLET_ADDRESS is still honored for backward compat.
 * Comparison is case-insensitive.
 */

function primaryWalletAddress() {
  const raw =
    process.env.PRIMARY_WALLET_ADDRESS ||
    process.env.BCD_WALLET_ADDRESS ||
    '';
  return String(raw).toLowerCase();
}

function isPrimaryWallet(address) {
  if (!address) return false;
  const primary = primaryWalletAddress();
  if (!primary) return false;
  return String(address).toLowerCase() === primary;
}

function labelFor(address, fallback) {
  if (isPrimaryWallet(address)) return fallback || 'Primary';
  return fallback || (address ? `${address.slice(0, 6)}…${address.slice(-4)}` : '?');
}

function isTrackedReferral(address) {
  // Optional list configured via env var REFERRAL_WALLETS=0x..,0x..
  const raw = process.env.REFERRAL_WALLETS || '';
  if (!raw) return false;
  return raw
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean)
    .includes(String(address || '').toLowerCase());
}

module.exports = {
  primaryWalletAddress,
  // legacy alias kept for any caller still importing bcdWalletAddress
  bcdWalletAddress: primaryWalletAddress,
  isPrimaryWallet,
  isTrackedReferral,
  labelFor,
};
