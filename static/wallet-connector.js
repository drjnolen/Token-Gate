(function (root) {
    'use strict';

    const CANONICAL_KEY = 'alphacity_wallet';
    const SUI_CHAIN = 'sui:mainnet';
    const standardWallets = new Set();
    const registryListeners = new Set();
    let standardReady = false;
    let dialogCounter = 0;

    function track(eventName, properties) {
        try { root.AlphaCityTelemetry?.track(eventName, properties); } catch (_) {}
    }

    function normalizeName(value) {
        return String(value || '').toLowerCase().replace(/\s+/g, ' ').trim();
    }

    function canonicalWalletName(value) {
        const name = String(value || '').replace(/\s+/g, ' ').trim();
        return /^phantom\s*\(\s*sui\s*\)$/i.test(name) ? 'Phantom' : name;
    }

    function walletKey(value) {
        return normalizeName(canonicalWalletName(value));
    }

    function telemetryWalletProvider(value) {
        const key = walletKey(value);
        for (const provider of ['slush', 'phantom', 'suiet', 'nightly', 'martian']) {
            if (key.includes(provider)) return provider;
        }
        if (key.includes('sui wallet')) return 'sui_wallet';
        return 'other';
    }

    function shortAddress(value) {
        const address = String(value || '');
        return address ? `${address.slice(0, 6)}...${address.slice(-4)}` : '';
    }

    function accountAddresses(accounts) {
        return [...new Set((accounts || []).map((account) => (
            typeof account === 'string' ? account : account?.address
        )).filter(Boolean))];
    }

    function suiAccountAddresses(accounts) {
        return accountAddresses((accounts || []).filter((account) => (
            typeof account === 'string' || !account?.chains?.length || account.chains.includes(SUI_CHAIN)
        )));
    }

    function readSession() {
        try {
            const parsed = JSON.parse(localStorage.getItem(CANONICAL_KEY) || 'null');
            if (!parsed?.walletName || !parsed?.address) return null;
            return { walletName: canonicalWalletName(parsed.walletName), address: parsed.address };
        } catch (_) {
            return null;
        }
    }

    function writeSession(walletName, address) {
        const nextSession = { walletName: canonicalWalletName(walletName), address };
        localStorage.setItem(CANONICAL_KEY, JSON.stringify(nextSession));
        window.dispatchEvent(new CustomEvent('alphacity-wallet-change', { detail: nextSession }));
    }

    function clearSession() {
        localStorage.removeItem(CANONICAL_KEY);
        window.dispatchEvent(new CustomEvent('alphacity-wallet-change', { detail: null }));
    }

    function setupWalletStandard() {
        if (standardReady || typeof window === 'undefined') return;
        standardReady = true;
        const register = (...wallets) => {
            wallets.filter(Boolean).forEach((wallet) => standardWallets.add(wallet));
            registryListeners.forEach((listener) => {
                try { listener(); } catch (_) {}
            });
            return () => wallets.forEach((wallet) => standardWallets.delete(wallet));
        };
        const api = Object.freeze({ register });
        window.addEventListener('wallet-standard:register-wallet', (event) => {
            try { event.detail(api); } catch (_) {}
        });
        try {
            window.dispatchEvent(new CustomEvent('wallet-standard:app-ready', { detail: api }));
        } catch (_) {}
    }

    function legacyAdapter(provider, name, options = {}) {
        let selectedAddress = null;
        const supportsAccountSwitch = options.supportsAccountSwitch !== false;
        const forceFreshConnect = options.forceFreshConnect === true;
        return {
            name: canonicalWalletName(name),
            source: 'legacy',
            supportsAccountSwitch,
            supportsPersonalMessage: Boolean(provider.signPersonalMessage || provider.signMessage),
            forceFreshConnect,
            async connect({ silent, preferredAddress } = {}) {
                selectedAddress = preferredAddress || selectedAddress;
                let result = null;
                if (!silent && forceFreshConnect) {
                    try { if (provider.disconnect) await provider.disconnect(); } catch (_) {}
                    try { if (provider.requestPermissions) await provider.requestPermissions(); } catch (_) {}
                }
                if (provider.connect) {
                    result = await provider.connect(selectedAddress ? {
                        onlyIfTrusted: Boolean(silent),
                        account: selectedAddress,
                    } : { onlyIfTrusted: Boolean(silent) });
                } else if (!silent && provider.requestPermissions) {
                    await provider.requestPermissions();
                }
                const addresses = accountAddresses(
                    result?.accounts || provider.accounts || (provider.getAccounts ? await provider.getAccounts() : []),
                );
                selectedAddress = result?.address || provider.account?.address || (
                    selectedAddress && addresses.includes(selectedAddress) ? selectedAddress : addresses[0]
                );
                if (!selectedAddress && provider.requestAccount && !silent) {
                    const account = await provider.requestAccount();
                    selectedAddress = typeof account === 'string' ? account : account?.address;
                }
                if (!selectedAddress) throw new Error(`${name} did not return a Sui account.`);
                return selectedAddress;
            },
            async getAccounts() {
                if (provider.getAccounts) {
                    const addresses = accountAddresses(await provider.getAccounts());
                    if (addresses.length) return addresses;
                }
                const addresses = accountAddresses(provider.accounts || []);
                return addresses.length ? addresses : (selectedAddress ? [selectedAddress] : []);
            },
            async selectAccount(address) {
                if (!supportsAccountSwitch) throw new Error(`${name} requires account switching inside the wallet.`);
                selectedAddress = address;
                if (provider.switchAccount) await provider.switchAccount(address);
                else if (provider.selectAccount) await provider.selectAccount(address);
                else if (provider.connect) {
                    try { await provider.connect({ account: address, onlyIfTrusted: false }); } catch (_) {}
                }
            },
            async signAndExecuteTransaction(transaction) {
                if (provider.signAndExecuteTransaction) {
                    return provider.signAndExecuteTransaction({ transaction });
                }
                if (provider.signAndExecuteTransactionBlock) {
                    return provider.signAndExecuteTransactionBlock({
                        transactionBlock: transaction,
                        options: { showEffects: true, showEvents: true, showObjectChanges: true },
                    });
                }
                throw new Error(`${name} cannot sign Sui transactions.`);
            },
            async signPersonalMessage(message) {
                const input = {
                    message,
                    account: selectedAddress,
                    address: selectedAddress,
                };
                if (provider.signPersonalMessage) return provider.signPersonalMessage(input);
                if (provider.signMessage) return provider.signMessage(input);
                throw new Error(`${name} cannot sign Sui personal messages.`);
            },
            subscribe(callback) {
                if (typeof provider.on !== 'function') return () => {};
                const disposers = [];
                const handleChange = (payload) => {
                    const values = payload?.accounts || (payload?.account ? [payload.account] : payload);
                    callback(accountAddresses(Array.isArray(values) ? values : [values]));
                };
                ['accountsChanged', 'accountChanged'].forEach((eventName) => {
                    try {
                        const returned = provider.on(eventName, handleChange);
                        if (typeof returned === 'function') disposers.push(returned);
                        else if (typeof provider.off === 'function') disposers.push(() => provider.off(eventName, handleChange));
                        else if (typeof provider.removeListener === 'function') disposers.push(() => provider.removeListener(eventName, handleChange));
                    } catch (_) {}
                });
                return () => disposers.forEach((dispose) => { try { dispose(); } catch (_) {} });
            },
            async disconnect() {
                if (provider.disconnect) await provider.disconnect();
                selectedAddress = null;
            },
        };
    }

    function standardAdapter(wallet) {
        const connectFeature = wallet.features?.['standard:connect'];
        const disconnectFeature = wallet.features?.['standard:disconnect'];
        const modernSignFeature = wallet.features?.['sui:signAndExecuteTransaction'];
        const legacySignFeature = wallet.features?.['sui:signAndExecuteTransactionBlock'];
        const personalMessageFeature = wallet.features?.['sui:signPersonalMessage'];
        const legacyMessageFeature = wallet.features?.['sui:signMessage'];
        const name = canonicalWalletName(wallet.name);
        const forceFreshConnect = walletKey(name).includes('slush');
        let connectedAccounts = [];
        let selectedAccount = null;
        return {
            name,
            source: 'standard',
            supportsAccountSwitch: true,
            supportsPersonalMessage: Boolean(
                personalMessageFeature?.signPersonalMessage || legacyMessageFeature?.signMessage
            ),
            forceFreshConnect,
            async connect({ silent, preferredAddress } = {}) {
                if (!silent && forceFreshConnect && disconnectFeature?.disconnect) {
                    try { await disconnectFeature.disconnect(); } catch (_) {}
                }
                let result;
                try { result = await connectFeature.connect({ silent: Boolean(silent) }); }
                catch (error) {
                    if (silent) throw error;
                    result = await connectFeature.connect();
                }
                connectedAccounts = result?.accounts || wallet.accounts || [];
                const availableAccounts = [...connectedAccounts, ...(wallet.accounts || [])]
                    .filter((account) => !account?.chains?.length || account.chains.includes(SUI_CHAIN));
                selectedAccount = availableAccounts.find((account) => account.address === preferredAddress)
                    || availableAccounts[0];
                if (!selectedAccount?.address) throw new Error(`${name} returned no Sui account.`);
                return selectedAccount.address;
            },
            async getAccounts() {
                return suiAccountAddresses([...connectedAccounts, ...(wallet.accounts || [])]);
            },
            async selectAccount(address) {
                selectedAccount = [...connectedAccounts, ...(wallet.accounts || [])]
                    .find((account) => account.address === address) || selectedAccount;
            },
            async signAndExecuteTransaction(transaction) {
                const account = selectedAccount || [...connectedAccounts, ...(wallet.accounts || [])]
                    .find((candidate) => !candidate.chains?.length || candidate.chains.includes(SUI_CHAIN));
                if (!account) throw new Error(`${name} returned no Sui account.`);
                if (modernSignFeature?.signAndExecuteTransaction) {
                    return modernSignFeature.signAndExecuteTransaction({ transaction, account, chain: SUI_CHAIN });
                }
                if (legacySignFeature?.signAndExecuteTransactionBlock) {
                    return legacySignFeature.signAndExecuteTransactionBlock({
                        transactionBlock: transaction,
                        account,
                        chain: SUI_CHAIN,
                        options: { showEffects: true, showEvents: true, showObjectChanges: true },
                    });
                }
                throw new Error(`${name} cannot sign Sui transactions.`);
            },
            async signPersonalMessage(message) {
                const account = selectedAccount || [...connectedAccounts, ...(wallet.accounts || [])]
                    .find((candidate) => !candidate.chains?.length || candidate.chains.includes(SUI_CHAIN));
                if (!account) throw new Error(`${name} returned no Sui account.`);
                if (personalMessageFeature?.signPersonalMessage) {
                    return personalMessageFeature.signPersonalMessage({ message, account });
                }
                if (legacyMessageFeature?.signMessage) {
                    return legacyMessageFeature.signMessage({ message, account });
                }
                throw new Error(`${name} cannot sign Sui personal messages.`);
            },
            subscribe(callback) {
                const eventsFeature = wallet.features?.['standard:events'];
                if (typeof eventsFeature?.on !== 'function') return () => {};
                const unsubscribe = eventsFeature.on('change', ({ accounts } = {}) => {
                    connectedAccounts = accounts || wallet.accounts || [];
                    const availableAccounts = connectedAccounts
                        .filter((account) => !account?.chains?.length || account.chains.includes(SUI_CHAIN));
                    selectedAccount = availableAccounts.find((account) => account.address === selectedAccount?.address)
                        || availableAccounts[0]
                        || null;
                    callback(suiAccountAddresses(connectedAccounts));
                });
                return typeof unsubscribe === 'function' ? unsubscribe : () => {};
            },
            async disconnect() {
                if (disconnectFeature?.disconnect) await disconnectFeature.disconnect();
                connectedAccounts = [];
                selectedAccount = null;
            },
        };
    }

    function discoverWallets() {
        setupWalletStandard();
        const adapters = [];
        const positions = new Map();
        const add = (adapter) => {
            const key = walletKey(adapter.name);
            const index = positions.get(key);
            if (index == null) {
                positions.set(key, adapters.length);
                adapters.push(adapter);
                return;
            }
            if (adapter.source === 'standard' && adapters[index].source !== 'standard') adapters[index] = adapter;
        };

        if (root.suiet) add(legacyAdapter(root.suiet, 'Suiet'));
        if (root.slush?.sui) add(legacyAdapter(root.slush.sui, 'Slush', {
            supportsAccountSwitch: false,
            forceFreshConnect: true,
        }));
        if (root.suiWallet) add(legacyAdapter(root.suiWallet, 'Sui Wallet', {
            supportsAccountSwitch: false,
            forceFreshConnect: true,
        }));
        if (root.phantom?.sui) add(legacyAdapter(root.phantom.sui, 'Phantom'));
        if (root.nightly?.sui || root.nightly) {
            add(legacyAdapter(root.nightly?.sui || root.nightly, 'Nightly'));
        }
        if (root.martian?.sui || root.martian) {
            add(legacyAdapter(root.martian?.sui || root.martian, 'Martian'));
        }
        standardWallets.forEach((wallet) => {
            const canConnect = Boolean(wallet.features?.['standard:connect']);
            const chains = wallet.chains || [];
            if (canConnect && (!chains.length || chains.includes(SUI_CHAIN))) add(standardAdapter(wallet));
        });
        return adapters;
    }

    function createElement(tag, className, text) {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (text != null) element.textContent = text;
        return element;
    }

    function ensureStyles() {
        if (document.documentElement?.dataset.walletConnectorStyles === 'external') return;
        if (document.getElementById('ac-wallet-styles')) return;
        const style = document.createElement('style');
        style.id = 'ac-wallet-styles';
        style.textContent = `
            .ac-wallet-overlay{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;padding:1rem;background:rgba(0,0,0,.72);backdrop-filter:blur(4px)}
            .ac-wallet-dialog{width:100%;max-width:28rem;border:1px solid #374151;border-radius:1rem;background:#1f2937;padding:1.25rem;box-shadow:0 24px 70px rgba(0,0,0,.45);font-family:Inter,ui-sans-serif,system-ui,sans-serif}
            .ac-wallet-dialog-title{margin:0;color:#fff;font-size:1.125rem;font-weight:700}
            .ac-wallet-dialog-copy{margin:.25rem 0 1rem;color:#9ca3af;font-size:.875rem;line-height:1.5}
            .ac-wallet-choice-list{display:grid;gap:.5rem}
            .ac-wallet-choice,.ac-wallet-cancel{width:100%;border:1px solid #4b5563;border-radius:.65rem;background:transparent;padding:.75rem 1rem;color:#fff;text-align:left;transition:border-color .18s,background .18s,color .18s}
            .ac-wallet-choice:hover,.ac-wallet-choice:focus-visible{border-color:#3b82f6;background:rgba(59,130,246,.08)}
            .ac-wallet-cancel{margin-top:1rem;color:#9ca3af;text-align:center}
            .ac-wallet-cancel:hover,.ac-wallet-cancel:focus-visible{border-color:#6b7280;color:#fff}
            .ac-wallet-choice:focus-visible,.ac-wallet-cancel:focus-visible{outline:2px solid #60a5fa;outline-offset:2px}
            .ac-wallet-dot{width:.5rem;height:.5rem;flex:0 0 auto;border-radius:999px;background:#22c55e}
            @media (prefers-reduced-motion:reduce){.ac-wallet-choice,.ac-wallet-cancel{transition:none}}
        `;
        document.head.append(style);
    }

    function walletIcon() {
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('class', 'h-5 w-5');
        svg.setAttribute('viewBox', '0 0 20 20');
        svg.setAttribute('fill', 'currentColor');
        svg.setAttribute('aria-hidden', 'true');
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('fill-rule', 'evenodd');
        path.setAttribute('clip-rule', 'evenodd');
        path.setAttribute('d', 'M18 8a6 6 0 01-7.743 5.743L10 14l-1 1-1 1H6v2H2v-4l4.257-4.257A6 6 0 1118 8zm-6-4a1 1 0 100 2 2 2 0 010-2z');
        svg.append(path);
        return svg;
    }

    function choose({ title, description, options, cancelLabel = 'Cancel' }) {
        return new Promise((resolve) => {
            ensureStyles();
            const previousFocus = document.activeElement;
            const overlay = createElement('div', 'ac-wallet-overlay');
            const card = createElement('section', 'ac-wallet-dialog');
            dialogCounter += 1;
            const dialogId = `ac-wallet-dialog-${dialogCounter}`;
            const descriptionId = `${dialogId}-description`;
            card.setAttribute('role', 'dialog');
            card.setAttribute('aria-modal', 'true');
            card.setAttribute('aria-labelledby', dialogId);
            card.setAttribute('aria-describedby', descriptionId);
            const heading = createElement('h2', 'ac-wallet-dialog-title', title);
            heading.id = dialogId;
            const copy = createElement('p', 'ac-wallet-dialog-copy', description);
            copy.id = descriptionId;
            card.append(heading, copy);
            const list = createElement('div', 'ac-wallet-choice-list');
            let background = [];
            const finish = (value) => {
                background.forEach(({ element, inert, ariaHidden }) => {
                    element.inert = inert;
                    if (ariaHidden == null) element.removeAttribute('aria-hidden');
                    else element.setAttribute('aria-hidden', ariaHidden);
                });
                overlay.remove();
                if (previousFocus instanceof HTMLElement) previousFocus.focus();
                resolve(value || null);
            };
            options.forEach((option) => {
                const button = createElement('button', 'ac-wallet-choice', option.label);
                button.type = 'button';
                button.addEventListener('click', () => finish(option.value));
                list.append(button);
            });
            const cancel = createElement('button', 'ac-wallet-cancel', cancelLabel);
            cancel.type = 'button';
            cancel.addEventListener('click', () => finish(null));
            card.append(list, cancel);
            overlay.append(card);
            overlay.addEventListener('click', (event) => { if (event.target === overlay) finish(null); });
            overlay.addEventListener('keydown', (event) => {
                if (event.key === 'Escape') {
                    event.preventDefault();
                    finish(null);
                    return;
                }
                if (event.key !== 'Tab') return;
                const focusable = [...card.querySelectorAll('button:not(:disabled), [href], [tabindex]:not([tabindex="-1"])')];
                if (!focusable.length) return;
                const first = focusable[0];
                const last = focusable[focusable.length - 1];
                if (event.shiftKey && document.activeElement === first) {
                    event.preventDefault();
                    last.focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                    event.preventDefault();
                    first.focus();
                }
            });
            document.body.append(overlay);
            background = [...document.body.children]
                .filter((element) => element !== overlay)
                .map((element) => ({ element, inert: element.inert, ariaHidden: element.getAttribute('aria-hidden') }));
            background.forEach(({ element }) => {
                element.inert = true;
                element.setAttribute('aria-hidden', 'true');
            });
            card.querySelector('button')?.focus();
        });
    }

    function createConnector(options = {}) {
        ensureStyles();
        const button = typeof options.button === 'string' ? document.querySelector(options.button) : options.button;
        if (!button) throw new Error('Wallet connector button was not found.');
        const onChange = typeof options.onChange === 'function' ? options.onChange : () => {};
        const persistSession = options.persistSession !== false;
        const autoReconnect = options.autoReconnect !== false && persistSession;
        const alwaysPrompt = options.alwaysPrompt === true;
        const requirePersonalMessage = options.requirePersonalMessage === true;
        const connectLabel = String(options.connectLabel || 'Connect Wallet');
        let adapter = null;
        let address = '';
        let walletName = '';
        let busy = false;
        let unsubscribe = null;
        let reconnectSession = null;
        let reconnectTimer = null;
        let reconnectAttempt = 0;
        let destroyed = false;
        let accountSwitchInProgress = false;

        function session() {
            return address && walletName ? { walletName, address } : null;
        }

        function render() {
            button.replaceChildren();
            if (address) {
                button.classList.remove('bg-brand-primary');
                button.classList.remove('bg-green-600', 'hover:bg-green-500');
                button.classList.add('bg-dark-card', 'border', 'border-gray-700');
                button.append(createElement('span', 'ac-wallet-dot'));
                button.append(createElement('span', '', shortAddress(address)));
                button.setAttribute('aria-label', `Wallet options for ${address}`);
            } else {
                button.classList.add('bg-brand-primary');
                button.classList.remove('bg-dark-card', 'border', 'border-gray-700', 'bg-green-600', 'hover:bg-green-500');
                button.append(walletIcon());
                button.append(createElement('span', '', busy ? 'Connecting...' : connectLabel));
                button.setAttribute('aria-label', busy ? 'Connecting wallet' : connectLabel);
            }
            button.disabled = busy;
        }

        function update(nextAdapter, nextAddress, notify = true, persist = true) {
            const adapterChanged = adapter !== (nextAdapter || null);
            if (adapterChanged && unsubscribe) {
                try { unsubscribe(); } catch (_) {}
                unsubscribe = null;
            }
            adapter = nextAdapter || null;
            address = nextAddress || '';
            walletName = adapter?.name || '';
            if (adapterChanged && adapter?.subscribe) {
                try { unsubscribe = adapter.subscribe(handleAccountsChanged); } catch (_) { unsubscribe = null; }
            }
            if (persistSession && persist) {
                if (address && walletName) writeSession(walletName, address);
                else clearSession();
            }
            render();
            if (notify) onChange(session());
        }

        function handleAccountsChanged(accounts) {
            if (!adapter || busy || accountSwitchInProgress) return;
            const addresses = accountAddresses(accounts);
            if (!addresses.length) {
                update(null, '');
                return;
            }
            const nextAddress = addresses.includes(address) ? address : addresses[0];
            if (nextAddress !== address) update(adapter, nextAddress);
        }

        async function selectProvider(preferredName, automatic = false) {
            let wallets = discoverWallets();
            if (requirePersonalMessage) {
                wallets = wallets.filter((wallet) => wallet.supportsPersonalMessage);
            }
            if (!wallets.length) {
                await new Promise((resolve) => setTimeout(resolve, 600));
                wallets = discoverWallets();
                if (requirePersonalMessage) {
                    wallets = wallets.filter((wallet) => wallet.supportsPersonalMessage);
                }
            }
            if (!wallets.length) throw new Error('No supported wallet found. Install Slush, Suiet, Sui Wallet, or Phantom with Sui enabled.');
            const preferred = wallets.find((wallet) => walletKey(wallet.name) === walletKey(preferredName));
            if (preferred) return preferred;
            if (automatic && preferredName) throw new Error(`${preferredName} is not available for automatic reconnection.`);
            if (wallets.length === 1 && !alwaysPrompt) return wallets[0];
            return choose({
                title: 'Select Wallet',
                description: 'Choose which wallet to connect.',
                options: wallets.map((wallet) => ({ label: wallet.name, value: wallet })),
            });
        }

        async function connect({ automatic = false, preferredSession = null, switchingFrom = null } = {}) {
            if (busy) return session();
            busy = true;
            render();
            try {
                if (!automatic) track('wallet_connect', { status: 'started' });
                const nextAdapter = await selectProvider(preferredSession?.walletName, automatic);
                if (!nextAdapter) return session();
                const nextAddress = await nextAdapter.connect({
                    silent: automatic,
                    preferredAddress: preferredSession?.address,
                });
                const accounts = await nextAdapter.getAccounts();
                let chosenAddress = nextAddress;
                if (!automatic && accounts.length > 1) {
                    chosenAddress = await choose({
                        title: 'Choose Account',
                        description: `Select the account to connect from ${nextAdapter.name}.`,
                        options: accounts.map((account) => ({
                            label: account === nextAddress ? `${shortAddress(account)} (Current)` : shortAddress(account),
                            value: account,
                        })),
                    }) || nextAddress;
                    await nextAdapter.selectAccount(chosenAddress);
                }
                update(nextAdapter, chosenAddress);
                const switchedProvider = switchingFrom &&
                    walletKey(switchingFrom.name) !== walletKey(nextAdapter.name);
                if (switchedProvider && switchingFrom?.disconnect) {
                    try { await switchingFrom.disconnect(); } catch (_) {}
                }
                track('wallet_connect', {
                    status: automatic ? 'reconnected' : 'connected',
                    provider: telemetryWalletProvider(nextAdapter.name),
                });
                return session();
            } catch (error) {
                if (!automatic) {
                    track('wallet_connect', {
                        status: 'error',
                        provider: telemetryWalletProvider(preferredSession?.walletName),
                    });
                    root.AlphaCityTelemetry?.error('wallet_connect', error);
                }
                throw error;
            } finally {
                busy = false;
                render();
            }
        }

        async function disconnect() {
            const disconnectedProvider = walletName;
            try { if (adapter?.disconnect) await adapter.disconnect(); } catch (_) {}
            update(null, '');
            track('wallet_connect', {
                status: 'disconnected',
                provider: telemetryWalletProvider(disconnectedProvider),
            });
        }

        async function switchAccount() {
            if (!adapter || adapter.supportsAccountSwitch === false) {
                await choose({
                    title: 'Switch Account in Wallet',
                    description: 'Switch the active account inside your wallet, then use Switch Wallet Provider to reconnect.',
                    options: [],
                    cancelLabel: 'OK',
                });
                return;
            }
            const currentAdapter = adapter;
            const accounts = await currentAdapter.getAccounts();
            let availableAccounts = accounts;
            if (availableAccounts.length <= 1 && currentAdapter.connect) {
                const previousAddress = address;
                let reconnectedAddress = '';
                accountSwitchInProgress = true;
                try {
                    reconnectedAddress = await currentAdapter.connect({ silent: false });
                    availableAccounts = await currentAdapter.getAccounts();
                } finally {
                    accountSwitchInProgress = false;
                }
                if (reconnectedAddress && reconnectedAddress !== previousAddress) {
                    update(currentAdapter, reconnectedAddress);
                    return;
                }
                if (currentAdapter.forceFreshConnect) {
                    if (reconnectedAddress) update(currentAdapter, reconnectedAddress);
                    return;
                }
            }
            if (availableAccounts.length <= 1) {
                await choose({
                    title: 'No Alternate Accounts',
                    description: 'Add or enable another account inside your wallet, then try again.',
                    options: [],
                    cancelLabel: 'OK',
                });
                return;
            }
            const nextAddress = await choose({
                title: 'Switch Account',
                description: `Select an account from ${adapter.name}.`,
                options: availableAccounts.map((account) => ({
                    label: account === address ? `${shortAddress(account)} (Current)` : shortAddress(account),
                    value: account,
                })),
            });
            if (!nextAddress || nextAddress === address) return;
            await currentAdapter.selectAccount(nextAddress);
            update(currentAdapter, nextAddress);
        }

        async function walletOptions() {
            const choices = [];
            if (adapter && adapter.supportsAccountSwitch !== false) choices.push({ label: 'Switch Account', value: 'account' });
            choices.push({ label: 'Switch Wallet Provider', value: 'provider' });
            choices.push({ label: 'Disconnect', value: 'disconnect' });
            const action = await choose({
                title: 'Wallet Options',
                description: `${shortAddress(address)} connected via ${walletName || 'wallet'}.`,
                options: choices,
            });
            if (action === 'account') await switchAccount();
            if (action === 'provider') {
                await connect({ switchingFrom: adapter });
            }
            if (action === 'disconnect') await disconnect();
        }

        async function signAndExecuteTransaction(transaction) {
            if (!adapter || !address) throw new Error('Connect a wallet first.');
            if (typeof adapter.signAndExecuteTransaction !== 'function') {
                throw new Error(`${walletName || 'This wallet'} cannot sign Sui transactions.`);
            }
            track('transaction_sign', { status: 'requested' });
            try {
                const result = await adapter.signAndExecuteTransaction(transaction);
                track('transaction_sign', { status: 'succeeded' });
                return result;
            } catch (error) {
                track('transaction_sign', { status: 'error' });
                root.AlphaCityTelemetry?.error('transaction_sign', error);
                throw error;
            }
        }

        async function signPersonalMessage(message) {
            if (!adapter || !address) throw new Error('Connect a wallet first.');
            if (typeof adapter.signPersonalMessage !== 'function') {
                throw new Error(`${walletName || 'This wallet'} cannot sign Sui personal messages.`);
            }
            return adapter.signPersonalMessage(message);
        }

        function clearReconnectTimer() {
            if (reconnectTimer) clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }

        function scheduleReconnect(nextSession, reset = false) {
            if (!nextSession?.walletName || !nextSession?.address || destroyed) return;
            if (reset || !reconnectSession ||
                walletKey(reconnectSession.walletName) !== walletKey(nextSession.walletName) ||
                reconnectSession.address !== nextSession.address) {
                reconnectAttempt = 0;
            }
            reconnectSession = {
                walletName: canonicalWalletName(nextSession.walletName),
                address: nextSession.address,
            };
            clearReconnectTimer();
            const delays = [0, 600, 1500, 3500, 7000];
            const delay = delays[Math.min(reconnectAttempt, delays.length - 1)];
            reconnectTimer = setTimeout(async () => {
                reconnectTimer = null;
                if (destroyed || address || busy || !reconnectSession) return;
                try {
                    await connect({ automatic: true, preferredSession: reconnectSession });
                    reconnectSession = null;
                    reconnectAttempt = 0;
                } catch (_) {
                    reconnectAttempt += 1;
                    if (reconnectAttempt < delays.length) scheduleReconnect(reconnectSession);
                }
            }, delay);
        }

        function handleExternalSession(nextSession) {
            if (!nextSession) {
                clearReconnectTimer();
                reconnectSession = null;
                if (address || adapter) update(null, '', true);
                return;
            }
            if (walletKey(nextSession.walletName) === walletKey(walletName) && nextSession.address === address) return;
            if (address || adapter) update(null, '', true, false);
            scheduleReconnect(nextSession, true);
        }

        function handleStorage(event) {
            if (event.key !== CANONICAL_KEY) return;
            let nextSession = null;
            try {
                const parsed = JSON.parse(event.newValue || 'null');
                if (parsed?.walletName && parsed?.address) nextSession = parsed;
            } catch (_) {}
            handleExternalSession(nextSession);
        }

        function handleSessionEvent(event) {
            handleExternalSession(event.detail || null);
        }

        button.addEventListener('click', () => {
            track('wallet_dialog', { action: address ? 'options' : 'connect' });
            const action = address ? walletOptions() : connect();
            action.catch((error) => {
                console.error('[wallet-connector]', error);
                choose({
                    title: 'Wallet Connection Failed',
                    description: error.message || 'The wallet could not be connected.',
                    options: [],
                    cancelLabel: 'OK',
                });
            });
        });

        if (autoReconnect) {
            window.addEventListener('storage', handleStorage);
            window.addEventListener('alphacity-wallet-change', handleSessionEvent);
        }
        const handleRegistryChange = () => {
            if (!address && reconnectSession) scheduleReconnect(reconnectSession);
        };
        if (autoReconnect) registryListeners.add(handleRegistryChange);

        const persisted = autoReconnect ? readSession() : null;
        if (persisted) {
            render();
            onChange(null);
            scheduleReconnect(persisted, true);
        } else {
            render();
            onChange(null);
        }

        return Object.freeze({
            connect,
            disconnect,
            switchAccount,
            walletOptions,
            signAndExecuteTransaction,
            signPersonalMessage,
            getSession: session,
            destroy() {
                destroyed = true;
                clearReconnectTimer();
                if (autoReconnect) {
                    window.removeEventListener('storage', handleStorage);
                    window.removeEventListener('alphacity-wallet-change', handleSessionEvent);
                    registryListeners.delete(handleRegistryChange);
                }
                if (unsubscribe) {
                    try { unsubscribe(); } catch (_) {}
                    unsubscribe = null;
                }
            },
        });
    }

    root.AlphaCityWalletConnector = Object.freeze({
        create: createConnector,
        shortAddress,
        canonicalWalletName,
    });
})(typeof window !== 'undefined' ? window : globalThis);
