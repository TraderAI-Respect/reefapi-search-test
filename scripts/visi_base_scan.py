"""One-shot VISI scan: 44 approved base first pages, then at most 3 miner details.

All 105 national phrases remain in a separate checkpoint. No resume is implemented
or authorized. No older workflow/checkpoint is executed, changed or resumed.
"""
import ast
import hashlib
import importlib.util
import json
import os
import re
import sys
import textwrap
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT=Path(__file__).resolve().parents[1]
_spec=importlib.util.spec_from_file_location('visi_base_helpers',ROOT/'scripts/visi_trial.py')
visi=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(visi)  # definitions only; never call either older runner
QUEUE_HASH='280164bea3d353f5c927e8c7dc5d475df976a071da2a0d4500cdce0079af11c2'
MAX_SEARCHES,MAX_DETAILS,MAX_CALLS,INITIAL_QUOTA=44,3,47,47
MAX_URL=2048
SUMMARY_LIMIT=900*1024
EXCLUDED_FAMILY=re.compile(r'^Innosilicon A1[01](?: Pro)?$',re.I)


def load_queries(path=None):
    text=(path or ROOT/'.github/workflows/asic-full-scan.yml').read_text(encoding='utf-8')
    block=text.split("          cat > /tmp/asic_scan.py <<'PY'\n",1)[1].split('\n          PY',1)[0]
    tree=ast.parse(textwrap.dedent(block))
    assignments=[n for n in tree.body if isinstance(n,ast.Assign) and
                 any(isinstance(t,ast.Name) and t.id=='QUERIES' for t in n.targets)]
    if len(assignments)!=1:
        raise ValueError('Ambiguous national query literal')
    queries=ast.literal_eval(assignments[0].value)  # never exec the scanner or CITY_TASKS
    encoded=json.dumps(queries,ensure_ascii=False,separators=(',',':')).encode()
    if len(queries)!=105 or hashlib.sha256(encoded).hexdigest()!=QUEUE_HASH:
        raise ValueError('Approved national query set changed')
    return tuple(queries)


FULL_QUERIES=load_queries()
SELECTED_INDICES=tuple(i for i,q in enumerate(FULL_QUERIES[:45]) if q!='Innosilicon A10 Pro')
SEARCH_URLS=tuple('https://www.avito.ru/all?'+urlencode({'q':FULL_QUERIES[i]}) for i in SELECTED_INDICES)
assert len(SELECTED_INDICES)==MAX_SEARCHES


def atomic_save(path,value):
    temp=path.with_suffix(path.suffix+'.tmp')
    visi.save(temp,value)
    os.replace(temp,path)


def safe_url(url,aid=None):
    value=visi.canonical_item_url(url,aid)
    return value if value and len(value)<=MAX_URL else None


def allowed_model(model):
    return not EXCLUDED_FAMILY.fullmatch(model['model'])


def quantity_evidence(row,general):
    """Ranking hint only; never used to manufacture a confirmed stock quantity."""
    values=[]
    for line in str(row.get('description_snippet') or '').splitlines():
        bundle=general['BUNDLE_CONTEXT'].search(line)
        if bundle:line=line[:bundle.start()]
        if general['PARTS'].search(line) or general['ACCESSORY'].search(re.sub(r'на\s+запчасти','',line,flags=re.I)):
            continue
        if re.search(r'продан|продал|продано|минимальн|мин\s+заказ|заказ\s+от|вмещ|мест\b|от\s*\d',line,re.I):
            continue
        for match in general['QTY'].finditer(line):
            if match['hi'] and int(match['hi'])<int(match['lo']):continue
            values.append(int(match['lo']))
    return max(values,default=0)


def classify_row(row,general,known_names):
    title=str(row.get('title') or '')
    desc=str(row.get('description_snippet') or '')
    title=re.sub(r'ин+осил+икон|inn?osil+icon|inosilicon','innosilicon',title,flags=re.I)
    normal=general['normalize_models'](title+'\n'+desc)
    title_models=general['identify'](title)
    models=general['identify'](normal)
    supported=[m for m in models if allowed_model(m) and (m['valid'] or m['model'] in known_names)]
    if row.get('active') is False:return [],[],'inactive',False,0
    if title_models and not supported and not any(allowed_model(m) for m in title_models):
        return [],[],'A10_A11_excluded_this_run',False,0
    negative=general['negative_title_reason'](title,desc)
    if negative in ('buyback_or_service','separate_accessory') or visi.filters.SCOPED_PARTS.search(title):
        return [],[],negative or 'separate_accessory',False,0
    if re.search(r'выкупаем|выкуплю|скупаем|куплю|утилизац|принимаем ваши',desc,re.I):
        return [],[],'buyback_or_service',False,0
    head=re.sub(r'^\s*(?:продам|продаю|продается|продаётся)\s+','',desc,flags=re.I)
    part=general['PARTS'].search(head)
    if part and part.start()==0:return [],[],'separate_accessory',False,0
    if re.match(r'\s*(?:ремонтируем|оказываем\s+услуги|принимаем\s+на\s+ремонт|обучение|хостинг)',desc,re.I):
        return [],[],'buyback_or_service',False,0
    device=general['BRANDED'].search(normal) or re.search(r'\b(?:асик\w*|майнер\w*|asics?|miners?)\b',normal,re.I)
    if not device or not supported:return [],[],'outside_supported_models',False,0
    if row.get('detail_error'):
        return [],[{**row,'reason':'Карточка не проверена: '+row['detail_error']}],None,False,0
    matches,pending=general['classify']({**row,'title':title})
    matches=[m for m in matches if not EXCLUDED_FAMILY.fullmatch(m['model'])]
    # An excluded-model title needs an independently confirmed other-model offer, not a comparison token.
    if title_models and not any(allowed_model(m) for m in title_models) and not matches:
        return [],[],'A10_A11_excluded_this_run',False,0
    qty=max((m['quantity_min'] for m in matches),default=quantity_evidence(row,general))
    if row.get('description_truncated'):
        return [],[{**row,'reason':'Сохранённое описание обрезано; требуется полная проверка'}],None,negative!='ambiguous_case',qty
    if matches:
        return [{**row,**m,'title':row['title'],'url':row['url'],
                 'availability':'объявление активно' if row.get('detail_checked') and row.get('active') is True else 'активность и наличие не подтверждены'}
                for m in matches],[],None,True,qty
    if pending:
        minimum=min(m['minimum'] for m in supported)
        eligible=negative!='ambiguous_case' and (not qty or qty>=minimum)
        return [],[{**row,'reason':pending[0]['reason']}],None,eligible,qty
    return [],[],'no_confirmed_supported_model',False,0


def classify_record(record,general,known_names):
    versions=[record['detail']] if record.get('detail') is not None else record['search_versions']
    matches={};review=[];reason='no_confirmed_supported_model';eligible=False;quantity=0
    for version in versions:
        row={**version,'provenance':record['provenance'],'detail_checked':record.get('detail') is not None}
        if record.get('detail_error'):row['detail_error']=record['detail_error']
        found,pending,excluded,can_detail,qty=classify_row(row,general,known_names)
        for match in found:matches.setdefault(match['model'],match)
        if not review:review=pending
        reason=excluded or reason
        eligible=eligible or can_detail
        quantity=max(quantity,qty)
    if matches:review=[]
    return list(matches.values()),review,reason,eligible,quantity


def refresh(state,general,known_names):
    state['matches'],state['review'],state['excluded'],state['links']=[],[],[],[]
    for aid,record in state['records'].items():
        found,pending,reason,eligible,quantity=classify_record(record,general,known_names)
        state['matches'].extend(found);state['review'].extend(pending)
        public=record.get('detail') or record['search_versions'][0]
        status='candidate' if found else 'review' if pending else 'excluded'
        if status=='excluded':state['excluded'].append({**public,'reason':reason,'provenance':record['provenance']})
        state['links'].append({**public,'classification':status,
            'reason':'; '.join(m['model'] for m in found) if found else pending[0]['reason'] if pending else reason,
            'quantity':'; '.join(m['quantity'] for m in found) if found else 'не подтверждено',
            'provenance':record['provenance'],'detail_checked':record.get('detail') is not None})
    state['unique_listings']=len(state['records'])


def scan(transport,folder,clock=None):
    if (folder/'checkpoint.json').exists():raise ValueError('Automatic or manual resume is not authorized')
    clock=clock or time
    state={'provider':'VISI','status':'STARTING','calls_reserved':0,'search_calls':0,'detail_calls':0,
        'quota_confirmed_before_run':INITIAL_QUOTA,'quota_last_confirmed':None,'ledger':[],
        'records':{},'detail_queue':[],'matches':[],'review':[],'excluded':[],'links':[],'unique_listings':0,
        'resume_authorized':False,'resume_implemented':False,'city_tasks_not_in_this_run':182,
        'trial_confirmed_at':'2026-11-01T18:02:10Z','conservative_expiry':'2026-11-02T16:02:10Z',
        'started_at':datetime.fromtimestamp(clock.time(),timezone.utc).isoformat(),
        'full_query_queue':[{'index':i,'query':q,'base_model':i<45,'selected_this_run':i in SELECTED_INDICES,
            'status':'pending' if i in SELECTED_INDICES else 'deferred','page':1,'has_next':None,
            'next_page_pending':False,'deferred_reason':None if i in SELECTED_INDICES else
            'A10_A11_excluded_this_run' if re.search(r'Innosilicon A1[01](?: Pro)?$',q) else 'not_authorized_this_run'}
            for i,q in enumerate(FULL_QUERIES)]}
    general=visi.filters.load_general()
    known_names={m['model'] for q in FULL_QUERIES[:45] for m in general['identify'](q) if allowed_model(m)}
    last_start=None;current_query=None;current_detail=None
    def persist():
        atomic_save(folder/'checkpoint.json',state)
        atomic_save(folder/'report.json',{k:v for k,v in state.items() if k not in ('records','full_query_queue')})
    def trial_check():
        if not visi.TRIAL_CONFIRMED_AT<=clock.time()<visi.TRIAL_EXPIRES_AT-90:
            raise visi.StopScan('TRIAL_PROOF_INVALID_OR_EXPIRED')
    def request(kind,url):
        nonlocal last_start
        trial_check()
        if state['calls_reserved']>=MAX_CALLS:raise visi.StopScan('CALL_CAP_REACHED')
        if state['calls_reserved'] and state['quota_last_confirmed']!=INITIAL_QUOTA-state['calls_reserved']:
            raise visi.StopScan('QUOTA_MISMATCH')
        if kind=='item' and (state['search_calls']!=MAX_SEARCHES or state['detail_calls']>=MAX_DETAILS):
            raise visi.StopScan('DETAIL_SCOPE_GUARD')
        if last_start is not None:
            delay=11-(clock.monotonic()-last_start)
            if delay>0:clock.sleep(delay)
        trial_check()
        state['calls_reserved']+=1;state['search_calls' if kind=='search' else 'detail_calls']+=1
        entry={'number':state['calls_reserved'],'kind':kind,'url':url,'page':1 if kind=='search' else None,
            'query_index':current_query,'ad_id':current_detail,'outcome':'reserved_unknown',
            'expected_remaining':INITIAL_QUOTA-state['calls_reserved'],'observed_remaining':None,
            'started_at':datetime.fromtimestamp(clock.time(),timezone.utc).isoformat()}
        state['ledger'].append(entry)
        if current_query is not None:state['full_query_queue'][current_query]['status']='in_flight'
        persist()  # atomic durable reservation before the gateway side effect
        try:
            trial_check()  # slow persistence must not carry a request past trial expiry
        except visi.StopScan:
            entry['outcome']='not_sent_trial_expired'
            raise
        last_start=clock.monotonic()  # spacing is measured at the actual transport boundary
        payload,http_status=transport(kind,url)
        audit=visi.audit_response(payload,http_status)
        audit.update(request_kind=kind,request_url=url)
        audit['has_next']=payload.get('has_next') if isinstance(payload,dict) and type(payload.get('has_next')) is bool else None
        atomic_save(folder/('response-%02d.json'%state['calls_reserved']),audit)
        entry.update(outcome='response_received',http_status=audit['http_status'],error=audit['error'],
                     observed_remaining=audit['daily_remaining'])
        persist()  # retain HTTP/error quota even when validation below fails
        if (type(http_status) is not int or not 200<=http_status<300 or not isinstance(payload,dict)
                or payload.get('error') or payload.get('ok') is False or payload.get('success') is False):
            raise visi.StopScan('HTTP_OR_API_ERROR')
        if payload.get('source') not in visi.SOURCES:raise visi.StopScan('RESPONSE_IDENTITY_MISMATCH')
        return payload,audit
    def quota(payload):
        value=payload.get('daily_remaining')
        if type(value) is not int or value!=INITIAL_QUOTA-state['calls_reserved']:
            raise visi.StopScan('QUOTA_MISMATCH')
        state['quota_last_confirmed']=value;state['ledger'][-1]['outcome']='success'
        print(f"VISI: search {state['search_calls']}/44; details {state['detail_calls']}/3; remaining {value}",flush=True)
    try:
        persist();trial_check()
        state['status']='SEARCHING'
        for index,url in zip(SELECTED_INDICES,SEARCH_URLS):
            current_query=index
            payload,audit=request('search',url)
            raw=payload.get('items')
            if not isinstance(raw,list) or len(raw)>50 or not all(isinstance(x,dict) for x in raw):
                raise visi.StopScan('RESPONSE_SCHEMA_INVALID')
            if type(payload.get('page')) is not int or payload['page']!=1 or payload.get('url')!=url:
                raise visi.StopScan('RESPONSE_IDENTITY_MISMATCH')
            entry=state['full_query_queue'][index]
            entry['has_next']=audit['has_next'];entry['next_page_pending']=audit['has_next'] is not False
            entry['listing_count']=len(raw)
            for row in audit['items']:
                aid=row.get('ad_id');canonical=safe_url(row.get('url'),aid)
                if not aid or not aid.isdigit() or not canonical or not row.get('title'):continue
                row={**row,'url':canonical}
                record=state['records'].setdefault(aid,{'ad_id':aid,'url':canonical,'search_versions':[],
                                                       'provenance':[],'detail':None})
                if canonical!=record['url']:continue  # conflicting URL is not a new allowed detail target
                if row not in record['search_versions']:record['search_versions'].append(row)
                query=FULL_QUERIES[index]
                if query not in record['provenance']:record['provenance'].append(query)
            refresh(state,general,known_names);persist()  # public partial results survive quota failure
            quota(payload);entry['status']='first_page_complete';persist()
            current_query=None
        ranked=[]
        for aid,record in state['records'].items():
            found,pending,reason,eligible,qty=classify_record(record,general,known_names)
            if eligible:
                lot=any(re.search(r'партия|оптом',str(r.get('title'))+' '+str(r.get('description_snippet')),re.I) for r in record['search_versions'])
                ranked.append((-qty,-int(bool(lot)),int(aid),aid))
        ranked.sort()
        state['detail_queue']=[{'ad_id':r[-1],'status':'pending','quantity_priority':-r[0]} for r in ranked[:MAX_DETAILS]]
        state['detail_eligible_count']=len(ranked);state['status']='FETCHING_DETAILS';persist()
        for planned in state['detail_queue']:
            aid=planned['ad_id'];current_detail=aid;expected=state['records'][aid]['url']
            planned['status']='in_flight'
            payload,audit=request('item',expected)
            detail=audit['item']
            if (not isinstance(payload.get('item'),dict) or not detail or detail.get('ad_id')!=aid
                    or safe_url(detail.get('url'),aid)!=expected or not detail.get('title')):
                raise visi.StopScan('RESPONSE_IDENTITY_MISMATCH')
            state['records'][aid]['detail']={**detail,'url':expected,'fetched_at':audit['fetched_at']}
            refresh(state,general,known_names);persist()
            quota(payload);planned['status']='complete';current_detail=None;persist()
        state['status']='BOUNDED_BASE_COMPLETE'
    except visi.StopScan as exc:
        state['status']=str(exc)
    except Exception as exc:
        state['status']='STOPPED_ERROR';state['error_type']=type(exc).__name__
    finally:
        if current_query is not None:
            state['full_query_queue'][current_query]['status']='blocked'
            state['full_query_queue'][current_query]['error']=state['status']
        if current_detail:
            if state['records'][current_detail].get('detail') is None:
                state['records'][current_detail]['detail_error']=state['status']
            for planned in state['detail_queue']:
                if planned['ad_id']==current_detail:planned['status']='blocked'
        if state['ledger'] and state['ledger'][-1]['outcome'] not in ('success','not_sent_trial_expired'):
            entry=state['ledger'][-1]
            entry['outcome']='transport_error_unknown' if entry['outcome']=='reserved_unknown' else state['status']
        refresh(state,general,known_names)
        state['finished_at']=datetime.fromtimestamp(clock.time(),timezone.utc).isoformat();persist()
    return state


def compact(value,limit):
    return visi.cell(str(value if value is not None else '')[:limit])


def summary_header(state):
    return '\n'.join(['# VISI: базовые ASIC, кроме A10/A11 в этом запуске','',
        '**Статус:** '+state.get('status','UNKNOWN'),
        'Не полный обзор рынка: 44 первые страницы; затем до 3 релевантных карточек. Без платной активации.',
        'Подтверждённо получено '+str(sum(e.get('kind')=='search' and e.get('outcome')=='success' for e in state.get('ledger',[])))+'/44 первых страниц.',
        'Все 105 национальных фраз сохранены в отдельной очереди. Остальные задания и следующие страницы не запускаются без нового разрешения.',
        'Вызовы: '+str(state.get('calls_reserved',0))+'/47; поиск '+str(state.get('search_calls',0))+'/44; карточки '+str(state.get('detail_calls',0))+'/3.',
        'Квота до запуска: 47; последний подтверждённый остаток: '+str(state.get('quota_last_confirmed','неизвестен'))+'.',
        'Уникальных объявлений: '+str(state.get('unique_listings',len(state.get('links',[]))))+
        '; подходящих модельных позиций: '+str(len(state.get('matches',[])))+'; ручная проверка: '+str(len(state.get('review',[])))+'.',
        'К = кандидат; П = ручная проверка; И = исключено. Цены и наличие заявлены продавцом, не подтверждены контактом.',
        'Текст ячеек сокращён; все ссылки ниже открыты и не свёрнуты. Полные тексты, очередь и журнал — в артефакте.',''])


TABLE_HEADER='| Статус | Объявление | Город | Цена карточки | Количество | Причина / модель | Ссылка |\n|---|---|---|---|---|---|---|\n'

def summary_rows(state):
    order={'candidate':0,'review':1,'excluded':2};labels={'candidate':'К','review':'П','excluded':'И'}
    for row in sorted(state.get('links',[]),key=lambda r:(order.get(r.get('classification'),3),str(r.get('ad_id')))):
        yield '| '+labels.get(row.get('classification'),'?')+' | '+ ' | '.join(compact(row.get(k),n) for k,n in
            [('title',48),('city',20),('price_text',16),('quantity',24),('reason',48)])+' | [Авито]('+row['url']+') |\n'


def markdown_summary(state):
    return summary_header(state)+'\n'+TABLE_HEADER+''.join(summary_rows(state))


def summary_parts(state):
    parts=[];current=summary_header(state)+'\n'+TABLE_HEADER;overflow=False
    for row in summary_rows(state):
        if len((current+row).encode())>SUMMARY_LIMIT-1024:
            parts.append(current);current='# VISI — продолжение списка\n\n'+TABLE_HEADER
        if len(parts)>=8 or len((current+row).encode())>SUMMARY_LIMIT-1024:
            overflow=True;break
        current+=row
    if current and len(parts)<8:parts.append(current)
    if overflow:
        warning='\n**Переполнение Summary: часть ссылок не поместилась. Полный открытый список сохранён в README.md артефакта; не считать показанный список полным.**\n'
        if parts:parts[-1]+=warning
    total=len(parts)
    parts=[f'# Ссылки — часть {i} из {total}\n\n'+part for i,part in enumerate(parts,1)]
    return parts,overflow


def make_transport(key,opener=None):
    opener=opener or urllib.request.build_opener(visi.NoRedirect())
    def transport(kind,url):
        if kind=='search' and url in SEARCH_URLS:params={'url':url,'page':1}
        elif kind=='item' and safe_url(url)==url:params={'url':url}
        else:raise ValueError('Unapproved endpoint or URL')
        req=urllib.request.Request('https://api.visi.su/api/v1/gate/'+kind+'?'+urlencode(params),method='GET',
                                   headers={'X-API-Key':key,'Accept':'application/json'})
        try:response=opener.open(req,timeout=30)
        except urllib.error.HTTPError as exc:response=exc
        with response:
            status=response.code;raw=response.read(10*1024*1024+1)
        if len(raw)>10*1024*1024:return {'error':{'code':'RESPONSE_TOO_LARGE'}},status
        try:payload=json.loads(raw)
        except (ValueError,UnicodeError):payload={'error':{'code':'NON_JSON_RESPONSE'}}
        return payload,status
    return transport


def main():
    if os.environ.get('GITHUB_RUN_ATTEMPT')!='1':raise SystemExit('Reruns are forbidden.')
    if os.environ.get('GITHUB_RUN_NUMBER')!='1':raise SystemExit('Only first dispatch is authorized; no resume.')
    run_id=os.environ.get('GITHUB_RUN_ID','')
    if not re.fullmatch(r'\d+',run_id):raise SystemExit('Missing GitHub run ID.')
    key=os.environ.get('VISI_API_KEY','')
    if not key:raise SystemExit('VISI_API_KEY is not configured.')
    folder=Path('visi-base-results')/run_id;folder.mkdir(parents=True,exist_ok=False)
    state=scan(make_transport(key),folder)
    parts,overflow=summary_parts(state)
    state['summary_overflow']=overflow
    atomic_save(folder/'checkpoint.json',state)
    atomic_save(folder/'report.json',{k:v for k,v in state.items() if k not in ('records','full_query_queue')})
    (folder/'README.md').write_text(markdown_summary(state),encoding='utf-8')
    for i,part in enumerate(parts,1):(folder/('summary-%02d.md'%i)).write_text(part,encoding='utf-8')
    print('VISI base status: '+state['status']+'; gateway attempts: '+str(state['calls_reserved'])+'/47')
    return 0 if state['status']=='BOUNDED_BASE_COMPLETE' and not overflow else 1


if __name__=='__main__':sys.exit(main())
