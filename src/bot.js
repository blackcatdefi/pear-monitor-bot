const TelegramBot = require('node-telegram-bot-api');
const {
  getWallets, addWallet, removeWallet,
  getBorrowWallets, addBorrowWallet, removeBorrowWallet,
  shortenAddress,
} = require('./store');

function createBot(token, hlApi, monitor, hlendApi = null) {
  const bot = new TelegramBot(token, { polling: true });

  // R-PUBLIC-START-FIX: log polling errors so they're visible in Railway logs
  // and don't silently kill the update stream.
  bot.on('polling_error', (err) => {
    console.error('[bot] polling_error:', err && err.message ? err.message : err);
  });

  // Register bot commands menu (R-AUTOCOPY: 14 user cmds + 11 operator cmds)
  // R-PUBLIC-START-FIX: must use .catch() — unawaited rejections crash Node 20
  // before any onText/callback_query handlers are registered.
  bot.setMyCommands([
    { command: 'start',         description: '🍐 Start' },
    { command: 'track',         description: '🎯 Track external wallets' },
    { command: 'signals',       description: '📡 Official signals channel' },
    { command: 'copy_auto',     description: '🤖 Copy auto (MANUAL/AUTO)' },
    { command: 'capital',       description: '💰 Capital per signal' },
    { command: 'timezone',      description: '🌐 Timezone' },
    { command: 'portfolio',     description: '📊 Your portfolio (read-only)' },
    { command: 'leaderboard',   description: '🏆 Top tracked wallets' },
    { command: 'alerts_config', description: '🔔 Alert granularity' },
    { command: 'stats',         description: '📈 Your personal stats' },
    { command: 'share',         description: '🎁 Invite friends' },
    { command: 'learn',         description: '📚 Tutorials' },
    { command: 'feedback',      description: '💬 Support / suggestions' },
    { command: 'help',          description: '🆘 Help' },
    // Operator commands (BCD personal)
    { command: 'menu',          description: '⚙️ Operator menu' },
    { command: 'positions',     description: '📊 Open positions' },
    { command: 'balance',       description: '💰 Available funds' },
    { command: 'wallets',       description: '📋 Monitored wallets' },
    { command: 'check',         description: '🔍 Check now' },
    { command: 'borrow',        description: '🏦 HyperLend Borrow' },
    { command: 'history',       description: '📜 Recent closes' },
    { command: 'pnl',           description: '💰 PnL by period' },
    { command: 'status',        description: '✅ Bot health' },
    { command: 'export',        description: '📤 Export CSV' },
    { command: 'summary',       description: '📊 Weekly summary' },
    { command: 'healthcheck',   description: '✅ Health check' },
  ]).catch((err) => {
    console.error('[bot] setMyCommands failed (non-fatal):', err && err.message ? err.message : err);
  });

  function mainMenu() {
    return {
      reply_markup: {
        inline_keyboard: [
          [{ text: '📊 Positions', callback_data: 'status' }, { text: '💰 Balance', callback_data: 'balance' }],
          [{ text: '📋 My Wallets', callback_data: 'wallets' }, { text: '🔍 Check Now', callback_data: 'check' }],
          [{ text: '➕ Add Wallet', callback_data: 'add_wallet' }, { text: '➖ Remove Wallet', callback_data: 'remove_wallet' }],
          [{ text: '🏦 HyperLend Borrow Available', callback_data: 'borrow_menu' }],
        ]
      },
      parse_mode: 'Markdown'
    };
  }

  function borrowMenu() {
    return {
      reply_markup: {
        inline_keyboard: [
          [{ text: '💸 Check Borrow Power', callback_data: 'borrow_status' }],
          [{ text: '➕ Add Borrow Wallet', callback_data: 'borrow_add' }, { text: '➖ Remove Borrow Wallet', callback_data: 'borrow_remove' }],
          [{ text: '📋 My Borrow Wallets', callback_data: 'borrow_list' }],
          [{ text: '◀️ Back', callback_data: 'menu' }],
        ]
      },
      parse_mode: 'Markdown'
    };
  }

  // /menu - inline-keyboard for personal wallet management.
  // /start is handled by commandsStart.js (R-START) — see extensions.js.
  // /menu intentionally still uses the legacy mainMenu so the bot operator
  // (BCD) keeps the Add/Remove Wallet flow they were used to.
  bot.onText(/^\/menu(?:@\w+)?$/i, (msg) => {
    const chatId = msg.chat.id;
    const wallets = getWallets(chatId);

    const lines = [
      '🍐 *Pear Protocol Monitor — Menu*',
      '',
      'I alert you when something important happens on your wallets:',
      '',
      '🎯 *Take Profit* hit',
      '🛑 *Stop Loss* triggered',
      '💰 *Available funds* to trade',
      '🏦 *Borrow available* on HyperLend',
    ];

    if (wallets.length > 0) {
      lines.push('', `✅ You have *${wallets.length} wallet(s)* monitored.`);
    } else {
      lines.push('', 'Tap ➕ *Add Wallet* to get started.');
    }

    bot.sendMessage(chatId, lines.join('\n'), mainMenu());
  });

  const waitingFor = {};

  // Handle button presses
  bot.on('callback_query', async (query) => {
    // R-PUBLIC-START-FIX: query.message can be null in inline mode or when
    // the originating message was deleted. Guard before accessing .chat.id to
    // prevent an unhandled rejection that crashes Node 20 and kills all handlers.
    if (!query.message) return;
    const chatId = query.message.chat.id;
    try {
      await bot.answerCallbackQuery(query.id);
    } catch (_) {
      // query already answered or expired — safe to ignore
    }

    switch (query.data) {
      case 'status':
        await handleStatus(chatId);
        break;
      case 'balance':
        await handleBalance(chatId);
        break;
      case 'wallets':
        await handleWallets(chatId);
        break;
      case 'check':
        await handleCheck(chatId);
        break;
      case 'add_wallet':
        waitingFor[chatId] = 'add_wallet';
        bot.sendMessage(chatId, '📝 Send me the wallet address to monitor:\n\n`0x...`', { parse_mode: 'Markdown' });
        break;
      case 'remove_wallet': {
        const wallets = getWallets(chatId);
        if (wallets.length === 0) {
          bot.sendMessage(chatId, 'No wallets to remove.');
          break;
        }
        const buttons = wallets.map(w => ([{
          text: `❌ ${w.label} (${shortenAddress(w.address)})`,
          callback_data: `rm_${w.address}`
        }]));
        buttons.push([{ text: '◀️ Back', callback_data: 'menu' }]);
        bot.sendMessage(chatId, 'Tap a wallet to remove:', {
          reply_markup: { inline_keyboard: buttons }
        });
        break;
      }
      case 'menu':
        bot.sendMessage(chatId, '🍐 *Main Menu*', mainMenu());
        break;
      case 'borrow_menu':
        bot.sendMessage(chatId, [
          '🏦 *HyperLend Borrow Available*',
          '',
          'Add your HyperEVM wallet and I\'ll alert you whenever you have',
          `≥ $${monitor.minBorrowAvailable} available to borrow on HyperLend.`,
        ].join('\n'), borrowMenu());
        break;
      case 'borrow_status':
        await handleBorrowStatus(chatId);
        break;
      case 'borrow_list':
        handleBorrowList(chatId);
        break;
      case 'borrow_add':
        if (!hlendApi) {
          bot.sendMessage(chatId, '⚠️ HyperLend monitoring is not enabled on this server.', borrowMenu());
          break;
        }
        waitingFor[chatId] = 'add_borrow_wallet';
        bot.sendMessage(chatId, '📝 Send me the HyperEVM wallet address:\n\n`0x...`', { parse_mode: 'Markdown' });
        break;
      case 'borrow_remove': {
        const bw = getBorrowWallets(chatId);
        if (bw.length === 0) { bot.sendMessage(chatId, 'No borrow wallets to remove.', borrowMenu()); break; }
        const buttons = bw.map(w => ([{
          text: `❌ ${w.label} (${shortenAddress(w.address)})`,
          callback_data: `brm_${w.address}`
        }]));
        buttons.push([{ text: '◀️ Back', callback_data: 'borrow_menu' }]);
        bot.sendMessage(chatId, 'Tap a borrow wallet to remove:', {
          reply_markup: { inline_keyboard: buttons }
        });
        break;
      }
    }

    if (query.data.startsWith('rm_0x')) {
      const addr = query.data.slice(3);
      removeWallet(chatId, addr);
      bot.sendMessage(chatId, '✅ Wallet removed.', mainMenu());
    }

    if (query.data.startsWith('brm_0x')) {
      const addr = query.data.slice(4);
      removeBorrowWallet(chatId, addr);
      bot.sendMessage(chatId, '✅ Borrow wallet removed.', borrowMenu());
    }
  });

  // Shortcut commands
  bot.onText(/\/positions/, async (msg) => { await handleStatus(msg.chat.id); });
  bot.onText(/\/balance/, async (msg) => { await handleBalance(msg.chat.id); });
  bot.onText(/\/wallets/, (msg) => { handleWallets(msg.chat.id); });
  bot.onText(/\/check/, async (msg) => { await handleCheck(msg.chat.id); });
  bot.onText(/\/borrow/, (msg) => {
    bot.sendMessage(msg.chat.id, [
      '🏦 *HyperLend Borrow Available*',
      '',
      'Add your HyperEVM wallet and I\'ll alert you whenever you have',
      `≥ $${monitor.minBorrowAvailable} available to borrow on HyperLend.`,
    ].join('\n'), borrowMenu());
  });

  // Handle text messages (add wallet flow)
  bot.on('message', async (msg) => {
    if (msg.text?.startsWith('/')) return;
    const chatId = msg.chat.id;

    if (waitingFor[chatId] === 'add_wallet') {
      const text = msg.text?.trim();
      if (!text || !/^0x[a-fA-F0-9]{40}$/.test(text)) {
        bot.sendMessage(chatId, '❌ Invalid address. Send a valid wallet like:\n`0x1234...abcd`', { parse_mode: 'Markdown' });
        return;
      }
      delete waitingFor[chatId];
      waitingFor[chatId] = { step: 'add_label', address: text };
      bot.sendMessage(chatId, 'Got it! Now send a *name* for this wallet (or tap Skip):', {
        parse_mode: 'Markdown',
        reply_markup: { inline_keyboard: [[{ text: '⏭️ Skip', callback_data: 'skip_label' }]] }
      });
      return;
    }

    if (waitingFor[chatId]?.step === 'add_label') {
      const { address } = waitingFor[chatId];
      const label = msg.text?.trim() || shortenAddress(address);
      delete waitingFor[chatId];
      await finishAddWallet(chatId, address, label);
      return;
    }

    if (waitingFor[chatId] === 'add_borrow_wallet') {
      const text = msg.text?.trim();
      if (!text || !/^0x[a-fA-F0-9]{40}$/.test(text)) {
        bot.sendMessage(chatId, '❌ Invalid address. Send a valid wallet like:\n`0x1234...abcd`', { parse_mode: 'Markdown' });
        return;
      }
      delete waitingFor[chatId];
      waitingFor[chatId] = { step: 'add_borrow_label', address: text };
      bot.sendMessage(chatId, 'Got it! Now send a *name* for this borrow wallet (or tap Skip):', {
        parse_mode: 'Markdown',
        reply_markup: { inline_keyboard: [[{ text: '⏭️ Skip', callback_data: 'skip_borrow_label' }]] }
      });
      return;
    }

    if (waitingFor[chatId]?.step === 'add_borrow_label') {
      const { address } = waitingFor[chatId];
      const label = msg.text?.trim() || shortenAddress(address);
      delete waitingFor[chatId];
      await finishAddBorrowWallet(chatId, address, label);
      return;
    }
  });

  bot.on('callback_query', async (query) => {
    if (query.data === 'skip_label') {
      const chatId = query.message.chat.id;
      await bot.answerCallbackQuery(query.id);
      if (waitingFor[chatId]?.step === 'add_label') {
        const { address } = waitingFor[chatId];
        delete waitingFor[chatId];
        await finishAddWallet(chatId, address, shortenAddress(address));
      }
    }
    if (query.data === 'skip_borrow_label') {
      const chatId = query.message.chat.id;
      await bot.answerCallbackQuery(query.id);
      if (waitingFor[chatId]?.step === 'add_borrow_label') {
        const { address } = waitingFor[chatId];
        delete waitingFor[chatId];
        await finishAddBorrowWallet(chatId, address, shortenAddress(address));
      }
    }
  });

  async function finishAddBorrowWallet(chatId, address, label) {
    if (!hlendApi) {
      bot.sendMessage(chatId, '⚠️ HyperLend monitoring is not enabled on this server.', mainMenu());
      return;
    }
    bot.sendMessage(chatId, '🔄 Verifying on HyperLend...');
    try {
      const data = await hlendApi.getAccountData(address);
      addBorrowWallet(chatId, address, label);
      const hf = data.healthFactor === Infinity ? '∞' : data.healthFactor.toFixed(2);
      bot.sendMessage(chatId, [
        `✅ *${label}* added to HyperLend monitoring!`, ``,
        `🔒 Collateral: $${data.totalCollateralUsd.toFixed(2)}`,
        `💳 Debt: $${data.totalDebtUsd.toFixed(2)}`,
        `💸 Available to borrow: $${data.availableBorrowsUsd.toFixed(2)}`,
        `❤️ Health factor: ${hf}`,
        ``,
        `I'll alert you whenever available borrow crosses ≥ $${monitor.minBorrowAvailable}.`,
      ].join('\n'), borrowMenu());
    } catch (e) {
      console.error('HyperLend verify error:', e.message);
      addBorrowWallet(chatId, address, label);
      bot.sendMessage(chatId, `⚠️ Couldn't reach HyperLend right now, but *${label}* was saved and will be monitored.`, borrowMenu());
    }
  }

  async function handleBorrowStatus(chatId) {
    const wallets = getBorrowWallets(chatId);
    if (wallets.length === 0) {
      bot.sendMessage(chatId, 'No borrow wallets yet. Tap ➕ Add Borrow Wallet!', borrowMenu());
      return;
    }
    if (!hlendApi) {
      bot.sendMessage(chatId, '⚠️ HyperLend monitoring is not enabled on this server.', borrowMenu());
      return;
    }
    for (const wallet of wallets) {
      try {
        const data = await hlendApi.getAccountData(wallet.address);
        const hf = data.healthFactor === Infinity ? '∞' : data.healthFactor.toFixed(2);
        await bot.sendMessage(chatId, [
          `🏦 *${wallet.label}* — HyperLend`, ``,
          `💸 Available to borrow: *$${data.availableBorrowsUsd.toFixed(2)}*`,
          `🔒 Collateral: $${data.totalCollateralUsd.toFixed(2)}`,
          `💳 Debt: $${data.totalDebtUsd.toFixed(2)}`,
          `❤️ Health factor: ${hf}`,
          `📐 LTV: ${(data.ltv * 100).toFixed(1)}%`,
        ].join('\n'), { parse_mode: 'Markdown' });
      } catch (e) {
        await bot.sendMessage(chatId, `📍 *${wallet.label}*: Error fetching HyperLend data (${e.message})`, { parse_mode: 'Markdown' });
      }
    }
  }

  function handleBorrowList(chatId) {
    const wallets = getBorrowWallets(chatId);
    if (wallets.length === 0) {
      bot.sendMessage(chatId, 'No borrow wallets yet. Tap ➕ Add Borrow Wallet!', borrowMenu());
      return;
    }
    const list = wallets.map((w, i) => `${i + 1}. *${w.label}*\n   \`${w.address}\``).join('\n\n');
    bot.sendMessage(chatId, `🏦 *HyperLend borrow wallets:*\n\n${list}`, { parse_mode: 'Markdown' });
  }

  async function finishAddWallet(chatId, address, label) {
    bot.sendMessage(chatId, '🔄 Verifying wallet...');
    const allStates = await hlApi.getAllClearinghouseStates(address);

    if (!allStates || allStates.length === 0) {
      bot.sendMessage(chatId, '⚠️ Wallet not found on Hyperliquid yet. Added anyway — it will be monitored once active.', mainMenu());
      addWallet(chatId, address, label);
      return;
    }

    addWallet(chatId, address, label);
    const agg = hlApi.aggregateBalances(allStates);
    const positions = hlApi.aggregatePositions(allStates);

    bot.sendMessage(chatId, [
      `✅ *${label}* added!`,
      ``,
      `💵 Account: $${agg.totalAccountValue.toFixed(2)}`,
      `📊 Open positions: ${positions.length} (across ${allStates.length} market(s))`,
      ``,
      `Monitoring started. You'll get notified automatically!`
    ].join('\n'), mainMenu());
  }

  async function handleStatus(chatId) {
    const wallets = getWallets(chatId);
    if (wallets.length === 0) {
      bot.sendMessage(chatId, 'No wallets yet. Tap ➕ Add Wallet to get started!', mainMenu());
      return;
    }

    for (const wallet of wallets) {
      const allStates = await hlApi.getAllClearinghouseStates(wallet.address);
      if (!allStates || allStates.length === 0) {
        bot.sendMessage(chatId, `📍 *${wallet.label}*: Could not fetch data`, { parse_mode: 'Markdown' });
        continue;
      }

      const positions = hlApi.aggregatePositions(allStates);
      if (positions.length === 0) {
        bot.sendMessage(chatId, `📍 *${wallet.label}*: No open positions`, { parse_mode: 'Markdown' });
        continue;
      }

      let text = `📍 *${wallet.label}* — ${positions.length} position(s):\n\n`;
      for (const pos of positions) {
        const pnl = pos.unrealizedPnl;
        const pnlStr = pnl >= 0 ? `+$${pnl.toFixed(2)}` : `-$${Math.abs(pnl).toFixed(2)}`;
        const pnlEmoji = pnl >= 0 ? '🟢' : '🔴';
        const roe = (pos.returnOnEquity * 100).toFixed(1);
        const dexTag = pos.dex !== 'Native' ? ` _(${pos.dexDisplay || pos.dex})_` : '';

        text += [
          `🪙 *${pos.coin}*${dexTag} ${pos.side}`,
          `   Entry: $${pos.entryPrice.toFixed(2)}`,
          `   ${pnlEmoji} PnL: ${pnlStr} (${roe}%)`,
          pos.leverage ? `   Leverage: ${pos.leverage}x` : '',
          pos.liquidationPrice ? `   Liq: $${pos.liquidationPrice.toFixed(2)}` : '',
          ''
        ].filter(Boolean).join('\n');
      }

      bot.sendMessage(chatId, text, { parse_mode: 'Markdown' });
    }
  }

  async function handleBalance(chatId) {
    const wallets = getWallets(chatId);
    if (wallets.length === 0) {
      bot.sendMessage(chatId, 'No wallets yet. Tap ➕ Add Wallet to get started!', mainMenu());
      return;
    }

    for (const wallet of wallets) {
      const allStates = await hlApi.getAllClearinghouseStates(wallet.address);
      if (!allStates || allStates.length === 0) {
        bot.sendMessage(chatId, `📍 *${wallet.label}*: Error`, { parse_mode: 'Markdown' });
        continue;
      }

      const agg = hlApi.aggregateBalances(allStates);
      let text = [
        `📍 *${wallet.label}*`, ``,
        `💵 Available: $${agg.totalWithdrawable.toFixed(2)}`,
        `📊 Account value: $${agg.totalAccountValue.toFixed(2)}`,
        `📈 Margin used: $${agg.totalMarginUsed.toFixed(2)}`,
      ];

      if (agg.perDex.length > 1) {
        text.push('', '*Breakdown:*');
        for (const d of agg.perDex) {
          text.push(`  ${d.dexDisplay || d.dex}: $${d.accountValue.toFixed(2)} (margin: $${d.totalMarginUsed.toFixed(2)})`);
        }
      }

      bot.sendMessage(chatId, text.join('\n'), { parse_mode: 'Markdown' });
    }
  }

  function handleWallets(chatId) {
    const wallets = getWallets(chatId);
    if (wallets.length === 0) {
      bot.sendMessage(chatId, 'No wallets yet. Tap ➕ Add Wallet!', mainMenu());
      return;
    }
    const list = wallets.map((w, i) => `${i + 1}. *${w.label}*\n   \`${w.address}\``).join('\n\n');
    bot.sendMessage(chatId, `📋 *Monitored wallets:*\n\n${list}`, { parse_mode: 'Markdown' });
  }

  async function handleCheck(chatId) {
    bot.sendMessage(chatId, '🔍 Checking all wallets...');
    // Run poll just for this user
    const wallets = getWallets(chatId);
    const { loadState, saveState } = require('./store');
    const state = loadState(chatId);
    for (const wallet of wallets) {
      try {
        await monitor.checkWallet(chatId, wallet, state, false);
      } catch (e) {
        console.error(`Check error: ${e.message}`);
      }
    }
    saveState(chatId, state);
    bot.sendMessage(chatId, '✅ Done!', mainMenu());
  }

  async function sendNotification(chatId, message, opts = {}) {
    // R-NOSPAM: opts is forwarded to Telegram Bot API. Important fields:
    //   - disable_notification: silent push (no sound) — used by borrow gate
    //   - parse_mode: Markdown by default, can be overridden
    // The wrappedNotify upstream (extensions.js) may also pass `silent`
    // (legacy alias for disable_notification).
    const merged = { parse_mode: 'Markdown', ...opts };
    if (merged.silent === true && merged.disable_notification === undefined) {
      merged.disable_notification = true;
    }
    delete merged.silent; // node-telegram-bot-api rejects unknown fields
    try {
      await bot.sendMessage(chatId, message, merged);
    } catch {
      try {
        // Fallback without parse_mode if Markdown choked the message
        const fallback = { ...merged };
        delete fallback.parse_mode;
        await bot.sendMessage(chatId, message, fallback);
      } catch (e) {
        console.error(`Failed to send to ${chatId}:`, e.message);
      }
    }
  }

  return { bot, sendNotification };
}

module.exports = createBot;
