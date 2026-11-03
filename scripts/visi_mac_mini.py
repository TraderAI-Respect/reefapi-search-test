"""Separate, bounded VISI API search for Mac mini M1–M5 listings.

Manual API-plan confirmation is mandatory. Five first pages, no detail requests,
retries, resume, schedule, purchases, or changes to the ASIC workflows.
Processor labels are seller claims, not evidence Apple released a configuration.
"""
import csv
import http.client
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('macmini_public_helpers', ROOT/'scripts/visi_trial.py')
visi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(visi)  # pure definitions only; never invoke any ASIC runner
QUERIES = tuple('Mac mini M' + str(n) for n in range(1, 6))
SEARCH_URLS = tuple('https://www.avito.ru/all?' + urlencode({'q': q}) for q in QUERIES)
MAX_CALLS, SPACING = 5, 11
DEVICE = re.compile(r'\b(?:mac[\s-]*mini|мак[\s-]*мини)\b', re.I)
PROCESSOR = re.compile(r'(?<!\w)[mм]\s*([1-5])(?:[\s-]*(pro|max|ultra))?(?!\w)', re.I)
OTHER_DEVICE = re.compile(r'\b(?:macbook|макбук|ipad|айпад|imac|аймак|mac\s*studio|mac\s*pro)\b', re.I)
INTEL = re.compile(r'\b(?:intel|core\s*i[3579]|i[3579][ -]\d{3,5})\b', re.I)
BUYBACK = re.compile(r'\b(?:куплю|выкуп\w*|скуп\w*|покупаем|принимаем)\b', re.I)
SERVICE = re.compile(r'^\s*(?:ремонт\w*|аренд\w*|прокат\w*|настройк\w*|услуги|обучение)\b', re.I)
SERVICE_TITLE = re.compile(r'\b(?:ремонт|аренда|прокат|настройка|услуги|обучение)\b', re.I)
PART = re.compile(r'\b(?:блок\s*питания|корпус\w*|подставк\w*|креплен\w*|креплён\w*|'
                  r'док[ -]?станци\w*|докинг\w*|dock\w*|хаб\w*|hub|кабел\w*|переходник\w*|'
                  r'клавиатур\w*|мыш[ьи]\w*|плат[аыу]\w*|кулер\w*|вентилятор\w*|ssd|'
                  r'накопител\w*|чехол\w*|упаковк\w*|коробк\w*)\b', re.I)
FAULT = re.compile(r'не\s*включается|не\s*работает|неисправ\w*|нерабоч\w*|на\s+запчасти|'
                   r'(?:под|на)\s+ремонт|не\s+рабочи\w*|заблокирован\w*|блокиров\w*|\bmdm\b|\bicloud\b|\bactivation\s*lock\b', re.I)
CPU_SPEC = re.compile(r'^(?:процессор|модель процессора|тип процессора|cpu|chip|processor)$', re.I)


def processors(text):
    return sorted({'M'+m[1] + (' '+m[2].title() if m[2] else '') for m in PROCESSOR.finditer(text or '')})


def public_item(raw):
    row = visi.public_item(raw)
    row['processor_specs'] = [visi.public_text(s.get('value')) for s in raw.get('specs', [])
        if isinstance(s, dict) and CPU_SPEC.fullmatch(str(s.get('name') or ''))
        and isinstance(s.get('value'), str)] if isinstance(raw.get('specs'), list) else []
    return row


def classify(row):
    title = str(row.get('title') or '')
    desc = str(row.get('description_snippet') or '')
    base = {**row, 'processor_claim': None, 'manufacturer_verified': False,
            'availability': 'наличие у продавца не проверено', 'detail_checked': False}
    def result(status, reason, processor=None):
        return {**base, 'classification': status, 'reason': reason, 'processor_claim': processor}
    if row.get('active') is False: return result('excluded', 'inactive')
    if OTHER_DEVICE.search(title): return result('excluded', 'other_apple_device')
    if INTEL.search(title): return result('excluded', 'intel_not_requested')
    service_title = re.sub(r'\b(?:под|на)\s+ремонт\b', '', title, flags=re.I)
    if BUYBACK.search(title) or SERVICE.search(title) or SERVICE_TITLE.search(service_title):
        return result('excluded', 'buyback_or_service')
    device = DEVICE.search(title)
    part = PART.search(title)
    if part and (not device or part.start() < device.start()):
        return result('excluded', 'accessory_or_component')
    head = re.sub(r'^\s*(?:продам|продаю|прода[её]тся)\s+', '', desc, flags=re.I)
    if BUYBACK.match(head) or SERVICE.match(head): return result('excluded', 'buyback_or_service')
    head = re.sub(r'^\s*только\s+(?:пуст\w*\s+)?', '', head, flags=re.I)
    part_head = PART.match(head)
    if part_head or re.search(r'\b(?:компьютера|самого\s+устройства)\s+нет\b', desc, re.I):
        return result('excluded', 'accessory_or_component')
    if not device and not DEVICE.search(desc): return result('excluded', 'not_mac_mini')
    title_cpus = processors(title)
    spec_cpus = processors(' '.join(row.get('processor_specs') or []))
    desc_cpus = processors(desc)
    cpu = title_cpus[0] if len(title_cpus) == 1 else None
    if (len(title_cpus) > 1 or (spec_cpus and title_cpus and spec_cpus != title_cpus)
            or (title_cpus and any(cpu not in title_cpus for cpu in desc_cpus))
            or (title_cpus and INTEL.search(desc))):
        return result('review', 'ambiguous_or_conflicting_processor')
    if not device or not cpu: return result('review', 'model_or_processor_not_explicit_in_title')
    if row.get('description_truncated'): return result('review', 'description_truncated', cpu)
    # A bundle must be explicit; a component named after a model is not a full computer.
    if part and not re.search(r'\+|\bс\s+|в\s+комплекте', title[device.end():part.start()], re.I):
        return result('review', 'accessory_or_bundle_unclear', cpu)
    fault_text = re.sub(r'без\s+(?:mdm|блокиров\w*|ремонт\w*)|не\s+заблокирован\w*|'
                        r'(?:без|нет)\s+неисправностей', '', title+'\n'+desc, flags=re.I)
    if FAULT.search(fault_text): return result('review', 'condition_or_activation_lock_requires_review', cpu)
    return result('candidate', 'seller_claim_not_manufacturer_verified', cpu)


def atomic_save(path, value):
    temporary = path.with_suffix(path.suffix+'.tmp')
    visi.save(temporary, value)
    os.replace(temporary, path)


def refresh(state):
    state['links'], state['matches'], state['review'], state['excluded'] = [], [], [], []
    for record in state['records'].values():
        versions = [classify(row) for row in record['versions']]
        row = dict(versions[0])
        signatures = {(v['classification'], v['processor_claim'], v['reason'],
                       v.get('price_value'), v.get('city')) for v in versions}
        if len(signatures) > 1:
            row.update(classification='review', reason='conflicting_search_snapshots', processor_claim=None)
        row['provenance'] = record['provenance']
        state['links'].append(row)
        state[{'candidate': 'matches', 'review': 'review', 'excluded': 'excluded'}[row['classification']]].append(row)
    state['unique_listings'] = len(state['records'])


def scan(transport, folder, clock=None, approved=False):
    if approved is not True: raise ValueError('Explicit API-plan confirmation required.')
    if (folder/'checkpoint.json').exists(): raise ValueError('Resume is not implemented; do not repeat uncertain calls.')
    clock = clock or time
    stamp = lambda: datetime.fromtimestamp(clock.time(), timezone.utc).isoformat()
    state = {'provider': 'VISI API', 'scope': 'Mac mini M1–M5', 'status': 'STARTING',
        'started_at': stamp(), 'calls_reserved': 0, 'pages_received': 0, 'invalid_rows': 0,
        'daily_remaining_first': None, 'daily_remaining_last': None,
        'max_calls': MAX_CALLS, 'market_exhausted': False, 'requests': [], 'records': {},
        'queries': [{'query': q, 'url': u, 'page': 1, 'status': 'pending', 'has_next': None,
                     'next_page_pending': True} for q,u in zip(QUERIES, SEARCH_URLS)]}
    last_start = None
    current = None
    def persist():
        refresh(state)
        atomic_save(folder/'checkpoint.json', state)
        atomic_save(folder/'report.json', {k:v for k,v in state.items() if k != 'records'})
    try:
        persist()
        for current in state['queries']:
            if state['calls_reserved'] >= MAX_CALLS: raise visi.StopScan('CALL_CAP_REACHED')
            if state['daily_remaining_last'] == 0: raise visi.StopScan('QUOTA_EXHAUSTED')
            if last_start is not None:
                delay = SPACING-(clock.monotonic()-last_start)
                if delay > 0: clock.sleep(delay)
            state['status'] = 'SEARCHING'
            state['calls_reserved'] += 1
            current['status'] = 'in_flight'
            entry = {'number': state['calls_reserved'], 'kind': 'search', 'url': current['url'],
                     'page': 1, 'outcome': 'reserved_unknown', 'started_at': stamp()}
            state['requests'].append(entry)
            persist()  # durable reservation before side effect, including uncertain responses
            last_start = clock.monotonic()
            payload, status = transport('search', current['url'])
            audit = visi.audit_response(payload, status)
            audit.update(request_kind='search', request_url=current['url'])
            atomic_save(folder/('response-%02d.json' % state['calls_reserved']), audit)
            entry.update(outcome='response_received', http_status=audit['http_status'],
                         error=audit['error'], daily_remaining=audit['daily_remaining'])
            persist()
            if (type(status) is not int or not 200 <= status < 300 or not isinstance(payload, dict)
                    or payload.get('error') or payload.get('success') is False or payload.get('ok') is False):
                raise visi.StopScan('HTTP_OR_API_ERROR')
            if (payload.get('source') not in visi.SOURCES or type(payload.get('page')) is not int
                    or payload['page'] != 1 or payload.get('url') != current['url']):
                raise visi.StopScan('RESPONSE_IDENTITY_MISMATCH')
            items = payload.get('items')
            if not isinstance(items, list) or len(items) > 50 or not all(isinstance(x, dict) for x in items):
                raise visi.StopScan('RESPONSE_SCHEMA_INVALID')
            for raw in items:
                row = public_item(raw)
                aid = row['ad_id']
                url = visi.canonical_item_url(row['url'], aid) if aid and re.fullmatch(r'[1-9]\d{4,19}', aid) else None
                if not url or len(url) > 2048:
                    state['invalid_rows'] += 1
                    continue
                row['url'] = url
                record = state['records'].setdefault(aid, {'versions': [], 'provenance': []})
                if row not in record['versions']: record['versions'].append(row)
                if current['query'] not in record['provenance']: record['provenance'].append(current['query'])
            current.update(status='received', listing_count=len(items),
                           has_next=payload.get('has_next') if type(payload.get('has_next')) is bool else None)
            current['next_page_pending'] = current['has_next'] is not False
            state['pages_received'] += 1
            persist()  # valid rows survive an absent or inconsistent quota field
            quota = payload.get('daily_remaining')
            if type(quota) is not int or quota < 0: raise visi.StopScan('QUOTA_INVALID')
            last_quota = state['daily_remaining_last']
            if last_quota is not None and quota != last_quota-1: raise visi.StopScan('QUOTA_CHANGED')
            if state['daily_remaining_first'] is None: state['daily_remaining_first'] = quota
            state['daily_remaining_last'] = quota
            entry['outcome'] = current['status'] = 'success'
            persist()
            print(f"Mac mini: first pages {state['pages_received']}/5; remaining {quota}", flush=True)
        state['status'] = 'FIRST_PAGES_COMPLETE'
    except visi.StopScan as exc:
        state['status'] = str(exc)
    except (OSError, ValueError, TypeError, KeyError, TimeoutError, http.client.HTTPException) as exc:
        state['status'] = 'TRANSPORT_ERROR'
        state['error_type'] = type(exc).__name__  # never store a secret-bearing exception body
    finally:
        if current is not None and current['status'] not in ('success', 'pending'):
            current['status'] = 'blocked'
            current['error'] = state['status']
        if state['requests'] and state['requests'][-1]['outcome'] != 'success':
            entry = state['requests'][-1]
            entry['outcome'] = 'transport_error_unknown' if entry['outcome'] == 'reserved_unknown' else state['status']
        state['finished_at'] = stamp()
        persist()
    return state


def markdown_summary(state):
    lines = ['# VISI API: Mac mini M1–M5', '', '**Статус:** '+state['status'],
        'Это не полный обзор рынка: только первые страницы пяти запросов по России, без открытия карточек.',
        'M1–M5 ниже — заявления продавцов, а не подтверждение выпуска Apple, подлинности или конфигурации.',
        'Цены, исправность, комплектность, блокировки и фактическое наличие требуют проверки у продавца.',
        f"Запросов: {state['calls_reserved']}/5; получено страниц: {state['pages_received']}/5; "
        f"уникальных объявлений: {state.get('unique_listings', 0)}.",
        'Последний подтверждённый остаток API-квоты: '+str(state['daily_remaining_last'])+'.',
        'Без автоповторов, продолжения, расписания и покупок. Пропущено строк с неверным ID/URL: '+str(state['invalid_rows'])+'.', '',
        '## Полученные страницы', '', '| Запрос | Статус | Объявлений | Есть следующая страница |', '|---|---|---|---|']
    for query in state['queries']:
        lines.append('| '+' | '.join(visi.cell(query.get(k)) for k in ('query','status','listing_count','has_next'))+' |')
    labels = {'candidate': 'Кандидат', 'review': 'Проверить', 'excluded': 'Исключено'}
    lines += ['', '## Все ссылки', '', '| Статус | Объявление | Процессор по тексту | Цена | Город | Причина | Ссылка |',
              '|---|---|---|---|---|---|---|']
    for row in state['links']:
        fields = [labels[row['classification']], str(row.get('title') or '')[:160], row['processor_claim'],
                  row['price_text'], str(row.get('city') or '')[:80], row['reason']]
        lines.append('| '+' | '.join(visi.cell(x) for x in fields)+' | [Авито]('+row['url']+') |')
    if not state['links']: lines.append('В полученных ответах нет безопасных ссылок. Это не означает отсутствия предложений.')
    return '\n'.join(lines)+'\n'


def write_outputs(state, folder):
    (folder/'README.md').write_text(markdown_summary(state), encoding='utf-8')
    columns = ['classification','title','processor_claim','price_value','city','reason','url','availability']
    with (folder/'ads.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in state['links']:
            safe = {}
            for key in columns:
                value = visi.redact(str(row.get(key) or ''))
                safe[key] = "'"+value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r', '\n')) else value
            writer.writerow(safe)


def make_transport(key, opener=None):
    opener = opener or urllib.request.build_opener(visi.NoRedirect())
    def transport(kind, url):
        if kind != 'search' or url not in SEARCH_URLS: raise ValueError('Unapproved endpoint or URL')
        request = urllib.request.Request('https://api.visi.su/api/v1/gate/search?'+urlencode({'url':url, 'page':1}),
            method='GET', headers={'X-API-Key':key, 'Accept':'application/json'})
        try: response = opener.open(request, timeout=30)
        except urllib.error.HTTPError as exc: response = exc
        with response:
            status = response.code
            raw = response.read(10*1024*1024+1)
        if len(raw) > 10*1024*1024: return {'error':{'code':'RESPONSE_TOO_LARGE'}}, status
        try: payload = json.loads(raw)
        except (ValueError, UnicodeError): payload = {'error':{'code':'NON_JSON_RESPONSE'}}
        return payload, status
    return transport


def main():
    if os.environ.get('GITHUB_RUN_ATTEMPT') != '1': raise SystemExit('Reruns are forbidden; inspect the previous result.')
    if os.environ.get('VISI_API_PLAN_CONFIRMED') != 'true': raise SystemExit('Confirm an active API plan; Start is not an API plan.')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    if not re.fullmatch(r'\d+', run_id): raise SystemExit('Missing GitHub run ID.')
    key = os.environ.get('VISI_API_KEY', '')
    if not key: raise SystemExit('VISI_API_KEY is not configured.')
    folder = Path('visi-mac-mini-results')/run_id
    folder.mkdir(parents=True, exist_ok=False)
    state = scan(make_transport(key), folder, approved=True)
    write_outputs(state, folder)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(markdown_summary(state))
    return 0 if state['status'] == 'FIRST_PAGES_COMPLETE' else 1


if __name__ == '__main__': sys.exit(main())
