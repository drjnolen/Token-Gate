"""Export the canonical verification UI into an Alphacity checkout.

Usage:
    python scripts/sync_verify_page.py /path/to/Alphacity
    python scripts/sync_verify_page.py --check /path/to/Alphacity
"""

from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML_SOURCE = ROOT / "templates" / "verify.html"
JS_SOURCE = ROOT / "static" / "verify.js"
CSS_SOURCE = ROOT / "static" / "verify.css"
CONNECTOR_SOURCE = ROOT / "static" / "wallet-connector.js"

VERIFY_TEST = r"""const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');

const html = fs.readFileSync('verify/index.html', 'utf8');
const app = fs.readFileSync('verify/app.js', 'utf8');
const css = fs.readFileSync('verify/styles.css', 'utf8');
const walletConnector = fs.readFileSync('shared/wallet-connector.js', 'utf8');
const ciWorkflow = fs.readFileSync('.github/workflows/ci.yml', 'utf8');
const deployWorkflow = fs.readFileSync('.github/workflows/deploy.yml', 'utf8');
const source = html + '\n' + app;

test('verification page contains no legacy Sui RPC client or browser authority', () => {
  for (const forbidden of [
    '/shared/sui-client.js',
    'AlphaCitySui',
    'jsonrpc',
    'suix_',
    'sui_getObject',
    'verify_token',
    'balance_verified',
    'token_balance:',
    'nft_count:',
  ]) {
    assert.equal(source.includes(forbidden), false, `found forbidden text: ${forbidden}`);
  }
});

test('verification page uses session context and a minimal signed payload', () => {
  assert.match(app, /verification_session: VERIFICATION_SESSION/);
  assert.match(app, /\/api\/verification-context/);
  assert.match(app, /wallet_address: selectedAddress/);
  assert.match(app, /wallet_signature: selectedSignature/);
  assert.match(app, /Session: ' \+ VERIFICATION_SESSION/);
  assert.match(app, /Telegram user: ' \+ context\.telegram_user_id/);
  assert.match(app, /Group: ' \+ context\.group_id/);
  assert.match(app, /Wallet: ' \+ canonicalAddress\(address\)/);
});

test('signing automatically starts server verification after explicit review', () => {
  assert.match(html, /id="connectWalletButton"/);
  assert.match(html, /id="ownershipMessage"/);
  assert.match(html, /id="signButton"/);
  assert.doesNotMatch(html, /id="submitButton"/);
  assert.match(
    html,
    /Confirm wallet and holdings via non-transactional signature\. Zero risk to your holdings/,
  );
  assert.match(app, /AlphaCityWalletConnector\.create/);
  assert.match(app, /alwaysPrompt:\s*true/);
  assert.match(app, /autoReconnect:\s*false/);
  assert.match(app, /persistSession:\s*false/);
  assert.match(app, /requirePersonalMessage:\s*true/);
  assert.match(app, /selectedSignature = await signOwnership\(\)/);
  assert.match(app, /await submitVerification\(\)/);
  assert.ok(
    app.indexOf('initWalletConnector()') < app.indexOf("signButton.addEventListener"),
  );
  assert.ok(
    app.indexOf('selectedSignature = await signOwnership()') <
      app.indexOf('await submitVerification()'),
  );
});

test('session secrets use fragments and are scrubbed after completion', () => {
  assert.match(app, /window\.location\.hash/);
  assert.match(app, /window\.history\.replaceState/);
  assert.match(app, /scrubSensitiveUrl/);
  assert.match(app, /query\.has\('verification_session'\)/);
});

test('verification page has recovery and registered-but-ineligible states', () => {
  assert.match(html, /Request a new link/);
  assert.match(html, /Return to Telegram/);
  assert.match(app, /Wallet registered — requirements not met/);
  assert.match(app, /result\.wallet_registered/);
  assert.match(app, /result\.eligibility_status === 'fail'/);
  assert.match(app, /verification_completed/);
  assert.match(app, /Result not confirmed/);
});

test('verification service routing fails closed and requests are bounded', () => {
  assert.match(app, /apiConfigurationError/);
  assert.match(app, /untrusted verification API URL/);
  assert.doesNotMatch(app, /apiHostAllowed \? requestedApiUrl : new URL\(DEFAULT_API_VERIFY_URL\)/);
  assert.match(app, /CONTEXT_TIMEOUT_MS/);
  assert.match(app, /SUBMISSION_TIMEOUT_MS/);
  assert.match(app, /fetchJsonWithTimeout/);
  assert.match(app, /submissionInFlight/);
});

test('universal connector supports explicit modern and legacy Sui wallet selection', () => {
  assert.match(html, /\/shared\/wallet-connector\.js/);
  assert.match(walletConnector, /new CustomEvent\('wallet-standard:app-ready'/);
  assert.match(walletConnector, /return \(\) => wallets\.forEach/);
  assert.match(walletConnector, /sui:mainnet/);
  assert.match(walletConnector, /root\.slush\?\.sui/);
  assert.match(walletConnector, /sui:signPersonalMessage/);
  assert.match(walletConnector, /sui:signMessage/);
  assert.doesNotMatch(walletConnector, /standard:signMessage/);
  assert.match(walletConnector, /alwaysPrompt/);
  assert.match(walletConnector, /signPersonalMessage/);
  assert.match(walletConnector, /wallets\.filter\(\(wallet\) => wallet\.supportsPersonalMessage\)/);
  assert.doesNotMatch(app, /wallet-standard:register-wallet/);
});

test('verification telemetry contains no wallet or session identifiers', () => {
  assert.match(app, /track\('transaction_sign'/);
  assert.match(app, /track\('gate_check'/);
  assert.match(walletConnector, /telemetryWalletProvider/);
  const trackCalls = [...(app + walletConnector).matchAll(/\btrack\('[^;]+\);/g)]
    .map(match => match[0]).join('\n');
  assert.doesNotMatch(trackCalls, /\b(?:address|signature|session|balance|telegram)\s*:/i);
  assert.doesNotMatch(trackCalls, /provider:\s*(?:wallet|adapter|nextAdapter)\.name/);
});

test('verification regressions gate pull requests and production deployment', () => {
  assert.match(ciWorkflow, /pull_request:/);
  assert.match(ciWorkflow, /npm test/);
  assert.match(ciWorkflow, /npm audit --audit-level=high/);
  assert.match(deployWorkflow, /npm test/);
  assert.match(deployWorkflow, /npm audit --audit-level=high/);
  assert.ok(
    deployWorkflow.indexOf('npm test') <
      deployWorkflow.indexOf('actions\/upload-pages-artifact'),
  );
});

test('verification page uses external assets without inline execution or styles', () => {
  assert.match(html, /href="styles\.css\?v=/);
  assert.match(html, /src="app\.js\?v=/);
  assert.match(html, /src="\/shared\/wallet-connector\.js\?v=/);
  assert.match(html, /data-wallet-connector-styles="external"/);
  assert.match(walletConnector, /dataset\.walletConnectorStyles === 'external'/);
  assert.equal(html.includes('<style>'), false);
  assert.equal(html.includes("'unsafe-inline'"), false);
  const executableInlineScripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
    .map(match => match[1])
    .filter(script => script.trim());
  assert.equal(executableInlineScripts.length, 0);
  assert.ok(css.length > 1000);
  assert.match(css, /\[hidden\]\s*\{\s*display:\s*none\s*!important/);
  assert.match(css, /--bg:\s*#111827/);
  assert.match(css, /--card:\s*#1f2937/);
  assert.match(css, /\.ac-wallet-overlay/);
  assert.match(css, /\.ac-wallet-dialog/);
});

test('verification page script parses', () => {
  new Function(app);
});
"""


def alphacity_html() -> str:
    html = HTML_SOURCE.read_text(encoding="utf-8")
    html = html.replace("{{ api_verify_url|e }}", "")
    html = html.replace("{{ verification_session|e }}", "")
    html = html.replace('href="/static/verify.css', 'href="styles.css')
    html = html.replace('src="/static/verify.js', 'src="app.js')
    html = html.replace(
        'src="/static/wallet-connector.js',
        'src="/shared/wallet-connector.js',
    )
    html = html.replace(
        '<script src="https://telegram.org/js/telegram-web-app.js"></script>',
        '<script defer src="/shared/telemetry-config.js"></script>\n'
        '  <script defer src="/shared/telemetry.js"></script>\n'
        '  <script src="https://telegram.org/js/telegram-web-app.js"></script>',
    )
    return "<!-- Generated by Token-Gate scripts/sync_verify_page.py -->\n" + html


def expected_files(target: Path) -> dict[Path, bytes]:
    return {
        target / "verify" / "index.html": alphacity_html().encode(),
        target / "verify" / "app.js": JS_SOURCE.read_bytes(),
        target / "verify" / "styles.css": CSS_SOURCE.read_bytes(),
        target / "shared" / "wallet-connector.js": CONNECTOR_SOURCE.read_bytes(),
        target / "tests" / "verify-page.test.cjs": VERIFY_TEST.encode(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("alphacity_checkout", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = args.alphacity_checkout.resolve()
    expected = expected_files(target)

    if args.check:
        drift = [
            str(path)
            for path, content in expected.items()
            if not path.exists() or path.read_bytes() != content
        ]
        if drift:
            raise SystemExit("Verification UI drift detected:\n" + "\n".join(drift))
        print("Alphacity verification UI matches the Token-Gate source.")
        return 0

    for path, content in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        print(f"Updated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
