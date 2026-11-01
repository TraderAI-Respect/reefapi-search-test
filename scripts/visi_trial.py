"""Isolated, one-shot free VISI trial; no paid activation, retries or other providers.

Trial confirmed by user 2026-11-01 18:02:10 UTC (23 hours remaining).
Use a conservative fixed 22-hour expiry and a 90-second request buffer.
Official contract: https://api.visi.su/api/v1/docs, inspected 2026-11-01.
"""
import html
import importlib.util
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('visi_pure_filters', ROOT / 'scripts/innosilicon_followup.py')
filters = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(filters)  # definitions only; never invoke its runner or secret-reading helpers
QUERIES = ('Innosilicon A10 Pro', 'Innosilicon A11 Pro')
SEARCH_URLS = ('https://www.avito.ru/all?q=Innosilicon+A10+Pro',
               'https://www.avito.ru/all?q=Innosilicon+A11+Pro')
TRIAL_CONFIRMED_AT = datetime(2026, 11, 1, 18, 2, 10, tzinfo=timezone.utc).timestamp()
TRIAL_EXPIRES_AT = datetime(2026, 11, 2, 16, 2, 10, tzinfo=timezone.utc).timestamp()
MAX_CALLS, MAX_DETAILS, SPACING, EXPIRY_BUFFER = 12, 10, 11, 90
SEED_ID = '2490324333'
SEED_URL = 'https://www.avito.ru/sankt-peterburg/tovary_dlya_kompyutera/korpus_asic_innosilicon_a11_pro_2490324333'
SEED_TITLE = 'Корпус Asic innosilicon a11 pro'
SOURCES = ('avito', 'avito_goods', 'avito_tech', 'avito_auto')
SPEC_NAMES = re.compile(r'^(?:состояние|модель|производитель|бренд|хешрейт|хэшрейт|мощность|потребление|память|алгоритм|количество|тип оборудования)$', re.I)


class StopScan(Exception):
    pass


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def redact(value):
    key = os.environ.get('VISI_API_KEY', '')
    if isinstance(value, str):
        return value.replace(key, '[REDACTED]') if key else value
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


def public_text(value):
    return redact(value)[:12000] if isinstance(value, str) else None


def save(path, value):
    path.write_text(json.dumps(redact(value), ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def canonical_item_url(url, aid=None):
    if not isinstance(url, str) or re.search(r'[\x00-\x20\x7f]', url):
        return None
    try:
        part = urlsplit(url)
        if (part.scheme != 'https' or part.hostname not in ('avito.ru', 'www.avito.ru')
                or part.username or part.password or part.port not in (None, 443)):
            return None
    except ValueError:
        return None
    match = re.search(r'_([1-9]\d{4,19})$', part.path)
    if not match or (aid is not None and str(aid) != match.group(1)):
        return None
    return 'https://www.avito.ru' + quote(part.path, safe='/%:._-~')


def public_item(raw):
    raw = raw if isinstance(raw, dict) else {}
    aid = raw.get('id')
    aid = str(aid) if isinstance(aid, (str, int)) and not isinstance(aid, bool) else None
    description = public_text(raw.get('description'))
    location = raw.get('location') if isinstance(raw.get('location'), dict) else {}
    specs = []
    for spec in raw.get('specs', []) if isinstance(raw.get('specs'), list) else []:
        if not isinstance(spec, dict) or not isinstance(spec.get('name'), str) or not SPEC_NAMES.fullmatch(spec['name']):
            continue
        value = public_text(spec.get('value'))
        if value is not None:
            specs.append({'name': public_text(spec['name']), 'value': value})
    price = raw.get('price') if finite_number(raw.get('price')) else None
    return {'ad_id': aid, 'url': public_text(raw.get('url')), 'title': public_text(raw.get('title')),
            'price_value': price, 'price_text': str(price) + ' ₽' if price is not None else 'не указана',
            'city': public_text(location.get('name')), 'description_snippet': description,
            'description_truncated': isinstance(raw.get('description'), str) and len(redact(raw['description'])) > 12000,
            'specs': specs, 'params_summary': public_text(raw.get('condition')),
            'published': public_text(raw.get('published')),
            'active': raw.get('active') if type(raw.get('active')) is bool else None}


def safe_error(payload):
    error = payload.get('error') if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return {key: public_text(error.get(key)) for key in ('code', 'message')}
    return {'code': public_text(payload.get('code')) if isinstance(payload, dict) else None,
            'message': public_text(error or payload.get('message')) if isinstance(payload, dict) else None}


def audit_response(payload, status):
    data = payload if isinstance(payload, dict) else {}
    quota = data.get('daily_remaining')
    # Preserve malformed scalar quota for diagnosis without serializing NaN/objects.
    safe_quota = public_text(quota) if isinstance(quota, str) else quota if type(quota) in (int, bool) or finite_number(quota) else None
    return {'http_status': status if type(status) is int else None, 'error': safe_error(data),
            'daily_remaining': safe_quota, 'source': public_text(data.get('source')),
            'fetched_at': public_text(data.get('fetched_at')), 'page': data.get('page') if type(data.get('page')) is int else None,
            'items': [public_item(x) for x in data.get('items', []) if isinstance(x, dict)] if isinstance(data.get('items'), list) else [],
            'item': public_item(data['item']) if isinstance(data.get('item'), dict) else None}


def normalized_evidence(row, general):
    text = str(row.get('title') or '') + '\n' + str(row.get('description_snippet') or '')
    return general['normalize_models'](re.sub(r'ин+осил+икон|inn?osil+icon|inosilicon', 'innosilicon', text, flags=re.I))


def classify_one(row, general):
    title, desc = str(row.get('title') or ''), str(row.get('description_snippet') or '')
    def pending(reason):
        return [], [{**row, 'reason': reason}], None
    if row.get('active') is False:
        return [], [], 'inactive'
    evidence = normalized_evidence(row, general)
    if (filters.OTHER_BRAND.search(title) or not filters.PRO_MODEL.search(evidence)
            or not filters.DEVICE_EVIDENCE.search(evidence)):
        return [], [], 'outside_requested_Pro_scope'
    if filters.SCOPED_PARTS.search(title):
        return [], [], 'separate_accessory'
    if row.get('detail_error'):
        return pending('Карточка не проверена: ' + row['detail_error'])
    if row.get('description_truncated'):
        return pending('Описание длиннее безопасного лимита; требуется ручная проверка полного текста')
    classify_row = {**row, 'title': re.sub(r'ин+осил+икон|inn?osil+icon|inosilicon', 'innosilicon', title, flags=re.I)}
    if re.search(r'\bкорпус\b', title, re.I):
        if row.get('detail_checked') and re.search(r'только\s+(?:пустой\s+)?корпус|пустой\s+корпус|без\s+(?:х[еэ]ш.?плат|плат)', desc, re.I):
            return [], [], 'separate_accessory'
        explicit_whole = bool(row.get('detail_checked')
            and not re.search(r'\bне\s+(?:продаю|продам|прода[её]тся|целый|полноценный|комплектный)', desc, re.I)
            and re.search(
            r'(?:^|[.!?]\s*)(?:продаю|продам|прода[её]тся)\s+(?:свой\s+)?(?:целый|полноценный|комплектный)\s+(?:майнер|асик|asic|miner)', desc, re.I)
            and filters.PRO_MODEL.search(general['normalize_models'](desc)))
        if explicit_whole:
            classify_row = {**classify_row, 'title': re.sub(r'\bкорпус\b', '', classify_row['title'], flags=re.I)}
        elif row['ad_id'] == SEED_ID or general['negative_title_reason'](title, desc) == 'ambiguous_case':
            return pending('Указан корпус; продажа целого ASIC не подтверждена полным описанием')
    matches, review = general['classify'](classify_row)
    matches = [m for m in matches if m['model'] in QUERIES]
    if matches:
        return [{**row, **m, 'title': title, 'url': row['url'],
                 'availability': 'объявление активно' if row.get('active') is True and row.get('detail_checked') else 'активность и наличие не подтверждены',
                 'needs_manual_review': bool(re.search(r'неисправ|нерабоч|не рабоч|на запчасти|(?:под|на) ремонт', title+' '+desc, re.I))}
                for m in matches], [], None
    if review:
        return [], [{**row, 'reason': review[0]['reason']}], None
    return [], [], general['negative_title_reason'](title, desc) or 'no_confirmed_requested_model'


def scan(transport, folder, clock=None):
    clock = clock or time
    report = {'provider': 'VISI', 'plan_evidence': 'Пробный; пользователь подтвердил 23 часа остатка',
              'trial_confirmed_at': '2026-11-01T18:02:10Z', 'conservative_expiry': '2026-11-02T16:02:10Z',
              'queries': list(QUERIES), 'search_urls': list(SEARCH_URLS), 'calls_reserved': 0,
              'search_calls': 0, 'detail_calls': 0, 'search_pages_received': 0, 'new_search_unique_count': 0,
              'seed_count': 1, 'daily_remaining_first': None, 'daily_remaining_last': None,
              'requests': [], 'matches': [], 'review': [], 'excluded': [], 'status': 'STARTING',
              'started_at': datetime.fromtimestamp(clock.time(), timezone.utc).isoformat()}
    records = {SEED_ID: {'ad_id': SEED_ID, 'url': SEED_URL, 'title': SEED_TITLE,
                        'description_snippet': '', 'city': 'Санкт-Петербург', 'price_text': 'не проверена',
                        'active': None, 'detail_checked': False, 'provenance': ['prior_run:41316786670']}}
    search_ids = set()
    general = None
    last_start = None
    current_item = None
    def check_trial():
        now = clock.time()
        if not TRIAL_CONFIRMED_AT <= now < TRIAL_EXPIRES_AT-EXPIRY_BUFFER:
            raise StopScan('TRIAL_PROOF_INVALID_OR_EXPIRED')
    def request(kind, url):
        nonlocal last_start
        check_trial()
        if report['calls_reserved'] >= MAX_CALLS:
            raise StopScan('CALL_CAP_REACHED')
        if report['calls_reserved'] and report['daily_remaining_last'] is None:
            raise StopScan('QUOTA_INVALID')
        if report['daily_remaining_last'] is not None and report['daily_remaining_last'] < 1:
            raise StopScan('QUOTA_EXHAUSTED')
        if last_start is not None:
            wait = SPACING-(clock.monotonic()-last_start)
            if wait > 0:
                clock.sleep(wait)
        check_trial()
        last_start = clock.monotonic()
        report['calls_reserved'] += 1
        report['search_calls' if kind == 'search' else 'detail_calls'] += 1
        entry = {'kind': kind, 'url': url, 'page': 1 if kind == 'search' else None, 'outcome': 'reserved'}
        report['requests'].append(entry)
        save(folder / 'report.json', report)
        payload, status = transport(kind, url)
        audit = audit_response(payload, status)
        audit.update(request_kind=kind, request_url=url)
        save(folder / ('response-%02d.json' % report['calls_reserved']), audit)
        entry.update(http_status=audit['http_status'], error=audit['error'], daily_remaining=audit['daily_remaining'], outcome='response_received')
        if (type(status) is not int or not 200 <= status < 300 or not isinstance(payload, dict)
                or payload.get('error') or payload.get('ok') is False or payload.get('success') is False):
            raise StopScan('HTTP_OR_API_ERROR')
        if payload.get('source') not in SOURCES:
            raise StopScan('RESPONSE_IDENTITY_MISMATCH')
        return payload, audit
    def accept_quota(payload):
        remaining = payload.get('daily_remaining')
        if type(remaining) is not int or not 0 <= remaining <= 49:
            raise StopScan('QUOTA_INVALID')
        previous = report['daily_remaining_last']
        if previous is not None and remaining > previous:
            raise StopScan('QUOTA_INCREASED')
        if report['daily_remaining_first'] is None:
            report['daily_remaining_first'] = remaining
        report['daily_remaining_last'] = remaining
        report['requests'][-1]['outcome'] = 'success'
    try:
        save(folder / 'report.json', report)
        check_trial()
        general = filters.load_general()
        for query, url in zip(QUERIES, SEARCH_URLS):
            payload, audit = request('search', url)
            if (type(payload.get('page')) is not int or payload['page'] != 1 or payload.get('url') != url
                    or not isinstance(payload.get('items'), list) or not all(isinstance(x, dict) for x in payload['items'])):
                raise StopScan('RESPONSE_IDENTITY_MISMATCH')
            for row in audit['items']:
                safe = canonical_item_url(row.get('url'), row.get('ad_id'))
                if (not row.get('ad_id') or not str(row['ad_id']).isdigit() or not safe
                        or not isinstance(row.get('title'), str) or not row['title'].strip()):
                    continue
                aid = row['ad_id']
                prior = records.get(aid, {})
                provenance = list(dict.fromkeys(prior.get('provenance', []) + ['search:' + query]))
                records[aid] = {**row, 'url': safe, 'provenance': provenance, 'detail_checked': False}
                search_ids.add(aid)
            report['search_pages_received'] += 1
            report['new_search_unique_count'] = len(search_ids)
            report['requests'][-1]['listing_count'] = len(audit['items'])
            accept_quota(payload)
        queue = []
        # Seed is already first in insertion order and is not a new-search discovery.
        for aid, row in records.items():
            matches, review, _ = classify_one(row, general)
            if matches or review:
                queue.append(aid)
            if len(queue) == MAX_DETAILS:
                break
        report['detail_eligible_count'] = sum(bool(classify_one(row, general)[0] or classify_one(row, general)[1]) for row in records.values())
        for aid in queue:
            current_item = aid
            expected = records[aid]['url']
            payload, audit = request('item', expected)
            raw = payload.get('item')
            detail = audit['item']
            if (not isinstance(raw, dict) or not detail or detail['ad_id'] != aid
                    or canonical_item_url(detail.get('url'), detail.get('ad_id')) != expected
                    or not isinstance(detail.get('title'), str) or not detail['title'].strip()):
                raise StopScan('RESPONSE_IDENTITY_MISMATCH')
            records[aid] = {**detail, 'url': expected, 'detail_checked': True,
                            'provenance': records[aid]['provenance'] + ['VISI_detail'],
                            'fetched_at': audit['fetched_at']}
            current_item = None
            accept_quota(payload)
        report['status'] = 'BOUNDED_SEARCH_COMPLETE'
    except StopScan as exc:
        report['status'] = str(exc)
        if current_item and report['requests'] and report['requests'][-1]['kind'] == 'item':
            records[current_item]['detail_error'] = str(exc)
    except Exception as exc:
        report['status'] = 'STOPPED_ERROR'
        report['error_type'] = type(exc).__name__
        if current_item:
            records[current_item]['detail_error'] = type(exc).__name__
    finally:
        if general:
            for row in records.values():
                matches, review, reason = classify_one(row, general)
                report['matches'].extend(matches)
                report['review'].extend(review)
                if not matches and not review:
                    report['excluded'].append({**row, 'reason': reason})
        if report['requests'] and report['requests'][-1]['outcome'] != 'success':
            report['requests'][-1]['outcome'] = report['status']
        report['finished_at'] = datetime.fromtimestamp(clock.time(), timezone.utc).isoformat()
        save(folder / 'report.json', report)
    return report


def cell(value):
    value = html.escape(redact(str(value if value is not None else '')), quote=False)
    value = re.sub(r'[\r\n\x00-\x1f]+', ' ', value)
    for char in ('\\', '|', '[', ']', '*', '`', '_', '!'):
        value = value.replace(char, '\\' + char)
    return value


def markdown_summary(report):
    lines = ['# VISI — Пробный поиск Innosilicon A10 Pro / A11 Pro', '',
             '**Статус:** ' + report['status'], '',
             'Не полный обзор рынка: только две первые страницы по России и до 10 релевантных карточек.',
             'Без платной активации. Минус-слова применяются локально к результатам.', '',
             'Вызовов зарезервировано: ' + str(report['calls_reserved']) + '/12; поиск: ' + str(report['search_calls']) +
             '; карточки: ' + str(report['detail_calls']) + '/10.',
             'Получено страниц: ' + str(report['search_pages_received']) + '/2; новых уникальных объявлений: ' + str(report['new_search_unique_count']) + '.',
             'Отдельный прежний кандидат: 1 (не включён в число новых, если не встретился в новой выдаче).',
             'Остаток пробника после первого / последнего подтверждённого ответа: ' + cell(report['daily_remaining_first']) + ' / ' + cell(report['daily_remaining_last']) +
             '. Начальный остаток неизвестен; число вызовов не выдаётся за подтверждённое списание.', '',
             '| Запрос | Результат | Объявлений |', '|---|---|---|']
    for entry in report['requests']:
        if entry['kind'] == 'search':
            query = QUERIES[SEARCH_URLS.index(entry['url'])]
            lines.append('| ' + ' | '.join(cell(x) for x in (query, entry['outcome'], entry.get('listing_count', 'неизвестно'))) + ' |')
    lines += ['', '## Кандидаты', '', '| Объявление | Модель | Количество | Город | Цена | Состояние / активность | Ссылка |', '|---|---|---|---|---|---|---|']
    for row in report['matches']:
        lines.append('| ' + ' | '.join(cell(row.get(k)) for k in ('title', 'model', 'quantity', 'city', 'price')) +
                     ' | ' + cell(str(row.get('condition', 'Уточнить')) + '; ' + row['availability']) + ' | [Авито](' + row['url'] + ') |')
    if not report['matches']:
        lines += ['', 'Подходящих кандидатов в полученных ответах не подтверждено; это не означает отсутствие на рынке.']
    lines += ['', 'Цена, работоспособность, комплектность и фактическое наличие требуют проверки у продавца.', '',
              '## Ручная проверка', '', '| Объявление | Город | Причина | Ссылка |', '|---|---|---|---|']
    for row in report['review']:
        lines.append('| ' + ' | '.join(cell(row.get(k)) for k in ('title', 'city', 'reason')) + ' | [Авито](' + row['url'] + ') |')
    lines += ['', '<details><summary>Отсеянные объявления: ' + str(len(report['excluded'])) + '</summary>', '',
              '| Объявление | Город | Причина | Ссылка |', '|---|---|---|---|']
    for row in report['excluded']:
        lines.append('| ' + ' | '.join(cell(row.get(k)) for k in ('title', 'city', 'reason')) + ' | [Авито](' + row['url'] + ') |')
    lines += ['', '</details>', '', '## Ошибки', '']
    errors = [entry for entry in report['requests'] if entry['outcome'] != 'success']
    for entry in errors:
        lines.append('- ' + cell(entry['kind']) + ': ' + cell(entry['outcome']) + '; HTTP ' + cell(entry.get('http_status')) +
                     '; ' + cell((entry.get('error') or {}).get('code')) + ' ' + cell((entry.get('error') or {}).get('message')))
    if not errors:
        lines += ['Ошибок в полученных ответах не зафиксировано.']
    return '\n'.join(lines) + '\n'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_transport(key, opener=None):
    opener = opener or urllib.request.build_opener(NoRedirect())
    def transport(kind, url):
        if kind == 'search' and url in SEARCH_URLS:
            params = {'url': url, 'page': 1}
        elif kind == 'item' and canonical_item_url(url) == url:
            params = {'url': url}
        else:
            raise ValueError('Unapproved endpoint or URL')
        req = urllib.request.Request('https://api.visi.su/api/v1/gate/' + kind + '?' + urlencode(params),
            method='GET', headers={'X-API-Key': key, 'Accept': 'application/json'})
        try:
            response = opener.open(req, timeout=60)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            status = response.code
            raw = response.read(10*1024*1024+1)
        if len(raw) > 10*1024*1024:
            return {'error': {'code': 'RESPONSE_TOO_LARGE'}}, status
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            payload = {'error': {'code': 'NON_JSON_RESPONSE'}}
        return payload, status
    return transport


def main():
    if os.environ.get('GITHUB_RUN_ATTEMPT') != '1':
        raise SystemExit('Reruns are forbidden.')
    if os.environ.get('GITHUB_RUN_NUMBER') != '1':
        raise SystemExit('Only the first dispatch of this isolated trial is authorized.')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    if not re.fullmatch(r'\d+', run_id):
        raise SystemExit('Missing GitHub run ID.')
    key = os.environ.get('VISI_API_KEY', '')
    if not key:
        raise SystemExit('VISI_API_KEY is not configured.')
    folder = Path('visi-trial-results') / run_id
    folder.mkdir(parents=True, exist_ok=False)
    report = scan(make_transport(key), folder)
    summary = markdown_summary(report)
    (folder / 'README.md').write_text(summary, encoding='utf-8')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(summary)
    print('VISI status: ' + report['status'] + '; reserved gateway calls: ' + str(report['calls_reserved']) + '/12')
    return 0 if report['status'] == 'BOUNDED_SEARCH_COMPLETE' else 1


if __name__ == '__main__':
    sys.exit(main())
