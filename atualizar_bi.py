#!/usr/bin/env python3
"""
Atualiza o BI Mestres do Seguro (index.html) com dados frescos da Meta Ads API.

Produz (entre os marcadores /*END_X*/):
- META        : metadados (conta, atualizado)
- CAD_DAILY   : diario das campanhas de CADASTROS de seguros (ativas + pausadas)
                [date, spend, impressions, clicks, link_clicks, cadastros]
- PERF_DAILY  : diario das campanhas de VISITAS AO PERFIL
                [date, spend, impressions, clicks, link_clicks, visitas]
- ADS         : anuncios dos ultimos 30 dias
                [id,nome,tipo,spend,imp,clicks,ctr,cpc,cpm,reach,resultado,custo,thumb]

Classificacao (operacao de SEGUROS; Plano de Saude / engajamento ficam de fora):
- CADASTROS: objective OUTCOME_LEADS e nome com "SEGUROS" (exclui WhatsApp).
- VISITAS  : nome com "VISITA AO PERFIL"/"VISITAS AO PERFIL"/"SEGUIDOR".
Override por env: CAD_CAMPAIGN_IDS / PERF_CAMPAIGN_IDS (lista separada por virgula).
"""
import requests, json, re, os, sys, time
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ========== CONFIG ==========
TOKEN   = os.getenv('META_ACCESS_TOKEN', '')
ACCOUNT = os.getenv('META_AD_ACCOUNT_ID', 'act_709738588372104')
PERIODO_INICIO = os.getenv('PERIODO_INICIO', '2025-12-01')
BASE = 'https://graph.facebook.com/v21.0'
BI_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'atualizar_bi.log')

CAD_IDS_ENV  = [x for x in os.getenv('CAD_CAMPAIGN_IDS', '').split(',') if x.strip()]
PERF_IDS_ENV = [x for x in os.getenv('PERF_CAMPAIGN_IDS', '').split(',') if x.strip()]

# Acoes que representam um CADASTRO concluido (somadas por dia).
CADASTRO_ACTIONS_SUFFIX = ('.EndForm',)  # custom pixel event principal
CADASTRO_ACTIONS_EXACT = {
    'offsite_conversion.fb_pixel_lead',
    'offsite_conversion.fb_pixel_complete_registration',
}
PROFILE_ACTIONS = {'onsite_conversion.view_profile', 'profile_visit_view'}

if not TOKEN:
    print('ERRO: META_ACCESS_TOKEN nao configurado (use .env ou env vars)')
    sys.exit(1)

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except Exception:
        pass

def _parse(r):
    try:
        data = r.json()
    except ValueError:
        return {'error': {'message': f'HTTP {r.status_code} non-JSON', 'status': r.status_code}}
    if isinstance(data, dict) and 'error' in data:
        e = data['error']
        return {'error': {'message': f'HTTP {r.status_code} code={e.get("code","")}: {e.get("message","")}', 'status': r.status_code}}
    return data

def api_get(endpoint, params):
    params['access_token'] = TOKEN
    last = 'desconhecido'
    for attempt in range(3):
        try:
            r = requests.get(f'{BASE}/{endpoint}', params=params, timeout=90)
        except requests.RequestException as e:
            last = f'network: {e}'; log(f'  req err {attempt+1}: {last}')
            if attempt < 2: time.sleep(2 ** (attempt + 1))
            continue
        data = _parse(r)
        if 'error' in data:
            last = data['error']['message']; log(f'  api err {attempt+1}: {last}')
            st = data['error'].get('status', 0)
            if 400 <= st < 500 and st != 429: return data
            if attempt < 2: time.sleep(2 ** (attempt + 1))
            continue
        return data
    return {'error': {'message': f'falhou apos 3 tentativas ({last})'}}

def paginate(first):
    out = list(first.get('data', []))
    nxt = first.get('paging', {}).get('next')
    while nxt:
        try:
            r = requests.get(nxt, timeout=90)
        except requests.RequestException as e:
            log(f'  paginate net err: {e}'); break
        data = _parse(r)
        if 'error' in data:
            log(f'  paginate err: {data["error"]["message"]}'); break
        out.extend(data.get('data', []))
        nxt = data.get('paging', {}).get('next')
    return out

def f(v):
    try: return float(v)
    except (TypeError, ValueError): return 0.0

def i(v):
    try: return int(float(v))
    except (TypeError, ValueError): return 0

def sum_actions(actions, exact=None, suffix=None):
    total = 0
    for a in (actions or []):
        at = a.get('action_type', '')
        if exact and at in exact:
            total += i(a.get('value', 0))
        elif suffix and at.endswith(suffix):
            total += i(a.get('value', 0))
    return total

def action_val(actions, types):
    return sum(i(a.get('value', 0)) for a in (actions or []) if a.get('action_type') in types)

# ========== 1. CLASSIFICA CAMPANHAS ==========
def classificar():
    log('Classificando campanhas...')
    data = api_get(f'{ACCOUNT}/campaigns', {'fields': 'name,objective,effective_status', 'limit': 500})
    if 'error' in data:
        log(f'  ERRO: {data["error"]["message"]}'); return [], []
    camps = paginate(data)
    cad, perf = [], []
    for c in camps:
        nome = (c.get('name') or '').upper()
        obj = c.get('objective', '')
        if 'VISITA AO PERFIL' in nome or 'VISITAS AO PERFIL' in nome or 'SEGUIDOR' in nome:
            perf.append(c['id'])
        elif obj == 'OUTCOME_LEADS' and 'SEGUROS' in nome and 'WHATSAPP' not in nome:
            cad.append(c['id'])
    if CAD_IDS_ENV:  cad  = CAD_IDS_ENV
    if PERF_IDS_ENV: perf = PERF_IDS_ENV
    log(f'  cadastros: {len(cad)} campanhas | visitas: {len(perf)} campanhas')
    return cad, perf

# ========== 2. DIARIO POR GRUPO (em chunks de campanhas) ==========
def chunks(lst, n):
    for k in range(0, len(lst), n):
        yield lst[k:k + n]

def fetch_daily(ids, tipo):
    """Retorna [[date, spend, imp, clicks, link_clicks, resultado], ...] agregado."""
    if not ids:
        return []
    until = datetime.now().strftime('%Y-%m-%d')
    days = {}
    for grp in chunks(ids, 5):  # <=5 campanhas por request evita truncamento
        data = api_get(f'{ACCOUNT}/insights', {
            'level': 'campaign',
            'fields': 'campaign_id,spend,impressions,clicks,actions',
            'time_range': json.dumps({'since': PERIODO_INICIO, 'until': until}),
            'time_increment': 1, 'limit': 500,
            'filtering': json.dumps([{'field': 'campaign.id', 'operator': 'IN', 'value': grp}]),
        })
        if 'error' in data:
            log(f'  ERRO diario {tipo}: {data["error"]["message"]}'); continue
        for row in paginate(data):
            d = row.get('date_start')
            if not d: continue
            acc = days.setdefault(d, [d, 0.0, 0, 0, 0, 0])
            actions = row.get('actions', [])
            acc[1] += f(row.get('spend')); acc[2] += i(row.get('impressions')); acc[3] += i(row.get('clicks'))
            acc[4] += action_val(actions, {'link_click'})
            if tipo == 'cad':
                acc[5] += sum_actions(actions, exact=CADASTRO_ACTIONS_EXACT) \
                        + sum_actions(actions, suffix='.EndForm')
            else:
                acc[5] += action_val(actions, PROFILE_ACTIONS)
    out = [[r[0], round(r[1], 2), r[2], r[3], r[4], r[5]] for r in (days[k] for k in sorted(days))]
    log(f'  {tipo}: {len(out)} dias')
    return out

# ========== 3. ANUNCIOS (ultimos 30 dias) ==========
def fetch_ads(cad_ids, perf_ids):
    log('Buscando anuncios (30d)...')
    idset_perf = set(perf_ids)
    data = api_get(f'{ACCOUNT}/insights', {
        'level': 'ad',
        'fields': ('ad_id,ad_name,campaign_id,spend,impressions,clicks,ctr,cpc,cpm,reach,actions,'
                   'video_p25_watched_actions,video_p50_watched_actions,'
                   'video_p75_watched_actions,video_p100_watched_actions'),
        'date_preset': 'last_30d', 'limit': 500, 'sort': 'impressions_descending',
        'filtering': json.dumps([{'field': 'campaign.id', 'operator': 'IN', 'value': cad_ids + perf_ids}]),
    })
    if 'error' in data:
        log(f'  ERRO ads: {data["error"]["message"]}'); return []
    creatives = fetch_creatives()      # {ad_id: {'thumb','ig','status','video'}}
    rows = []

    def vid(d, key):
        arr = d.get(key)
        if isinstance(arr, list) and arr:
            return i(arr[0].get('value', 0))
        return 0

    for d in paginate(data):
        if i(d.get('impressions')) == 0: continue
        actions = d.get('actions', [])
        is_perf = d.get('campaign_id') in idset_perf
        if is_perf:
            tipo = 'perf'; res = action_val(actions, PROFILE_ACTIONS)
        else:
            tipo = 'cad'; res = sum_actions(actions, exact=CADASTRO_ACTIONS_EXACT) + sum_actions(actions, suffix='.EndForm')
        spend = round(f(d.get('spend')), 2)
        cpr = round(spend / res, 2) if res else 0
        p25 = vid(d, 'video_p25_watched_actions'); p50 = vid(d, 'video_p50_watched_actions')
        p75 = vid(d, 'video_p75_watched_actions'); p100 = vid(d, 'video_p100_watched_actions')
        cr = creatives.get(d.get('ad_id'), {})
        is_video = 1 if (cr.get('video') or p25 or p50 or p75 or p100) else 0
        status = 'Ativo' if cr.get('status') == 'ACTIVE' else 'Pausado'
        rows.append([
            d.get('ad_id'), d.get('ad_name', ''), tipo, spend, i(d.get('impressions')), i(d.get('clicks')),
            round(f(d.get('ctr')), 2), round(f(d.get('cpc')), 2), round(f(d.get('cpm')), 2),
            i(d.get('reach')), res, cpr,
            cr.get('thumb', ''), cr.get('ig', ''), status, is_video, p25, p50, p75, p100,
        ])
    log(f'  {len(rows)} anuncios')
    return rows

def fetch_creatives():
    """Retorna {ad_id: {'thumb','ig','status','video'}}."""
    data = api_get(f'{ACCOUNT}/ads', {
        'fields': 'id,effective_status,creative{thumbnail_url,image_url,instagram_permalink_url,object_type,video_id}',
        'limit': 400,
    })
    out = {}
    if 'error' in data:
        log(f'  AVISO creatives: {data["error"]["message"]}'); return out
    for ad in paginate(data):
        cr = ad.get('creative', {}) or {}
        is_video = (cr.get('object_type') == 'VIDEO') or bool(cr.get('video_id'))
        out[ad['id']] = {
            'thumb': cr.get('thumbnail_url') or cr.get('image_url') or '',
            'ig': cr.get('instagram_permalink_url') or '',
            'status': ad.get('effective_status', ''),
            'video': is_video,
        }
    return out

# ========== INJECAO ==========
def replace_var(html, name, value):
    js = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    pat = rf'var {name} = .*?; /\*END_{name}\*/'
    new = f'var {name} = {js}; /*END_{name}*/'
    if re.search(pat, html, flags=re.DOTALL):
        return re.sub(pat, lambda m: new, html, flags=re.DOTALL)
    log(f'  AVISO: marker END_{name} nao encontrado'); return html

def main():
    log('=' * 60); log('INICIO ATUALIZACAO BI MESTRES'); log('=' * 60)
    if not os.path.exists(BI_PATH):
        log(f'ERRO: nao encontrado {BI_PATH}'); sys.exit(1)
    with open(BI_PATH, 'r', encoding='utf-8') as fh:
        html = fh.read()

    cad_ids, perf_ids = classificar()
    cad_daily = fetch_daily(cad_ids, 'cad')
    perf_daily = fetch_daily(perf_ids, 'perf')
    ads = fetch_ads(cad_ids, perf_ids)

    meta = {'conta': ACCOUNT.replace('act_', ''), 'conta_nome': 'Mestres do Seguro',
            'atualizado': datetime.now().strftime('%d/%m %H:%M')}

    any_data = False
    html = replace_var(html, 'META', meta)
    if cad_daily:  html = replace_var(html, 'CAD_DAILY', cad_daily);   any_data = True
    if perf_daily: html = replace_var(html, 'PERF_DAILY', perf_daily); any_data = True
    if ads:        html = replace_var(html, 'ADS', ads);               any_data = True
    if not any_data:
        log('Nenhum dado da Meta API — abortando.'); sys.exit(1)

    with open(BI_PATH, 'w', encoding='utf-8') as fh:
        fh.write(html)
    log(f'HTML salvo: {len(html)} chars'); log('CONCLUIDO'); log('')

if __name__ == '__main__':
    main()
