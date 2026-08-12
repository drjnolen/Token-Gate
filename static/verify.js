(() => {
  'use strict';

  if (window.top !== window.self) {
    document.documentElement.classList.add('blocked-frame');
    try { window.top.location = window.location.href; } catch (_) {}
    return;
  }

  const DEFAULT_API_VERIFY_URL =
    'https://token-gate-bot-production.up.railway.app/api/verify';
  const CONTEXT_TIMEOUT_MS = 20000;
  const SUBMISSION_TIMEOUT_MS = 210000;
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
  let requestedApiUrl = null;
  let apiConfigurationError = '';
  try {
    requestedApiUrl = new URL(requestedApiValue, window.location.href);
    const isLocalApi = ['localhost', '127.0.0.1'].includes(requestedApiUrl.hostname) &&
      requestedApiUrl.protocol === 'http:';
    const apiHostAllowed = ALLOWED_API_HOSTS.has(requestedApiUrl.hostname) &&
      (requestedApiUrl.protocol === 'https:' || isLocalApi);
    const apiPathAllowed = requestedApiUrl.pathname.replace(/\/$/, '') === '/api/verify';
    if (!apiHostAllowed || !apiPathAllowed || requestedApiUrl.username ||
        requestedApiUrl.password || requestedApiUrl.search || requestedApiUrl.hash) {
      throw new Error('untrusted verification API URL');
    }
  } catch (_) {
    apiConfigurationError =
      'This verification link names an invalid service. Request a new link in Telegram.';
  }
  const API_VERIFY_URL = apiConfigurationError ? '' : requestedApiUrl.toString();
  const VERIFICATION_SESSION = serverSession ||
    hash.get('verification_session') ||
    query.get('verification_session') ||
    '';
  const contextUrl = API_VERIFY_URL ? new URL(API_VERIFY_URL) : null;
  if (contextUrl) {
    contextUrl.pathname = '/api/verification-context';
    contextUrl.searchParams.set('verification_session', VERIFICATION_SESSION);
  }
  const CONTEXT_URL = contextUrl ? contextUrl.toString() : '';
  const telegram = window.Telegram && window.Telegram.WebApp;
  let context = null;
  let selectedAddress = '';
  let selectedSignature = '';
  let walletConnector = null;
  let restartUrl = '';
  let telegramReturnUrl = '';
  let contextLoading = false;
  let submissionInFlight = false;

  const walletCard = document.getElementById('walletCard');
  const connectWalletButton = document.getElementById('connectWalletButton');
  const signButton = document.getElementById('signButton');
  const changeButton = document.getElementById('changeButton');
  const reviewPanel = document.getElementById('reviewPanel');
  const contextRecovery = document.getElementById('contextRecovery');

  if (query.has('verification_session') || query.has('api_verify_url')) {
    const cleanUrl = new URL(window.location.href);
    cleanUrl.searchParams.delete('verification_session');
    cleanUrl.searchParams.delete('api_verify_url');
    if (!serverSession) {
      const fragment = new URLSearchParams({
        verification_session: VERIFICATION_SESSION
      });
      if (API_VERIFY_URL) fragment.set('api_verify_url', API_VERIFY_URL);
      cleanUrl.hash = fragment.toString();
    }
    window.history.replaceState(null, '', cleanUrl);
  }

  if (telegram) {
    try { telegram.ready(); telegram.expand(); } catch (_) {}
  }

  function isRealTelegramContext() {
    return Boolean(
      window.TelegramWebviewProxy ||
      (telegram && telegram.initData) ||
      (telegram && telegram.platform && telegram.platform !== 'unknown')
    );
  }

  function track(eventName, properties) {
    try {
      if (window.AlphaCityTelemetry &&
          typeof window.AlphaCityTelemetry.track === 'function') {
        window.AlphaCityTelemetry.track(eventName, properties || {});
      }
    } catch (_) {}
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
    if (value instanceof ArrayBuffer) value = new Uint8Array(value);
    if (ArrayBuffer.isView(value) && !(value instanceof Uint8Array)) {
      value = new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    }
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

  function handleWalletChange(session) {
    selectedSignature = '';
    document.getElementById('signedStatus').hidden = true;
    signButton.hidden = false;
    signButton.disabled = false;
    if (!session) {
      selectedAddress = '';
      reviewPanel.hidden = true;
      return;
    }
    if (!isValidAddress(session.address)) {
      selectedAddress = '';
      reviewPanel.hidden = true;
      showNotice('walletNotice', 'The wallet returned an invalid Sui address.', 'error');
      return;
    }
    selectedAddress = canonicalAddress(session.address);
    reviewPanel.hidden = false;
    document.getElementById('connectedWallet').textContent =
      session.walletName || 'Sui Wallet';
    document.getElementById('connectedAddress').textContent = selectedAddress;
    document.getElementById('ownershipMessage').textContent =
      ownershipMessage(selectedAddress);
    clearNotice('walletNotice');
  }

  function initWalletConnector() {
    if (walletConnector) return;
    if (!window.AlphaCityWalletConnector ||
        typeof window.AlphaCityWalletConnector.create !== 'function') {
      throw new Error('The Alpha City wallet connector could not be loaded. Please refresh.');
    }
    walletConnector = window.AlphaCityWalletConnector.create({
      button: connectWalletButton,
      autoReconnect: false,
      persistSession: false,
      alwaysPrompt: true,
      requirePersonalMessage: true,
      connectLabel: 'Choose Wallet',
      onChange: handleWalletChange
    });
  }

  async function signOwnership() {
    if (!walletConnector || !selectedAddress) {
      throw new Error('Choose the wallet and account you want to register first.');
    }
    const message = new TextEncoder().encode(ownershipMessage(selectedAddress));
    const result = await walletConnector.signPersonalMessage(message);
    const signature = toBase64(result && result.signature ? result.signature : result);
    if (!signature) throw new Error('The wallet returned an invalid signature.');
    return signature;
  }

  function setRecoveryButtons() {
    const hasRestart = Boolean(restartUrl);
    document.getElementById('newLinkButton').hidden = !hasRestart;
    document.getElementById('resultNewLinkButton').hidden = !hasRestart;
  }

  async function fetchJsonWithTimeout(url, options, timeoutMs) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, { ...options, signal: controller.signal });
      let result;
      try {
        result = await response.json();
      } catch (_) {
        throw new Error('The verification service returned an invalid response.');
      }
      return { response, result };
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function loadContext() {
    if (contextLoading) return;
    contextLoading = true;
    document.getElementById('contextRetryButton').disabled = true;
    try {
      setBusy(true);
      contextRecovery.hidden = true;
      clearNotice('walletNotice');
      if (apiConfigurationError) throw new Error(apiConfigurationError);
      if (!VERIFICATION_SESSION) throw new Error('The verification session is missing.');
      const { response, result } = await fetchJsonWithTimeout(
        CONTEXT_URL,
        {
          headers: { Accept: 'application/json' },
          cache: 'no-store'
        },
        CONTEXT_TIMEOUT_MS
      );
      restartUrl = result.restart_url || '';
      telegramReturnUrl = result.telegram_return_url || '';
      setRecoveryButtons();
      if (!response.ok || !result.success) {
        throw new Error(result.error || 'This verification link is unavailable.');
      }
      if (result.verification_completed && result.verification_result) {
        showResult(
          result.verification_result,
          Boolean(result.verification_result.success)
        );
        return;
      }
      context = result;
      renderRequirements();
      initWalletConnector();
    } finally {
      contextLoading = false;
      document.getElementById('contextRetryButton').disabled = false;
      walletCard.setAttribute('aria-busy', 'false');
    }
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
    document.getElementById('retryButton').hidden = true;
    clearNotice('resultNotice');
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
      track('gate_check', { result: 'pass', source: 'wallet_verification' });
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
      track('gate_check', { result: 'fail', source: 'wallet_verification' });
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
    track('gate_check', { result: 'error', source: 'wallet_verification' });
  }

  async function submitVerification() {
    if (!selectedAddress || !selectedSignature || submissionInFlight) return;
    submissionInFlight = true;
    document.getElementById('retryButton').disabled = true;
    setStep(2);
    try {
      const { response, result } = await fetchJsonWithTimeout(
        API_VERIFY_URL,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Accept: 'application/json'
          },
          cache: 'no-store',
          body: JSON.stringify({
            verification_session: VERIFICATION_SESSION,
            wallet_address: selectedAddress,
            wallet_signature: selectedSignature
          })
        },
        SUBMISSION_TIMEOUT_MS
      );
      restartUrl = result.restart_url || restartUrl;
      telegramReturnUrl = result.telegram_return_url || telegramReturnUrl;
      setRecoveryButtons();
      showResult(result, response.ok);
    } catch (error) {
      setStep(3);
      document.getElementById('resultIcon').textContent = '⚠️';
      document.getElementById('resultTitle').textContent = 'Result not confirmed';
      document.getElementById('resultMessage').textContent =
        'The server response was interrupted. Try again to safely check the same registration.';
      document.getElementById('retryButton').hidden = false;
      showNotice(
        'resultNotice',
        error.name === 'AbortError'
          ? 'The request timed out.'
          : 'Could not reach the server.',
        'error'
      );
      track('client_error', {
        area: 'verification_submit',
        code: error.name || 'network_error'
      });
    } finally {
      submissionInFlight = false;
      document.getElementById('retryButton').disabled = false;
    }
  }

  function returnToTelegram() {
    if (isRealTelegramContext() && telegram && typeof telegram.close === 'function') {
      telegram.close();
      return;
    }
    if (telegramReturnUrl) {
      window.location.assign(telegramReturnUrl);
      return;
    }
    window.history.back();
  }

  function requestNewLink() {
    if (restartUrl) window.location.assign(restartUrl);
  }

  signButton.addEventListener('click', async () => {
    clearNotice('walletNotice');
    signButton.disabled = true;
    setBusy(true, 'Waiting for your wallet signature…');
    try {
      selectedSignature = await signOwnership();
      document.getElementById('signedStatus').hidden = false;
      signButton.hidden = true;
      clearNotice('walletNotice');
      track('transaction_sign', { status: 'success' });
      await submitVerification();
    } catch (error) {
      signButton.disabled = false;
      showNotice(
        'walletNotice',
        error.message || 'Signature request was cancelled.',
        'error'
      );
      track('transaction_sign', { status: 'failure' });
    } finally {
      setBusy(false);
    }
  });

  changeButton.addEventListener('click', async () => {
    try {
      clearNotice('walletNotice');
      if (!walletConnector) throw new Error('The wallet connector is not ready.');
      await walletConnector.walletOptions();
    } catch (error) {
      showNotice(
        'walletNotice',
        error.message || 'Wallet options could not be opened.',
        'error'
      );
    }
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
    connectWalletButton.textContent = 'Verification unavailable';
    connectWalletButton.disabled = true;
    contextRecovery.hidden = false;
    showNotice('walletNotice', error.message, 'error');
  }

  loadContext().catch(handleContextError);
})();
