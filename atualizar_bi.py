#!/usr/bin/env python3
"""
Atualiza o BI Mestres do Seguro (index.html) com dados frescos da Meta Ads API.
Periodo: ultimos 30 dias (date_preset=last_30d).

Estrutura injetada no HTML (entre os marcadores /*END_X*/):
- META       : metadados (conta, periodo, atualizado)
- TOTAIS     : totais da conta no periodo
- CAMP_CAD   : campanha de cadastros (resultado = cadastros / EndForm)
- CAMP_PERF  : campanha de visitas ao perfil (resultado = profile_visit_view)
- DAILY      : serie diaria [date, spend, imp, clicks, cadastros, visitas]
- ADS        : por anuncio [id,nome,tipo,spend,imp,clicks,ctr,cpc,cpm,reach,res,cpr,thumb]
"""
import requests, json, re, os, sys, time
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ========== CONFIG ==========
TOKEN   = os.getenv('META_ACCESS_TOKEN', '')
ACCOUNT = os.getenv('META_AD_ACCOUNT_ID', 'act_709738588372104')
DATE_PRESET = os.getenv('DATE_PRESET', 'last_30d')
BASE = 'https://graph.facebook.com/v21.0'
BI_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'atualizar_bi.log')

# IDs das campanhas (override por env). Se vazios, sao auto-detectados.
CAD_CAMPAIGN_ID  = os.getenv('CAD_CAMPAIGN_ID', '')
PERF_CAMPAIGN_ID = os.getenv('PERF_CAMPAIGN_ID', '')

# Indicador de "cadastro" no campo results (custom pixel event)
PROFILE_INDICATOR = 'profile_visit_view'

if not TOKEN:
    print('ERRO: META_ACCESS_TOKEN nao configurado (use .env ou env vars)')
    sys.exit(1)

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass

def _parse_response(r):
    body = r.text or ''
    try:
        data = r.json()
    except ValueError:
        return {'error': {'message': f'HTTP {r.status_code} non-JSON: {body[:200]!r}', 'status': r.status_code}}
    if isinstance(data, dict) and 'error' in data:
        err = data['error']
        msg = f'HTTP {r.status_code} {err.get("type","")} code={err.get("code","")}: {err.get("message","")}'
        return {'error': {'message': msg, 'status': r.status_code}}
    return data

def api_get(endpoint, params):
    params['access_token'] = TOKEN
    last_err = 'desconhecido'
    for attempt in range(3):
        try:
            r = requests.get(f'{BASE}/{endpoint}', params=params, timeout=90)
        except requests.RequestException as e:
            last_err = f'network: {e}'; log(f'  Request error (tent {attempt+1}): {last_err}')
            if attempt < 2: time.sleep(2 ** (attempt + 1))
            continue
        data = _parse_response(r)
        if 'error' in data:
            last_err = data['error']['message']; log(f'  API error (tent {attempt+1}): {last_err}')
            status = data['error'].get('status', 0)
            if 400 <= status < 500 and status != 429:
                return data
            if attempt < 2: time.sleep(2 ** (attempt + 1))
            continue
        return data
    return {'error': {'message': f'Falhou apos 3 tentativas ({last_err})'}}

def paginate(first_response):
    all_data = list(first_response.get('data', []))
    next_url = first_response.get('paging', {}).get('next')
    while next_url:
        try:
            r = requests.get(next_url, timeout=90)
        except requests.RequestException as e:
            log(f'  paginate network error: {e}'); break
        data = _parse_response(r)
        if 'error' in data:
            log(f'  paginate error: {data["error"]["message"]}'); break
        all_data.extend(data.get('data', []))
        next_url = data.get('paging', {}).get('next')
    return all_data

def f(v):
    try: return float(v)
    except (TypeError, ValueError): return 0.0

def i(v):
    try: return int(float(v))
    except (TypeError, ValueError): return 0

def res_value(results):
    """Extrai o valor do campo 'results' (estrutura aninhada da API)."""
    try:
        vals = results['value'][0].get('values', [])
        return i(vals[0]['value']) if vals else 0
    except (KeyError, IndexError, TypeError):
        return 0

def res_indicator(results):
    try:
        return results['value'][0].get('indicator', '')
    except (KeyError, IndexError, TypeError):
        return ''

def cpr_value(cpr):
    try:
        vals = cpr['value'][0].get('values', [])
        return round(f(vals[0]['value']), 2) if vals else 0.0
    except (KeyError, IndexError, TypeError):
        return 0.0

# ========== 1. CAMPANHAS (metadata p/ detectar cad e perf) ==========
def fetch_campaigns():
    log('Buscando campanhas ativas...')
    data = api_get(f'{ACCOUNT}/campaigns', {
        'fields': 'name,effective_status,objective',
        'effective_status': '["ACTIVE"]', 'limit': 200
    })
    if 'error' in data:
        log(f'  ERRO: {data["error"]["message"]}'); return []
    return paginate(data)

def detect_campaigns(campaigns):
    """Retorna (cad_id, perf_id), respeitando overrides por env."""
    cad, perf = CAD_CAMPAIGN_ID, PERF_CAMPAIGN_ID
    for c in campaigns:
        nome = (c.get('name') or '').upper()
        if not perf and ('VISITAS AO PERFIL' in nome or 'VISITA AO PERFIL' in nome):
            perf = c['id']
        if not cad and ('END FORM' in nome or 'CADASTRO' in nome) and c.get('objective') == 'OUTCOME_LEADS':
            cad = c['id']
    # fallback: 1a OUTCOME_LEADS ativa como cad
    if not cad:
        for c in campaigns:
            if c.get('objective') == 'OUTCOME_LEADS':
                cad = c['id']; break
    log(f'  campanha cadastros = {cad} | visitas = {perf}')
    return cad, perf

# ========== 2. TOTAIS DA CONTA ==========
def fetch_totais():
    log('Buscando totais da conta...')
    data = api_get(f'{ACCOUNT}/insights', {
        'fields': 'spend,impressions,clicks,ctr,cpc,cpm,reach',
        'date_preset': DATE_PRESET, 'level': 'account'
    })
    if 'error' in data:
        log(f'  ERRO: {data["error"]["message"]}'); return None
    rows = data.get('data', [])
    if not rows: return None
    d = rows[0]
    return {
        'spend': round(f(d.get('spend')), 2), 'impressions': i(d.get('impressions')),
        'clicks': i(d.get('clicks')), 'ctr': round(f(d.get('ctr')), 2),
        'cpc': round(f(d.get('cpc')), 2), 'cpm': round(f(d.get('cpm')), 2),
        'reach': i(d.get('reach')),
    }

# ========== 3. METRICAS POR CAMPANHA ==========
def fetch_campaign_metrics(cad_id, perf_id):
    log('Buscando metricas das campanhas (cad/perf)...')
    data = api_get(f'{ACCOUNT}/insights', {
        'level': 'campaign',
        'fields': 'campaign_id,campaign_name,spend,impressions,clicks,ctr,cpc,cpm,reach,results,cost_per_result',
        'date_preset': DATE_PRESET, 'limit': 500,
        'filtering': json.dumps([{'field': 'campaign.id', 'operator': 'IN',
                                  'value': [x for x in [cad_id, perf_id] if x]}]),
    })
    cad = perf = None
    if 'error' in data:
        log(f'  ERRO: {data["error"]["message"]}'); return cad, perf
    for d in paginate(data):
        base = {
            'nome': d.get('campaign_name', ''), 'spend': round(f(d.get('spend')), 2),
            'impressions': i(d.get('impressions')), 'clicks': i(d.get('clicks')),
            'ctr': round(f(d.get('ctr')), 2), 'cpc': round(f(d.get('cpc')), 2),
            'cpm': round(f(d.get('cpm')), 2), 'reach': i(d.get('reach')),
        }
        cid = d.get('campaign_id')
        if cid == perf_id:
            base['visitas'] = res_value(d.get('results', {}))
            base['cpv'] = cpr_value(d.get('cost_per_result', {}))
            perf = base
        else:
            base['cadastros'] = res_value(d.get('results', {}))
            base['cpa'] = cpr_value(d.get('cost_per_result', {}))
            cad = base
    return cad, perf

# ========== 4. SERIE DIARIA ==========
def fetch_daily(cad_id, perf_id):
    log('Buscando serie diaria...')
    data = api_get(f'{ACCOUNT}/insights', {
        'level': 'campaign',
        'fields': 'campaign_id,spend,impressions,clicks,results',
        'date_preset': DATE_PRESET, 'time_increment': 1, 'limit': 500,
        'filtering': json.dumps([{'field': 'campaign.id', 'operator': 'IN',
                                  'value': [x for x in [cad_id, perf_id] if x]}]),
    })
    if 'error' in data:
        log(f'  ERRO: {data["error"]["message"]}'); return []
    days = {}
    for d in paginate(data):
        dt = d.get('date_start')
        if not dt: continue
        row = days.setdefault(dt, {'spend': 0.0, 'imp': 0, 'clk': 0, 'cad': 0, 'perf': 0})
        row['spend'] += f(d.get('spend')); row['imp'] += i(d.get('impressions')); row['clk'] += i(d.get('clicks'))
        rv = res_value(d.get('results', {}))
        if d.get('campaign_id') == perf_id: row['perf'] += rv
        else: row['cad'] += rv
    out = []
    for dt in sorted(days):
        r = days[dt]
        out.append([dt, round(r['spend'], 2), r['imp'], r['clk'], r['cad'], r['perf']])
    log(f'  {len(out)} dias')
    return out

# ========== 5. ANUNCIOS ==========
def fetch_ad_thumbs():
    log('Buscando thumbnails dos anuncios...')
    data = api_get(f'{ACCOUNT}/ads', {
        'fields': 'id,creative{thumbnail_url,image_url}', 'limit': 400
    })
    thumbs = {}
    if 'error' in data:
        log(f'  AVISO thumbs: {data["error"]["message"]}'); return thumbs
    for ad in paginate(data):
        cr = ad.get('creative', {}) or {}
        thumbs[ad['id']] = cr.get('thumbnail_url') or cr.get('image_url') or ''
    return thumbs

def fetch_ads(cad_id, perf_id):
    log('Buscando metricas por anuncio...')
    data = api_get(f'{ACCOUNT}/insights', {
        'level': 'ad',
        'fields': 'ad_id,ad_name,campaign_id,spend,impressions,clicks,ctr,cpc,cpm,reach,results,cost_per_result',
        'date_preset': DATE_PRESET, 'limit': 500, 'sort': 'impressions_descending',
        'filtering': json.dumps([{'field': 'campaign.id', 'operator': 'IN',
                                  'value': [x for x in [cad_id, perf_id] if x]}]),
    })
    if 'error' in data:
        log(f'  ERRO: {data["error"]["message"]}'); return []
    thumbs = fetch_ad_thumbs()
    rows = []
    for d in paginate(data):
        if i(d.get('impressions')) == 0:
            continue
        ind = res_indicator(d.get('results', {}))
        tipo = 'perf' if PROFILE_INDICATOR in ind else 'cad'
        rows.append([
            d.get('ad_id'), d.get('ad_name', ''), tipo,
            round(f(d.get('spend')), 2), i(d.get('impressions')), i(d.get('clicks')),
            round(f(d.get('ctr')), 2), round(f(d.get('cpc')), 2), round(f(d.get('cpm')), 2),
            i(d.get('reach')), res_value(d.get('results', {})),
            cpr_value(d.get('cost_per_result', {})), thumbs.get(d.get('ad_id'), ''),
        ])
    log(f'  {len(rows)} anuncios com veiculacao')
    return rows

# ========== INJECAO ==========
def replace_var(html, varname, value):
    js = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    pattern = rf'var {varname} = .*?; /\*END_{varname}\*/'
    new = f'var {varname} = {js}; /*END_{varname}*/'
    if re.search(pattern, html, flags=re.DOTALL):
        return re.sub(pattern, lambda m: new, html, flags=re.DOTALL)
    log(f'  AVISO: marker END_{varname} nao encontrado')
    return html

# ========== MAIN ==========
def main():
    log('=' * 60); log('INICIO ATUALIZACAO BI MESTRES'); log('=' * 60)
    if not os.path.exists(BI_PATH):
        log(f'ERRO: nao encontrado: {BI_PATH}'); sys.exit(1)
    with open(BI_PATH, 'r', encoding='utf-8') as fh:
        html = fh.read()

    campaigns = fetch_campaigns()
    cad_id, perf_id = detect_campaigns(campaigns)
    totais = fetch_totais()
    cad, perf = fetch_campaign_metrics(cad_id, perf_id)
    daily = fetch_daily(cad_id, perf_id)
    ads = fetch_ads(cad_id, perf_id)

    now = datetime.now()
    meta = {
        'conta': ACCOUNT.replace('act_', ''), 'conta_nome': 'Mestres do Seguro',
        'periodo': 'Últimos 30 dias',
        'periodo_label': (daily[0][0] if daily else '') + ' a ' + (daily[-1][0] if daily else ''),
        'atualizado': now.strftime('%d/%m %H:%M'),
    }

    any_data = False
    html = replace_var(html, 'META', meta)
    if totais: html = replace_var(html, 'TOTAIS', totais); any_data = True
    if cad:    html = replace_var(html, 'CAMP_CAD', cad);  any_data = True
    if perf:   html = replace_var(html, 'CAMP_PERF', perf); any_data = True
    if daily:  html = replace_var(html, 'DAILY', daily);   any_data = True
    if ads:    html = replace_var(html, 'ADS', ads);       any_data = True

    if not any_data:
        log('Nenhum dado obtido da Meta API — abortando sem mexer no HTML.'); sys.exit(1)

    with open(BI_PATH, 'w', encoding='utf-8') as fh:
        fh.write(html)
    log(f'HTML salvo: {len(html)} chars'); log('CONCLUIDO'); log('')

if __name__ == '__main__':
    main()
