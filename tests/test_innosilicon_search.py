import importlib.util
import pathlib
import tempfile
import unittest

PATH = pathlib.Path(__file__).parents[1] / 'scripts' / 'innosilicon_search.py'
spec = importlib.util.spec_from_file_location('search', PATH) if PATH.exists() else None
search = importlib.util.module_from_spec(spec) if spec else None
if search:
    spec.loader.exec_module(search)


class ScopedSearchTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(search, 'Scoped search implementation does not exist yet')

    def run_scan(self, balance=8, pages=None, failure=None):
        calls = []
        def transport(method, path, body):
            calls.append((method, path, body))
            if method == 'GET':
                return {'ok': True, 'data': {'remaining': balance}}, None
            if failure:
                raise failure
            page = body['page']
            more = pages.get(body['query'], {}).get(page, True) if pages else True
            return {'ok': True, 'data': {'page': page, 'listings': [], 'has_more': more},
                    'meta': {'charged_credits': 2}}, balance - 2 * (len(calls) - 1)
        with tempfile.TemporaryDirectory() as folder:
            result = search.scan(transport, pathlib.Path(folder))
        return result, calls

    def test_at_most_four_searches_only_two_models(self):
        result, calls = self.run_scan()
        self.assertEqual(4, len(calls) - 1)
        self.assertEqual(8, result['credits_reserved'])
        self.assertEqual(['Innosilicon A10 Pro', 'Innosilicon A11 Pro'] * 2,
                         [call[2]['query'] for call in calls[1:]])
        self.assertEqual([1, 1, 2, 2], [call[2]['page'] for call in calls[1:]])
        self.assertTrue(all(c[2]['location'] == 'all' and not c[2]['include_pii'] for c in calls[1:]))

    def test_balance_is_not_budget(self):
        result, calls = self.run_scan(balance=2000)
        self.assertEqual(4, len(calls) - 1)
        self.assertEqual(8, result['credits_reserved'])

    def test_smaller_balance_is_respected(self):
        result, calls = self.run_scan(balance=4)
        self.assertEqual(2, len(calls) - 1)
        self.assertEqual(4, result['credits_reserved'])

    def test_zero_balance_does_not_search(self):
        result, calls = self.run_scan(balance=0)
        self.assertEqual(1, len(calls))
        self.assertEqual(0, result['credits_reserved'])

    def test_no_pagination_after_exhaustion(self):
        result, calls = self.run_scan(pages={q: {1: False} for q in search.QUERIES})
        self.assertEqual(2, len(calls) - 1)
        self.assertEqual('exhausted', result['status'])

    def test_uncertain_request_is_reserved_and_not_retried(self):
        result, calls = self.run_scan(failure=TimeoutError('uncertain'))
        self.assertEqual(2, len(calls))
        self.assertEqual(2, result['credits_reserved'])
        self.assertEqual('stopped_error', result['status'])

    def test_unknown_balance_stops_without_search(self):
        calls = []
        def transport(*args):
            calls.append(args)
            return {'ok': True, 'data': {}}, None
        with tempfile.TemporaryDirectory() as folder:
            result = search.scan(transport, pathlib.Path(folder))
        self.assertEqual(1, len(calls))
        self.assertEqual('stopped_error', result['status'])

    def test_unknown_pagination_never_claims_exhaustion(self):
        with self.assertRaises(ValueError):
            search.next_page({'listings': []}, 1)

    def test_missing_or_higher_cost_stops_before_second_search(self):
        for charge in (None, 3):
            calls = []
            def transport(method, path, body):
                calls.append((method, path, body))
                if method == 'GET':
                    return {'remaining': 8}, None
                return {'ok': True, 'data': {'listings': [], 'has_more': True},
                        'meta': {'charged_credits': charge}}, 6
            with tempfile.TemporaryDirectory() as folder:
                result = search.scan(transport, pathlib.Path(folder))
            self.assertEqual(2, len(calls))
            self.assertEqual('stopped_error', result['status'])

    def test_valid_results_survive_missing_cost(self):
        row = {'title': 'Innosilicon A10 Pro', 'ad_id': '12345678',
               'url': 'https://www.avito.ru/moskva/example_12345678', 'location': {'name': 'Москва'}}
        def transport(method, path, body):
            if method == 'GET':
                return {'remaining': 8}, None
            return {'ok': True, 'data': {'listings': [row], 'has_more': False}, 'meta': {}}, 6
        with tempfile.TemporaryDirectory() as folder:
            result = search.scan(transport, pathlib.Path(folder))
            self.assertTrue((pathlib.Path(folder) / 'a10-page-1.json').exists())
        self.assertEqual('stopped_error', result['status'])
        self.assertEqual(1, len(result['matches']))
        self.assertEqual('Москва', result['matches'][0]['city'])

    def test_new_dispatch_after_first_is_blocked(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_RUN_NUMBER': '2'}):
            with self.assertRaisesRegex(SystemExit, 'first dispatch'):
                search.main()

    def test_rerun_guard_precedes_paid_transport(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '2'}):
            with self.assertRaises(SystemExit):
                search.main()

    def test_exact_models_and_obvious_typos(self):
        for title, model in [('Inosilicon A10 Pro 7Gb', 'Innosilicon A10 Pro'),
                             ('Инносиликон а11 про', 'Innosilicon A11 Pro')]:
            self.assertEqual([model], search.classify({'title': title})['models'])
        self.assertTrue(search.classify({'title': 'Innosilicon A10 Pro на запчасти'})['needs_manual_review'])

    def test_parts_service_bare_models_and_other_brands_are_excluded(self):
        for title in ['Innosilicon A10', 'Innosilicon A11', 'Блок питания Innosilicon A10 Pro',
                      'Куплю Innosilicon A11 Pro', 'Ремонт Innosilicon A10 Pro',
                      'Хэш плата Innosilicon A10 Pro', 'Avalon A10 Pro', 'Innosilicon A10 Pro+',
                      'Control board Innosilicon A11 Pro', 'Samsung A10 Pro',
                      'Antminer A10 Pro', 'Samsung A10 Pro майнер', 'Радиатор Innosilicon A11 Pro',
                      'Запчасти Innosilicon A10 Pro', 'Хешборд Innosilicon A11 Pro', 'Bitmain ASIC A10 Pro',
                      'Прошивка Innosilicon A10 Pro']:
            self.assertIsNone(search.classify({'title': title}), title)

    def test_workflow_is_manual_and_cannot_write_repository(self):
        workflow = (PATH.parents[1] / '.github/workflows/innosilicon-a10-a11.yml').read_text()
        self.assertIn('on:\n  workflow_dispatch:\n\n', workflow)
        self.assertIn('permissions:\n  contents: read\n\n', workflow)
        self.assertIn('group: asic-full-scan', workflow)
        self.assertNotIn('schedule:', workflow)
        self.assertNotIn('push:', workflow)
        self.assertIn('github.run_number == 1', workflow)


if __name__ == '__main__':
    unittest.main()
