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

# Planilha de MQL (qualificacao). Coluna H = resposta; Coluna R = data/hora.
MQL_SHEET_ID  = os.getenv('MQL_SHEET_ID', '1lqKVUzMPYhs0DLeARzP_eNEDe_-OJopO6Dz29P42wSs')
MQL_SHEET_GID = os.getenv('MQL_SHEET_GID', '0')
MQL_COL_RESP = 7   # coluna H (0-based)
MQL_COL_DATE = 17  # coluna R (0-based)
# MQL = coluna H começa com "Sim" (intencao/capital)
#   OU renda com "5 mil"/"10 mil" (= ate 5 mil, ate 10 mil, acima de 10 mil).
#   NAO conta "Ganho até R$ 3 mil por mês". Match flexivel a espacos/"R$".
def _is_mql(resp):
    r = (resp or '').strip().lower()
    if r.startswith('sim'):
        return True
    norm = r.replace(' ', '').replace('$', '')
    return '5mil' in norm or '10mil' in norm

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

def res_value(results):
    """Valor do campo 'results' da API (= coluna Resultados do Meta)."""
    try:
        vals = results['value'][0].get('values', [])
        return i(vals[0]['value']) if vals else 0
    except (KeyError, IndexError, TypeError):
        return 0

def cpr_value(cpr):
    try:
        vals = cpr['value'][0].get('values', [])
        return round(f(vals[0]['value']), 2) if vals else 0.0
    except (KeyError, IndexError, TypeError):
        return 0.0

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
            'fields': 'campaign_id,spend,impressions,clicks,actions,conversions',
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
            conversions = row.get('conversions', [])
            acc[1] += f(row.get('spend')); acc[2] += i(row.get('impressions')); acc[3] += i(row.get('clicks'))
            acc[4] += action_val(actions, {'link_click'})
            if tipo == 'cad':
                # cadastro = evento EndForm (em conversions) + leads padrao (em actions)
                acc[5] += sum_actions(conversions, suffix='.EndForm') \
                        + sum_actions(actions, exact=CADASTRO_ACTIONS_EXACT)
            else:
                # visitas ao perfil ~= cliques no link da campanha de perfil
                acc[5] += action_val(actions, {'link_click'})
    out = [[r[0], round(r[1], 2), r[2], r[3], r[4], r[5]] for r in (days[k] for k in sorted(days))]
    log(f'  {tipo}: {len(out)} dias')
    return out

# ========== 3. ANUNCIOS (ultimos 30 dias) ==========
def fetch_ads(cad_ids, perf_ids):
    log('Buscando anuncios (30d)...')
    idset_perf = set(perf_ids)
    data = api_get(f'{ACCOUNT}/insights', {
        'level': 'ad',
        'fields': ('ad_id,ad_name,campaign_id,spend,impressions,clicks,ctr,cpc,cpm,reach,actions,conversions,'
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
        actions = d.get('actions', []); conversions = d.get('conversions', [])
        if d.get('campaign_id') in idset_perf:
            tipo = 'perf'; res = action_val(actions, {'link_click'})
        else:
            tipo = 'cad'; res = sum_actions(conversions, suffix='.EndForm') + sum_actions(actions, exact=CADASTRO_ACTIONS_EXACT)
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

# ========== 4. DIARIO POR ANUNCIO (historico p/ filtro de datas) ==========
def date_chunks(since, until, size=15):
    s = datetime.strptime(since, '%Y-%m-%d'); u = datetime.strptime(until, '%Y-%m-%d')
    out = []; cur = s
    while cur <= u:
        end = min(cur + timedelta(days=size - 1), u)
        out.append((cur.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')))
        cur = end + timedelta(days=1)
    return out

def fetch_ads_daily(cad_ids, perf_ids):
    log('Buscando diario por anuncio (historico)...')
    idset_perf = set(perf_ids)
    until = datetime.now().strftime('%Y-%m-%d')
    rows = []
    for since, end in date_chunks(PERIODO_INICIO, until, 15):
        data = api_get(f'{ACCOUNT}/insights', {
            'level': 'ad',
            'fields': 'ad_id,campaign_id,spend,impressions,clicks,actions,conversions',
            'time_range': json.dumps({'since': since, 'until': end}),
            'time_increment': 1, 'limit': 500,
            'filtering': json.dumps([{'field': 'campaign.id', 'operator': 'IN', 'value': cad_ids + perf_ids}]),
        })
        if 'error' in data:
            log(f'  ERRO ads_daily {since}: {data["error"]["message"]}'); continue
        for d in data.get('data', []) if isinstance(data, dict) else []:
            if i(d.get('impressions')) == 0: continue
            actions = d.get('actions', []); conversions = d.get('conversions', [])
            lc = action_val(actions, {'link_click'})
            if d.get('campaign_id') in idset_perf:
                res = lc
            else:
                res = sum_actions(conversions, suffix='.EndForm') + sum_actions(actions, exact=CADASTRO_ACTIONS_EXACT)
            rows.append([d.get('ad_id'), d.get('date_start'), round(f(d.get('spend')), 2),
                         i(d.get('impressions')), i(d.get('clicks')), lc, res])
        # pagina extra do chunk
        nxt = data.get('paging', {}).get('next') if isinstance(data, dict) else None
        while nxt:
            try:
                r = requests.get(nxt, timeout=90)
            except requests.RequestException:
                break
            pg = _parse(r)
            if 'error' in pg: break
            for d in pg.get('data', []):
                if i(d.get('impressions')) == 0: continue
                actions = d.get('actions', []); conversions = d.get('conversions', [])
                lc = action_val(actions, {'link_click'})
                res = lc if d.get('campaign_id') in idset_perf else (sum_actions(conversions, suffix='.EndForm') + sum_actions(actions, exact=CADASTRO_ACTIONS_EXACT))
                rows.append([d.get('ad_id'), d.get('date_start'), round(f(d.get('spend')), 2),
                             i(d.get('impressions')), i(d.get('clicks')), lc, res])
            nxt = pg.get('paging', {}).get('next')
    log(f'  {len(rows)} linhas (anuncio x dia)')
    return rows

# ========== 5. MQL (planilha Google) ==========
def _mql_date(raw):
    raw = (raw or '').strip()
    if not raw:
        return None
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', raw)        # 2026-06-10 07:32:14
    if m:
        return f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
    m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', raw)     # 10/06/2026
    if m:
        return f'{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}'
    return None

def fetch_mqls():
    log('Buscando MQLs da planilha...')
    url = f'https://docs.google.com/spreadsheets/d/{MQL_SHEET_ID}/export?format=csv&gid={MQL_SHEET_GID}'
    try:
        r = requests.get(url, timeout=30)
    except requests.RequestException as e:
        log(f'  ERRO rede MQL: {e}'); return []
    if r.status_code != 200:
        log(f'  ERRO HTTP {r.status_code} (planilha precisa estar "Qualquer pessoa com o link: Leitor")')
        return []
    import csv, io
    counts = {}
    reader = csv.reader(io.StringIO(r.text))
    next(reader, None)  # cabecalho
    for row in reader:
        if len(row) <= MQL_COL_DATE:
            continue
        resp = (row[MQL_COL_RESP] or '').strip()
        if not resp:
            continue
        if not _is_mql(resp):
            continue
        d = _mql_date(row[MQL_COL_DATE])
        if not d:
            continue
        counts[d] = counts.get(d, 0) + 1
    out = [[d, counts[d]] for d in sorted(counts)]
    log(f'  {sum(counts.values())} MQLs em {len(out)} dias')
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
    ads_daily = fetch_ads_daily(cad_ids, perf_ids)
    mqls = fetch_mqls()

    meta = {'conta': ACCOUNT.replace('act_', ''), 'conta_nome': 'Mestres do Seguro',
            'atualizado': datetime.now().strftime('%d/%m %H:%M')}

    any_data = False
    html = replace_var(html, 'META', meta)
    if cad_daily:  html = replace_var(html, 'CAD_DAILY', cad_daily);   any_data = True
    if perf_daily: html = replace_var(html, 'PERF_DAILY', perf_daily); any_data = True
    if ads:        html = replace_var(html, 'ADS', ads);               any_data = True
    if ads_daily:  html = replace_var(html, 'ADS_DAILY', ads_daily);   any_data = True
    # MQL sempre injeta (mesmo vazio) pra zerar dados antigos se a planilha sair do ar
    html = replace_var(html, 'MQL_DAILY', mqls)
    if not any_data:
        log('Nenhum dado da Meta API — abortando.'); sys.exit(1)

    with open(BI_PATH, 'w', encoding='utf-8') as fh:
        fh.write(html)
    log(f'HTML salvo: {len(html)} chars'); log('CONCLUIDO'); log('')

if __name__ == '__main__':
    main()
