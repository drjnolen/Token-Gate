(() => {
  'use strict';

  if (window.top !== window.self) {
    document.documentElement.classList.add('blocked-frame');
    try { window.top.location = window.location.href; } catch (_) {}
    return;
  }

  const DEFAULT_API_VERIFY_URL =
    'https://token-gate-bot-production.up.railway.app/api/verify';
  const ALLOWED_API_HOSTS = new Set([
    'token-gate-bot-production.up.railway.app',
    'token-gate-bot.onrender.com',
    'localhost',
    '127.0.0.1'
  ]);
  const body = document.body;
  const hash = new URLSearchParams(window.location.hash.replace(/^#/, ''));
  const query = new URLSearchParams(window.location.search);
  const serverApiUrl = body.dataset.apiVerifyUrl || '';
  const serverSession = body.dataset.verificationSession || '';
  const requestedApiValue = serverApiUrl || hash.get('api_verify_url') ||
    query.get('api_verify_url') || DEFAULT_API_VERIFY_URL;
  const requestedApiUrl = new URL(requestedApiValue, window.location.href);
  const isLocalApi = ['localhost', '127.0.0.1'].includes(requestedApiUrl.hostname) &&
    requestedApiUrl.protocol === 'http:';
  const apiHostAllowed = Boolean(serverApiUrl) || (
    ALLOWED_API_HOSTS.has(requestedApiUrl.hostname) &&
    (requestedApiUrl.protocol === 'https:' || isLocalApi)
  );
  const API_VERIFY_URL = (
    apiHostAllowed ? requestedApiUrl : new URL(DEFAULT_API_VERIFY_URL)
  ).toString();
  const VERIFICATION_SESSION = serverSession ||
    hash.get('verification_session') ||
    query.get('verification_session') ||
    '';
  const CONTEXT_URL = API_VERIFY_URL.replace(
    /\/api\/verify(?:\?.*)?$/,
    '/api/verification-context'
  ) + '?verification_session=' + encodeURIComponent(VERIFICATION_SESSION);
  const telegram = window.Telegram && window.Telegram.WebApp;
  const registeredWallets = new Set();

  let context = null;
  let selectedWallet = null;
  let selectedAccount = null;
  let selectedAddress = '';
  let selectedSignature = '';
  let restartUrl = '';

  const walletCard = document.getElementById('walletCard');
  const discoverButton = document.getElementById('discoverButton');
  const signButton = document.getElementById('signButton');
  const submitButton = document.getElementById('submitButton');
  const changeButton = document.getElementById('changeButton');
  const walletList = document.getElementById('walletList');
  const accountPanel = document.getElementById('accountPanel');
  const accountList = document.getElementById('accountList');
  const reviewPanel = document.getElementById('reviewPanel');
  const contextRecovery = document.getElementById('contextRecovery');

  if (query.has('verification_session') || query.has('api_verify_url')) {
    const cleanUrl = new URL(window.location.href);
    cleanUrl.searchParams.delete('verification_session');
    cleanUrl.searchParams.delete('api_verify_url');
    if (!serverSession) {
      cleanUrl.hash = new URLSearchParams({
        verification_session: VERIFICATION_SESSION,
        api_verify_url: API_VERIFY_URL
      }).toString();
    }
    window.history.replaceState(null, '', cleanUrl);
  }

  if (telegram) {
    try { telegram.ready(); telegram.expand(); } catch (_) {}
  }

  function showNotice(id, message, kind) {
    const element = document.getElementById(id);
    element.textContent = message;
    element.className = 'notice show ' + kind;
  }

  function clearNotice(id) {
    const element = document.getElementById(id);
    element.textContent = '';
    element.className = 'notice';
  }

  function setBusy(busy, label) {
    walletCard.setAttribute('aria-busy', busy ? 'true' : 'false');
    if (label) showNotice('walletNotice', label, 'warning');
  }

  function setStep(step) {
    for (let index = 1; index <= 3; index += 1) {
      document.getElementById('section' + index).classList.toggle('active', index === step);
      const marker = document.getElementById('step' + index);
      marker.classList.toggle('active', index === step);
      marker.classList.toggle('done', index < step);
      if (index < 3) {
        document.getElementById('line' + index).classList.toggle('done', index < step);
      }
    }
  }

  function isValidAddress(address) {
    return /^0x[0-9a-fA-F]{1,64}$/.test(String(address || '').trim());
  }

  function canonicalAddress(address) {
    return '0x' + String(address).trim().slice(2).toLowerCase().padStart(64, '0');
  }

  function abbreviatedAddress(address) {
    return address.slice(0, 8) + '…' + address.slice(-6);
  }

  function ownershipMessage(address) {
    return 'Token Gate wallet ownership verification\n' +
      'Session: ' + VERIFICATION_SESSION + '\n' +
      'Telegram user: ' + context.telegram_user_id + '\n' +
      'Group: ' + context.group_id + '\n' +
      'Wallet: ' + canonicalAddress(address);
  }

  function formatDecimalString(value) {
    const raw = String(value == null ? '0' : value).trim();
    const match = raw.match(/^(-?)(\d+)(?:\.(\d+))?$/);
    if (!match) return raw;
    const integer = match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    const fraction = (match[3] || '').replace(/0+$/, '');
    return match[1] + integer + (fraction ? '.' + fraction : '');
  }

  function toBase64(value) {
    if (typeof value === 'string') return value;
    if (!(value instanceof Uint8Array)) return '';
    let binary = '';
    value.forEach(byte => { binary += String.fromCharCode(byte); });
    return btoa(binary);
  }

  function renderRequirements() {
    const requirements = context.requirements;
    const target = document.getElementById('requirements');
    target.textContent = '';
    const rows = [];
    if (requirements.has_token_requirement) {
      rows.push([
        '💰',
        requirements.token_label || 'Token requirement',
        formatDecimalString(requirements.minimum_holding) + ' minimum'
      ]);
    }
    if (requirements.has_nft_requirement) {
      rows.push([
        '🖼️',
        requirements.collection_label || 'NFT collection',
        requirements.nft_threshold + ' minimum'
      ]);
    }
    if (requirements.has_trait_requirement) {
      const trait = requirements.trait_value
        ? requirements.trait_name + ' = ' + requirements.trait_value
        : requirements.trait_name + ' (any value)';
      rows.push(['🎨', 'NFT trait requirement', trait]);
    }
    rows.forEach(([icon, label, value]) => {
      const row = document.createElement('div');
      row.className = 'requirement';
      const iconElement = document.createElement('span');
      iconElement.textContent = icon;
      const copy = document.createElement('div');
      const strong = document.createElement('strong');
      strong.textContent = label;
      const small = document.createElement('small');
      small.textContent = value;
      copy.append(strong, small);
      row.append(iconElement, copy);
      target.appendChild(row);
    });
  }

  function walletAppReadyEvent(api) {
    const event = new Event('wallet-standard:app-ready');
    event.detail = api;
    return event;
  }

  function initWalletStandard() {
    const api = Object.freeze({
      register(...wallets) {
        wallets.forEach(wallet => registeredWallets.add(wallet));
      }
    });
    window.addEventListener('wallet-standard:register-wallet', event => {
      if (event.detail && typeof event.detail === 'function') event.detail(api);
    });
    try { window.dispatchEvent(walletAppReadyEvent(api)); } catch (_) {}
  }

  function discoverWallets() {
    const wallets = [];
    const seen = new Set();
    const add = wallet => {
      if (!wallet || seen.has(wallet.name)) return;
      const chains = wallet.chains || [];
      if (chains.length && !chains.some(chain => String(chain).startsWith('sui:'))) return;
      seen.add(wallet.name);
      wallets.push(wallet);
    };
    registeredWallets.forEach(add);
    const registry = window.__wallet_standard__;
    if (registry) {
      const direct = typeof registry.get === 'function' ? registry.get() :
        (registry.wallets && typeof registry.wallets.get === 'function'
          ? registry.wallets.get()
          : []);
      Array.from(direct || []).forEach(add);
    }
    [
      ['suiWallet', 'Sui Wallet'],
      ['slush', 'Slush'],
      ['suiet', 'Suiet'],
      ['nightly', 'Nightly'],
      ['martian', 'Martian']
    ].forEach(([key, name]) => {
      const provider = window[key];
      if (provider) {
        add({
          name,
          _provider: provider,
          features: provider.features || {},
          accounts: provider.accounts || []
        });
      }
    });
    if (window.phantom && window.phantom.sui) {
      const provider = window.phantom.sui;
      add({
        name: 'Phantom',
        _provider: provider,
        features: provider.features || {},
        accounts: provider.accounts || []
      });
    }
    return wallets;
  }

  function accountDetails(rawAccount, fallbackName) {
    const address = typeof rawAccount === 'string'
      ? rawAccount
      : rawAccount && rawAccount.address;
    if (!isValidAddress(address)) return null;
    return {
      raw: rawAccount,
      address: canonicalAddress(address),
      label: (rawAccount && rawAccount.label) || fallbackName || 'Sui account'
    };
  }

  async function connectWallet(wallet) {
    const feature = wallet.features && wallet.features['standard:connect'];
    const connect = feature && typeof feature.connect === 'function'
      ? feature.connect.bind(feature)
      : (wallet._provider && typeof wallet._provider.connect === 'function'
        ? wallet._provider.connect.bind(wallet._provider)
        : null);
    if (!connect) throw new Error('This wallet does not expose a supported connect feature.');
    const result = await connect();
    let accounts = Array.from((result && result.accounts) || wallet.accounts || []);
    if (!accounts.length && result && result.publicKey) {
      const address = typeof result.publicKey.toSuiAddress === 'function'
        ? result.publicKey.toSuiAddress()
        : String(result.publicKey);
      accounts = [{ address }];
    }
    if (!accounts.length && wallet._provider &&
        typeof wallet._provider.getAccounts === 'function') {
      accounts = Array.from(await wallet._provider.getAccounts() || []);
    }
    const normalized = accounts
      .map(account => accountDetails(account, wallet.name))
      .filter(Boolean);
    if (!normalized.length) throw new Error('The wallet did not return a valid Sui account.');
    return normalized;
  }

  async function signOwnership(wallet, account, address) {
    const message = new TextEncoder().encode(ownershipMessage(address));
    for (const key of ['sui:signPersonalMessage', 'standard:signMessage']) {
      const feature = wallet.features && wallet.features[key];
      const sign = feature && (feature.signPersonalMessage || feature.signMessage);
      if (typeof sign !== 'function') continue;
      const input = account ? { message, account } : { message };
      const result = await sign.call(feature, input);
      const signature = result && toBase64(result.signature);
      if (signature) return signature;
    }
    if (wallet._provider && typeof wallet._provider.signPersonalMessage === 'function') {
      const result = await wallet._provider.signPersonalMessage({ message });
      const signature = result && toBase64(result.signature);
      if (signature) return signature;
    }
    if (wallet._provider && typeof wallet._provider.signMessage === 'function') {
      const result = await wallet._provider.signMessage({ message });
      const signature = result && toBase64(result.signature);
      if (signature) return signature;
    }
    throw new Error('This wallet does not support Sui personal-message signing.');
  }

  function selectAccount(account) {
    selectedAccount = account.raw;
    selectedAddress = account.address;
    selectedSignature = '';
    accountPanel.hidden = true;
    walletList.hidden = true;
    reviewPanel.hidden = false;
    document.getElementById('connectedWallet').textContent =
      selectedWallet.name || 'Sui Wallet';
    document.getElementById('connectedAddress').textContent = selectedAddress;
    document.getElementById('ownershipMessage').textContent =
      ownershipMessage(selectedAddress);
    document.getElementById('signedStatus').hidden = true;
    signButton.hidden = false;
    signButton.disabled = false;
    submitButton.hidden = true;
    submitButton.disabled = true;
  }

  function renderAccounts(accounts) {
    accountList.textContent = '';
    accountPanel.hidden = false;
    accounts.forEach(account => {
      const button = document.createElement('button');
      button.className = 'account';
      button.type = 'button';
      const copy = document.createElement('span');
      const name = document.createElement('span');
      name.className = 'account-name';
      name.textContent = account.label;
      const address = document.createElement('span');
      address.className = 'account-address';
      address.textContent = abbreviatedAddress(account.address);
      copy.append(name, address);
      const arrow = document.createElement('span');
      arrow.textContent = '→';
      button.append(copy, arrow);
      button.addEventListener('click', () => selectAccount(account));
      accountList.appendChild(button);
    });
    if (accounts.length === 1) selectAccount(accounts[0]);
  }

  function renderWallets(wallets) {
    walletList.textContent = '';
    walletList.hidden = false;
    if (!wallets.length) {
      const inTelegram = Boolean(
        window.TelegramWebviewProxy ||
        (telegram && telegram.platform && telegram.platform !== 'unknown')
      );
      showNotice(
        'walletNotice',
        inTelegram
          ? 'Wallet extensions cannot run in Telegram’s browser. Open this page in your system browser.'
          : 'No Sui wallet was detected. Install or unlock Slush, Phantom, Suiet, or Nightly, then retry.',
        'error'
      );
      discoverButton.hidden = false;
      discoverButton.textContent = inTelegram
        ? 'Open in external browser'
        : 'Retry wallet detection';
      discoverButton.disabled = false;
      discoverButton.dataset.external = inTelegram ? 'true' : 'false';
      return;
    }
    discoverButton.hidden = true;
    wallets.forEach(wallet => {
      const button = document.createElement('button');
      button.className = 'wallet';
      button.type = 'button';
      const icon = document.createElement('span');
      icon.className = 'wallet-icon';
      if (wallet.icon) {
        const image = document.createElement('img');
        image.src = typeof wallet.icon === 'string' ? wallet.icon : wallet.icon.src;
        image.alt = '';
        icon.appendChild(image);
      } else {
        icon.textContent = '👛';
      }
      const copy = document.createElement('span');
      const name = document.createElement('span');
      name.className = 'wallet-name';
      name.textContent = wallet.name || 'Sui Wallet';
      const hint = document.createElement('span');
      hint.className = 'wallet-hint';
      hint.textContent = 'Connect';
      copy.append(name, hint);
      const arrow = document.createElement('span');
      arrow.textContent = '→';
      button.append(icon, copy, arrow);
      button.addEventListener('click', async () => {
        clearNotice('walletNotice');
        Array.from(walletList.querySelectorAll('button')).forEach(item => {
          item.disabled = true;
        });
        hint.textContent = 'Waiting for wallet…';
        setBusy(true);
        try {
          const accounts = await connectWallet(wallet);
          selectedWallet = wallet;
          walletList.hidden = true;
          renderAccounts(accounts);
          clearNotice('walletNotice');
        } catch (error) {
          hint.textContent = 'Connect';
          Array.from(walletList.querySelectorAll('button')).forEach(item => {
            item.disabled = false;
          });
          showNotice(
            'walletNotice',
            error.message || 'Wallet connection was cancelled.',
            'error'
          );
        } finally {
          setBusy(false);
        }
      });
      walletList.appendChild(button);
    });
  }

  function setRecoveryButtons() {
    const hasRestart = Boolean(restartUrl);
    document.getElementById('newLinkButton').hidden = !hasRestart;
    document.getElementById('resultNewLinkButton').hidden = !hasRestart;
  }

  async function loadContext() {
    setBusy(true);
    contextRecovery.hidden = true;
    clearNotice('walletNotice');
    if (!VERIFICATION_SESSION) throw new Error('The verification session is missing.');
    const response = await fetch(CONTEXT_URL, {
      headers: { Accept: 'application/json' },
      cache: 'no-store'
    });
    const result = await response.json();
    if (!response.ok || !result.success) {
      restartUrl = result.restart_url || '';
      throw new Error(result.error || 'This verification link is unavailable.');
    }
    context = result;
    restartUrl = result.restart_url || '';
    setRecoveryButtons();
    renderRequirements();
    discoverButton.disabled = false;
    discoverButton.textContent = 'Find Sui wallets';
    walletCard.setAttribute('aria-busy', 'false');
  }

  function scrubSensitiveUrl() {
    if (serverSession) return;
    const cleanUrl = new URL(window.location.href);
    cleanUrl.searchParams.delete('verification_session');
    cleanUrl.searchParams.delete('api_verify_url');
    cleanUrl.hash = '';
    window.history.replaceState(null, '', cleanUrl);
  }

  function showResult(result, responseOk) {
    setStep(3);
    const success = responseOk && result.success;
    const registeredButIneligible = Boolean(
      result.wallet_registered && result.eligibility_status === 'fail'
    );
    if (success) {
      document.getElementById('resultIcon').textContent = '✅';
      document.getElementById('resultTitle').textContent = 'Wallet verified';
      document.getElementById('resultMessage').textContent =
        result.message || 'Wallet verified and registered successfully.';
      showNotice(
        'resultNotice',
        'Check Telegram for your confirmation and group link.',
        'success'
      );
      scrubSensitiveUrl();
      return;
    }
    if (registeredButIneligible) {
      document.getElementById('resultIcon').textContent = '⚠️';
      document.getElementById('resultTitle').textContent =
        'Wallet registered — requirements not met';
      document.getElementById('resultMessage').textContent =
        result.message || 'Ownership was verified, but current holdings are below this group’s requirements.';
      showNotice(
        'resultNotice',
        'Your wallet was saved. Update your holdings and request a new verification link when ready.',
        'warning'
      );
      scrubSensitiveUrl();
      return;
    }
    document.getElementById('resultIcon').textContent = '❌';
    document.getElementById('resultTitle').textContent = 'Verification not completed';
    document.getElementById('resultMessage').textContent =
      result.message || result.error || 'Please try again.';
    if (result.retryable) {
      document.getElementById('retryButton').hidden = false;
      showNotice('resultNotice', 'Your verification link is still valid.', 'error');
    }
  }

  async function submitVerification() {
    if (!selectedAddress || !selectedSignature) return;
    setStep(2);
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 150000);
      const response = await fetch(API_VERIFY_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json'
        },
        cache: 'no-store',
        signal: controller.signal,
        body: JSON.stringify({
          verification_session: VERIFICATION_SESSION,
          wallet_address: selectedAddress,
          wallet_signature: selectedSignature
        })
      });
      clearTimeout(timeout);
      const result = await response.json();
      restartUrl = result.restart_url || restartUrl;
      setRecoveryButtons();
      showResult(result, response.ok);
    } catch (error) {
      setStep(3);
      document.getElementById('resultIcon').textContent = '⚠️';
      document.getElementById('resultTitle').textContent = 'Network error';
      document.getElementById('resultMessage').textContent =
        'No result was recorded. Please retry.';
      document.getElementById('retryButton').hidden = false;
      showNotice(
        'resultNotice',
        error.name === 'AbortError'
          ? 'The request timed out.'
          : 'Could not reach the server.',
        'error'
      );
    }
  }

  function returnToTelegram() {
    if (telegram && typeof telegram.close === 'function') {
      telegram.close();
      return;
    }
    if (restartUrl) {
      window.location.assign(restartUrl);
      return;
    }
    window.history.back();
  }

  function requestNewLink() {
    if (restartUrl) window.location.assign(restartUrl);
  }

  discoverButton.addEventListener('click', () => {
    clearNotice('walletNotice');
    if (discoverButton.dataset.external === 'true') {
      if (telegram && typeof telegram.openLink === 'function') {
        telegram.openLink(window.location.href);
      } else {
        window.open(window.location.href, '_blank', 'noopener');
      }
      return;
    }
    discoverButton.disabled = true;
    discoverButton.textContent = 'Detecting wallets…';
    window.setTimeout(() => renderWallets(discoverWallets()), 300);
  });

  signButton.addEventListener('click', async () => {
    clearNotice('walletNotice');
    signButton.disabled = true;
    setBusy(true, 'Waiting for your wallet signature…');
    try {
      selectedSignature = await signOwnership(
        selectedWallet,
        selectedAccount,
        selectedAddress
      );
      document.getElementById('signedStatus').hidden = false;
      signButton.hidden = true;
      submitButton.hidden = false;
      submitButton.disabled = false;
      clearNotice('walletNotice');
    } catch (error) {
      signButton.disabled = false;
      showNotice(
        'walletNotice',
        error.message || 'Signature request was cancelled.',
        'error'
      );
    } finally {
      setBusy(false);
    }
  });

  changeButton.addEventListener('click', async () => {
    try {
      const disconnect = selectedWallet && selectedWallet.features &&
        selectedWallet.features['standard:disconnect'];
      if (disconnect && typeof disconnect.disconnect === 'function') {
        await disconnect.disconnect.call(disconnect);
      }
    } catch (_) {}
    selectedWallet = null;
    selectedAccount = null;
    selectedAddress = '';
    selectedSignature = '';
    reviewPanel.hidden = true;
    accountPanel.hidden = true;
    renderWallets(discoverWallets());
  });

  document.getElementById('copyMessageButton').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(
        document.getElementById('ownershipMessage').textContent
      );
      showNotice('walletNotice', 'Ownership message copied.', 'success');
    } catch (_) {
      showNotice('walletNotice', 'Could not copy automatically. Select the message text to copy it.', 'error');
    }
  });
  submitButton.addEventListener('click', submitVerification);
  document.getElementById('retryButton').addEventListener('click', () => {
    document.getElementById('retryButton').hidden = true;
    clearNotice('resultNotice');
    submitVerification();
  });
  document.getElementById('contextRetryButton').addEventListener('click', () => {
    loadContext().catch(handleContextError);
  });
  document.getElementById('newLinkButton').addEventListener('click', requestNewLink);
  document.getElementById('resultNewLinkButton').addEventListener('click', requestNewLink);
  document.getElementById('telegramButton').addEventListener('click', returnToTelegram);
  document.getElementById('resultTelegramButton').addEventListener('click', returnToTelegram);

  function handleContextError(error) {
    walletCard.setAttribute('aria-busy', 'false');
    discoverButton.textContent = 'Verification unavailable';
    discoverButton.disabled = true;
    contextRecovery.hidden = false;
    showNotice('walletNotice', error.message, 'error');
  }

  initWalletStandard();
  loadContext().catch(handleContextError);
})();
