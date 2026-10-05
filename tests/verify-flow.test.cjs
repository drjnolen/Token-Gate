// Runs the actual browser controller with wallet/network boundaries stubbed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname,
  fs.existsSync(path.join(__dirname, '../static/verify.js'))
    ? '../static/verify.js' : '../verify/app.js'), 'utf8');
const parent = 'p'.repeat(43);
const child = 'n'.repeat(43);
const base = 'https://alphacity.tech/verify/';
const active = { success: true, group_id: '1', telegram_user_id: '2',
  requirements: {}, restart_url: 'https://t.me/bot?start=register', telegram_return_url: 'https://t.me/bot' };
const failed = { success: false, wallet_registered: true, eligibility_status: 'fail',
  next_verification_session: child, holdings_progress: { message: 'Tokens: 500,000 / 1,000,000 required' } };
const flush = () => new Promise(resolve => setImmediate(resolve));

async function page({ url = `${base}#verification_session=${parent}`, context = active,
  result = failed, serverSession = '', serverApiUrl = '', networkError = false } = {}) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      hidden: true, disabled: false, textContent: '', handlers: {},
      classList: { add() {}, toggle() {} }, setAttribute() {}, append() {}, appendChild() {},
      addEventListener(event, fn) { this.handlers[event] = fn; },
    });
    return elements.get(id);
  };
  const calls = [], signatures = [], navigations = [];
  let current = new URL(url), connectorOptions, reloads = 0;
  const window = {
    location: {
      get href() { return current.href; }, get hash() { return current.hash; },
      get search() { return current.search; },
      reload() { reloads += 1; }, assign(url) { navigations.push(url); },
    },
    history: { replaceState(_a, _b, url) { current = new URL(url); }, back() {} },
    setTimeout, clearTimeout,
    AlphaCityWalletConnector: { create(options) {
      connectorOptions = options;
      return {
        async signPersonalMessage(message) {
          signatures.push(new TextDecoder().decode(message));
          return { signature: 'signed-' + signatures.length };
        }, async walletOptions() {},
      };
    } },
  };
  window.top = window.self = window;
  vm.runInNewContext(source, {
    window, document: { body: { dataset: { verificationSession: serverSession, apiVerifyUrl: serverApiUrl } },
      getElementById: element, createElement: () => element(Symbol()),
      documentElement: { classList: { add() {} } } },
    URL, URLSearchParams, TextEncoder, Uint8Array, ArrayBuffer, AbortController,
    navigator: {}, btoa: value => Buffer.from(value, 'binary').toString('base64'),
    async fetch(url, options) {
      calls.push({ url: String(url), options });
      if (options.method === 'POST' && networkError) throw new Error('interrupted');
      const payload = options.method === 'POST' ? result : context;
      return { ok: payload.success, async json() { return payload; } };
    },
  });
  await flush();
  return {
    element, calls, signatures, navigations,
    get options() { return connectorOptions; },
    get url() { return current.href; }, get reloads() { return reloads; },
    async sign(address = '0x1') {
      connectorOptions.onChange({ address, walletName: 'Test Wallet' });
      await element('signButton').handlers.click();
      await flush();
    },
    async click(id) { await element(id).handlers.click(); await flush(); },
  };
}

test('below threshold shows totals and continues on same site with fresh signature', async () => {
  const first = await page();
  await first.sign();
  assert.equal(first.element('addWalletButton').hidden, false);
  assert.match(first.element('resultMessage').textContent, /500,000 \/ 1,000,000/);
  assert.match(first.element('resultNotice').textContent, /no return to Telegram/);
  assert.match(first.url, /verification_session=/); // refresh remains recoverable
  await first.click('addWalletButton');
  assert.equal(first.reloads, 1);
  assert.equal(new URL(first.url).origin, 'https://alphacity.tech');
  assert.equal(new URL(first.url).pathname, '/verify/');
  assert.equal(new URLSearchParams(new URL(first.url).hash.slice(1)).get('verification_session'), child);
  assert.equal(new URLSearchParams(new URL(first.url).hash.slice(1)).has('api_verify_url'), false);
  assert.deepEqual(first.navigations, []);

  const second = await page({ url: first.url, result: { success: true,
    message: 'Wallet verified and registered successfully. 1,000,000 qualifying tokens found across 2 registered wallets.' } });
  assert.equal(second.options.autoReconnect, false);
  assert.equal(second.options.alwaysPrompt, true);
  assert.equal(second.calls.length, 1); // only context, never automatic signature reuse
  await second.sign('0x2');
  assert.match(first.signatures[0], new RegExp('Session: ' + parent));
  assert.match(second.signatures[0], new RegExp('Session: ' + child));
  const submitted = JSON.parse(second.calls[1].options.body);
  assert.deepEqual(Object.keys(submitted).sort(), ['verification_session', 'wallet_address', 'wallet_signature']);
  assert.equal(submitted.verification_session, child);
  assert.equal(second.element('addWalletButton').hidden, true);
  assert.match(second.element('resultMessage').textContent, /across 2 registered wallets/);
  assert.equal(new URL(second.url).hash, '');
});

test('refresh of completed result restores continuation without signing again', async () => {
  const view = await page({ context: { ...active, verification_completed: true, verification_result: failed } });
  assert.equal(view.options, undefined);
  assert.equal(view.element('addWalletButton').hidden, false);
  await view.click('addWalletButton');
  assert.equal(view.reloads, 1);
  assert.equal(view.calls.length, 1);
});

test('expired, malformed, and old-backend continuations use existing recovery', async () => {
  for (const token of [null, undefined, 'https://evil.example', parent]) {
    const view = await page({ context: { ...active, verification_completed: true,
      verification_result: { ...failed, next_verification_session: token } } });
    assert.equal(view.element('addWalletButton').hidden, true);
    assert.match(view.element('resultNotice').textContent, /registered wallets will still count/);
    assert.equal(new URL(view.url).hash, '');
  }
});

test('interrupted submission keeps the same signature and session for safe retry', async () => {
  const view = await page({ networkError: true });
  await view.sign();
  assert.equal(view.element('retryButton').hidden, false);
  assert.equal(view.element('addWalletButton').hidden, true);
  await view.click('retryButton');
  assert.equal(view.signatures.length, 1);
  assert.equal(view.calls[1].options.body, view.calls[2].options.body);
});

test('backend-hosted query session moves to fragment and child survives reload', async () => {
  const view = await page({ url: `https://token-gate-bot-production.up.railway.app/verify?verification_session=${parent}`,
    serverSession: parent });
  assert.equal(new URL(view.url).search, '');
  assert.match(new URL(view.url).hash, /verification_session=/);
  await view.sign();
  await view.click('addWalletButton');
  assert.equal(new URL(view.url).search, '');
  assert.equal(new URLSearchParams(new URL(view.url).hash.slice(1)).get('verification_session'), child);
});

test('session-only links use the trusted default and sign the server-owned group context', async () => {
  for (const [session, group, requirements] of [
    [parent, '-1001', { registration_mode: 'token', has_token_requirement: true, minimum_holding: '100' }],
    [child, '-1002', { registration_mode: 'nft', has_nft_requirement: true, nft_threshold: 2 }],
  ]) {
    const view = await page({ url: `${base}#verification_session=${session}&group_id=999`,
      context: { ...active, group_id: group, requirements } });
    const request = new URL(view.calls[0].url);
    assert.equal(request.origin, 'https://token-gate-bot-production.up.railway.app');
    assert.equal(request.searchParams.get('verification_session'), session);
    assert.equal(request.searchParams.has('group_id'), false);
    await view.sign();
    assert.ok(view.signatures[0].includes('Group: ' + group + '\n'));
    assert.ok(view.signatures[0].includes('Session: ' + session + '\n'));
  }
});

test('legacy query and fragment links still connect and produce short default continuations', async () => {
  for (const separator of ['?', '#']) {
    const params = new URLSearchParams({ verification_session: parent,
      api_verify_url: 'https://token-gate-bot-production.up.railway.app/api/verify' });
    const view = await page({ url: base + separator + params });
    assert.equal(view.calls.length, 1);
    await view.sign();
    await view.click('addWalletButton');
    assert.equal(view.url, base + '#verification_session=' + child);
    const reloaded = await page({ url: view.url });
    assert.equal(new URL(reloaded.calls[0].url).searchParams.get('verification_session'), child);
  }
});

test('alternate services survive query migration, continuation, and reload', async () => {
  for (const endpoint of ['https://token-gate-bot.onrender.com/api/verify',
    'http://localhost:8080/api/verify', 'http://127.0.0.1:8000/api/verify']) {
    const params = new URLSearchParams({ verification_session: parent, api_verify_url: endpoint });
    const view = await page({ url: base + '?' + params });
    assert.equal(new URL(view.calls[0].url).origin, new URL(endpoint).origin);
    await view.sign();
    assert.equal(view.calls[1].url, endpoint);
    await view.click('addWalletButton');
    const reloaded = await page({ url: view.url });
    assert.equal(new URL(reloaded.calls[0].url).origin, new URL(endpoint).origin);
    assert.equal(new URL(reloaded.calls[0].url).searchParams.get('verification_session'), child);
  }
});

test('backend-rendered service configuration retains precedence over link parameters', async () => {
  const view = await page({ url: `${base}#verification_session=${parent}&api_verify_url=https://evil.example/api/verify`,
    serverApiUrl: 'https://token-gate-bot.onrender.com/api/verify' });
  assert.equal(new URL(view.calls[0].url).origin, 'https://token-gate-bot.onrender.com');
  await view.sign();
  await view.click('addWalletButton');
  const reloaded = await page({ url: view.url });
  assert.equal(new URL(reloaded.calls[0].url).origin, 'https://token-gate-bot.onrender.com');
});

test('invalid legacy endpoints fail closed before wallet/network activity, including after reload', async () => {
  for (const endpoint of ['https://evil.example/api/verify', 'https://token-gate-bot.onrender.com/wrong',
    'https%3A%2F%2Ftoken-gate-bot-production.up.railway.app%2Fapi%2Fverify',
    'https://token-gate-bot.onrender.com/api/verify#extra']) {
    for (const separator of ['?', '#']) {
      const params = new URLSearchParams({ verification_session: parent, api_verify_url: endpoint });
      const view = await page({ url: base + separator + params });
      const reloaded = await page({ url: view.url });
      for (const state of [view, reloaded]) {
        assert.match(state.element('walletNotice').textContent, /invalid service/);
        assert.equal(state.calls.length, 0);
        assert.equal(state.options, undefined);
      }
    }
  }
});

test('short links keep missing-session and expired-session recovery', async () => {
  const missing = await page({ url: base });
  assert.match(missing.element('walletNotice').textContent, /session is missing/);
  assert.equal(missing.calls.length, 0);
  assert.equal(missing.options, undefined);
  const expired = await page({ context: { success: false, error: 'Verification link is expired.',
    restart_url: active.restart_url, telegram_return_url: active.telegram_return_url } });
  assert.match(expired.element('walletNotice').textContent, /expired/);
  assert.equal(expired.options, undefined);
  await expired.click('newLinkButton');
  assert.deepEqual(expired.navigations, [active.restart_url]);
});
