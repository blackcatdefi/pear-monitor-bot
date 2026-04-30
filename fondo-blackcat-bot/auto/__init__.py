"""R-FINAL — autonomous fix modules layered above existing bot code.

Each module here is an additive fix that supersedes a stale/buggy code path
in the legacy modules. They never mutate the legacy files; instead, the bot
opt-in imports the new helper at the relevant call site. Disabling each
module is a single env var:

    FUND_STATE_AUTODETECT=false   → fund_state_v2 falls back to legacy state
    HYPERLEND_AUTOREADER=false    → hyperlend_reader passthroughs to legacy
    BOOT_DEDUP_ENABLED=false      → boot_dedup.should_announce always True

Created 2026-04-30 (R-FINAL). See project memory for round context.
"""
