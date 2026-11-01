"""One manual search: two Pro models, Russia, at most 8 ReefAPI credits.

Pricing verified 2026-11-01: https://reefapi.com/docs/avito (2/search).
Free balance endpoint: https://reefapi.com/docs. No retries or details.
"""
import json
import math
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

QUERIES = ('Innosilicon A10 Pro', 'Innosilicon A11 Pro')
COST = 2
BUDGET = 8
MAX_REQUESTS = 4
SERVICE = re.compile(r'выкуп|скуп|куплю|покупаем|при[её]м|ремонт|хостинг|размещен|прошивк|настройк', re.I)
PARTS = re.compile(r'х[еэ]ш.?(?:плат|борд)|\bплат[аыуы]|контрол|блок.{0,8}питани|\bбп\b|кабел|шлейф|кулер|вентил|чип|корпус|фильтр|ванна|стойк|контейнер|шумбокс|радиатор|(?<!на )запчаст|\b(?:board|hashboard|controller|psu|cable|fan|chip|case|filter)\b', re.I)
MODEL = re.compile(r'(?<![a-z0-9])a\s*(10|11)\s*pro(?![a-z0-9+])', re.I)


def normalize(text):
    text = str(text or '').lower().replace('ё', 'е')
    text = re.sub(r'ин+осил+икон|inn?osil+icon|inosilicon', 'innosilicon', text)
    text = re.sub(r'(?<!\w)а(?=\s*1[01])', 'a', text)
    text = re.sub(r'(?<!\w)про(?!\w)|(?<=[01])про(?!\w)', 'pro', text)
    return text


def classify(row):
    title = normalize(row.get('title'))
    snippet = normalize(row.get('description_snippet'))
    if SERVICE.search(title) or PARTS.search(title):
        return None
    if re.search(r'avalon|canaan|antminer|whatsminer|bitmain|samsung|xiaomi|антмайнер|ватсмайнер|битмайн|битмейн', title):
        return None
    if not re.search(r'innosilicon|асик|майнер|asic|miner', title + ' ' + snippet):
        return None
    title_models = sorted(set(m.group(1) for m in MODEL.finditer(title)))
    models = title_models
    if not models and ('innosilicon' in title or re.search(r'асик|майнер|asic|miner', title)):
        if not SERVICE.search(snippet[:120]):
            models = sorted(set(m.group(1) for m in MODEL.finditer(snippet)))
    if not models:
        return None
    return {'models': ['Innosilicon A' + n + ' Pro' for n in models],
            'evidence_location': 'title' if title_models else 'snippet',
            'needs_manual_review': not bool(title_models) or bool(re.search(r'на запчасти|неисправ|нерабоч', title))}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def save(path, value):
    # A response is untrusted. Never preserve an accidentally echoed API key.
    text = json.dumps(value, ensure_ascii=False, indent=2)
    key = os.environ.get('REEF_API_KEY', '')
    if key:
        text = text.replace(key, '[REDACTED]')
    path.write_text(text + '\n', encoding='utf-8')


def remaining_balance(payload):
    if not isinstance(payload, dict) or payload.get('ok') is False:
        raise ValueError('Invalid balance response')
    data = payload.get('data', payload)
    remaining = data.get('remaining') if isinstance(data, dict) else None
    if not number(remaining):
        raise ValueError('Unknown balance')
    return remaining


def next_page(data, page):
    nested = data.get('pagination') or {}
    if not isinstance(nested, dict):
        raise ValueError('Invalid pagination')
    more = data.get('has_more', nested.get('has_more'))
    has_next = data.get('has_next', nested.get('has_next'))
    total = data.get('total_pages', nested.get('total_pages'))
    if more is False or has_next is False or (type(total) is int and page >= total):
        return False
    if more is True or has_next is True or (type(total) is int and page < total):
        return True
    raise ValueError('Unknown pagination; cannot claim exhaustion')


def scan(transport, folder):
    report = {'queries': list(QUERIES), 'location': 'all', 'credit_limit': BUDGET,
              'credits_reserved': 0, 'charged_reported': 0, 'requests': [], 'matches': [],
              'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'starting'}
    save(folder / 'report.json', report)
    active = {query: True for query in QUERIES}
    seen = set()
    try:
        balance, _ = transport('GET', '/account/v1/credits', None)
        remaining = remaining_balance(balance)
        report['balance_before'] = remaining
        report['status'] = 'searching'
        # Fixed order gives each requested model its first page before pagination.
        for page in (1, 2):
            for query in QUERIES:
                if not active[query]:
                    continue
                if (report['credits_reserved'] + COST > BUDGET or remaining < COST
                        or len(report['requests']) >= MAX_REQUESTS):
                    report['status'] = 'budget_stop'
                    return report
                body = {'query': query, 'location': 'all', 'page': page, 'sort': 'relevance',
                        'category_detection': False, 'include_sponsored': True,
                        'include_fallback_results': False, 'include_pii': False}
                report['credits_reserved'] += COST
                request = {'query': query, 'page': page, 'outcome': 'pending'}
                report['requests'].append(request)
                save(folder / 'report.json', report)  # reserve before any network side effect
                payload, header_remaining = transport('POST', '/avito/v1/search', body)
                if not isinstance(payload, dict):
                    raise ValueError('Invalid search response')
                # Project the response to public listing fields; no seller PII or raw headers.
                data = payload.get('data')
                if payload.get('ok') is not True or not isinstance(data, dict):
                    request['outcome'] = 'api_error'
                    raise ValueError('API rejected search')
                rows = data.get('listings')
                if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                    raise ValueError('Invalid listings')
                echoed_page = data.get('page')
                if echoed_page is not None and str(echoed_page) != str(page):
                    raise ValueError('Page mismatch')
                public_rows = []
                for row in rows:
                    public = {key: row.get(key) for key in ('ad_id', 'title', 'url', 'price',
                        'price_text', 'price_per_unit', 'price_is_from', 'city', 'published_at',
                        'description_snippet', 'params_summary')}
                    if not public['city'] and isinstance(row.get('location'), dict):
                        public['city'] = row['location'].get('name')
                    public_rows.append(public)
                    match = classify(public)
                    if not match:
                        continue
                    url = urlsplit(str(public.get('url') or ''))
                    if url.scheme not in ('http', 'https') or not (url.hostname == 'avito.ru' or (url.hostname or '').endswith('.avito.ru')):
                        continue
                    public['url'] = urlunsplit((url.scheme, url.netloc, url.path, '', ''))
                    identity = str(public.get('ad_id') or public['url'])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    report['matches'].append({**public, **match,
                        'price_note': 'Цена карточки; цена за один ASIC и остаток требуют подтверждения продавцом.'})
                page_path = folder / ('a' + ('10' if query == QUERIES[0] else '11') + '-page-' + str(page) + '.json')
                save(page_path, {'query': query, 'page': page, 'listings': public_rows})
                meta = payload.get('meta')
                charged = meta.get('charged_credits') if isinstance(meta, dict) else None
                if not number(charged) or charged > COST:
                    request['outcome'] = 'cost_unconfirmed'
                    raise ValueError('Unexpected cost; no further requests')
                report['charged_reported'] += charged
                remaining -= COST  # conservatively reserve even a free/cached/error response
                if number(header_remaining):
                    remaining = min(remaining, header_remaining)
                report['balance_after_last_response'] = header_remaining
                active[query] = next_page(data, page)
                request.update(outcome='success', listing_count=len(rows), has_more=active[query], charged_credits=charged)
                save(page_path,
                     {'query': query, 'page': page, 'has_more': active[query], 'listings': public_rows})
        report['status'] = 'page_cap' if any(active.values()) else 'exhausted'
        return report
    except Exception as exc:
        report['status'] = 'stopped_error'
        report['error_type'] = type(exc).__name__  # no exception text or credentials
        return report
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save(folder / 'report.json', report)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    if os.environ.get('GITHUB_RUN_ATTEMPT') != '1':
        raise SystemExit('Reruns are forbidden; review saved results instead.')
    if os.environ.get('GITHUB_RUN_NUMBER') != '1':
        raise SystemExit('Only the first dispatch is authorized; no further credits may be spent.')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    if not re.fullmatch(r'\d+', run_id):
        raise SystemExit('Missing GitHub run ID.')
    key = os.environ.get('REEF_API_KEY', '')
    if not key:
        raise SystemExit('REEF_API_KEY is not configured.')
    folder = Path('innosilicon-results') / run_id
    folder.mkdir(parents=True, exist_ok=False)
    opener = urllib.request.build_opener(NoRedirect())
    def transport(method, path, body):
        if (method, path) not in (('GET', '/account/v1/credits'), ('POST', '/avito/v1/search')):
            raise ValueError('Endpoint not allowed')
        request = urllib.request.Request('https://api.reefapi.com' + path, method=method,
            data=json.dumps(body).encode('utf-8') if body is not None else None,
            headers={'x-api-key': key, 'Content-Type': 'application/json', 'Accept': 'application/json'})
        with opener.open(request, timeout=90) as response:
            raw = response.read(10 * 1024 * 1024 + 1)
            if len(raw) > 10 * 1024 * 1024:
                raise ValueError('Response too large')
            value = response.headers.get('X-Credits-Remaining')
            remaining = float(value) if value is not None else None
        return json.loads(raw), remaining
    report = scan(transport, folder)
    summary = ('Innosilicon A10 Pro / A11 Pro, Russia. Status: ' + report['status'] +
        '. Search attempts: ' + str(len(report['requests'])) +
        '. Reserved: ' + str(report['credits_reserved']) + '/8 credits.' +
        ' Reported charge: ' + str(report['charged_reported']) +
        '. Candidates: ' + str(len(report['matches'])) + '.\n')
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write(summary)
    return 1 if report['status'] == 'stopped_error' else 0


if __name__ == '__main__':
    sys.exit(main())
