"""Offline contract tests for the one-shot free VISI trial."""
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = pathlib.Path(__file__).parents[1]
PATH = ROOT / 'scripts/visi_trial.py'
spec = importlib.util.spec_from_file_location('visi_trial', PATH) if PATH.exists() else None
trial = importlib.util.module_from_spec(spec) if spec else None
if trial:
    spec.loader.exec_module(trial)


class Clock:
    def __init__(self, timestamp=None):
        self.current = timestamp if timestamp is not None else datetime(2026, 11, 1, 18, 3, tzinfo=timezone.utc).timestamp()
        self.elapsed = 0
        self.waits = []
    def time(self):
        return self.current
    def monotonic(self):
        return self.elapsed
    def sleep(self, seconds):
        self.waits.append(seconds)
        self.current += seconds
        self.elapsed += seconds


def item(aid='12345678', title='Innosilicon A10 Pro', description='Продам свой асик. 1 шт.', active=True):
    return {'id': aid, 'url': 'https://www.avito.ru/moskva/tovary_dlya_kompyutera/asic_' + aid,
            'title': title, 'description': description, 'price': 25000,
            'location': {'name': 'Москва', 'lat': 1, 'lng': 2}, 'address': 'Private address',
            'seller': {'name': 'Do not save'}, 'active': active, 'condition': 'Б/у',
            'specs': [{'name': 'Состояние', 'value': 'Б/у'}], 'published': 'сегодня в 10:00'}


class VisiTrialTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(trial, 'VISI runner does not exist')

    def run_scan(self, ads=None, details=None, quotas=None, response_hook=None, clock=None, error=None):
        calls = []
        clock = clock or Clock()
        details = details or {}
        def transport(kind, url):
            calls.append((kind, url, clock.monotonic()))
            if error:
                raise error
            remaining = quotas[len(calls)-1] if quotas is not None and len(calls) <= len(quotas) else 50-len(calls)
            if kind == 'search':
                payload = {'source': 'avito_tech', 'url': url, 'page': 1, 'items': ads or [],
                           'daily_remaining': remaining, 'fetched_at': '2026-11-01T18:03:00Z'}
            else:
                aid = url.rsplit('_', 1)[1]
                row = details.get(aid) or item(aid)
                row = {**row, 'url': url}
                if aid == '2490324333' and aid not in details:
                    row.update(title='Корпус Asic innosilicon a11 pro', description='Пол года был включён в розетку.')
                payload = {'source': 'avito_tech', 'item': row, 'daily_remaining': remaining,
                           'fetched_at': '2026-11-01T18:03:00Z'}
            return response_hook(payload, len(calls)) if response_hook else (payload, 200)
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            result = trial.scan(transport, folder, clock=clock)
            saved = {p.name: json.loads(p.read_text()) for p in folder.glob('*.json')}
        return result, calls, saved, clock

    def test_two_fixed_first_pages_then_ten_deduplicated_relevant_details(self):
        ads = [item(str(12345670+i)) for i in range(14)]
        result, calls, _, clock = self.run_scan(ads=ads)
        self.assertEqual(12, len(calls))
        self.assertEqual(['search', 'search'] + ['item']*10, [x[0] for x in calls])
        self.assertEqual(['https://www.avito.ru/all?q=Innosilicon+A10+Pro',
                          'https://www.avito.ru/all?q=Innosilicon+A11+Pro'], [x[1] for x in calls[:2]])
        self.assertEqual(10, len({x[1] for x in calls[2:]}))
        self.assertTrue(all(b[2]-a[2] >= 11 for a,b in zip(calls,calls[1:])))
        self.assertEqual(12, result['calls_reserved'])
        self.assertEqual(14, result['new_search_unique_count'])
        self.assertEqual(1, result['seed_count'])
        self.assertEqual(10, result['detail_calls'])
        self.assertEqual('BOUNDED_SEARCH_COMPLETE', result['status'])

    def test_expired_or_future_trial_proof_never_reaches_transport(self):
        for now in [trial.TRIAL_CONFIRMED_AT-1, trial.TRIAL_EXPIRES_AT-90, trial.TRIAL_EXPIRES_AT+1]:
            result, calls, _, _ = self.run_scan(clock=Clock(now))
            self.assertEqual([], calls)
            self.assertEqual('TRIAL_PROOF_INVALID_OR_EXPIRED', result['status'])

    def test_expiry_is_rechecked_after_rate_wait(self):
        result, calls, _, _ = self.run_scan(clock=Clock(trial.TRIAL_EXPIRES_AT-95))
        self.assertEqual(1, len(calls))
        self.assertEqual('TRIAL_PROOF_INVALID_OR_EXPIRED', result['status'])

    def test_unknown_malformed_or_paid_plan_sized_quota_stops_after_first_call(self):
        for quota in (None, True, '49', 49.0, -1, 50, 1499):
            result, calls, saved, _ = self.run_scan(ads=[item()], quotas=[quota])
            self.assertEqual(1, len(calls), quota)
            self.assertEqual('QUOTA_INVALID', result['status'])
            self.assertEqual(1, len(result['matches']))
            self.assertIn('response-01.json', saved)

    def test_exhausted_and_increasing_quota_stop_without_extra_requests(self):
        for quotas, count, status in [([0], 1, 'QUOTA_EXHAUSTED'),
                                      ([3, 4], 2, 'QUOTA_INCREASED'), ([1, 0], 2, 'QUOTA_EXHAUSTED')]:
            result, calls, _, _ = self.run_scan(quotas=quotas)
            self.assertEqual(count, len(calls))
            self.assertEqual(status, result['status'])

    def test_http_error_preserved_and_never_retried(self):
        def hook(payload, count):
            return {'error': {'code': 'UPSTREAM_FAILED', 'message': 'Safe message'}, 'daily_remaining': 49}, 502
        result, calls, saved, _ = self.run_scan(response_hook=hook)
        self.assertEqual(1, len(calls))
        self.assertEqual('HTTP_OR_API_ERROR', result['status'])
        self.assertEqual('UPSTREAM_FAILED', saved['response-01.json']['error']['code'])
        self.assertEqual(502, saved['response-01.json']['http_status'])
        self.assertEqual(49, saved['response-01.json']['daily_remaining'])

    def test_transport_timeout_reserved_once_no_retry(self):
        result, calls, _, _ = self.run_scan(error=TimeoutError('Do not save sensitive exception'))
        self.assertEqual(1, len(calls))
        self.assertEqual(1, result['calls_reserved'])
        self.assertEqual('TimeoutError', result['error_type'])
        self.assertNotIn('sensitive', json.dumps(result))

    def test_details_skip_buyback_services_parts_phone_bare_models(self):
        titles = ['Ремонт Innosilicon A10 Pro', 'Выкуп Innosilicon A11 Pro',
                  'Блок питания Innosilicon A10 Pro', 'Innosilicon A10',
                  'Control board Innosilicon A11 Pro', 'A10 Pro']
        ads = [item(str(12345670+i), title, 'Новый, 1 шт.') for i,title in enumerate(titles)]
        result, calls, _, _ = self.run_scan(ads=ads)
        self.assertEqual(3, len(calls))  # two queries plus separately authorized ambiguous seed
        self.assertEqual([], result['matches'])
        self.assertEqual(6, len(result['excluded']))
        self.assertEqual(['2490324333'], [x['ad_id'] for x in result['review']])

    def test_broken_whole_and_bundle_remain_relevant(self):
        ads = [item('12345678', 'Innosilicon A10 Pro под ремонт'),
               item('12345679', 'Inosilicon A11 Pro с блоком питания')]
        result, calls, _, _ = self.run_scan(ads=ads,
            details={x['id']: x for x in ads})
        self.assertEqual(5, len(calls))
        self.assertEqual({'12345678', '12345679'}, {x['ad_id'] for x in result['matches']})

    def test_inactive_detail_replaces_search_match_not_stale_available(self):
        result, _, _, _ = self.run_scan(ads=[item()], details={'12345678': item(active=False)})
        self.assertEqual([], result['matches'])
        self.assertTrue(any(x['ad_id']=='12345678' and x['reason']=='inactive' for x in result['excluded']))

    def test_detail_revealing_parts_replaces_search_match(self):
        result, _, _, _ = self.run_scan(ads=[item()], details={'12345678': item(title='Блок питания Innosilicon A10 Pro')})
        self.assertEqual([], result['matches'])
        self.assertTrue(any(x['ad_id']=='12345678' for x in result['excluded']))

    def test_seed_stays_review_unless_explicit_detail_resolves_whole_device(self):
        result, _, _, _ = self.run_scan()
        self.assertEqual([], result['matches'])
        self.assertEqual(['2490324333'], [x['ad_id'] for x in result['review']])
        whole = item('2490324333', 'Корпус Asic innosilicon a11 pro',
                     'Продаю целый майнер Innosilicon A11 Pro в сборе. 1 шт. Не отдельный корпус.')
        result, _, _, _ = self.run_scan(details={'2490324333': whole})
        self.assertEqual(['2490324333'], [x['ad_id'] for x in result['matches']])
        self.assertEqual(0, result['new_search_unique_count'])
        self.assertIn('prior_run:41316786670', result['matches'][0]['provenance'])

    def test_negated_or_compatibility_corpus_text_never_promotes_to_whole_miner(self):
        descriptions = ['Не продаю целый майнер Innosilicon A11 Pro. Описание уточню.',
                        'Не целый майнер Innosilicon A11 Pro, без плат.',
                        'Корпус для майнера Innosilicon A11 Pro в сборе.',
                        'Не продаю целый майнер Innosilicon A11 Pro, только корпус.']
        for desc in descriptions:
            result, _, _, _ = self.run_scan(details={'2490324333': item('2490324333',
                'Корпус Asic innosilicon a11 pro', desc)})
            self.assertEqual([], result['matches'], desc)

    def test_empty_case_detail_is_excluded_not_a_miner(self):
        shell = item('2490324333', 'Корпус Asic innosilicon a11 pro', 'Продаю только пустой корпус, без плат.')
        result, _, _, _ = self.run_scan(details={'2490324333': shell})
        self.assertEqual([], result['matches'])
        self.assertEqual([], result['review'])
        self.assertTrue(any(x['ad_id']=='2490324333' for x in result['excluded']))

    def test_hostile_item_links_never_enter_detail_queue_or_summary(self):
        ads = []
        for url in ['https://evil.invalid/asic_12345678', 'https://www.avito.ru@evil.invalid/asic_12345678',
                    'https://user:pass@www.avito.ru/asic_12345678', 'https://www.avito.ru:444/asic_12345678',
                    'http://www.avito.ru/asic_12345678']:
            ads.append({**item(), 'url': url})
        result, calls, _, _ = self.run_scan(ads=ads)
        self.assertEqual(3, len(calls))
        self.assertEqual(0, result['new_search_unique_count'])
        self.assertNotIn('evil.invalid', trial.markdown_summary(result))

    def test_search_requires_explicit_matching_numeric_item_id(self):
        ads = [{**item(), 'id': value} for value in (None, True, '', '99999999')]
        result, calls, _, _ = self.run_scan(ads=ads)
        self.assertEqual(0, result['new_search_unique_count'])
        self.assertEqual(3, len(calls))
        self.assertEqual([], result['matches'])

    def test_detail_identity_mismatch_stops_and_never_uses_wrong_listing(self):
        def hook(payload, count):
            if 'item' in payload:
                payload['item']['id'] = '99999999'
            return payload, 200
        result, calls, saved, _ = self.run_scan(ads=[item()], response_hook=hook)
        self.assertEqual(3, len(calls))
        self.assertEqual('RESPONSE_IDENTITY_MISMATCH', result['status'])
        self.assertNotIn('99999999', [x['ad_id'] for x in result['matches']])
        self.assertEqual('99999999', saved['response-03.json']['item']['ad_id'])

    def test_public_projection_excludes_seller_address_coordinates(self):
        result, _, saved, _ = self.run_scan(ads=[item()])
        text = json.dumps(saved, ensure_ascii=False)
        for forbidden in ('Private address', 'Do not save', '"lat"', '"lng"', '"seller"', '"address"'):
            self.assertNotIn(forbidden, text)
        self.assertEqual('Москва', result['matches'][0]['city'])
        self.assertEqual('Б/у', result['matches'][0]['condition'])

    def test_secret_redacted_before_truncation_json_and_markdown(self):
        key = 'visi_secret_"key'
        with patch.dict(os.environ, {'VISI_API_KEY': key}):
            self.assertNotIn('visi_se', trial.public_text('x'*11995 + key))
            self.assertNotIn('visi', trial.cell(key))
            result, _, saved, _ = self.run_scan(response_hook=lambda payload,n:
                ({'error': {'code': 'FAIL', 'message': key}, 'daily_remaining': 49}, 401))
            self.assertNotIn('visi_secret', json.dumps(saved))
            self.assertIn('[REDACTED]', json.dumps(saved))

    def test_run_guards_precede_key_or_transport(self):
        for env in ({'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_RUN_NUMBER': '1'},
                    {'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_RUN_NUMBER': '2'}):
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(SystemExit):
                    trial.main()

    def test_transport_uses_fixed_get_endpoints_and_preserves_http_error_json(self):
        calls=[]
        class Opener:
            def open(self, request, timeout):
                calls.append((request, timeout))
                raise HTTPError(request.full_url, 429, 'Limit', {}, io.BytesIO(b'{"error":"RATE_LIMIT","daily_remaining":49}'))
        payload,status = trial.make_transport('not-a-real-key', Opener())('search', 'https://www.avito.ru/all?q=Innosilicon+A10+Pro')
        self.assertEqual(429, status)
        self.assertEqual('RATE_LIMIT', payload['error'])
        self.assertEqual('GET', calls[0][0].get_method())
        self.assertTrue(calls[0][0].full_url.startswith('https://api.visi.su/api/v1/gate/search?'))
        self.assertIn('page=1', calls[0][0].full_url)
        self.assertEqual(60, calls[0][1])
        for kind,url in [('events','https://www.avito.ru/all'), ('search','https://www.avito.ru/all?q=other'),
                         ('item','https://evil.invalid/123')]:
            with self.assertRaises(ValueError):
                trial.make_transport('key', Opener())(kind,url)
        self.assertEqual(1, len(calls))

    def test_markdown_escapes_titles_and_uses_safe_direct_links(self):
        ad=item(title='Innosilicon A10 Pro <script>\n[bad](https://evil.invalid)')
        result, _, _, _ = self.run_scan(ads=[ad], details={'12345678': ad})
        summary=trial.markdown_summary(result)
        self.assertNotIn('<script>', summary)
        self.assertNotIn('[bad](https://evil.invalid)', summary)
        self.assertIn('[Авито](https://www.avito.ru/', summary)
        self.assertIn('Не полный обзор рынка', summary)
        self.assertIn('Пробный', summary)


if __name__ == '__main__':
    unittest.main()
