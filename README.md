# Agregador de Monitoramento Solar FV -- Ingestao (repo publico)

Job de ingestao de um agregador de monitoramento de usinas solares
fotovoltaicas: puxa leituras periodicas de portais de fabricante (GoodWe/SEMS
e WEG/SunWEG), normaliza num modelo unico e grava num banco Postgres
(Supabase). Roda a cada 15 minutos via GitHub Actions, mais um job diario de
agregacao que consolida energia/disponibilidade/alarmes por usina/dia.

Este e o **repo publico, so-codigo**, de um projeto maior cujos artefatos de
planejamento (decisoes de fase, notas de pesquisa, DDL do banco, dados de
cliente) permanecem num repositorio privado separado. A divisao existe para
contornar o limite de minutos gratuitos de GitHub Actions em repos privados
(Actions e ilimitado/gratuito em repos publicos) -- este repo contem apenas o
que e necessario para RODAR a ingestao, nada de dados de cliente ou de
planejamento interno.

## O que tem aqui

- `connectors/` -- clientes HTTP + normalizacao por fabricante (GoodWe/SEMS,
  WEG/SunWEG), atras de um Protocol comum (`connectors/base.py`).
- `ingest/` -- CLI de ingestao (`run.py`), agregacao diaria (`aggregate.py`),
  deteccao de alarmes derivados de dados (`alarms.py`) e a fiacao do canal de
  notificacao (`notify.py`).
- `notifiers/` -- entrega de alerta por canal, atras de um Protocol comum
  (`notifiers/base.py`), hoje WhatsApp via API oficial da Meta
  (`notifiers/whatsapp/`).
- `.github/workflows/` -- os dois workflows agendados (ingestao a cada 15 min,
  agregacao diaria).
- `config/plant_coordinates.toml` -- coordenadas geograficas (nao-secretas,
  granularidade de cidade) usadas pelo pvlib para calcular janela solar.

O que **nao** esta aqui: migracoes de banco (DDL), dashboard, dados/config de
cliente, documentacao de planejamento de fase. Essas coisas vivem no repo
privado.

## Segredos necessarios (GitHub Actions Secrets)

O workflow de ingestao espera os seguintes secrets, configurados em
Settings -> Secrets and variables -> Actions deste repo. Nenhum valor deve
ser commitado -- apenas os nomes abaixo:

- `SEMS_ACCOUNT_<REF>` / `SEMS_PASSWORD_<REF>` -- credencial do portal SEMS
  (GoodWe) para cada `credential_ref` cadastrado na tabela `plants`.
- `SUNWEG_ACCOUNT_<REF>` / `SUNWEG_PASSWORD_<REF>` -- credencial do portal
  SunWEG (WEG), mesmo esquema de `<REF>`.
- `SUPABASE_INGESTION_URL` -- connection string do Postgres (pooler
  Supavisor, transaction mode) para a role de escrita `ingestion_writer`.

Opcionais (canal de WhatsApp -- **sem eles a ingestao roda igual, so nao
notifica**; ver a secao abaixo):

- `WHATSAPP_TOKEN` -- access token da WhatsApp Cloud API.
- `WHATSAPP_PHONE_NUMBER_ID` -- id do numero remetente (WABA).
- `WHATSAPP_ALERT_TO` -- destinatarios, CSV em formato internacional
  (ex.: `5565999998888,5511988887777`). `+`, espaco, hifen e parenteses sao
  aceitos e limpos pelo codigo.
- `WHATSAPP_TEMPLATE_NAME` / `WHATSAPP_TEMPLATE_LANG` -- nome e idioma do
  template aprovado (defaults: `solar_alarm` / `pt_BR`).

O prefixo (`SEMS_`/`SUNWEG_`) e derivado do fabricante e o sufixo `<REF>` do
`credential_ref` de cada usina -- ver `ingest/run.py::_build_connector`. Uma
mesma usina pode ter credencial em mais de um portal sem ambiguidade, porque
o par (fabricante, credential_ref) e sempre explicito.

## Rodando localmente

```bash
pip install -r requirements.txt
cp .env.example .env   # se existir; senao, defina as mesmas vars do workflow
python -m ingest.run --manufacturer=weg --dry-run
python -m ingest.run --manufacturer=goodwe --dry-run
```

`--dry-run` autentica, busca e normaliza, mas nao escreve no banco. Remova a
flag para gravar de verdade. `--plant <external_plant_id>` roda so uma usina.

Rodar os testes (sem rede, sem banco):

```bash
python -m pytest .
```

## Notificacao por WhatsApp

Cada **transicao** de alarme (`offline` e `zero_generation` abrindo ou
fechando, ver `ingest/alarms.py`) vira uma mensagem de WhatsApp, enviada no
mesmo ciclo de 15 min, logo depois do commit do alarme no Postgres.

**So a borda notifica.** O envio e disparado por `cur.rowcount > 0` na
abertura/fechamento -- e como a abertura e `ON CONFLICT DO NOTHING` e o
fechamento e `WHERE closed_at IS NULL`, um alarme que apenas *continua*
aberto casa 0 linhas e nao reenvia nada. Uma usina offline a manha inteira
gera **uma** mensagem, nao uma a cada 15 min. Nao ha coluna `notified_at`
nem migracao nova: a idempotencia da notificacao e herdada da que o banco ja
garante (este repo nunca altera schema).

Falhar ao notificar **nunca** derruba a ingestao: o envio acontece fora da
transacao e cada destinatario e isolado -- um numero invalido na lista nao
silencia os demais nem marca a usina como falha.

### Por que template (e nao texto livre)

A API oficial da Meta so entrega mensagem **iniciada pelo negocio** como
*template* previamente aprovado; texto livre so funciona nas 24h seguintes a
uma mensagem do proprio destinatario. Alarme e, por definicao, iniciado pelo
negocio -- entao o modo de producao e `template`. O modo `text` existe apenas
para teste manual dentro da janela de 24h.

### Template a criar no WhatsApp Manager

Categoria **Utility**, idioma **pt_BR**, nome **`solar_alarm`** (ou o que
voce puser em `WHATSAPP_TEMPLATE_NAME`), com **5 parametros no corpo**:

```
Alerta solar: {{1}}
Usina: {{2}}
Problema: {{3}}
Severidade: {{4}}
Quando: {{5}}
```

A ordem dos parametros e fixada por `AlarmEvent.template_params()` em
`notifiers/base.py`:

| # | Conteudo | Exemplo |
|---|----------|---------|
| 1 | Status da transicao | `ABERTO` / `RESOLVIDO` |
| 2 | Nome da usina | `Usina Piloto` |
| 3 | Problema | `Geracao zero durante o dia` |
| 4 | Severidade | `critical` |
| 5 | Quando, no fuso da usina | `21/07/2026 11:05` |

Mudar essa ordem no codigo exige **reaprovar** o template na Meta.

### Testando

```bash
python -m ingest.notify              # so valida a configuracao, nao envia
python -m ingest.notify --test       # envia uma mensagem de teste (template)
python -m ingest.notify --test --mode text   # so dentro da janela de 24h
```

Para rodar a ingestao sem notificar (backfill manual, que reprocessa dias
antigos e nao deve reavisar ninguem):

```bash
python -m ingest.run --manufacturer=goodwe --no-whatsapp
```

`--dry-run` tambem nunca envia mensagem.

### Seguranca

Os logs de Actions deste repo sao **publicos** (o repo e publico de proposito,
para ter Actions gratis). O mascaramento automatico do GitHub e por valor
literal do secret, entao ele **nao** cobre um numero individual extraido do
CSV de `WHATSAPP_ALERT_TO` -- por isso o codigo so imprime telefone via
`notifiers.base.mask_phone()` (`*********8888`), e nunca o token nem o corpo
das requisicoes.

## Migracoes de banco

O schema do Postgres (DDL, migracoes numeradas) vive no repositorio privado
e e aplicado manualmente contra o Supabase pelo mantenedor. Este repo so
consome as tabelas ja existentes (`plants`, `readings`, `readings_daily`,
`alarms`, `ingestion_state`, `devices`) -- nunca cria ou altera schema.
