import base64
from collections import defaultdict, deque
import unittest

import requests

from sui_gateway import (
    SuiGatewayError,
    SuiGraphQLGateway,
    encode_personal_message,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.responses = defaultdict(deque)
        self.calls = []

    def mount(self, *_args):
        return None

    def queue(self, endpoint, *responses):
        self.responses[endpoint].extend(responses)

    def post(self, endpoint, *, json, timeout):
        self.calls.append((endpoint, json, timeout))
        response = self.responses[endpoint].popleft()
        if isinstance(response, Exception):
            raise response
        return response


class SuiGraphQLGatewayTests(unittest.TestCase):
    def gateway(self, session, endpoints=("https://one.example/graphql",), retries=1):
        return SuiGraphQLGateway(
            endpoints,
            session=session,
            max_retries=retries,
            circuit_failures=1,
        )

    def test_personal_message_is_raw_utf8_base64(self):
        self.assertEqual(base64.b64decode(encode_personal_message("abc")), b"abc")
        long_message = "x" * 130
        self.assertEqual(
            base64.b64decode(encode_personal_message(long_message)),
            long_message.encode(),
        )

    def test_balance_remains_an_exact_atomic_integer(self):
        session = FakeSession()
        session.queue(
            "https://one.example/graphql",
            FakeResponse({"data": {"address": {"balance": {"totalBalance": "900719925474099312345"}}}}),
        )
        balance = self.gateway(session).get_balance_atomic("0x1", "0x2::sui::SUI")
        self.assertEqual(balance, 900719925474099312345)

    def test_owned_objects_paginate_and_normalize_move_json(self):
        session = FakeSession()
        session.queue(
            "https://one.example/graphql",
            FakeResponse({
                "data": {
                    "address": {
                        "objects": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                            "nodes": [{
                                "address": "0xa",
                                "contents": {
                                    "type": {"repr": "0x1::nft::Item"},
                                    "json": {"name": "A"},
                                },
                            }],
                        }
                    }
                }
            }),
            FakeResponse({
                "data": {
                    "address": {
                        "objects": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [{
                                "address": "0xb",
                                "contents": {
                                    "type": {"repr": "0x1::nft::Item"},
                                    "json": {"name": "B"},
                                },
                            }],
                        }
                    }
                }
            }),
        )
        objects = self.gateway(session).list_owned_objects("0x1", "0x1::nft::Item")
        self.assertEqual([item["objectId"] for item in objects], ["0xa", "0xb"])
        self.assertEqual(objects[0]["content"]["fields"]["name"], "A")
        self.assertEqual(session.calls[1][1]["variables"]["after"], "next")

    def test_provider_failover_uses_second_endpoint(self):
        session = FakeSession()
        session.queue("https://one.example/graphql", requests.ConnectionError("down"))
        session.queue(
            "https://two.example/graphql",
            FakeResponse({"data": {"chainIdentifier": "mainnet"}}),
        )
        gateway = self.gateway(
            session,
            ("https://one.example/graphql", "https://two.example/graphql"),
        )
        self.assertEqual(gateway.chain_identifier(), "mainnet")
        self.assertEqual(
            [call[0] for call in session.calls],
            ["https://one.example/graphql", "https://two.example/graphql"],
        )

    def test_dynamic_fields_preserve_kiosk_name_and_move_object_value(self):
        session = FakeSession()
        session.queue(
            "https://one.example/graphql",
            FakeResponse({
                "data": {
                    "object": {
                        "dynamicFields": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [{
                                "name": {
                                    "type": {"repr": "0x2::kiosk::Item"},
                                    "json": {"id": "0xnft"},
                                },
                                "value": {
                                    "__typename": "MoveObject",
                                    "address": "0xnft",
                                    "contents": {
                                        "type": {"repr": "0xcollection::nft::Item"},
                                        "json": {"rarity": "rare"},
                                    },
                                },
                            }],
                        }
                    }
                }
            }),
        )
        fields = self.gateway(session).list_dynamic_fields("0xkiosk")
        self.assertEqual(fields[0]["name"]["json"]["id"], "0xnft")
        self.assertEqual(fields[0]["value"]["contents"]["json"]["rarity"], "rare")

    def test_bad_signature_input_is_a_conclusive_failure(self):
        session = FakeSession()
        session.queue(
            "https://one.example/graphql",
            FakeResponse({
                "data": {"verifySignature": None},
                "errors": [{
                    "message": "Cannot parse signature",
                    "extensions": {"code": "BAD_USER_INPUT"},
                }],
            }),
        )
        self.assertFalse(
            self.gateway(session).verify_personal_message(
                author="0x1",
                message="message",
                signature="not-a-signature",
            )
        )

    def test_all_provider_failures_are_indeterminate(self):
        session = FakeSession()
        session.queue("https://one.example/graphql", requests.Timeout("timeout"))
        with self.assertRaises(SuiGatewayError):
            self.gateway(session).chain_identifier()


if __name__ == "__main__":
    unittest.main()
