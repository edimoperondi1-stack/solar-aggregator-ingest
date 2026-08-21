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
- `ingest/` -- CLI de ingestao (`run.py`), agregacao diaria (`aggregate.py`)
  e deteccao de alarmes derivados de dados (`alarms.py`).
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

## Migracoes de banco

O schema do Postgres (DDL, migracoes numeradas) vive no repositorio privado
e e aplicado manualmente contra o Supabase pelo mantenedor. Este repo so
consome as tabelas ja existentes (`plants`, `readings`, `readings_daily`,
`alarms`, `ingestion_state`, `devices`) -- nunca cria ou altera schema.
