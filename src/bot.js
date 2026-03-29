const TelegramBot = require('node-telegram-bot-api');
const { getWallets, addWallet, removeWallet, shortenAddress } = require('./store');

function createBot(token, hlApi, monitor) {
  const bot = new TelegramBot(token, { polling: true });

  // Register bot commands menu
  bot.setMyCommands([
    { command: 'start', description: '🍐 Get started' },
    { command: 'menu', description: '🍐 Open main menu' },
    { command: 'positions', description: '📊 View open positions' },
    { command: 'balance', description: '💰 Check available funds' },
    { command: 'wallets', description: '📋 List monitored wallets' },
    { command: 'check', description: '🔍 Force check now' },
  ]);

  function mainMenu() {
    return {
      reply_markup: {
        inline_keyboard: [
          [{ text: '📊 Positions', callback_data: 'status' }, { text: '💰 Balance', callback_data: 'balance' }],
          [{ text: '📋 My Wallets', callback_data: 'wallets' }, { text: '🔍 Check Now', callback_data: 'check' }],
          [{ text: '➕ Add Wallet', callback_data: 'add_wallet' }, { text: '➖ Remove Wallet', callback_data: 'remove_wallet' }],
        ]
      },
      parse_mode: 'Markdown'
    };
  }

  // /start and /menu
  bot.onText(/\/(start|menu)/, (msg) => {
    bot.sendMessage(msg.chat.id, [
      '🍐 *Pear Protocol Monitor*',
      '',
      'I watch your positions on Pear/Hyperliquid and notify you when:',
      '',
      '🎯 Your *Take Profit* hits',
      '🛑 Your *Stop Loss* triggers',
      '💰 You have *funds available* to trade',
      '',
      'Tap a button below to get started!',
    ].join('\n'), mainMenu());
  });

  const waitingFor = {};

  // Handle button presses
  bot.on('callback_query', async (query) => {
    const chatId = query.message.chat.id;
    await bot.answerCallbackQuery(query.id);

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
    }

    if (query.data.startsWith('rm_0x')) {
      const addr = query.data.slice(3);
      removeWallet(chatId, addr);
      bot.sendMessage(chatId, '✅ Wallet removed.', mainMenu());
    }
  });

  // Shortcut commands
  bot.onText(/\/positions/, async (msg) => { await handleStatus(msg.chat.id); });
  bot.onText(/\/balance/, async (msg) => { await handleBalance(msg.chat.id); });
  bot.onText(/\/wallets/, (msg) => { handleWallets(msg.chat.id); });
  bot.onText(/\/check/, async (msg) => { await handleCheck(msg.chat.id); });

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
  });

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
        const dexTag = pos.dex !== 'Native' ? ` _(${pos.dex})_` : '';

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
          text.push(`  ${d.dex}: $${d.accountValue.toFixed(2)} (margin: $${d.totalMarginUsed.toFixed(2)})`);
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

  async function sendNotification(chatId, message) {
    try {
      await bot.sendMessage(chatId, message, { parse_mode: 'Markdown' });
    } catch {
      try {
        await bot.sendMessage(chatId, message);
      } catch (e) {
        console.error(`Failed to send to ${chatId}:`, e.message);
      }
    }
  }

  return { bot, sendNotification };
}

module.exports = createBot;
