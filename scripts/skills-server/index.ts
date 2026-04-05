// index.ts (Bun v1.3 runtime)
import { Hono } from "hono";
import { readFileSync, existsSync, readdirSync, statSync } from "fs";
import { createHash } from "crypto";
import { join } from "path";

const app = new Hono();

const SKILLS_ROOT =
  process.env.SKILLS_ROOT ?? "/data/.hermes/well-known-skills";
const BASE_URL =
  process.env.PUBLIC_URL ??
  (process.env.RAILWAY_PUBLIC_DOMAIN
    ? `https://${process.env.RAILWAY_PUBLIC_DOMAIN}`
    : `http://localhost:${process.env.PORT ?? 3000}`);

function isPublished(skillMd: string): boolean {
  if (!skillMd.startsWith("---")) return false;
  const end = skillMd.indexOf("---", 3);
  if (end < 0) return false;
  return /\npublished:\s*true\s*\n/.test(skillMd.slice(3, end));
}

function parseDescription(skillMd: string): string {
  if (!skillMd.startsWith("---")) return "";
  const end = skillMd.indexOf("---", 3);
  if (end < 0) return "";
  const fm = skillMd.slice(3, end);

  const block = fm.match(/\ndescription:\s*>\n((?:[ \t]+.+\n?)+)/);
  if (block) return block[1].replace(/[ \t]+/g, " ").trim();

  const inline = fm.match(/\ndescription:\s*["']?(.+?)["']?\s*\n/);
  if (inline) return inline[1].trim();

  return "";
}

function sha256(buf: Buffer): string {
  return "sha256:" + createHash("sha256").update(buf).digest("hex");
}

let cachedIndex: string | null = null;
let cacheBuiltAt = 0;
const CACHE_TTL_MS = 60_000;

function buildIndex(): string {
  const skills: object[] = [];

  if (!existsSync(SKILLS_ROOT)) {
    return JSON.stringify(
      {
        $schema: "https://schemas.agentskills.io/discovery/0.2.0/schema.json",
        skills: [],
      },
      null,
      2
    );
  }

  let entries: string[] = [];
  try {
    entries = readdirSync(SKILLS_ROOT).sort();
  } catch {
    return JSON.stringify(
      {
        $schema: "https://schemas.agentskills.io/discovery/0.2.0/schema.json",
        skills: [],
      },
      null,
      2
    );
  }

  for (const entry of entries) {
    try {
      const skillDir = join(SKILLS_ROOT, entry);
      if (!statSync(skillDir).isDirectory()) continue;

      const skillMdPath = join(skillDir, "SKILL.md");
      if (!existsSync(skillMdPath)) continue;

      const buf = readFileSync(skillMdPath);
      const content = buf.toString("utf8");
      if (!isPublished(content)) continue;
      const description = parseDescription(content);

      skills.push({
        name: entry,
        type: "skill-md",
        description,
        url: `${BASE_URL}/.well-known/agent-skills/${entry}/SKILL.md`,
        digest: sha256(buf),
      });
    } catch {
      continue;
    }
  }

  return JSON.stringify(
    {
      $schema: "https://schemas.agentskills.io/discovery/0.2.0/schema.json",
      skills,
    },
    null,
    2
  );
}

function getIndex(): string {
  const now = Date.now();
  if (!cachedIndex || now - cacheBuiltAt > CACHE_TTL_MS) {
    cachedIndex = buildIndex();
    cacheBuiltAt = now;
  }
  return cachedIndex;
}

app.onError((err, c) => {
  console.error("Hono route error:", err);
  return c.json({ error: "Internal Server Error", message: String(err) }, 500);
});

app.use("/.well-known/agent-skills/*", async (c, next) => {
  c.header("Access-Control-Allow-Origin", "*");
  c.header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS");
  c.header("Access-Control-Allow-Headers", "Content-Type");

  if (c.req.method === "OPTIONS") {
    return c.body(null, 204);
  }

  await next();
});

app.on(["GET", "HEAD"], "/.well-known/agent-skills/index.json", (c) => {
  const body = getIndex();

  c.header("Content-Type", "application/json; charset=utf-8");
  c.header("Cache-Control", "public, max-age=60");

  return c.req.method === "HEAD" ? c.body(null, 200) : c.body(body, 200);
});

app.on(
  ["GET", "HEAD"],
  "/.well-known/agent-skills/:name/SKILL.md",
  (c) => {
    const skillName = c.req.param("name");

    if (!/^[a-z0-9][a-z0-9-]*[a-z0-9]$/.test(skillName)) {
      return c.text("Not Found", 404);
    }

    const skillPath = join(SKILLS_ROOT, skillName, "SKILL.md");
    if (!existsSync(skillPath)) {
      return c.text("Not Found", 404);
    }

    try {
      const buf = readFileSync(skillPath);
      if (!isPublished(buf.toString("utf8"))) {
        return c.text("Not Found", 404);
      }

      c.header("Content-Type", "text/markdown; charset=utf-8");
      c.header("Cache-Control", "public, max-age=300");
      c.header("Content-Length", String(buf.byteLength));

      return c.req.method === "HEAD" ? c.body(null, 200) : c.body(buf, 200);
    } catch (err) {
      console.error("Failed reading skill file:", skillPath, err);
      return c.text("Internal Server Error", 500);
    }
  }
);

app.get("/.well-known/agent-registration.json", (c) => {
  c.header("Access-Control-Allow-Origin", "*");
  c.header("Cache-Control", "public, max-age=60");

  let walletAddress = process.env.RADIUS_WALLET_ADDRESS;
  const manifestPath = process.env.RADIUS_WALLET_MANIFEST ?? '/data/.hermes/.radius/wallets/manifest.json';
  if (!walletAddress && existsSync(manifestPath)) {
    try {
      const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
      const selected = (manifest.wallets || []).find((w: any) => w.name === manifest.defaultWallet) ?? manifest.wallets?.[0];
      walletAddress = selected?.address;
    } catch {}
  }
  const agentName = process.env.AGENT_NAME ?? "Hermes Agent";

  const registration: Record<string, unknown> = {
    schemaVersion: "1.0",
    name: agentName,
    x402Support: true,
    trustSchemes: ["reput"],
    identityRegistry: "eip155:72344:0x5cd923Ce1244d5498Bf3f9E0F3a374C2567F1A31",
    services: {
      rpc: "https://rpc.radiustech.xyz",
      rpcTestnet: "https://rpc.testnet.radiustech.xyz",
      faucet: "https://network.radiustech.xyz/api/v1/faucet/doc",
      faucetTestnet: "https://testnet.radiustech.xyz/api/v1/faucet/doc",
    },
  };

  if (walletAddress) {
    registration.wallet = walletAddress;
    registration.owner = walletAddress;
  }

  return c.json(registration);
});

app.get("/", (c) => {
  return c.html(`<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Hermes Agent</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #000;
      color: #fff;
      font-family: monospace;
      font-size: 14px;
      line-height: 1.6;
      padding: 48px 32px;
      max-width: 720px;
    }
    h1 { font-size: 18px; font-weight: normal; margin-bottom: 32px; }
    section { margin-bottom: 32px; }
    .label { color: #555; margin-bottom: 8px; text-transform: uppercase; font-size: 11px; letter-spacing: 0.08em; }
    .value { color: #fff; word-break: break-all; }
    .dim { color: #555; }
    .skill { margin-bottom: 16px; }
    .skill-name { color: #fff; }
    .skill-desc { color: #888; margin-top: 2px; }
    a { color: #fff; text-decoration: none; border-bottom: 1px solid #333; }
    a:hover { border-color: #fff; }
    .error { color: #555; font-style: italic; }
    .fork {
      position: fixed;
      top: 48px;
      right: 32px;
      text-align: right;
      font-size: 11px;
      color: #555;
      line-height: 1.5;
    }
    .fork a { color: #555; border-bottom-color: #222; }
    .fork a:hover { color: #fff; border-color: #555; }
  </style>
</head>
<body>
  <div class="fork">
    <img src="https://railway.com/brand/logo-light.svg" alt="Railway" width="72" style="display:block;margin-left:auto;margin-bottom:6px;opacity:0.4;">
    clone &amp; deploy your own<br>
    <a href="https://github.com/radius-workshop/hermes-railway-template" target="_blank" rel="noopener">radius-workshop/hermes-railway-template</a>
  </div>

  <h1 id="agent-name">—</h1>

  <section id="section-wallet" style="display:none">
    <div class="label">wallet</div>
    <div class="value"><a id="wallet-address" href="#" target="_blank" rel="noopener"></a></div>
  </section>

  <section id="section-registry" style="display:none">
    <div class="label">erc-8004 identity registry</div>
    <div class="value"><a id="registry-address" href="#" target="_blank" rel="noopener"></a></div>
  </section>

  <section id="section-services" style="display:none">
    <div class="label">services</div>
    <div id="services-list"></div>
  </section>

  <section>
    <div class="label">skills</div>
    <div id="skills-list"><span class="dim">loading...</span></div>
  </section>

  <script>
    async function load() {
      try {
        const reg = await fetch('/.well-known/agent-registration.json').then(r => r.json());

        document.getElementById('agent-name').textContent = reg.name ?? 'Hermes Agent';

        if (reg.wallet) {
          const walletEl = document.getElementById('wallet-address');
          walletEl.textContent = reg.wallet;
          walletEl.href = \`https://testnet.radiustech.xyz/address/\${reg.wallet}\`;
          document.getElementById('section-wallet').style.display = '';
        }

        if (reg.identityRegistry) {
          const contractAddr = reg.identityRegistry.split(':').pop();
          const registryEl = document.getElementById('registry-address');
          registryEl.textContent = reg.identityRegistry;
          registryEl.href = \`https://testnet.radiustech.xyz/address/\${contractAddr}\`;
          document.getElementById('section-registry').style.display = '';
        }

        const svcEl = document.getElementById('services-list');
        const services = reg.services ?? {};
        const svcKeys = Object.keys(services);
        if (svcKeys.length) {
          svcEl.innerHTML = svcKeys.map(k =>
            \`<div><span class="dim">\${k}</span> <a href="\${services[k]}" target="_blank" rel="noopener">\${services[k]}</a></div>\`
          ).join('');
          document.getElementById('section-services').style.display = '';
        }
      } catch (e) {
        document.getElementById('agent-name').textContent = 'Hermes Agent';
      }

      try {
        const idx = await fetch('/.well-known/agent-skills/index.json').then(r => r.json());
        const el = document.getElementById('skills-list');
        const skills = idx.skills ?? [];
        if (!skills.length) {
          el.innerHTML = '<span class="dim">no published skills</span>';
          return;
        }
        el.innerHTML = skills.map(s => \`
          <div class="skill">
            <div class="skill-name"><a href="\${s.url}" target="_blank" rel="noopener">\${s.name}</a></div>
            \${s.description ? \`<div class="skill-desc">\${s.description}</div>\` : ''}
          </div>
        \`).join('');
      } catch (e) {
        document.getElementById('skills-list').innerHTML = '<span class="error">failed to load skills</span>';
      }
    }

    load();
  </script>
</body>
</html>`);
});

if (process.env.DEBUG_SKILLS === "1") {
  app.get("/debug/skills", (c) => {
    try {
      return c.json({
        SKILLS_ROOT,
        BASE_URL,
        rootExists: existsSync("/data"),
        hermesExists: existsSync("/data/.hermes"),
        wellKnownSkillsExists: existsSync(SKILLS_ROOT),
        skillsList: existsSync(SKILLS_ROOT) ? readdirSync(SKILLS_ROOT) : [],
      });
    } catch (err) {
      return c.json({ error: String(err) }, 500);
    }
  });
}

const port = Number(process.env.PORT ?? 3000);
console.log(`[skills-server] Listening on port ${port}, BASE_URL=${BASE_URL}`);

Bun.serve({
  port,
  fetch: app.fetch,
});
