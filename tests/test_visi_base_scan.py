"""Offline contract tests for the explicitly scoped 44-page VISI base scan."""
import importlib.util
import io
from contextlib import redirect_stdout
import json
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

ROOT = pathlib.Path(__file__).parents[1]
PATH = ROOT / 'scripts/visi_base_scan.py'
spec = importlib.util.spec_from_file_location('visi_base', PATH) if PATH.exists() else None
base = importlib.util.module_from_spec(spec) if spec else None
if base:
    spec.loader.exec_module(base)

class Clock:
    def __init__(self, now=None):
        self.current = now if now is not None else datetime(2026,11,1,18,35,tzinfo=timezone.utc).timestamp()
        self.elapsed=0
    def time(self): return self.current
    def monotonic(self): return self.elapsed
    def sleep(self, seconds): self.current+=seconds; self.elapsed+=seconds

def item(aid='12345678', title='Antminer S19 95T', desc='В наличии 15 шт.', active=True):
    return {'id':aid,'title':title,'url':'https://www.avito.ru/moskva/asic_'+aid,
            'description':desc,'price':25000,'location':{'name':'Москва','lat':1,'lng':2},
            'seller':{'name':'private-seller'},'address':'private-address','active':active,'condition':'Б/у'}

class VisiBaseTests(unittest.TestCase):
    def setUp(self): self.assertIsNotNone(base, 'Base scan implementation missing')

    def run_scan(self, ads=None, details=None, hook=None, clock=None, fail_at=None):
        calls=[]; snapshots=[]; clock=clock or Clock(); details=details or {}
        with tempfile.TemporaryDirectory() as tmp:
            folder=pathlib.Path(tmp)
            def transport(kind,url):
                calls.append((kind,url,clock.monotonic()))
                snapshots.append(json.loads((folder/'checkpoint.json').read_text()))
                n=len(calls)
                if n==fail_at: raise TimeoutError('secret exception must not persist')
                if kind=='search':
                    rows=ads(n) if callable(ads) else (ads or [])
                    payload={'source':'avito_tech','url':url,'page':1,'has_next':True,'items':rows,'daily_remaining':47-n}
                else:
                    aid=url.rsplit('_',1)[1]; row=details.get(aid) or item(aid)
                    payload={'source':'avito_tech','item':{**row,'url':url},'daily_remaining':47-n}
                return hook(payload,n) if hook else (payload,200)
            output=io.StringIO()
            with redirect_stdout(output):
                result=base.scan(transport,folder,clock=clock)
            self.progress_output=output.getvalue()
            saved={p.name:json.loads(p.read_text()) for p in folder.glob('*.json')}
        return result,calls,saved,snapshots

    def test_exact_44_first_pages_full105_checkpoint_and_no_a10_a11_requests(self):
        result,calls,saved,_=self.run_scan()
        self.assertEqual(44,len(calls))
        queries=[parse_qs(urlsplit(c[1]).query)['q'][0] for c in calls]
        self.assertEqual('Antminer S19',queries[0]); self.assertEqual('Cheetah Miner F1',queries[-1])
        self.assertEqual(44,len(set(queries)))
        self.assertFalse(any('A10' in q or 'A11' in q for q in queries))
        self.assertTrue(all(c[0]=='search' for c in calls))
        queue=saved['checkpoint.json']['full_query_queue']
        self.assertEqual(105,len(queue))
        self.assertEqual(45,len([q for q in queue if q['base_model']]))
        a10=next(q for q in queue if q['query']=='Innosilicon A10 Pro')
        self.assertEqual('deferred',a10['status']); self.assertFalse(a10['selected_this_run'])
        self.assertEqual(44,sum(q['status']=='first_page_complete' for q in queue))
        self.assertEqual(61,sum(q['status']=='deferred' for q in queue))
        self.assertTrue(all(q['has_next'] is True for q in queue if q['selected_this_run']))
        self.assertEqual(3,result['quota_last_confirmed'])
        self.assertFalse(saved['checkpoint.json']['resume_authorized'])

    def test_reserve_and_persist_before_every_network_effect(self):
        result,calls,saved,snapshots=self.run_scan(fail_at=2)
        self.assertEqual(2,len(calls))
        for n,snapshot in enumerate(snapshots,1):
            self.assertEqual(n,snapshot['calls_reserved'])
            self.assertEqual('reserved_unknown',snapshot['ledger'][-1]['outcome'])
        cp=saved['checkpoint.json']
        self.assertEqual('transport_error_unknown',cp['ledger'][-1]['outcome'])
        self.assertEqual('blocked',cp['full_query_queue'][1]['status'])
        self.assertEqual('pending',cp['full_query_queue'][2]['status'])
        self.assertEqual(46,cp['quota_last_confirmed'])
        self.assertEqual('TimeoutError',result['error_type'])
        self.assertNotIn('secret exception',json.dumps(saved))

    def test_strict_quota_first46_and_exact_decrement_no_retries(self):
        for value in (None,True,'46',46.0,45,47,50,1499):
            result,calls,saved,_=self.run_scan(ads=[item()],hook=lambda p,n,v=value:({**p,'daily_remaining':v},200))
            self.assertEqual(1,len(calls),value)
            self.assertEqual('QUOTA_MISMATCH',result['status'])
            self.assertEqual(1,len(result['matches']))
            self.assertIsNone(result['quota_last_confirmed'])
            self.assertIn('response-01.json',saved)
        result,calls,_,_=self.run_scan(hook=lambda p,n:({**p,'daily_remaining':46} if n==2 else p,200))
        self.assertEqual(2,len(calls));self.assertEqual('QUOTA_MISMATCH',result['status'])

    def test_http_error_keeps_observed_error_quota_separate_from_confirmed(self):
        def hook(payload,n):
            if n==2:return {'error':{'code':'UPSTREAM_FAILED','message':'safe'},'daily_remaining':46},502
            return payload,200
        result,calls,saved,_=self.run_scan(hook=hook)
        self.assertEqual(2,len(calls));self.assertEqual(46,result['quota_last_confirmed'])
        self.assertEqual(46,saved['checkpoint.json']['ledger'][-1]['observed_remaining'])
        self.assertEqual('UPSTREAM_FAILED',saved['response-02.json']['error']['code'])
        self.assertEqual(502,saved['response-02.json']['http_status'])

    def test_all_searches_before_only_three_best_whole_asic_details(self):
        ads=[item('12345670','Antminer S19 95T','В наличии 20 шт.'),
             item('12345671','Antminer S19 95T','В наличии 100 шт.'),
             item('12345672','Avalon 841','В наличии 30 шт.'),
             item('12345673','Antminer S19 95T','Количество уточняйте.'),
             item('12345674','Innosilicon A10 Pro','В наличии 200 шт.'),
             item('12345675','Ремонт Antminer S19','Обслужим 1000 шт.'),
             item('12345676','Блок питания Antminer S19','В наличии 1000 шт.')]
        result,calls,saved,_=self.run_scan(ads=ads,details={x['id']:x for x in ads})
        self.assertEqual(47,len(calls))
        self.assertEqual(['search']*44+['item']*3,[c[0] for c in calls])
        self.assertEqual(['12345671','12345672','12345670'],[c[1].rsplit('_',1)[1] for c in calls[44:]])
        self.assertEqual(0,result['quota_last_confirmed'])
        self.assertEqual(7,result['unique_listings'])
        self.assertEqual(44,len(saved['checkpoint.json']['records']['12345670']['provenance']))
        self.assertTrue(all(b[2]-a[2]>=11 for a,b in zip(calls,calls[1:])))

    def test_general_thresholds_broken_miners_and_bundles_are_preserved(self):
        ads=[item('12345670','Antminer S19 95T под ремонт','15 шт.'),
             item('12345671','Avalon 841','19 шт.'),
             item('12345672','Whatsminer M31S 74T','20 шт.'),
             item('12345673','Bitfury B8 50T','5 шт.'),
             item('12345674','Antminer S19 XP','100 шт.'),
             item('12345675','Antminer S19 95T с блоками питания','15 асиков с БП 20 шт.')]
        result,_,_,_=self.run_scan(ads=ads,details={x['id']:x for x in ads})
        byid={x['ad_id']:x for x in result['matches']}
        self.assertEqual({'12345670','12345672','12345673','12345675'},set(byid))
        self.assertEqual(15,byid['12345675']['quantity_min'])

    def test_missing_hashrate_manual_candidate_but_a11_and_services_no_details(self):
        ads=[item('12345670','Whatsminer M31S','В наличии 50 шт.'),
             item('12345671','Innosilicon A11 Pro','1 шт.'),
             item('12345672','Выкуп Antminer S19','Выкупаем ваши асики.'),
             item('12345673','Control board Antminer S19','100 шт.')]
        result,calls,_,_=self.run_scan(ads=ads,details={x['id']:x for x in ads})
        self.assertEqual(45,len(calls))
        self.assertTrue(calls[-1][1].endswith('_12345670'))
        self.assertTrue(any(x['ad_id']=='12345670' for x in result['review']))

    def test_incidental_a10_mention_does_not_hide_valid_s19_lot(self):
        ad=item('12345670','Antminer S19 95T','15 шт.\nСравнивали с Innosilicon A10 Pro.')
        result,calls,_,_=self.run_scan(ads=[ad],details={'12345670':ad})
        self.assertEqual(45,len(calls))
        self.assertEqual(['12345670'],[x['ad_id'] for x in result['matches']])

    def test_a10_titled_mixed_listing_keeps_independent_s19_lot(self):
        ad=item('12345670','Innosilicon A10 Pro',
                'Innosilicon A10 Pro — 1 шт.\nAntminer S19 95T — 15 шт.')
        result,calls,_,_=self.run_scan(ads=[ad],details={'12345670':ad})
        self.assertEqual(45,len(calls))
        self.assertEqual(['Antminer S19 95T'],[x['model'] for x in result['matches']])

    def test_ambiguous_corpus_is_review_only_and_never_spends_detail(self):
        ad=item('12345670','Корпус Antminer S19 95T','Включён в розетку. 100 шт.')
        result,calls,_,_=self.run_scan(ads=[ad])
        self.assertEqual(44,len(calls))
        self.assertEqual([],result['matches'])
        self.assertEqual(['12345670'],[x['ad_id'] for x in result['review']])

    def test_truncated_ambiguous_corpus_never_spends_detail(self):
        raw=item('12345670','Корпус Antminer S19 95T','Включён в розетку. 100 шт. '+'x'*13000)
        row=base.visi.public_item(raw)
        general=base.visi.filters.load_general()
        found,pending,reason,eligible,quantity=base.classify_row(row,general,set())
        self.assertEqual([],found)
        self.assertTrue(pending)
        self.assertFalse(eligible)

    def test_a10_titled_truncated_comparison_is_not_another_model_offer(self):
        raw=item('12345670','Innosilicon A10 Pro',
            'Продаю только Innosilicon A10 Pro. Сравнивал с Antminer S19. '+'x'*13000)
        general=base.visi.filters.load_general()
        found,pending,reason,eligible,quantity=base.classify_row(base.visi.public_item(raw),general,set())
        self.assertEqual([],found)
        self.assertFalse(eligible)

    def test_inactive_or_parts_detail_replaces_previous_match(self):
        ads=[item('12345670'),item('12345671')]
        details={'12345670':item('12345670',active=False),
                 '12345671':item('12345671','Блок питания Antminer S19','15 шт.')}
        result,_,_,_=self.run_scan(ads=ads,details=details)
        self.assertEqual([],result['matches'])
        self.assertEqual(2,len(result['excluded']))

    def test_bad_ids_urls_and_detail_identity_never_enter_unrelated_requests(self):
        good=item();bad=[{**good,'id':None},{**good,'id':True},{**good,'id':'99999999'},
                          {**good,'url':'https://evil.invalid/asic_12345678'}]
        result,calls,_,_=self.run_scan(ads=bad)
        self.assertEqual(44,len(calls));self.assertEqual(0,result['unique_listings'])
        def hook(p,n):
            if n==45:p['item']['id']='99999999'
            return p,200
        result,calls,saved,_=self.run_scan(ads=[good],hook=hook)
        self.assertEqual(45,len(calls));self.assertEqual('RESPONSE_IDENTITY_MISMATCH',result['status'])
        self.assertEqual([],result['matches']);self.assertEqual(1,len(result['review']))

    def test_partial_results_checkpoint_and_pending_has_next_survive_error(self):
        result,calls,saved,_=self.run_scan(ads=[item()],fail_at=3)
        self.assertEqual(3,len(calls));self.assertEqual(1,len(result['matches']))
        cp=saved['checkpoint.json'];self.assertEqual(1,len(cp['records']))
        self.assertEqual(2,len(cp['records']['12345678']['provenance']))
        self.assertIn('Подтверждённо получено 2/44',base.markdown_summary(result))
        self.assertTrue(cp['full_query_queue'][0]['next_page_pending'])
        self.assertEqual(41,sum(q['status']=='pending' for q in cp['full_query_queue']))

    def test_safe_progress_only_after_confirmed_response(self):
        self.run_scan(fail_at=2)
        self.assertEqual('VISI: search 1/44; details 0/3; remaining 46\n',self.progress_output)
        self.assertNotIn('https:',self.progress_output)

    def test_old_checkpoint_or_rerun_cannot_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=pathlib.Path(tmp);(folder/'checkpoint.json').write_text('{"calls_reserved":47}')
            with self.assertRaisesRegex(ValueError,'resume'):
                base.scan(lambda *a: self.fail('No transport allowed'),folder,clock=Clock())
        for env in ({'GITHUB_RUN_ATTEMPT':'2','GITHUB_RUN_NUMBER':'1'},
                    {'GITHUB_RUN_ATTEMPT':'1','GITHUB_RUN_NUMBER':'2'}):
            with patch.dict(os.environ,env,clear=True):
                with self.assertRaises(SystemExit):base.main()

    def test_expiry_checked_before_first_call_and_after_spacing(self):
        for now,count in [(base.visi.TRIAL_EXPIRES_AT,0),(base.visi.TRIAL_EXPIRES_AT-95,1)]:
            result,calls,_,_=self.run_scan(clock=Clock(now))
            self.assertEqual(count,len(calls));self.assertEqual('TRIAL_PROOF_INVALID_OR_EXPIRED',result['status'])

    def test_spacing_uses_transport_start_after_slow_reservation(self):
        clock=Clock();save=base.atomic_save;delayed=False
        def slow_save(path,value):
            nonlocal delayed
            save(path,value)
            if not delayed and path.name=='checkpoint.json' and value.get('calls_reserved')==1:
                clock.sleep(5);delayed=True
        with patch.object(base,'atomic_save',side_effect=slow_save):
            _,calls,_,_=self.run_scan(clock=clock)
        self.assertTrue(all(b[2]-a[2]>=11 for a,b in zip(calls,calls[1:])),calls[:3])

    def test_expiry_during_reservation_prevents_transport(self):
        clock=Clock(base.visi.TRIAL_EXPIRES_AT-95);save=base.atomic_save;delayed=False
        def slow_save(path,value):
            nonlocal delayed
            save(path,value)
            if not delayed and path.name=='checkpoint.json' and value.get('calls_reserved')==1:
                clock.sleep(10);delayed=True
        with patch.object(base,'atomic_save',side_effect=slow_save):
            result,calls,saved,_=self.run_scan(clock=clock)
        self.assertEqual([],calls)
        self.assertEqual('TRIAL_PROOF_INVALID_OR_EXPIRED',result['status'])
        self.assertEqual('not_sent_trial_expired',saved['checkpoint.json']['ledger'][-1]['outcome'])

    def test_summary_all_unique_links_visible_no_collapsed_sections(self):
        ads=[item('12345670'),item('12345671','Ремонт Antminer S19','Услуги'),item('12345672','Innosilicon A10 Pro','1 шт.')]
        result,_,_,_=self.run_scan(ads=ads,details={x['id']:x for x in ads})
        summary=base.markdown_summary(result)
        self.assertNotIn('<details>',summary)
        for ad in ads:self.assertIn(ad['url'],summary)
        self.assertIn('Не полный обзор рынка',summary)
        self.assertIn('105',summary)

    def test_public_projection_and_visi_redaction_preserved(self):
        key='visi_secret_"key'
        ad=item(title='Antminer S19 95T <script> [bad](https://evil.invalid) '+key)
        with patch.dict(os.environ,{'VISI_API_KEY':key}):
            result,_,saved,_=self.run_scan(ads=[ad],fail_at=2)
            text=json.dumps(saved,ensure_ascii=False);summary=base.markdown_summary(result)
        for value in ('private-seller','private-address','"lat"','"lng"','visi_secret'):
            self.assertNotIn(value,text)
        self.assertNotIn('<script>',summary);self.assertNotIn('[bad](https://evil.invalid)',summary)

    def test_summary_worst_case_2200_links_fit_eight_bounded_parts(self):
        links=[]
        for i in range(2200):
            aid=str(12345670+i)
            prefix='https://www.avito.ru/x/'
            url=prefix+'a'*(2048-len(prefix)-len(aid)-1)+'_'+aid
            links.append({'ad_id':aid,'url':url,'title':'<'*100,'city':'&'*40,
                          'reason':'>'*100,'classification':'excluded','price_text':'9'*100,'quantity':'9'*100})
        parts,overflow=base.summary_parts({'links':links,'status':'TEST'})
        self.assertFalse(overflow);self.assertLessEqual(len(parts),8)
        self.assertTrue(all(len(p.encode())<=900*1024 for p in parts))
        for number,part in enumerate(parts,1):
            self.assertTrue(f'Ссылки — часть {number} из {len(parts)}' in part, 'Missing numbered part heading')
        combined=''.join(parts)
        self.assertNotIn('<details>',combined)
        for row in links:self.assertEqual(1,combined.count(row['url']))

    def test_oversize_page_stops_and_preserves_audit_without_ingesting_extra_rows(self):
        result,calls,saved,_=self.run_scan(ads=[item(str(12345670+i)) for i in range(51)])
        self.assertEqual(1,len(calls));self.assertEqual('RESPONSE_SCHEMA_INVALID',result['status'])
        self.assertEqual(0,result['unique_listings'])
        self.assertEqual(51,len(saved['response-01.json']['items']))

    def test_transport_fixed_allowlist_get_and_http_error_no_retry(self):
        calls=[]
        class Opener:
            def open(self,request,timeout):
                calls.append((request,timeout))
                raise HTTPError(request.full_url,503,'busy',{},io.BytesIO(b'{"error":{"code":"BUSY"},"daily_remaining":47}'))
        transport=base.make_transport('fake-key',Opener())
        payload,status=transport('search',base.SEARCH_URLS[0])
        self.assertEqual(503,status);self.assertEqual('BUSY',payload['error']['code'])
        self.assertEqual('GET',calls[0][0].get_method());self.assertIn('page=1',calls[0][0].full_url)
        self.assertEqual(30,calls[0][1])
        for kind,url in [('search','https://www.avito.ru/all?q=Innosilicon+A10+Pro'),
                         ('item','https://evil.invalid/asic_12345678'),('events','https://www.avito.ru/all')]:
            with self.assertRaises(ValueError):transport(kind,url)
        self.assertEqual(1,len(calls))

if __name__=='__main__':unittest.main()
