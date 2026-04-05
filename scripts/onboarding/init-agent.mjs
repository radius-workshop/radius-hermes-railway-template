#!/usr/bin/env node
import { writeFileSync } from 'fs';
import readline from 'readline/promises';
import { stdin as input, stdout as output } from 'process';

const rl = readline.createInterface({ input, output });

async function ask(question, fallback = '') {
  const prompt = fallback ? `${question} [${fallback}]: ` : `${question}: `;
  const value = (await rl.question(prompt)).trim();
  return value || fallback;
}

async function main() {
  console.log('\nRadius + Hermes onboarding\n');

  const agentName = await ask('Agent name', 'Hermes Agent');
  const telegramToken = await ask('Telegram bot token (blank to skip)');
  const openRouterKey = await ask('OpenRouter API key (blank to skip)');

  const walletChoice = (await ask('Wallets to configure? (local|para|both)', 'both')).toLowerCase();
  const wallets = walletChoice === 'both' ? ['local', 'para'] : [walletChoice];

  let localPrivateKey = '';
  let localAutoGenerate = 'true';
  if (wallets.includes('local')) {
    const localMode = (await ask('Local wallet mode? (import|auto)', 'auto')).toLowerCase();
    if (localMode === 'import') {
      localPrivateKey = await ask('RADIUS_LOCAL_PRIVATE_KEY');
      localAutoGenerate = 'false';
    }
  }

  let paraApiKey = '';
  let paraEnvironment = 'beta';
  let paraIdentifier = '';
  if (wallets.includes('para')) {
    paraApiKey = await ask('PARA_API_KEY');
    paraEnvironment = await ask('PARA_ENVIRONMENT', 'beta');
    const slug = agentName.toLowerCase().replace(/[^a-z0-9-]/g, '-');
    paraIdentifier = await ask('PARA_WALLET_IDENTIFIER', `agent:${paraEnvironment}:${slug}`);
  }

  const defaultWallet = await ask(`Default wallet (${wallets.join('|')})`, wallets[0]);
  const autoFundWallets = await ask('Auto-fund wallets on boot (comma-separated)', wallets.join(','));
  const outputFile = await ask('Write env file (.env.local or .env.railway)', '.env.railway');

  const lines = [
    `AGENT_NAME=${agentName}`,
    'RADIUS_NETWORK=testnet',
    `RADIUS_WALLETS=${wallets.join(',')}`,
    `RADIUS_DEFAULT_WALLET=${defaultWallet}`,
    'RADIUS_AUTO_FUND_ON_BOOT=true',
    `RADIUS_AUTO_FUND_WALLETS=${autoFundWallets}`,
    'RADIUS_LOCAL_AUTO_GENERATE=true',
  ];

  if (telegramToken) lines.push(`TELEGRAM_BOT_TOKEN=${telegramToken}`);
  if (openRouterKey) lines.push(`OPENROUTER_API_KEY=${openRouterKey}`);

  if (wallets.includes('local')) {
    lines.push(`RADIUS_LOCAL_AUTO_GENERATE=${localAutoGenerate}`);
    if (localPrivateKey) lines.push(`RADIUS_LOCAL_PRIVATE_KEY=${localPrivateKey}`);
  }

  if (wallets.includes('para')) {
    lines.push(`PARA_API_KEY=${paraApiKey}`);
    lines.push(`PARA_ENVIRONMENT=${paraEnvironment}`);
    lines.push(`PARA_WALLET_IDENTIFIER=${paraIdentifier}`);
    lines.push('PARA_WALLET_IDENTIFIER_TYPE=CUSTOM_ID');
    lines.push('PARA_AUTO_CREATE=true');
  }

  writeFileSync(outputFile, `${lines.join('\n')}\n`);

  console.log(`\nWrote ${outputFile}\n`);
  console.log('Summary:');
  console.log(`- Agent: ${agentName}`);
  console.log(`- Wallets: ${wallets.join(', ')}`);
  console.log(`- Default wallet: ${defaultWallet}`);
  console.log(`- Auto-fund wallets: ${autoFundWallets}`);
  console.log('\nRailway checklist:');
  for (const line of lines) {
    console.log(`- ${line.split('=')[0]}`);
  }
}

main().finally(() => rl.close());
