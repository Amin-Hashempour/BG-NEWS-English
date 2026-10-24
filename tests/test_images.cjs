// Optional live browser regression: requires Playwright and Chromium for development.
// Application users still need only Python. Run: node tests/test_images.cjs
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const net = require('node:net');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
let server;
let browser;
const transportCache = process.env.BGNEWS_FORCE_CURL || process.env.BGNEWS_DENY_PUSH_CDN ? fs.mkdtempSync(path.join(os.tmpdir(), 'bgnews-image-test-')) : '';

async function startServer(port) {
  const forcedTransport = `
import server, ssl
from pathlib import Path
original = server.request_bytes
def fail_python_images(url, **kwargs):
    if ${process.env.BGNEWS_FORCE_CURL ? 'True' : 'False'} and kwargs.get('accept', '').startswith('image/'):
        raise ssl.SSLCertVerificationError('Simulated Python-only TLS failure')
    return original(url, **kwargs)
server.request_bytes = fail_python_images
original_image_transport = server.request_image_transport
def denied_cdn(url, **kwargs):
    if ${process.env.BGNEWS_DENY_PUSH_CDN ? 'True' : 'False'} and server.urlsplit(url).hostname == 'images.pushsquare.com':
        raise server.HTTPError(url, 403, 'Simulated publisher CDN denial', {}, None)
    return original_image_transport(url, **kwargs)
server.request_image_transport = denied_cdn
server.IMAGE_FILES = Path(${JSON.stringify(transportCache)})
httpd = server.ThreadingHTTPServer(('127.0.0.1', ${port}), server.Handler)
print('Testing publisher images with a fresh cache and controlled transport failures', flush=True)
httpd.serve_forever()
`;
  const args = transportCache ? ['-u', '-c', forcedTransport] : ['-u', 'server.py', '--port', String(port)];
  server = spawn(process.env.PYTHON || 'python3', args, { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
  server.stderr.on('data', () => {});
  await Promise.race([
    once(server.stdout, 'data'),
    once(server, 'exit').then(([code]) => { throw new Error(`Server exited with ${code}`); }),
  ]);
}
async function stopServer() {
  if (!server || server.exitCode !== null) return;
  const stopped = once(server, 'exit');
  server.kill('SIGTERM');
  await stopped;
  server = null;
}

async function checkImages(page, phase, expected) {
  for (const code of ['PS', 'GE', 'VGC']) {
    await page.locator(`[data-filter="${code}"]`).click();
    const cards = page.locator('.card:not([hidden])');
    assert.equal(await cards.count(), expected[code], `${phase}: ${code} story count`);
    for (let index = 0; index < expected[code]; index++) {
      const card = cards.nth(index);
      await card.scrollIntoViewIfNeeded();
      await card.locator('img').waitFor({ timeout: 45000 });
      await page.waitForFunction(({ code, index }) => {
        const card = document.querySelectorAll('.card:not([hidden])')[index];
        const img = card?.querySelector('img');
        return img?.complete && img.naturalWidth > 0;
      }, { code, index }, { timeout: 45000 });
      const geometry = await card.evaluate(node => {
        const img = node.querySelector('img');
        const imageBox = img.getBoundingClientRect();
        return { src: img.currentSrc, width: img.naturalWidth, height: img.naturalHeight,
          renderedWidth: imageBox.width, renderedHeight: imageBox.height,
          left: imageBox.left, textLeft: node.querySelector('h2').getBoundingClientRect().left };
      });
      assert.ok(geometry.src.includes('/api/story-image?'), 'Must decode the local image, without direct-CDN fallback');
      assert.ok(geometry.width > 1 && geometry.height > 1, 'Must be a real story image');
      assert.ok(geometry.renderedWidth > 0 && geometry.renderedHeight > 0, 'Image must be visible');
      assert.ok(geometry.left < geometry.textLeft, 'Image must be on the left of the title');
    }
    console.log(`PASS ${phase}: ${code} ${expected[code]}/${expected[code]} images decoded and visible`);
  }
}

(async () => {
  const probe = net.createServer();
  probe.listen(0, '127.0.0.1');
  await once(probe, 'listening');
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  await startServer(port);
  const installedChromium = process.env.CHROMIUM_PATH || (fs.existsSync('/usr/bin/chromium') ? '/usr/bin/chromium' : undefined);
  browser = await chromium.launch({ headless: true, executablePath: installedChromium, args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  let imageRequests = 0;
  page.on('request', request => { if (request.url().includes('/api/story-image?')) imageRequests++; });
  // External browser image requests are blocked, so a passing test proves that
  // our server downloaded the publisher image and the browser decoded its bytes.
  await page.route(/^https:\/\//, route => route.abort());
  await page.goto(base);
  const news = page.waitForResponse(response => response.url() === base + '/api/news');
  await page.locator('#refresh').click();
  const payload = await (await news).json();
  await page.waitForFunction(() => !document.getElementById('refresh').disabled, {}, { timeout: 120000 });
  const expected = {};
  for (const code of ['PS', 'GE', 'VGC']) {
    assert.equal(payload[code].error, null);
    expected[code] = payload[code].items.length;
    assert.ok(expected[code] > 0);
  }
  await checkImages(page, 'live news', expected);
  const beforeSwitch = imageRequests;
  await page.locator('.card[data-source="VGC"] img').first().evaluate(image => { window.retainedTestImage = image; });
  for (let round = 0; round < 3; round++) {
    for (const code of ['GE', 'PS', 'VGC']) {
      await page.locator(`[data-filter="${code}"]`).click();
      const decoded = await page.locator('.card:not([hidden]) img').evaluateAll(images => images.every(image => image.complete && image.naturalWidth > 0));
      assert.ok(decoded, `${code} images must be ready immediately on return`);
    }
  }
  const otherTab = await context.newPage();
  await otherTab.goto('about:blank');
  await page.bringToFront();
  await otherTab.close();
  await page.waitForTimeout(150);
  assert.equal(imageRequests, beforeSwitch, 'Switching source tabs must make ZERO image requests');
  assert.ok(await page.locator('.card[data-source="VGC"] img').first().evaluate(image => image === window.retainedTestImage), 'The decoded image node must be retained');
  console.log('PASS tab switching: zero image requests, no image node recreation, and all images immediately decoded');
  await page.locator('[data-filter="VGC"]').click();
  await page.evaluate(() => scrollTo(0, 0));
  if (process.env.BGNEWS_SCREENSHOT) await page.screenshot({ path: process.env.BGNEWS_SCREENSHOT });

  await stopServer();
  await startServer(port);
  // No refresh: restored browser history must work immediately after restart.
  await page.route('**/api/news', route => route.abort());
  await page.reload();
  await checkImages(page, 'after server restart without refreshing news', expected);

  const legacy = { items: [], sources: {}, lastUpdate: null };
  for (const code of ['PS', 'GE', 'VGC']) {
    const item = { ...payload[code].items[0], source: code, image: '/api/image/' + '0'.repeat(64) };
    delete item.originalImage;
    legacy.items.push(item);
  }
  await page.evaluate(state => localStorage.setItem('checkpoint.global-news.v2', JSON.stringify(state)), legacy);
  await page.reload();
  await checkImages(page, 'legacy history with missing tokens and no original URLs', { PS: 1, GE: 1, VGC: 1 });
  assert.equal(await page.locator('.retry-image').count(), 0);

  // A local publisher denial must expose its precise cause in the UI.
  await page.route('**/api/story-image?*', route => route.fulfill({ status: 502, body: 'Blocked' }));
  await page.route('**/api/image-status?*', route => route.fulfill({ json: { error: 'HTTP 403 from www.gematsu.com', candidates: [] } }));
  await page.reload();
  await page.locator('[data-filter="GE"]').click();
  await page.getByText('Gematsu image download failed: HTTP 403 from www.gematsu.com', { exact: true }).waitFor();
  console.log('PASS publisher denial: exact upstream status is visible beside the image failure');

  await page.route('**/api/health', route => route.fulfill({ json: { ok: true, sources: 5 } }));
  await page.reload();
  await page.getByText('An older server is still running.', { exact: false }).waitFor();
  console.log('PASS stale server: the UI explains that the old Python process needs restarting');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  await stopServer();
  if (transportCache) fs.rmSync(transportCache, { recursive: true, force: true });
});
