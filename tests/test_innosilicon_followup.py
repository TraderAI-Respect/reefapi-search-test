"""Offline tests for the separately authorized remainder-only search."""
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = pathlib.Path(__file__).parents[1]
PATH = ROOT / 'scripts/innosilicon_followup.py'
spec = importlib.util.spec_from_file_location('followup', PATH) if PATH.exists() else None
followup = importlib.util.module_from_spec(spec) if spec else None
if followup:
    spec.loader.exec_module(followup)


def listing(title='Innosilicon A10 Pro', aid='12345678', description='Продам свой асик. 1 шт.'):
    return {'ad_id': aid, 'title': title, 'url': 'https://www.avito.ru/moskva/asic_' + aid,
            'description_snippet': description, 'location': {'name': 'Москва'}, 'price': 25000,
            'price_text': '25 000 ₽', 'seller': {'phone': '+70000000000'}}


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(followup, 'Remainder-only search implementation is missing')

    def run_scan(self, balance=4, replies=None, final_balance=0, failure=None):
        calls = []
        responses = iter(replies or [({'ok': True, 'data': {'page': 1, 'listings': []},
                                     'meta': {'charged_credits': 2}}, 2, 200),
                                    ({'ok': True, 'data': {'page': 1, 'listings': []},
                                     'meta': {'charged_credits': 2}}, 0, 200)])
        def transport(method, path, body):
            calls.append((method, path, body))
            if method == 'GET':
                value = balance if len(calls) == 1 else final_balance
                return {'ok': True, 'data': {'remaining': value, 'used': 1050}}, value, 200
            if failure:
                raise failure
            return next(responses)
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            result = followup.scan(transport, folder)
            saved = {p.name: json.loads(p.read_text()) for p in folder.glob('*.json')}
        return result, calls, saved

    def test_exact_four_remaining_allows_only_two_requested_first_pages(self):
        result, calls, saved = self.run_scan()
        self.assertEqual('FIRST_PAGES_COMPLETE', result['status'])
        self.assertEqual(['GET', 'POST', 'POST', 'GET'], [c[0] for c in calls])
        paid = [c for c in calls if c[0] == 'POST']
        self.assertEqual(['Innosilicon A10 Pro', 'Innosilicon A11 Pro'], [c[2]['query'] for c in paid])
        self.assertEqual([1, 1], [c[2]['page'] for c in paid])
        self.assertTrue(all(c[1] == '/avito/v1/search' and c[2]['location'] == 'all'
                            and c[2]['include_pii'] is False for c in paid))
        self.assertEqual(4, result['credits_reserved'])
        self.assertEqual(4, result['charged_reported'])
        self.assertEqual(0, result['balance_final'])
        self.assertEqual(4, result['account_balance_delta'])
        self.assertIn('balance-before.json', saved)
        self.assertIn('balance-final.json', saved)

    def test_insufficient_remainder_never_selects_one_model_for_user(self):
        for balance, status in [(0, 'INSUFFICIENT_CREDITS'), (1, 'INSUFFICIENT_CREDITS'),
                                (2, 'NEEDS_MODEL_SELECTION'), (3, 'NEEDS_MODEL_SELECTION'),
                                (3.5, 'NEEDS_MODEL_SELECTION'), (5, 'UNRECONCILED_BALANCE'),
                                (100, 'UNRECONCILED_BALANCE')]:
            result, calls, _ = self.run_scan(balance=balance)
            self.assertEqual(status, result['status'], balance)
            self.assertEqual(['GET'], [c[0] for c in calls], balance)
            self.assertEqual(0, result['credits_reserved'])

    def test_invalid_balance_does_not_search(self):
        for balance in (None, True, -1, float('nan'), '4'):
            result, calls, _ = self.run_scan(balance=balance)
            self.assertEqual('STOPPED_ERROR', result['status'])
            self.assertEqual(['GET'], [c[0] for c in calls])

    def test_api_error_preserves_code_charge_and_header_without_retry(self):
        reply = ({'ok': False, 'error': {'code': 'BLOCKED', 'message': 'Provider blocked query'},
                  'meta': {'charged_credits': 0}, 'debug': {'key': 'never-save'}}, 4, 429)
        result, calls, saved = self.run_scan(replies=[reply], final_balance=4)
        self.assertEqual(['GET', 'POST', 'GET'], [c[0] for c in calls])
        self.assertEqual('STOPPED_ERROR', result['status'])
        self.assertEqual(2, result['credits_reserved'])
        self.assertEqual(0, result['charged_reported'])
        response = saved['a10-page-1.json']
        self.assertEqual({'code': 'BLOCKED', 'message': 'Provider blocked query'}, response['error'])
        self.assertEqual(0, response['charged_credits'])
        self.assertEqual(4, response['header_remaining'])
        self.assertEqual(429, response['http_status'])
        self.assertNotIn('debug', response)

    def test_missing_or_overcost_preserves_public_results_before_stopping(self):
        for charge in (None, 3):
            reply = ({'ok': True, 'data': {'page': 1, 'listings': [listing()]},
                      'meta': {'charged_credits': charge}}, 2, 200)
            result, calls, saved = self.run_scan(replies=[reply], final_balance=2)
            self.assertEqual(['GET', 'POST', 'GET'], [c[0] for c in calls])
            self.assertEqual('STOPPED_ERROR', result['status'])
            self.assertEqual(1, len(result['matches']))
            self.assertEqual('Москва', saved['a10-page-1.json']['listings'][0]['city'])
            self.assertNotIn('seller', saved['a10-page-1.json']['listings'][0])

    def test_timeout_is_reserved_and_free_final_balance_is_attempted(self):
        result, calls, _ = self.run_scan(failure=TimeoutError('secret exception details'), final_balance=2)
        self.assertEqual(['GET', 'POST', 'GET'], [c[0] for c in calls])
        self.assertEqual(2, result['credits_reserved'])
        self.assertEqual('TimeoutError', result['error_type'])
        self.assertNotIn('secret exception', json.dumps(result))

    def test_lower_response_balance_stops_second_query(self):
        reply = ({'ok': True, 'data': {'listings': [], 'page': 1}, 'meta': {'charged_credits': 2}}, 1, 200)
        result, calls, _ = self.run_scan(replies=[reply], final_balance=1)
        self.assertEqual(['GET', 'POST', 'GET'], [c[0] for c in calls])
        self.assertEqual('BALANCE_CHANGED_STOP', result['status'])

    def test_general_filter_is_reused_with_strict_pro_scope(self):
        ads = [listing(), listing('Innosilicon A11 Pro под ремонт', '12345679'),
               listing('Innosilicon A11 Pro с блоком питания', '12345680'),
               listing('Innosilicon A10', '12345681'), listing('Ремонт Innosilicon A10 Pro', '12345682'),
               listing('Кабель питания Innosilicon A11 Pro', '12345683'),
               listing('Майнинг-отель Innosilicon A11 Pro', '12345684'),
               listing('Корпус Asic innosilicon a11 pro', '12345685', 'Пол года был включён в розетку.')]
        replies = [({'ok': True, 'data': {'page': 1, 'listings': ads}, 'meta': {'charged_credits': 2}}, 2, 200),
                   ({'ok': True, 'data': {'page': 1, 'listings': ads[:1]}, 'meta': {'charged_credits': 2}}, 0, 200)]
        result, _, _ = self.run_scan(replies=replies)
        self.assertEqual({'12345678', '12345679', '12345680'}, {r['ad_id'] for r in result['matches']})
        self.assertEqual(3, len(result['matches']))
        self.assertEqual(['12345685'], [r['ad_id'] for r in result['review']])
        broken = next(r for r in result['matches'] if r['ad_id'] == '12345679')
        self.assertTrue(broken['needs_manual_review'])

    def test_device_evidence_required_and_obvious_brand_typos_preserved(self):
        ads = [listing('A10 Pro', '12345670', 'Телефон, новый, полный комплект, 1 шт.'),
               listing('Inosilicon A10 Pro 7Gb', '12345671', '1 шт.'),
               listing('Инносиликон а11 про', '12345672', '1 шт.'),
               listing('Асик A11 Pro', '12345673', '1 шт.')]
        ads[0]['url'] = 'https://www.avito.ru/moskva/telefony/a10_pro_12345670'
        replies = [({'ok': True, 'data': {'page': 1, 'listings': ads}, 'meta': {'charged_credits': 2}}, 2, 200),
                   ({'ok': True, 'data': {'page': 1, 'listings': []}, 'meta': {'charged_credits': 2}}, 0, 200)]
        result, _, _ = self.run_scan(replies=replies)
        self.assertEqual({'12345671', '12345672', '12345673'}, {r['ad_id'] for r in result['matches']})
        self.assertEqual([], result['review'])

    def test_original_narrow_accessory_exclusions_are_preserved(self):
        titles = ['Control board Innosilicon A11 Pro', 'Хешборд Innosilicon A11 Pro',
                  'Радиатор Innosilicon A11 Pro', 'PSU Innosilicon A10 Pro']
        ads = [listing(title, str(12345670 + i), 'Новый, 1 шт.') for i, title in enumerate(titles)]
        replies = [({'ok': True, 'data': {'page': 1, 'listings': ads}, 'meta': {'charged_credits': 2}}, 2, 200),
                   ({'ok': True, 'data': {'page': 1, 'listings': []}, 'meta': {'charged_credits': 2}}, 0, 200)]
        result, _, _ = self.run_scan(replies=replies)
        self.assertEqual([], result['matches'])
        self.assertEqual([], result['review'])

    def test_contradictory_free_balance_header_stops_before_paid_call(self):
        for header in (2, 5):
            calls = []
            def transport(method, path, body):
                calls.append(method)
                return {'ok': True, 'data': {'remaining': 4}}, header, 200
            with tempfile.TemporaryDirectory() as tmp:
                result = followup.scan(transport, pathlib.Path(tmp))
            self.assertEqual(['GET'], calls)
            self.assertEqual('STOPPED_ERROR', result['status'])
            self.assertEqual(0, result['credits_reserved'])

    def test_wrong_page_and_non_avito_links_never_become_matches(self):
        bad = listing(); bad['url'] = 'https://example.com/asic_12345678'
        reply = ({'ok': True, 'data': {'page': 2, 'listings': [bad]}, 'meta': {'charged_credits': 2}}, 2, 200)
        result, calls, saved = self.run_scan(replies=[reply], final_balance=2)
        self.assertEqual([], result['matches'])
        self.assertEqual('STOPPED_ERROR', result['status'])
        self.assertEqual(1, len(saved['a10-page-1.json']['listings']))
        self.assertEqual(1, sum(c[0] == 'POST' for c in calls))

    def test_api_key_is_redacted_from_saved_error(self):
        with patch.dict(os.environ, {'REEF_API_KEY': 'secret-value'}):
            result, _, saved = self.run_scan(replies=[({'ok': False, 'error': {'code': 'FAIL',
                'message': 'Key secret-value failed'}, 'meta': {'charged_credits': 0}}, 4, 403)], final_balance=4)
        self.assertNotIn('secret-value', json.dumps(saved))
        self.assertIn('[REDACTED]', json.dumps(saved))

    def test_secret_redaction_precedes_truncation_and_markdown_escaping(self):
        key = 'secret_key"value'
        with patch.dict(os.environ, {'REEF_API_KEY': key}):
            self.assertNotIn('secre', followup.scalar('x' * 11995 + key))
            self.assertNotIn('secret', followup.cell(key))
            with tempfile.TemporaryDirectory() as tmp:
                path = pathlib.Path(tmp) / 'out.json'
                followup.save(path, {'message': key})
                self.assertEqual('[REDACTED]', json.loads(path.read_text())['message'])

    def test_final_balance_failure_does_not_destroy_partial_results(self):
        calls = []
        def transport(method, path, body):
            calls.append(method)
            if len(calls) == 1:
                return {'ok': True, 'data': {'remaining': 4}}, 4, 200
            if method == 'GET':
                raise TimeoutError('No final balance')
            return {'ok': True, 'data': {'listings': [listing()], 'page': 1}, 'meta': {}}, 2, 200
        with tempfile.TemporaryDirectory() as tmp:
            result = followup.scan(transport, pathlib.Path(tmp))
        self.assertEqual(['GET', 'POST', 'GET'], calls)
        self.assertEqual(1, len(result['matches']))
        self.assertEqual('ValueError', result['error_type'])
        self.assertEqual('TimeoutError', result['final_balance_error_type'])

    def test_guards_stop_before_any_transport_or_secret_lookup(self):
        for env in ({'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_RUN_NUMBER': '1'},
                    {'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_RUN_NUMBER': '2'}):
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(SystemExit):
                    followup.main()

    def test_http_error_body_is_returned_by_transport(self):
        body = b'{"ok":false,"error":{"code":"RATE_LIMIT","message":"Later"},"meta":{"charged_credits":0}}'
        class Opener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 429, 'Too many', {'X-Credits-Remaining': '4'}, io.BytesIO(body))
        payload, remaining, status = followup.make_transport('test-key', Opener())('POST', '/avito/v1/search', {})
        self.assertEqual('RATE_LIMIT', payload['error']['code'])
        self.assertEqual(4, remaining)
        self.assertEqual(429, status)

    def test_summary_contains_safe_direct_links_and_never_claims_exhaustion(self):
        row = listing('Innosilicon A10 Pro <script>\n[click](https://evil.invalid)')
        row['url'] = 'https://www.avito.ru/moskva/asic)_12345678'
        reply = ({'ok': True, 'data': {'page': 1, 'listings': [row]}, 'meta': {'charged_credits': 2}}, 2, 200)
        result, _, _ = self.run_scan(replies=[reply, ({'ok': True, 'data': {'page': 1, 'listings': []},
            'meta': {'charged_credits': 2}}, 0, 200)])
        summary = followup.markdown_summary(result)
        self.assertIn('https://www.avito.ru/moskva/asic%29_12345678', summary)
        self.assertIn('FIRST_PAGES_COMPLETE', summary)
        self.assertIn('Не полный обзор рынка', summary)
        self.assertNotIn('<script>', summary)
        self.assertNotIn('[click](https://evil.invalid)', summary)

    def test_transport_rejects_unapproved_endpoints_before_network(self):
        class NoNetwork:
            def open(self, *args, **kwargs):
                raise AssertionError('Network must not be reached')
        transport = followup.make_transport('key', NoNetwork())
        with self.assertRaises(ValueError):
            transport('GET', '/avito/v1/item', None)


if __name__ == '__main__':
    unittest.main()
