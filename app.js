'use strict';
const STORAGE = 'checkpoint.global-news.v2';
const names = { GS: 'GameSpot', GI: 'Game Informer', PS: 'Push Square', GE: 'Gematsu', VGC: 'VGC' };
let state = { items: [], sources: {}, lastUpdate: null };
let filter = 'all';
const imageFailureNotices = new Set();
const $ = id => document.getElementById(id);
try {
  const saved = JSON.parse(localStorage.getItem(STORAGE));
  if (saved && Array.isArray(saved.items) && saved.sources) state = saved;
} catch { /* A private browser may deny storage. */ }

function httpURL(value) {
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : ''; }
  catch { return ''; }
}
function originalImageURL(item) {
  return httpURL(item.originalImage) || httpURL(item.image);
}
function directImageURLs(item) {
  const urls = new Set([originalImageURL(item), ...(Array.isArray(item.imageAlternates) ? item.imageAlternates.map(httpURL) : [])].filter(Boolean));
  // Keep BOTH publisher routes, including for saved history from v1.3.2.
  // Rewriting every URL to the main domain removed a working CDN fallback.
  if (item.source === 'PS') {
    for (const value of [...urls]) {
      const url = new URL(value);
      if (url.hostname === 'images.pushsquare.com' && /^\/[a-f0-9]+\//i.test(url.pathname)) {
        urls.add('https://www.pushsquare.com/images' + url.pathname + url.search);
      } else if (['www.pushsquare.com', 'pushsquare.com'].includes(url.hostname) && /^\/images\/[a-f0-9]+\//i.test(url.pathname)) {
        urls.add('https://images.pushsquare.com' + url.pathname.slice('/images'.length) + url.search);
      }
    }
  }
  return [...urls];
}
function storyImageURL(item) {
  const article = httpURL(item.url);
  if (!article) return '';
  const original = originalImageURL(item);
  return '/api/story-image?' + new URLSearchParams({ article, image: original });
}
function message(text, error = false) {
  const node = document.createElement('div');
  node.className = 'notice' + (error ? ' error' : '');
  node.textContent = text;
  $('messages').append(node);
}
function persist() {
  try { localStorage.setItem(STORAGE, JSON.stringify(state)); }
  catch { message('Browser storage is unavailable. Your list will not persist after closing this page.', true); }
}
function render() {
  $('articles').replaceChildren();
  for (const item of state.items) {
    if (!httpURL(item.url) || !names[item.source]) continue;
    const card = document.createElement('article'); card.className = 'card'; card.dataset.source = item.source;
    card.hidden = filter !== 'all' && item.source !== filter;
    const mark = document.createElement('span'); mark.className = `source-mark source-${item.source.toLowerCase()}`; mark.textContent = item.source; mark.title = names[item.source];
    const link = document.createElement('a'); link.className = 'story-link'; link.href = item.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    const imageWrap = document.createElement('div'); imageWrap.className = 'image-wrap';
    // Reconstruct the local URL from the article, including for legacy history
    // whose RAM tokens or original image fields are missing or incorrect.
    const candidates = [...new Set([storyImageURL(item), ...directImageURLs(item)].filter(Boolean))];
    const image = document.createElement('img'); image.alt = ''; image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
    let candidate = 0;
    let failureDetail = '';
    image.addEventListener('error', async () => {
      if (candidate === 0) {
        try {
          const response = await fetch(candidates[0].replace('/api/story-image?', '/api/image-status?'), { cache: 'no-store' });
          const status = await response.json();
          failureDetail = status.error || '';
          for (const value of status.candidates || []) {
            const url = httpURL(value);
            if (url && !candidates.includes(url)) candidates.push(url);
          }
        } catch { failureDetail = 'Could not read the local image status'; }
      }
      candidate++;
      if (candidate < candidates.length) { image.src = candidates[candidate]; return; }
      imageWrap.replaceChildren();
      const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'retry-image'; retry.textContent = 'Retry image';
      retry.title = failureDetail || 'The publisher image could not be loaded';
      if (!imageFailureNotices.has(item.source)) {
        imageFailureNotices.add(item.source);
        message(`${names[item.source]} image download failed: ${retry.title}`, true);
      }
      retry.addEventListener('click', event => {
        event.preventDefault(); event.stopPropagation(); candidate = 0; imageFailureNotices.delete(item.source);
        imageWrap.replaceChildren(image); image.src = candidates[0];
      });
      imageWrap.append(retry);
    });
    if (candidates.length) { imageWrap.append(image); image.src = candidates[0]; }
    const copy = document.createElement('div'); copy.className = 'story-copy';
    const meta = document.createElement('div'); meta.className = 'card-meta';
    const source = document.createElement('span'); source.textContent = names[item.source];
    const time = document.createElement('time'); const date = new Date(item.published);
    if (!Number.isNaN(date.getTime())) { time.dateTime = date.toISOString(); time.textContent = new Intl.DateTimeFormat('en-US', { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(date); }
    const title = document.createElement('h2'); title.textContent = item.title;
    title.lang = 'en'; title.dir = 'ltr'; title.style.textAlign = 'left'; title.style.lineHeight = '1.65';
    meta.append(source, time); copy.append(meta, title); link.append(imageWrap, copy); card.append(mark, link); $('articles').append(card);
  }
  $('total').textContent = state.items.length.toLocaleString('en-US');
  applyFilter();
  if (state.lastUpdate) $('update-time').textContent = 'Last checked: ' + new Date(state.lastUpdate).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function applyFilter() {
  let visible = 0;
  for (const card of $('articles').children) {
    card.hidden = filter !== 'all' && card.dataset.source !== filter;
    if (!card.hidden) visible++;
  }
  $('empty').hidden = visible > 0;
}

function mergeSource(current, code, incoming, complete = true) {
  const previous = current.sources[code] || { seen: [] };
  const incomingById = new Map(incoming.map(item => [item.id, item]));
  current.items = current.items.map(item => {
    const latest = item.source === code ? incomingById.get(item.id) : null;
    return latest ? { ...item, ...latest, source: code } : item;
  });
  const known = new Set(previous.seen);
  const existing = new Set(current.items.map(item => `${item.source}:${item.id}`));
  const fresh = incoming.filter(item => !known.has(item.id) && !existing.has(`${code}:${item.id}`));
  current.items.unshift(...fresh.map(item => ({ ...item, source: code })));
  if (complete) current.sources[code] = { seen: [...new Set([...previous.seen, ...incoming.map(item => item.id)])] };
  return fresh.length;
}

async function refresh() {
  const button = $('refresh'); button.disabled = true; button.setAttribute('aria-busy', 'true'); $('refresh-label').textContent = 'Checking…'; $('messages').replaceChildren(); imageFailureNotices.clear();
  const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 120000);
  try {
    const known = Object.fromEntries(Object.keys(names).map(code => [code, state.sources[code]?.seen || []]));
    const response = await fetch('/api/news', { method: 'POST', cache: 'no-store', signal: controller.signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ known }) });
    if (!response.ok) throw new Error('server');
    const result = await response.json(); let added = 0; let success = 0;
    for (const code of Object.keys(names)) {
      const source = result[code];
      if (!source || !Array.isArray(source.items)) { message(`No response was received from ${names[code]}.`, true); continue; }
      const items = source.items.filter(item => item && typeof item.id === 'string' && typeof item.title === 'string' && httpURL(item.url));
      if (source.error) message(source.error, true);
      if (source.complete) { added += mergeSource(state, code, items, true); success++; }
    }
    if (success) {
      state.items.sort((a, b) => (Date.parse(b.published) || 0) - (Date.parse(a.published) || 0));
      state.lastUpdate = new Date().toISOString(); persist(); render();
      message(added ? `${added.toLocaleString('en-US')} new stories added.` : 'All sources checked. No new stories were found.');
    }
  } catch (error) {
    message(error.name === 'AbortError' ? 'Checking the sources took more than two minutes. Please try again.' : 'Could not connect to the local server.', true);
  } finally { clearTimeout(timeout); button.disabled = false; button.removeAttribute('aria-busy'); $('refresh-label').textContent = 'Get latest news'; }
}

document.querySelectorAll('.filter').forEach(button => button.addEventListener('click', () => {
  filter = button.dataset.filter;
  document.querySelectorAll('.filter').forEach(item => { const active = item === button; item.classList.toggle('active', active); item.setAttribute('aria-pressed', String(active)); });
  applyFilter();
}));
$('refresh').addEventListener('click', refresh);
render();
if (location.protocol === 'file:') message('Run python3 server.py, then open http://127.0.0.1:8766 in your browser.', true);
else fetch('/api/health', { cache: 'no-store' }).then(response => response.json()).then(health => {
  if (health.version !== '1.3.3') message('An older server is still running. Stop it with Ctrl+C, then run server.py from this version of the project.', true);
  else if (!health.browser_https) message('Enable Push Square image support: stop the server, run python -m pip install -r requirements.txt, then start it again.', true);
}).catch(() => message('The local server is unavailable. Run python3 server.py from this project folder.', true));
