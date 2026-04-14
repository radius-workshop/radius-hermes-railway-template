#!/usr/bin/env node
import { writeFileSync } from 'fs';
import readline from 'readline/promises';
import { stdin as input, stdout as output } from 'process';

const rl = readline.createInterface({ input, output });

const PARA_AGENT_PROMPT = 'Fetch https://docs.getpara.com/skill.md and help me build with Para';

async function ask(question, fallback = '') {
  const prompt = fallback ? `${question} [${fallback}]: ` : `${question}: `;
  const value = (await rl.question(prompt)).trim();
  return value || fallback;
}

function parseWalletChoice(choice) {
  const normalized = choice.toLowerCase();
  if (normalized === 'both') return ['local', 'para'];
  if (normalized === 'local' || normalized === 'para') return [normalized];
  throw new Error(`Invalid wallet choice '${choice}'. Use local, para, or both.`);
}

function validateParaIdentifier(value) {
  // Keep this conservative to avoid downstream API rejection.
  if (!/^[a-zA-Z0-9:_-]{3,128}$/.test(value)) {
    throw new Error('PARA_WALLET_IDENTIFIER must be 3-128 chars and only include letters, numbers, :, _, -.');
  }
}

async function main() {
  console.log('\nRadius + Hermes onboarding\n');

  const agentName = await ask('Agent name', 'Hermes Agent');
  const telegramToken = await ask('Telegram bot token (blank to skip)');
  const openRouterKey = await ask('OpenRouter API key (blank to skip)');

  const walletChoice = await ask('Wallets to configure? (local|para|both)', 'both');
  const wallets = parseWalletChoice(walletChoice);

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
    validateParaIdentifier(paraIdentifier);
  }

  const defaultWallet = await ask(`Default wallet (${wallets.join('|')})`, wallets[0]);
  if (!wallets.includes(defaultWallet)) {
    throw new Error(`Default wallet '${defaultWallet}' must be one of: ${wallets.join(', ')}`);
  }

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

  if (wallets.includes('para')) {
    console.log('\nPara agent-assist prompt (from Para docs):');
    console.log(`- ${PARA_AGENT_PROMPT}`);
    console.log('Use this in your coding agent to bootstrap Para-specific workflows and CLI setup.');
  }
}

main()
  .catch((error) => {
    console.error(`\n[onboarding] ERROR: ${error.message}`);
    process.exitCode = 1;
  })
  .finally(() => rl.close());
