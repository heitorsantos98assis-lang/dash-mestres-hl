# Dashboard BI — Mestres do Seguro

Dashboard de acompanhamento das campanhas Meta Ads da **Mestres do Seguro**
(conta `act_709738588372104`). Período: **últimos 30 dias** (rolling).

## Estrutura

- `index.html` — dashboard (1 arquivo, sem dependências externas). Servido pelo Cloudflare Pages.
- `atualizar_bi.py` — busca dados da Meta API e injeta no HTML entre os marcadores `/*END_X*/`.
- `.github/workflows/atualizar-bi.yml` — Action que roda a cada 2h (horário comercial Brasília).
- `.env.example` — template de variáveis (copie para `.env` localmente).

## Páginas

- **Visão Geral** — totais da conta + KPIs da campanha de cadastros (cadastros, CPA,
  investimento, impressões, CPM, cliques, CPC, CTR) + bloco de visitas ao perfil +
  gráfico de evolução diária (investimento, cadastros e visitas).
- **Anúncios** — quebra por anúncio, com filtro Cadastros / Visitas ao Perfil e
  ordenação por qualquer coluna.

## Setup local

```bash
cp .env.example .env
# edite .env e cole seu META_ACCESS_TOKEN
pip install -r requirements.txt
python atualizar_bi.py
```

Abra o `index.html` no navegador.

## Deploy

### 1. Secrets no GitHub
Settings → Secrets and variables → Actions → **New repository secret**
- `MESTRES_META_ACCESS_TOKEN` = token de longa duração (System User) com `ads_read`
- `MESTRES_META_AD_ACCOUNT_ID` = `act_709738588372104`

### 2. Cloudflare Pages
1. dash.cloudflare.com → **Workers & Pages** → **Create** → **Pages** → **Connect to Git**
2. Selecionar o repo `dash-mestres-hl` → branch `main`
3. Build command: (vazio) · **Build output directory: `/`** (raiz)
4. Save and Deploy

## Notas técnicas

- "Cadastro" = evento de pixel custom **EndForm** (coluna *Resultados* do Meta Ads Manager).
- "Visitas ao perfil" = indicador `profile_visit_view`.
- As campanhas de cadastros e de visitas são **auto-detectadas pelo nome**; é possível
  fixá-las via `CAD_CAMPAIGN_ID` / `PERF_CAMPAIGN_ID` no `.env`.
- Métricas usam a janela de atribuição padrão de cada campanha.
