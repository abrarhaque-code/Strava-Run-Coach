// Renders docs/img/hero-conversation.html to docs/img/hero-conversation.png at 2x.
//
//   NODE_PATH=$(npm root -g) node docs/img/render-hero.mjs
//
// Needs Playwright (npm i -g playwright) and a Chromium it can find. Environment
// knobs: PW_CHROMIUM (explicit browser binary), HTTPS_PROXY (an egress proxy; the
// render ignores its TLS interception because it only fetches fonts).
import { createRequire } from 'node:module';
// ESM ignores NODE_PATH, so resolve Playwright the CommonJS way (NODE_PATH honoured),
// or from PW_MODULE when it lives somewhere unusual.
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PW_MODULE || 'playwright');
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const html = join(here, 'hero-conversation.html');
const out = join(here, 'hero-conversation.png');

const launch = { args: ['--no-sandbox'] };
if (process.env.PW_CHROMIUM) launch.executablePath = process.env.PW_CHROMIUM;
if (process.env.HTTPS_PROXY) launch.proxy = { server: process.env.HTTPS_PROXY };

const browser = await chromium.launch(launch);
const page = await browser.newPage({ viewport: { width: 1100, height: 1000 }, deviceScaleFactor: 2,
                                     ignoreHTTPSErrors: true });
await page.goto('file://' + html, { waitUntil: 'networkidle' });
await page.evaluate(() => document.fonts.ready);
// check() is false while any matching face is still unloaded, so ask for the
// exact faces the page uses.
const fonts = await page.evaluate(() => ({
  serif: document.fonts.check('400 16px "Instrument Serif"') && document.fonts.check('italic 400 16px "Instrument Serif"'),
  grotesk: document.fonts.check('400 16px "Archivo"') && document.fonts.check('900 16px "Archivo"'),
  mono: document.fonts.check('400 16px "Space Mono"'),
}));
await page.locator('.card').screenshot({ path: out });
await browser.close();
console.log('wrote', out, 'fonts loaded:', JSON.stringify(fonts));
if (!fonts.serif || !fonts.grotesk || !fonts.mono) {
  console.error('WARNING: a brand font did not load; the PNG is set in a fallback face.');
  process.exit(2);
}
