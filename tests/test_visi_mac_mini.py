"""Offline tests: no real VISI calls or credentials."""
import importlib.util
import io
import http.client
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / 'scripts/visi_mac_mini.py'
spec = importlib.util.spec_from_file_location('visi_mac_mini', PATH) if PATH.exists() else None
mac = importlib.util.module_from_spec(spec) if spec else None
if mac:
    spec.loader.exec_module(mac)


class Clock:
    def __init__(self):
        self.current = 1793714400
        self.elapsed = 0
    def time(self): return self.current
    def monotonic(self): return self.elapsed
    def sleep(self, seconds):
        self.current += seconds
        self.elapsed += seconds


def item(aid='12345678', title='Apple Mac mini M2 16/512', description='Полный комплект, исправен.', **extra):
    return {'id': aid, 'url': 'https://www.avito.ru/moskva/nastolnye_kompyutery/mac_mini_' + aid,
            'title': title, 'description': description, 'price': 45000,
            'location': {'name': 'Москва', 'lat': 10, 'lng': 20},
            'seller': {'name': 'Private'}, 'address': 'Private street', 'phone': 'Private phone',
            'active': True, 'condition': None, 'specs': None,
            'published': 'сегодня', **extra}


class MacMiniTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(mac, 'Standalone Mac mini parser is not implemented')

    def run_scan(self, pages=None, quotas=None, hook=None, error=None):
        calls = []
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            def transport(kind, url):
                saved = json.loads((folder / 'checkpoint.json').read_text())
                self.assertEqual(len(calls)+1, saved['calls_reserved'])
                self.assertEqual('reserved_unknown', saved['requests'][-1]['outcome'])
                calls.append((kind, url, clock.monotonic()))
                if error: raise error
                n = len(calls)
                payload = {'source': 'avito_tech', 'url': url, 'page': 1,
                           'daily_remaining': quotas[n-1] if quotas is not None else 100-n,
                           'items': (pages or {}).get(n, []), 'has_next': False,
                           'fetched_at': '2026-11-03T14:00:00Z'}
                return hook(payload, n) if hook else (payload, 200)
            report = mac.scan(transport, folder, clock=clock, approved=True)
            saved = {p.name: json.loads(p.read_text()) for p in folder.glob('*.json')}
        return report, calls, saved

    def test_exact_five_first_pages_and_durable_reservations(self):
        report, calls, saved = self.run_scan()
        self.assertEqual(['https://www.avito.ru/all?q=Mac+mini+M1',
                          'https://www.avito.ru/all?q=Mac+mini+M2',
                          'https://www.avito.ru/all?q=Mac+mini+M3',
                          'https://www.avito.ru/all?q=Mac+mini+M4',
                          'https://www.avito.ru/all?q=Mac+mini+M5'], [c[1] for c in calls])
        self.assertEqual(['search']*5, [c[0] for c in calls])
        self.assertTrue(all(b[2]-a[2] >= 11 for a,b in zip(calls,calls[1:])))
        self.assertEqual('FIRST_PAGES_COMPLETE', report['status'])
        self.assertEqual(5, report['pages_received'])
        self.assertFalse(report['market_exhausted'])
        self.assertEqual(5, saved['report.json']['calls_reserved'])

    def test_processor_must_be_listing_evidence_not_query(self):
        rows = [item('12345671', 'Mac mini M1 8/256'), item('12345672', 'Мак мини М2 Pro'),
                item('12345673', 'Mac mini M3'), item('12345674', 'Mac mini M4'),
                item('12345675', 'Mac mini M5'), item('12345676', 'Mac mini', 'Продам компьютер.')]
        for row in rows: row['specs'] = []
        report, _, _ = self.run_scan(pages={1: rows})
        self.assertEqual(['M1', 'M2 Pro', 'M3', 'M4', 'M5'], sorted(r['processor_claim'] for r in report['matches']))
        self.assertTrue(all(r['manufacturer_verified'] is False for r in report['matches']))
        self.assertEqual('12345676', report['review'][0]['ad_id'])
        self.assertIsNone(report['review'][0]['processor_claim'])

    def test_other_devices_intel_services_accessories_are_excluded(self):
        titles = ['MacBook Pro M2, аналог Mac mini', 'iPad mini M1', 'Mac Studio M2 Max',
                  'Mac mini Intel i5', 'Куплю Mac mini M4', 'Ремонт Mac mini M2',
                  'Аренда Mac mini M1', 'Блок питания для Mac mini M2',
                  'Подставка Mac mini M4', 'Корпус Mac mini M1', 'SSD для Mac mini M4',
                  'Док-станция для Mac mini M2', 'Клавиатура Mac mini M1']
        for title in titles:
            with self.subTest(title=title):
                row = mac.classify(mac.public_item(item(title=title)))
                self.assertEqual('excluded', row['classification'])

    def test_bundle_is_candidate_but_faulty_or_ambiguous_is_review(self):
        cases = [('Mac mini M2 + клавиатура и мышь', 'Исправный компьютер, блок питания в комплекте.', 'candidate'),
                 ('Mac mini M2 не включается', 'Целиком, на запчасти.', 'review'),
                 ('Mac mini M1 M2 M4', 'Разные модели, цена за одну.', 'review'),
                 ('Mac mini M4', 'MDM, есть блокировка.', 'review'),
                 ('Mac mini', 'Продам Mac mini M2 8/256.', 'review')]
        for title, desc, expected in cases:
            with self.subTest(title=title):
                raw = item(title=title, description=desc, specs=[])
                self.assertEqual(expected, mac.classify(mac.public_item(raw))['classification'])

    def test_description_contradictions_never_become_candidates(self):
        cases = [
            ('Mac mini M2', 'Фактически Mac mini M1, ошибся в заголовке.', 'review'),
            ('Mac mini M2', 'Фактически Intel Core i5, ошибся в заголовке.', 'review'),
            ('Mac mini M2', 'Только коробка для Mac mini M2, компьютера нет.', 'excluded'),
            ('Mac mini M2 ремонт и настройка', 'Сервисный центр.', 'excluded'),
            ('Mac mini M2 на ремонт', 'Целиком, не включается.', 'review'),
            ('Mac mini M2', 'Не рабочий. Activation Lock.', 'review'),
        ]
        for title, desc, expected in cases:
            with self.subTest(title=title, description=desc):
                row = mac.classify(mac.public_item(item(title=title, description=desc, specs=[])))
                self.assertEqual(expected, row['classification'])

    def test_incomplete_http_body_stops_and_preserves_final_outputs(self):
        report, calls, saved = self.run_scan(error=http.client.IncompleteRead(b'partial secret', 50))
        self.assertEqual(1, len(calls))
        self.assertEqual('TRANSPORT_ERROR', report['status'])
        self.assertEqual('transport_error_unknown', report['requests'][0]['outcome'])
        self.assertNotIn('partial secret', json.dumps(saved))
        with tempfile.TemporaryDirectory() as tmp:
            mac.write_outputs(report, Path(tmp))
            self.assertTrue((Path(tmp)/'README.md').exists())
            self.assertTrue((Path(tmp)/'ads.csv').exists())

    def test_spec_conflict_and_truncation_are_review_not_false_match(self):
        conflict = item(title='Mac mini M1', specs=[{'name': 'Процессор', 'value': 'M2'}])
        self.assertEqual('review', mac.classify(mac.public_item(conflict))['classification'])
        long = item(description='x'*12001)
        self.assertEqual('review', mac.classify(mac.public_item(long))['classification'])

    def test_identity_validation_and_deduplication(self):
        good = item()
        bad = [item('12345679', url='https://evil.test/mac_mini_12345679'),
               item('12345670', url='https://www.avito.ru/x/mac_mini_12345671'),
               item('12345672', id=True), item('12345673', id=None)]
        report, _, _ = self.run_scan(pages={1: [good]+bad, 2: [good]})
        self.assertEqual(1, report['unique_listings'])
        self.assertEqual(4, report['invalid_rows'])
        self.assertEqual(2, len(report['matches'][0]['provenance']))

    def test_conflicting_snapshots_are_not_silently_merged(self):
        report, _, _ = self.run_scan(pages={1: [item()], 2: [item(title='Mac mini M1', specs=[])]})
        self.assertEqual([], report['matches'])
        self.assertEqual('conflicting_search_snapshots', report['review'][0]['reason'])

    def test_material_price_city_change_is_not_silently_merged(self):
        report, _, _ = self.run_scan(pages={1: [item()], 2: [item(price=55000, location={'name':'Санкт-Петербург'})]})
        self.assertEqual([], report['matches'])
        self.assertEqual('conflicting_search_snapshots', report['review'][0]['reason'])

    def test_incomplete_http_body_retains_successful_first_page(self):
        def hook(payload, number):
            if number == 2: raise http.client.IncompleteRead(b'partial secret', 50)
            return payload, 200
        report, calls, saved = self.run_scan(pages={1:[item()]}, hook=hook)
        self.assertEqual(2, len(calls))
        self.assertEqual(1, report['pages_received'])
        self.assertEqual(1, len(report['matches']))
        self.assertEqual('TRANSPORT_ERROR', report['status'])
        self.assertEqual('success', report['requests'][0]['outcome'])
        self.assertEqual('transport_error_unknown', report['requests'][1]['outcome'])
        self.assertEqual('TRANSPORT_ERROR', saved['report.json']['status'])

    def test_inactive_listing_is_excluded(self):
        report, _, _ = self.run_scan(pages={1: [item(active=False)]})
        self.assertEqual([], report['matches'])
        self.assertEqual('inactive', report['excluded'][0]['reason'])

    def test_api_error_preserved_no_retry(self):
        def hook(payload, n):
            return ({'error': {'code': 'PLAN_REQUIRED', 'message': 'API plan required'}, 'daily_remaining': 0}, 403)
        report, calls, saved = self.run_scan(hook=hook)
        self.assertEqual(1, len(calls))
        self.assertEqual('HTTP_OR_API_ERROR', report['status'])
        self.assertEqual('PLAN_REQUIRED', saved['response-01.json']['error']['code'])

    def test_timeout_is_uncertain_not_retried(self):
        report, calls, saved = self.run_scan(error=TimeoutError('secret should not be saved'))
        self.assertEqual(1, len(calls))
        self.assertEqual('TRANSPORT_ERROR', report['status'])
        self.assertEqual('transport_error_unknown', report['requests'][0]['outcome'])
        self.assertNotIn('secret should not', json.dumps(saved))

    def test_missing_invalid_or_increasing_quota_stops_and_preserves_rows(self):
        for quotas, count, reason in [([None], 1, 'QUOTA_INVALID'), ([True], 1, 'QUOTA_INVALID'),
                                     ([-1], 1, 'QUOTA_INVALID'), ([3, 4], 2, 'QUOTA_CHANGED'),
                                     ([3, 1], 2, 'QUOTA_CHANGED'), ([0], 1, 'QUOTA_EXHAUSTED')]:
            with self.subTest(quotas=quotas):
                report, calls, _ = self.run_scan(pages={1: [item()]}, quotas=quotas)
                self.assertEqual(count, len(calls))
                self.assertEqual(reason, report['status'])
                self.assertEqual(1, len(report['links']))

    def test_identity_or_schema_error_never_ingests_rows(self):
        mutations = [{'page': 2}, {'url': 'https://www.avito.ru/all?q=other'},
                     {'source': 'other'}, {'items': [item()]*51}, {'items': None},
                     {'items': [item(), 'bad']}]
        for change in mutations:
            with self.subTest(change=list(change)):
                report, calls, _ = self.run_scan(pages={1: [item()]}, hook=lambda p,n: ({**p, **change}, 200))
                self.assertEqual(1, len(calls))
                self.assertEqual([], report['links'])
                self.assertNotEqual('FIRST_PAGES_COMPLETE', report['status'])

    def test_unknown_pagination_does_not_claim_full_market(self):
        report, _, _ = self.run_scan(hook=lambda p,n: ({**p, 'has_next': None}, 200))
        self.assertTrue(all(p['next_page_pending'] for p in report['queries']))
        self.assertFalse(report['market_exhausted'])
        self.assertIn('не полный обзор рынка', mac.markdown_summary(report).lower())

    def test_public_projection_redacts_key_and_private_fields(self):
        with patch.dict(os.environ, {'VISI_API_KEY': 'test-secret'}):
            report, _, saved = self.run_scan(pages={1: [item(description='test-secret description')]})
            text = json.dumps(saved, ensure_ascii=False)
            self.assertNotIn('test-secret', text)
            for value in ('Private phone', 'Private street', 'Private', '"lat"', '"lng"', '"seller"'):
                self.assertNotIn(value, text)
            self.assertIn('[REDACTED]', text)

    def test_summary_and_csv_show_every_safe_link_and_escape_text(self):
        report, _, _ = self.run_scan(pages={1: [item(title='Mac mini M2 [x](javascript:alert(1)) | <script>'),
                                               item('12345679', 'Куплю Mac mini M1')]})
        summary = mac.markdown_summary(report)
        self.assertNotIn('<script>', summary)
        self.assertNotIn('<details>', summary)
        for row in report['links']: self.assertIn(row['url'], summary)
        with tempfile.TemporaryDirectory() as tmp:
            report['links'][0]['title'] = '=DANGEROUS()'
            mac.write_outputs(report, Path(tmp))
            self.assertIn("'=DANGEROUS()", (Path(tmp)/'ads.csv').read_text())

    def test_unapproved_run_and_existing_checkpoint_make_no_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with self.assertRaises(ValueError): mac.scan(lambda *a: self.fail('network'), folder)
            (folder/'checkpoint.json').write_text('{}')
            with self.assertRaises(ValueError): mac.scan(lambda *a: self.fail('network'), folder, approved=True)

    def test_main_guards_precede_secret_and_transport(self):
        for env in [{}, {'GITHUB_RUN_ATTEMPT': '2', 'VISI_API_PLAN_CONFIRMED': 'true'},
                    {'GITHUB_RUN_ATTEMPT': '1', 'VISI_API_PLAN_CONFIRMED': 'false'}]:
            with patch.dict(os.environ, env, clear=True), patch.object(mac, 'make_transport', side_effect=AssertionError('network')):
                with self.assertRaises(SystemExit): mac.main()

    def test_transport_fixed_search_allowlist_and_http_error(self):
        captured = []
        class Opener:
            def open(self, request, timeout):
                captured.append(request)
                raise HTTPError(request.full_url, 403, 'Forbidden', {}, io.BytesIO(b'{"error":{"code":"PLAN_REQUIRED"}}'))
        transport = mac.make_transport('unit-test-key', Opener())
        for kind, url in [('item', 'https://www.avito.ru/x_12345678'), ('search', 'https://evil.test')]:
            with self.assertRaises(ValueError): transport(kind, url)
        self.assertEqual([], captured)
        payload, status = transport('search', 'https://www.avito.ru/all?q=Mac+mini+M1')
        self.assertEqual(403, status)
        self.assertEqual('PLAN_REQUIRED', payload['error']['code'])
        request = captured[0]
        self.assertEqual('https://api.visi.su/api/v1/gate/search', request.full_url.split('?')[0])
        self.assertEqual({'url':['https://www.avito.ru/all?q=Mac+mini+M1'], 'page':['1']}, parse_qs(urlsplit(request.full_url).query))
        self.assertEqual('GET', request.method)
        self.assertNotIn('unit-test-key', request.full_url)


if __name__ == '__main__': unittest.main()
