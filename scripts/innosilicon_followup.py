"""One separately authorized search using only the original 8-credit remainder.

First run 41312297721 began at 8 credits and reported 4 charged; its third
request's charge was unknown. Only an unchanged account balance of exactly 4
reconciles enough remainder for BOTH requested first pages. Other balances
stop without paid requests. No top-up, pagination, retry or details calls.
Pricing: https://reefapi.com/docs/avito (2/search); balance GET is free.
"""
import html
import json
import math
import os
import re
import sys
import textwrap
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

QUERIES = ('Innosilicon A10 Pro', 'Innosilicon A11 Pro')
COST = 2
ADDITIONAL_CAP = 4
ORIGINAL_CAP = 8
ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FIELDS = ('ad_id', 'title', 'url', 'price', 'price_text', 'price_per_unit',
                 'price_is_from', 'city', 'published_at', 'description_snippet', 'params_summary')
PRO_MODEL = re.compile(r'(?<![a-z0-9])a\s*(?:10|11)\s*pro(?![a-z0-9+])', re.I)
OTHER_BRAND = re.compile(r'avalon|canaan|antminer|whatsminer|bitmain|samsung|xiaomi|антмайнер|ватсмайнер|битмайн|битмейн', re.I)
# Preserve narrow-parser component exclusions not covered by the general Russian lexicon.
SCOPED_PARTS = re.compile(r'х[еэ]ш.?борд|радиатор|\b(?:board|hashboard|controller|psu|cable|fan|chip|case|filter)\b', re.I)
DEVICE_EVIDENCE = re.compile(r'\b(?:innosilicon|асик\w*|майнер\w*|asics?|miners?)\b', re.I)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def redact(value):
    key = os.environ.get('REEF_API_KEY', '')
    if isinstance(value, str):
        return value.replace(key, '[REDACTED]') if key else value
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {name: redact(item) for name, item in value.items()}
    return value


def scalar(value):
    if isinstance(value, str):
        return redact(value)[:12000]
    if value is None or isinstance(value, bool) or number(value):
        return value
    return None


def save(path, value):
    content = json.dumps(redact(value), ensure_ascii=False, indent=2, allow_nan=False)
    path.write_text(content + '\n', encoding='utf-8')


def load_general():
    """Reuse the general classifier without running the full scanner or report main."""
    path = ROOT / '.github/workflows/asic-full-scan.yml'
    text = path.read_text(encoding='utf-8')
    marker = '      - name: Build matching ASIC table'
    if text.count(marker) != 1:
        raise ValueError('General report block missing or ambiguous')
    block = text.split(marker, 1)[1].split("          python3 - <<'PY'\n", 1)[1].split('\n          PY', 1)[0]
    namespace = {'__name__': 'general_filter_for_followup'}
    exec(compile(textwrap.dedent(block), str(path) + ':report', 'exec'), namespace)
    if not all(callable(namespace.get(name)) for name in ('classify', 'negative_title_reason', 'normalize_models', 'listing_url')):
        raise ValueError('General classifier unavailable')
    return namespace


def public_listing(row):
    public = {key: scalar(row.get(key)) for key in PUBLIC_FIELDS}
    if not public['city'] and isinstance(row.get('location'), dict):
        public['city'] = scalar(row['location'].get('name'))
    return public


def response_audit(payload, header_remaining, http_status):
    payload = payload if isinstance(payload, dict) else {}
    meta = payload.get('meta') if isinstance(payload.get('meta'), dict) else {}
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    raw_error = payload.get('error')
    error = {key: scalar(raw_error.get(key)) for key in ('code', 'message')} if isinstance(raw_error, dict) else {
        'code': scalar(payload.get('code')), 'message': scalar(raw_error or payload.get('message'))}
    return {'ok': payload.get('ok') is True, 'http_status': http_status if type(http_status) is int else None,
            'header_remaining': header_remaining if number(header_remaining) else None,
            'charged_credits': scalar(meta.get('charged_credits')), 'error': error,
            'page': scalar(data.get('page')),
            'listings': [public_listing(row) for row in data.get('listings', []) if isinstance(row, dict)]
                        if isinstance(data.get('listings'), list) else []}


def account_balance(transport, folder, name):
    payload, header, status = transport('GET', '/account/v1/credits', None)
    audit = response_audit(payload, header, status)
    data = payload.get('data', payload) if isinstance(payload, dict) else {}
    audit['account'] = {key: scalar(data.get(key)) for key in ('remaining', 'used', 'granted')} if isinstance(data, dict) else {}
    save(folder / ('balance-' + name + '.json'), audit)
    remaining = data.get('remaining') if isinstance(data, dict) else None
    if (not isinstance(payload, dict) or payload.get('ok') is False or not number(remaining)
            or type(status) is not int or not 200 <= status < 300):
        raise ValueError('Unknown account balance')
    if number(header) and header != remaining:
        raise ValueError('Contradictory account balance')
    return remaining


def safe_url(row, general):
    value = general['listing_url'](row)
    if not value:
        return None
    part = urlsplit(value)
    try:
        if part.username or part.password or part.port not in (None, 443):
            return None
    except ValueError:
        return None
    if re.search(r'[\x00-\x20\x7f]', value):
        return None
    return part._replace(path=quote(part.path, safe='/%:._-~'), query='', fragment='').geturl()


def collect_rows(public_rows, general, report, seen):
    for row in public_rows:
        url = safe_url(row, general)
        if not url:
            continue
        row = {**row, 'url': url}
        identity = str(row.get('ad_id'))
        title = str(row.get('title') or '')
        desc = str(row.get('description_snippet') or '')
        evidence = re.sub(r'ин+осил+икон|inn?osil+icon|inosilicon', 'innosilicon', title + '\n' + desc, flags=re.I)
        normalized = general['normalize_models'](evidence)
        reason = general['negative_title_reason'](title, desc)
        if OTHER_BRAND.search(title) or not PRO_MODEL.search(normalized) or not DEVICE_EVIDENCE.search(normalized):
            matches, review = [], []
            reason = 'outside_requested_Pro_scope'
        elif SCOPED_PARTS.search(title):
            matches, review = [], []
            reason = 'separate_accessory'
        else:
            matches, review = general['classify'](row)
            matches = [item for item in matches if item['model'] in QUERIES]
        for match in matches:
            key = (identity, match['model'])
            if key in seen['matches']:
                continue
            seen['matches'].add(key)
            report['matches'].append({**row, **match, 'url': url,
                'needs_manual_review': bool(re.search(r'неисправ|нерабоч|не рабоч|на запчасти|(?:под|на) ремонт', title + ' ' + desc, re.I)),
                'price_note': 'Цена и наличие заявлены в карточке; подтвердить у продавца.'})
        for pending in review:
            if identity not in seen['review']:
                seen['review'].add(identity)
                report['review'].append({**pending, 'url': url})
        if not matches and not review and identity not in seen['excluded']:
            seen['excluded'].add(identity)
            report['excluded'].append({key: row.get(key) for key in ('ad_id', 'title', 'url', 'city', 'price_text')})
            report['excluded'][-1]['reason'] = reason or 'no_confirmed_requested_model'
    confirmed = {row['ad_id'] for row in report['matches']}
    report['review'] = [row for row in report['review'] if row['ad_id'] not in confirmed]
    review_ids = {row['ad_id'] for row in report['review']}
    report['excluded'] = [row for row in report['excluded'] if str(row['ad_id']) not in confirmed | review_ids]


def scan(transport, folder):
    report = {'queries': list(QUERIES), 'location': 'all', 'original_credit_cap': ORIGINAL_CAP,
              'prior_run_id': '41312297721', 'prior_confirmed_charge': 4,
              'additional_credit_cap': ADDITIONAL_CAP, 'credits_reserved': 0,
              'charged_reported': 0, 'requests': [], 'matches': [], 'review': [], 'excluded': [],
              'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'STARTING',
              'coverage': 'Two first pages only; not a full market search.'}
    save(folder / 'report.json', report)
    seen = {'matches': set(), 'review': set(), 'excluded': set()}
    try:
        general = load_general()  # fail before any account/API call if shared filter is unavailable
        remaining = account_balance(transport, folder, 'before')
        report['balance_before'] = remaining
        if remaining < 2:
            report['status'] = 'INSUFFICIENT_CREDITS'
            return report
        if remaining < ADDITIONAL_CAP:
            report['status'] = 'NEEDS_MODEL_SELECTION'
            return report
        if remaining != ADDITIONAL_CAP:
            report['status'] = 'UNRECONCILED_BALANCE'
            return report
        for query in QUERIES:
            if remaining < COST or report['credits_reserved'] + COST > ADDITIONAL_CAP:
                report['status'] = 'BALANCE_CHANGED_STOP'
                return report
            body = {'query': query, 'location': 'all', 'page': 1, 'sort': 'relevance',
                    'category_detection': False, 'include_sponsored': True,
                    'include_fallback_results': False, 'include_pii': False}
            request = {'query': query, 'page': 1, 'outcome': 'pending'}
            report['credits_reserved'] += COST
            report['requests'].append(request)
            save(folder / 'report.json', report)  # reserve before the irreversible paid request
            payload, header, status = transport('POST', '/avito/v1/search', body)
            audit = response_audit(payload, header, status)
            audit.update(query=query, requested_page=1)
            name = 'a10-page-1.json' if query == QUERIES[0] else 'a11-page-1.json'
            save(folder / name, audit)  # save error/charge/header and public results BEFORE validation
            request.update({key: audit[key] for key in ('http_status', 'header_remaining', 'charged_credits', 'error')})
            request['outcome'] = 'response_received'
            charge = audit['charged_credits']
            if number(charge):
                report['charged_reported'] += charge
            valid_payload = (isinstance(payload, dict) and payload.get('ok') is True
                             and type(status) is int and 200 <= status < 300)
            data = payload.get('data') if isinstance(payload, dict) else None
            if not valid_payload or not isinstance(data, dict):
                request['outcome'] = 'api_error'
                raise ValueError('API rejected search')
            rows = data.get('listings')
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError('Invalid listings')
            if data.get('page') is not None and str(data['page']) != '1':
                raise ValueError('Page mismatch')
            collect_rows(audit['listings'], general, report, seen)
            request['listing_count'] = len(rows)
            if not number(charge) or charge > COST:
                request['outcome'] = 'cost_unconfirmed'
                raise ValueError('Unexpected cost')
            remaining -= COST  # never reuse a free/cached response reservation
            if number(header):
                remaining = min(remaining, header)
            request['outcome'] = 'success'
            save(folder / 'report.json', report)
        report['status'] = 'FIRST_PAGES_COMPLETE'
        return report
    except Exception as exc:
        report['status'] = 'STOPPED_ERROR'
        report['error_type'] = type(exc).__name__  # no exception text, request headers or credentials
        if report['requests'] and report['requests'][-1]['outcome'] == 'pending':
            report['requests'][-1]['outcome'] = 'transport_error'
        return report
    finally:
        if report['requests']:
            try:
                final = account_balance(transport, folder, 'final')
                report['balance_final'] = final
                report['account_balance_delta'] = report['balance_before'] - final
                report['balance_delta_note'] = 'Account-wide change; reported per-call charges remain separate.'
            except Exception as exc:
                report['final_balance_error_type'] = type(exc).__name__
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save(folder / 'report.json', report)


def cell(value):
    value = html.escape(redact(str(value if value is not None else '')), quote=False)
    value = re.sub(r'[\r\n\x00-\x1f]+', ' ', value)
    for char in ('\\', '|', '[', ']', '*', '`', '_', '!'):
        value = value.replace(char, '\\' + char)
    return value


def markdown_summary(report):
    lines = ['# Innosilicon A10 Pro / A11 Pro — остаток прежних 8 кредитов', '',
             '**Статус:** ' + report['status'], '',
             'Не полный обзор рынка: только первые страницы двух заданных запросов по России.',
             'Минус-слова применены к результатам локально, не к платному запросу.', '',
             'Попыток поиска: ' + str(len(report['requests'])) +
             '; резерв: ' + str(report['credits_reserved']) + '/4 дополнительных кредитов' +
             '; списание по ответам: ' + str(report['charged_reported']) + '.',
             'Баланс до: ' + cell(report.get('balance_before', 'неизвестен')) +
             '; итоговый баланс: ' + cell(report.get('balance_final', 'не проверялся / недоступен')) + '.', '',
             '## Кандидаты', '', '| Объявление | Модель | Количество | Город | Цена | Состояние | Ссылка |', '|---|---|---|---|---|---|---|']
    for row in report['matches']:
        lines.append('| ' + ' | '.join(cell(row.get(key)) for key in ('title', 'model', 'quantity', 'city', 'price', 'condition')) +
                     ' | [Авито](' + row['url'] + ') |')
    if not report['matches']:
        lines += ['', 'Подтверждённых автоматическим фильтром кандидатов в этих ответах нет.']
    lines += ['', 'Цена, комплектность, состояние и остаток требуют проверки у продавца.', '',
              '## Ручная проверка', '', '| Объявление | Город | Причина | Ссылка |', '|---|---|---|---|']
    for row in report['review']:
        lines.append('| ' + ' | '.join(cell(row.get(key)) for key in ('title', 'city', 'reason')) + ' | [Авито](' + row['url'] + ') |')
    lines += ['', '<details><summary>Отсеянные объявления: ' + str(len(report['excluded'])) + '</summary>', '',
              '| Объявление | Город | Цена карточки | Причина | Ссылка |', '|---|---|---|---|---|']
    for row in report['excluded']:
        lines.append('| ' + ' | '.join(cell(row.get(key)) for key in ('title', 'city', 'price_text', 'reason')) + ' | [Авито](' + row['url'] + ') |')
    lines += ['', '</details>', '']
    return '\n'.join(lines)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_transport(key, opener=None):
    opener = opener or urllib.request.build_opener(NoRedirect())
    def transport(method, path, body):
        if (method, path) not in (('GET', '/account/v1/credits'), ('POST', '/avito/v1/search')):
            raise ValueError('Endpoint not allowed')
        request = urllib.request.Request('https://api.reefapi.com' + path, method=method,
            data=json.dumps(body).encode('utf-8') if body is not None else None,
            headers={'x-api-key': key, 'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            response = opener.open(request, timeout=90)
        except urllib.error.HTTPError as exc:
            response = exc  # HTTP failures have useful JSON/charge/header data; never retry.
        with response:
            status = response.code
            value = response.headers.get('X-Credits-Remaining')
            try:
                remaining = float(value) if value is not None else None
            except (ValueError, TypeError):
                remaining = None
            raw = response.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            return {'ok': False, 'error': {'code': 'RESPONSE_TOO_LARGE'}}, remaining, status
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            payload = {'ok': False, 'error': {'code': 'NON_JSON_RESPONSE'}}
        return payload, remaining, status
    return transport


def main():
    if os.environ.get('GITHUB_RUN_ATTEMPT') != '1':
        raise SystemExit('Reruns are forbidden.')
    if os.environ.get('GITHUB_RUN_NUMBER') != '1':
        raise SystemExit('Only the first dispatch of this separate follow-up is authorized.')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    if not re.fullmatch(r'\d+', run_id):
        raise SystemExit('Missing GitHub run ID.')
    key = os.environ.get('REEF_API_KEY', '')
    if not key:
        raise SystemExit('REEF_API_KEY is not configured.')
    folder = Path('innosilicon-followup-results') / run_id
    folder.mkdir(parents=True, exist_ok=False)
    report = scan(make_transport(key), folder)
    summary = markdown_summary(report).replace(key, '[REDACTED]')
    (folder / 'README.md').write_text(summary, encoding='utf-8')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(summary)
    print('Status: ' + report['status'] + '; paid attempts: ' + str(len(report['requests'])) +
          '; reserved: ' + str(report['credits_reserved']) + '/4; reported charge: ' + str(report['charged_reported']))
    return 1 if report['status'] == 'STOPPED_ERROR' else 0


if __name__ == '__main__':
    sys.exit(main())
