"""Short links retain group/user authority in the server-side session."""

import threading
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from flask import Flask, jsonify, request

from test_multi_wallet_verification import load
from verification_config import build_hosted_verification_url, DEFAULT_PUBLIC_API_BASE_URL
from verification_security import is_valid_verification_session_id


class SessionLinkContextTests(unittest.TestCase):
    def test_short_links_resolve_distinct_groups_and_ignore_browser_group_settings(self):
        sessions = {}

        def create(group, user, **kwargs):
            token = ("a" if group == -1001 else "b") * 43
            sessions[token] = {"group_id": group, "user_id": user}
            return token

        ns = load(
            "build_wallet_connect_url", "verification_context", request=request, jsonify=jsonify,
            build_hosted_verification_url=build_hosted_verification_url,
            get_public_api_base_url=lambda: DEFAULT_PUBLIC_API_BASE_URL,
            WALLET_CONNECT_URL="https://alphacity.tech/verify/", create_verification_session=create,
            is_valid_verification_session_id=is_valid_verification_session_id,
            get_active_verification_session=sessions.get,
            verification_rate_limiter=Mock(allow=Mock(return_value=True)),
            _add_cors_headers=lambda response: response, config_lock=threading.Lock(),
            SUBSCRIBER_CONFIGS={
                -1001: {"token": "0x1::city::CITY", "minimum_holding": 100, "registration_mode": "token"},
                -1002: {"nft_collection_id": "0x2::nft::NFT", "nft_threshold": 2,
                        "registration_mode": "nft", "nft_trait_name": "Rarity", "nft_trait_value": "Gold"},
            },
            build_registration_restart_url=lambda group: f"https://t.me/bot?start=register_{group}",
            build_telegram_return_url=lambda: "https://t.me/bot",
        )
        app = Flask(__name__)
        app.add_url_rule("/api/verification-context", view_func=ns["verification_context"])
        client = app.test_client()
        for group, user, mode in [(-1001, 10, "token"), (-1002, 20, "nft")]:
            link = ns["build_wallet_connect_url"](group, user)
            parsed = urlsplit(link)
            fragment = parse_qs(parsed.fragment)
            self.assertEqual(set(fragment), {"verification_session"})
            self.assertEqual(parsed.query, "")
            response = client.get("/api/verification-context", query_string={
                "verification_session": fragment["verification_session"][0],
                "group_id": 999, "telegram_user_id": 999, "minimum_holding": 0,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["group_id"], str(group))
            self.assertEqual(response.json["telegram_user_id"], str(user))
            requirements = response.json["requirements"]
            self.assertEqual(requirements["registration_mode"], mode)
            if mode == "token":
                self.assertEqual(requirements["minimum_holding"], "100")
            else:
                self.assertEqual(requirements["nft_threshold"], 2)
                self.assertEqual(requirements["trait_value"], "Gold")
