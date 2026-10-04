"""Run real verification functions with isolated database/provider boundaries."""

import ast
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import json
import logging
from pathlib import Path
import secrets
import threading
import time
import unittest
from unittest.mock import Mock

from flask import Flask, jsonify, request
import psycopg2

from enforcement_policy import evaluate_gate
from sui_ownership import collection_matches
from verification_security import (
    RegistrationWalletsChanged, build_wallet_ownership_message,
    canonical_sui_address, canonical_wallets, is_valid_verification_session_id,
)
from verification_results import (
    qualifying_holdings_summary, verification_holdings_progress, verification_success_message,
)


TREE = ast.parse(Path('main.py').read_text(encoding='utf-8'))
A, B = canonical_wallets(['0x1', '0x2'])
CONFIG = {'registration_mode': 'token', 'token': 'CITY', 'minimum_holding': 1000000}


def load(*names, **dependencies):
    functions = []
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            # No production startup, route decorators, or retry sleeps.
            import copy
            node = copy.deepcopy(node)
            node.decorator_list = []
            functions.append(node)
    ns = {
        'Decimal': Decimal, 'InvalidOperation': InvalidOperation, 'time': time,
        'logging': logging, 'json': json, 'secrets': secrets,
        'canonical_wallets': canonical_wallets, 'canonical_sui_address': canonical_sui_address,
        'require_canonical_sui_address': canonical_sui_address,
        'RegistrationWalletsChanged': RegistrationWalletsChanged,
        'evaluate_gate': evaluate_gate, 'SUI_OPERATION_TIMEOUT_SECONDS': 120,
        'NFT_PROVIDER_RETRY_DELAY': 0, 'VERIFICATION_PROCESSING_LEASE_SECONDS': 180,
        'verification_success_message': verification_success_message,
        **dependencies,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), 'main.py', 'exec'), ns)
    return ns


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.balances = Mock(return_value={A: Decimal(500000), B: Decimal(500000)})
        self.nfts = Mock(return_value=2)
        self.traits = Mock(return_value=2)
        self.ns = load('evaluate_wallet_requirements',
                       fetch_wallet_balances=self.balances, get_user_nft_count=self.nfts,
                       get_user_nft_trait_count=self.traits, get_user_nft_category_count=self.traits)

    def evaluate(self, wallets, config=CONFIG):
        return self.ns['evaluate_wallet_requirements'](
            wallets, config, force_fresh=True, deadline_monotonic=123)

    def test_two_half_balances_pass_but_one_fails(self):
        self.assertEqual(self.evaluate(A)['status'], 'fail')
        result = self.evaluate([A, B])
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['token_balance'], Decimal(1000000))
        self.balances.assert_called_with([A, B], 'CITY', 6, use_cache=False, deadline_monotonic=123)

    def test_repeated_and_short_addresses_never_double_count(self):
        result = self.evaluate(['0x1', A, '0X1'.lower()])
        self.assertEqual(result['token_balance'], Decimal(500000))
        self.assertEqual(result['wallet_count'], 1)
        self.assertEqual(result['status'], 'fail')

    def test_missing_balance_is_unknown_not_zero(self):
        self.balances.return_value = {A: Decimal(1000000)}
        self.assertEqual(self.evaluate([A, B])['status'], 'indeterminate')

    def test_nft_and_trait_checks_receive_all_wallets_and_deadline(self):
        cfg = {'registration_mode': 'nft', 'nft_collection_id': 'NFT', 'nft_threshold': 2,
               'nft_trait_name': 'Rarity', 'nft_trait_value': 'Rare', 'nft_trait_threshold': 2}
        result = self.evaluate([A, B], cfg)
        self.assertEqual(result['status'], 'pass')
        self.nfts.assert_called_with([A, B], 'NFT', use_cache=False, deadline_monotonic=123)
        self.traits.assert_called_with([A, B], 'NFT', 'Rarity', 'Rare',
                                       use_cache=False, deadline_monotonic=123)
        cfg['nft_trait_value'] = ''
        self.assertEqual(self.evaluate([A, B], cfg)['status'], 'pass')
        self.traits.assert_called_with([A, B], 'NFT', 'Rarity', use_cache=False, deadline_monotonic=123)
        self.traits.return_value = None
        self.assertEqual(self.evaluate([A, B], cfg)['status'], 'indeterminate')

    def test_either_branch_can_still_pass_both_mode(self):
        self.balances.return_value = {}
        cfg = {**CONFIG, 'registration_mode': 'both', 'nft_collection_id': 'NFT'}
        self.assertEqual(self.evaluate([A, B], cfg)['status'], 'pass')
        self.nfts.return_value = 0
        self.assertEqual(self.evaluate([A, B], cfg)['status'], 'indeterminate')


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.cur = Mock(rowcount=1)
        self.cur.fetchone.return_value = ('processing',)
        self.save = Mock()

        @contextmanager
        def cursor():
            yield Mock(), self.cur

        self.ns = load('finalize_verified_wallet', 'get_completed_verification_result',
                       '_completed_verification_payload', get_db_cursor=cursor,
                       _save_wallet_for_user_with_cursor=self.save)

    def finalize(self, status='fail'):
        return self.ns['finalize_verified_wallet'](
            'parent', 1, 2, 'user', A, 'token', status, {}, 'claim', expected_wallets=[B])

    def test_child_and_result_commit_together_without_extending_expiry(self):
        self.assertTrue(self.finalize())
        self.save.assert_called_once_with(self.cur, 1, 2, 'user', [A],
                                          registration_type='token', expected_wallets=[B])
        calls = self.cur.execute.call_args_list
        insert = next(call for call in calls if 'INSERT INTO verification_sessions' in call.args[0])
        self.assertIn('SELECT %s, group_id, user_id, expires_at', insert.args[0])
        self.assertIn('expires_at > NOW()', insert.args[0])
        child = insert.args[1][0]
        self.assertTrue(is_valid_verification_session_id(child))
        self.assertEqual(calls[-1].args[1][-2], child)

    def test_pass_and_duplicate_completion_do_not_mint_children(self):
        self.assertTrue(self.finalize('pass'))
        self.assertFalse(any('INSERT INTO verification_sessions' in call.args[0]
                             for call in self.cur.execute.call_args_list))
        self.cur.reset_mock()
        self.cur.fetchone.side_effect = [None, (A, 'fail')]
        self.assertTrue(self.finalize())
        self.assertFalse(any('INSERT INTO verification_sessions' in call.args[0]
                             for call in self.cur.execute.call_args_list))

    def test_expired_link_can_complete_but_does_not_offer_child(self):
        def execute(sql, args):
            self.cur.rowcount = 0 if 'INSERT INTO verification_sessions' in sql else 1
        self.cur.execute.side_effect = execute
        self.assertTrue(self.finalize())
        self.assertIsNone(self.cur.execute.call_args_list[-1].args[1][-2])

    def test_replay_restores_child_and_progress_but_never_grants_access(self):
        progress = {'message': 'Tokens: 500,000 / 1,000,000 required'}
        self.cur.fetchone.return_value = (1, 2, A, 'fail', {'progress': progress}, 'x' * 43)
        completed = self.ns['get_completed_verification_result']('parent', A)
        payload, status = self.ns['_completed_verification_payload'](completed, 'restart')
        self.assertEqual(status, 403)
        self.assertFalse(payload['requirements_met'])
        self.assertEqual(payload['next_verification_session'], 'x' * 43)
        self.assertEqual(payload['holdings_progress'], progress)
        self.assertIn('CASE WHEN expires_at > NOW()', self.cur.execute.call_args.args[0])

    def test_concurrent_wallet_change_aborts_before_any_wallet_overwrite(self):
        ns = load('_save_wallet_for_user_with_cursor')
        self.cur.fetchone.return_value = (json.dumps([B]), False)
        with self.assertRaises(RegistrationWalletsChanged):
            ns['_save_wallet_for_user_with_cursor'](
                self.cur, 1, 2, 'user', [A], expected_wallets=[])
        self.assertEqual(self.cur.execute.call_count, 2)  # materialize + locked read only

    def test_snapshot_guard_accepts_equivalent_legacy_address_and_preserves_exemption(self):
        ns = load('_save_wallet_for_user_with_cursor')
        self.cur.fetchone.return_value = (json.dumps(['0x2']), True)
        ns['_save_wallet_for_user_with_cursor'](
            self.cur, 1, 2, 'user', [A], expected_wallets=[B])
        upsert = next(call for call in self.cur.execute.call_args_list
                      if 'DO UPDATE SET' in call.args[0])
        self.assertEqual(json.loads(upsert.args[1][3]), [A, B])
        self.assertTrue(upsert.args[1][4])


class NftDeduplicationTests(unittest.TestCase):
    def test_owned_and_kiosk_objects_are_unique_across_wallets(self):
        nft = {'objectId': '0xabc', 'type': '0x1::nft::NFT'}
        gateway = Mock()
        gateway.iter_owned_objects.return_value = [nft]
        ns = load('_fetch_owned_nfts', _normalize_collection_id=lambda value: value,
                  _graphql_type_filter=lambda value: value, sui_gateway=gateway,
                  collection_matches=collection_matches,
                  SUI_MAX_PAGES=10, SUI_MAX_OBJECTS=100,
                  _fetch_kiosk_nfts=Mock(return_value=[nft]))
        self.assertEqual(ns['_fetch_owned_nfts']([A, B], '0x1::nft::NFT'), [nft])
        self.assertEqual(gateway.iter_owned_objects.call_count, 2)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.completed = {}
        self.registered = []
        self.ns = load(
            'api_verify', 'evaluate_wallet_requirements', '_completed_verification_payload',
            request=request, jsonify=jsonify, psycopg2=psycopg2, SuiGatewayError=RuntimeError,
            build_wallet_ownership_message=build_wallet_ownership_message,
            is_valid_verification_session_id=is_valid_verification_session_id,
            qualifying_holdings_summary=qualifying_holdings_summary,
            verification_holdings_progress=verification_holdings_progress,
            config_lock=threading.Lock(), SUBSCRIBER_CONFIGS={1: CONFIG},
            _add_cors_headers=lambda response: response, runtime_metrics=Mock(),
            verification_rate_limiter=Mock(), _verification_work_slots=Mock(),
            get_active_verification_session=Mock(return_value={'group_id': 1, 'user_id': 2}),
            get_completed_verification_result=lambda session, *args: self.completed.get(session),
            build_registration_restart_url=lambda group: 'https://t.me/bot',
            claim_verification_session=Mock(return_value=True), release_verification_session=Mock(),
            sui_gateway=Mock(), wallet_already_registered=Mock(return_value=False),
            get_user_registration=Mock(side_effect=lambda *args: {'wallets': self.registered[:]}),
            fetch_wallet_balances=Mock(return_value={A: Decimal(500000), B: Decimal(500000)}),
            _get_user_display_name=lambda uid: 'user', update_user_cached_holdings=Mock(),
            bot=Mock(), _background_executor=Mock(), _send_group_verified_notification=Mock(),
            _build_verification_success_message=Mock(return_value=(['Success'], None)),
        )

        def finalize(session, group, user, username, wallet, mode, status, summary, claim, **kwargs):
            self.assertEqual(kwargs['expected_wallets'], self.registered)
            self.registered = canonical_wallets(self.registered + [wallet])
            self.completed[session] = {'group_id': group, 'eligibility_status': status,
                                       'holdings_summary': summary,
                                       'next_verification_session': 'n' * 43 if status == 'fail' else None}
            return True
        self.ns['finalize_verified_wallet'] = Mock(side_effect=finalize)
        app = Flask(__name__)
        app.add_url_rule('/api/verify', view_func=self.ns['api_verify'], methods=['POST'])
        self.client = app.test_client()

    def post(self, wallet=A, session='p' * 43):
        return self.client.post('/api/verify', json={
            'verification_session': session, 'wallet_address': wallet, 'wallet_signature': 'sig',
            # Untrusted browser fields must have no authority.
            'wallets': ['0x3'], 'group_id': 999, 'telegram_user_id': 999, 'token_balance': '999999999',
        })

    def test_two_wallet_journey_and_refresh_replay(self):
        first = self.post()
        self.assertEqual(first.status_code, 403)
        self.assertTrue(first.json['wallet_registered'])
        self.assertIn('500,000 / 1,000,000', first.json['holdings_progress']['message'])
        child = first.json['next_verification_session']
        self.assertEqual(self.post().json['next_verification_session'], child)
        self.ns['finalize_verified_wallet'].assert_called_once()
        second = self.post(B, child)
        self.assertEqual(second.status_code, 200)
        self.assertIn('1,000,000 qualifying tokens found across 2 registered wallets', second.json['message'])
        self.assertEqual(self.registered, [A, B])
        self.ns['get_user_registration'].assert_called_with(1, 2)
        self.ns['fetch_wallet_balances'].assert_called_with(
            [A, B], 'CITY', 6, use_cache=False,
            deadline_monotonic=self.ns['fetch_wallet_balances'].call_args.kwargs['deadline_monotonic'])

    def test_provider_error_is_retryable_and_does_not_save_or_mint(self):
        self.ns['fetch_wallet_balances'].return_value = {}
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json['retryable'])
        self.ns['finalize_verified_wallet'].assert_not_called()
        self.ns['release_verification_session'].assert_called_once()

    def test_concurrent_edit_requires_fresh_recheck(self):
        self.ns['finalize_verified_wallet'].side_effect = RegistrationWalletsChanged()
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json['retryable'])
        self.ns['release_verification_session'].assert_called_once()

    def test_bad_signature_or_other_users_wallet_does_not_read_holdings(self):
        self.ns['sui_gateway'].verify_personal_message.return_value = False
        self.assertEqual(self.post().status_code, 403)
        self.ns['sui_gateway'].verify_personal_message.return_value = True
        self.ns['wallet_already_registered'].return_value = True
        self.assertEqual(self.post().status_code, 409)
        self.ns['fetch_wallet_balances'].assert_not_called()
        self.ns['finalize_verified_wallet'].assert_not_called()
