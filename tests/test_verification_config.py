from urllib.parse import parse_qs, urlsplit
from pathlib import Path
import re
import unittest

from verification_config import (
    DEFAULT_PUBLIC_API_BASE_URL,
    DEFAULT_WALLET_CONNECT_URL,
    PUBLIC_API_ALLOWED_HOSTS,
    build_hosted_verification_url,
    normalize_public_api_base_url,
    normalize_wallet_connect_url,
)


class VerificationUrlTests(unittest.TestCase):
    def test_default_page_is_alphacity_and_secrets_are_fragment_only(self):
        page = normalize_wallet_connect_url("", {"alphacity.tech"})
        self.assertEqual(page, DEFAULT_WALLET_CONNECT_URL)
        url = build_hosted_verification_url(
            page,
            "a" * 43,
            f"{DEFAULT_PUBLIC_API_BASE_URL}/api/verify",
        )
        parsed = urlsplit(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "alphacity.tech")
        self.assertEqual(parsed.path, "/verify/")
        self.assertEqual(parsed.query, "")
        self.assertEqual(
            parse_qs(parsed.fragment),
            {
                "verification_session": ["a" * 43],
                "api_verify_url": [f"{DEFAULT_PUBLIC_API_BASE_URL}/api/verify"],
            },
        )

    def test_invalid_or_credentialed_page_urls_fail_closed(self):
        invalid = (
            "https://evil.example/verify/",
            "https://user:password@alphacity.tech/verify/",
            "https://alphacity.tech/not-verify/",
            "http://alphacity.tech/verify/",
            "https://alphacity.tech/verify/?session=leak",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_wallet_connect_url(value, {"alphacity.tech"})

    def test_local_development_and_public_api_origins_are_canonicalized(self):
        self.assertEqual(
            normalize_wallet_connect_url("http://localhost:8080/verify", {"alphacity.tech"}),
            "http://localhost:8080/verify/",
        )
        self.assertEqual(
            normalize_public_api_base_url("https://token-gate-bot.onrender.com/"),
            "https://token-gate-bot.onrender.com",
        )
        with self.assertRaises(ValueError):
            normalize_public_api_base_url("https://token-gate-bot.onrender.com/base")
        with self.assertRaises(ValueError):
            normalize_public_api_base_url("https://api.example.test")

    def test_www_page_is_canonicalized_to_the_production_apex(self):
        self.assertEqual(
            normalize_wallet_connect_url(
                "https://www.alphacity.tech/verify/",
                {"alphacity.tech", "www.alphacity.tech"},
            ),
            DEFAULT_WALLET_CONNECT_URL,
        )

    def test_backend_and_browser_api_host_allowlists_stay_in_sync(self):
        browser = Path("static/verify.js").read_text(encoding="utf-8")
        match = re.search(
            r"const ALLOWED_API_HOSTS = new Set\(\[(.*?)\]\);",
            browser,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        browser_hosts = set(re.findall(r"'([^']+)'", match.group(1)))
        self.assertEqual(
            browser_hosts,
            set(PUBLIC_API_ALLOWED_HOSTS) | {"localhost", "127.0.0.1"},
        )


if __name__ == "__main__":
    unittest.main()
