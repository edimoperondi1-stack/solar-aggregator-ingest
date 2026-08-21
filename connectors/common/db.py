"""connectors/common/db.py -- camada de escrita idempotente (CONN-05, metade).

Todas as funcoes aqui recebem um cursor/conexao psycopg ja aberto (via
`connect_as_ingestion_writer`) e NAO abrem/commitam sua propria transacao --
quem controla commit/rollback e o CLI de ingestao (plano 02-05). Isso deixa
o CLI livre para tratar falha parcial de uma usina (continuar as demais)
sem essas funcoes precisarem saber de loop/orquestracao.

Reusa verbatim o upsert de `readings` ja provado em
db/tests/test_idempotency.py (nao reinventar), adiciona o device sintetico
'plant' (D-07, fallback quando o SEMS nao expoe inversor confiavel) e o
watermark de ingestao por (tenant_id, plant_id, manufacturer).

Seguranca: nenhuma funcao aqui deve logar/imprimir a connection string
(pode conter senha) -- ver notas internas do projeto, "Secrets Handling".
"""

from datetime import datetime

import psycopg

# ---------------------------------------------------------------------------
# readings: upsert idempotente na chave natural (tenant_id, plant_id,
# device_id, ts) -- verbatim de db/tests/test_idempotency.py, ja provado
# contra o Supabase real na Fase 1. NAO reescrever.
# ---------------------------------------------------------------------------
UPSERT_READING_SQL = """
INSERT INTO readings (tenant_id, plant_id, device_id, manufacturer, ts,
                      power_w, energy_day_kwh, energy_total_kwh, status_raw)
VALUES (%(tenant_id)s, %(plant_id)s, %(device_id)s, %(manufacturer)s, %(ts)s,
        %(power_w)s, %(energy_day_kwh)s, %(energy_total_kwh)s, %(status_raw)s)
ON CONFLICT (tenant_id, plant_id, device_id, ts) DO UPDATE SET
  power_w = EXCLUDED.power_w,
  energy_day_kwh = EXCLUDED.energy_day_kwh,
  energy_total_kwh = EXCLUDED.energy_total_kwh,
  status_raw = EXCLUDED.status_raw;
"""

# Mesma logica do upsert acima, mas retornando se a linha foi inserida (True)
# ou atualizada (False), para contar inserted vs updated (D-05: o resumo humano
# do CLI precisa dessas duas contagens).
#
# NAO usar `RETURNING (xmax = 0)` aqui: `readings` e PARTICIONADA (por ts), e o
# Postgres recusa ler colunas de sistema no RETURNING atraves da tabela-pai
# particionada -- erro "cannot retrieve a system column in this context". Isso
# so aparece contra uma usina que REALMENTE gera leituras (a usina piloto tem
# geracao zero, entao este caminho nunca executava e o bug ficou latente ate a
# 1a usina produtiva -- segunda usina, 2026-07-20).
#
# Em vez disso, um CTE data-modifying: `pre` fotografa se a linha ja existia no
# snapshot inicial; o INSERT ... ON CONFLICT roda sempre (CTEs que modificam
# dados executam ate o fim, referenciadas ou nao); `inserted` = a linha NAO
# existia antes. Mesma info que xmax=0, sem tocar coluna de sistema.
UPSERT_READING_RETURNING_SQL = """
WITH pre AS (
  SELECT 1 FROM readings
  WHERE tenant_id = %(tenant_id)s AND plant_id = %(plant_id)s
    AND device_id = %(device_id)s AND ts = %(ts)s
),
upserted AS (
  INSERT INTO readings (tenant_id, plant_id, device_id, manufacturer, ts,
                        power_w, energy_day_kwh, energy_total_kwh, status_raw)
  VALUES (%(tenant_id)s, %(plant_id)s, %(device_id)s, %(manufacturer)s, %(ts)s,
          %(power_w)s, %(energy_day_kwh)s, %(energy_total_kwh)s, %(status_raw)s)
  ON CONFLICT (tenant_id, plant_id, device_id, ts) DO UPDATE SET
    power_w = EXCLUDED.power_w,
    energy_day_kwh = EXCLUDED.energy_day_kwh,
    energy_total_kwh = EXCLUDED.energy_total_kwh,
    status_raw = EXCLUDED.status_raw
  RETURNING 1
)
SELECT NOT EXISTS (SELECT 1 FROM pre) AS inserted;
"""

# ---------------------------------------------------------------------------
# devices: upsert idempotente na chave natural (tenant_id, plant_id,
# external_device_id). DO NOTHING em conflito (o device ja existe e nao
# muda) -- se nao retornar id, um SELECT resolve pela chave natural.
# ---------------------------------------------------------------------------
UPSERT_DEVICE_SQL = """
INSERT INTO devices (tenant_id, plant_id, manufacturer, external_device_id, device_kind)
VALUES (%(tenant_id)s, %(plant_id)s, %(manufacturer)s, %(external_device_id)s, %(device_kind)s)
ON CONFLICT (tenant_id, plant_id, external_device_id) DO NOTHING
RETURNING id;
"""

SELECT_DEVICE_ID_SQL = """
SELECT id FROM devices
WHERE tenant_id = %(tenant_id)s
  AND plant_id = %(plant_id)s
  AND external_device_id = %(external_device_id)s;
"""

# ---------------------------------------------------------------------------
# ingestion_state: watermark por (tenant_id, plant_id, manufacturer).
# ---------------------------------------------------------------------------
UPSERT_WATERMARK_SQL = """
INSERT INTO ingestion_state (tenant_id, plant_id, manufacturer, last_watermark, last_run_at, last_status)
VALUES (%(tenant_id)s, %(plant_id)s, %(manufacturer)s, %(watermark)s, now(), %(status)s)
ON CONFLICT (tenant_id, plant_id, manufacturer) DO UPDATE SET
  last_watermark = EXCLUDED.last_watermark,
  last_run_at = now(),
  last_status = EXCLUDED.last_status;
"""


def connect_as_ingestion_writer(conn_str: str) -> psycopg.Connection:
    """Conecta como a role de menor privilegio `ingestion_writer`.

    Sempre via pooler Supavisor em TRANSACTION mode (porta 6543) --
    `prepare_threshold=None` e obrigatorio nesse modo: sem isso, o
    comportamento default do psycopg3 de preparar statements no lado do
    servidor conflita com o jeito que o Supavisor recicla conexoes entre
    transacoes (cada transacao pode cair numa conexao fisica diferente do
    Postgres, entao um prepared statement "lembrado" pelo psycopg pode nao
    existir na conexao fisica seguinte). Ver notas internas do projeto.
    """
    return psycopg.connect(conn_str, prepare_threshold=None)


def get_plants_by_manufacturer(
    cur, manufacturer: str, only_external_id: str | None = None
) -> list[dict]:
    """Le as usinas cadastradas (Fase 1, a mao) para um fabricante.

    `only_external_id`, se informado, filtra para uma unica usina --
    o suporte a flag `--plant <external_plant_id>` do CLI (D-04).
    """
    if only_external_id is not None:
        cur.execute(
            "SELECT id, tenant_id, external_plant_id, name, timezone, capacity_kwp, credential_ref "
            "FROM plants WHERE manufacturer = %s AND external_plant_id = %s",
            (manufacturer, only_external_id),
        )
    else:
        cur.execute(
            "SELECT id, tenant_id, external_plant_id, name, timezone, capacity_kwp, credential_ref "
            "FROM plants WHERE manufacturer = %s",
            (manufacturer,),
        )
    columns = [desc.name for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def resolve_device_id(
    cur,
    tenant_id: int,
    plant_id: int,
    manufacturer: str,
    external_device_id: str,
    device_kind: str,
) -> int:
    """Upsert idempotente de um device pela chave natural; NUNCA retorna None.

    `ON CONFLICT DO NOTHING` nao retorna linha quando o device ja existe --
    nesse caso, um SELECT pela mesma chave natural resolve o id existente.
    """
    params = dict(
        tenant_id=tenant_id,
        plant_id=plant_id,
        manufacturer=manufacturer,
        external_device_id=external_device_id,
        device_kind=device_kind,
    )
    cur.execute(UPSERT_DEVICE_SQL, params)
    row = cur.fetchone()
    if row is None:
        cur.execute(SELECT_DEVICE_ID_SQL, params)
        row = cur.fetchone()
    return row[0]


def resolve_plant_device(
    cur,
    tenant_id: int,
    plant_id: int,
    manufacturer: str,
    external_plant_id: str,
    inverters: list[dict],
) -> list[tuple[int, dict | None]]:
    """Resolve o(s) device_id(s) que as leituras desta usina devem usar.

    Caso normal da usina piloto hoje (D-07): `inverters` vazio -> device sintetico
    `plant`, `external_device_id='PLANT:<external_plant_id>'`,
    `device_kind='plant'`. Uma lista vazia de inversores e ESTADO NORMAL
    (usina offline/sem inversor vinculado), nunca erro -- ver Pitfall B do
    02-RESEARCH.md; nao copiar o sys.exit() do pygoodwe.

    Quando a usina expuser inversores reais, um device por inversor
    (`device_kind='inverter'`, `external_device_id=<sn do inversor>`).

    NUNCA retorna lista vazia -- readings.device_id e NOT NULL.
    """
    if not inverters:
        device_id = resolve_device_id(
            cur,
            tenant_id,
            plant_id,
            manufacturer,
            external_device_id=f"PLANT:{external_plant_id}",
            device_kind="plant",
        )
        return [(device_id, None)]

    resolved: list[tuple[int, dict | None]] = []
    for inverter in inverters:
        device_id = resolve_device_id(
            cur,
            tenant_id,
            plant_id,
            manufacturer,
            external_device_id=inverter["sn"],
            device_kind="inverter",
        )
        resolved.append((device_id, inverter))
    return resolved


def upsert_readings(cur, readings: list[dict]) -> tuple[int, int]:
    """Upsert idempotente de leituras; retorna (inserted, updated) para o
    resumo humano do CLI (D-05).

    Executa uma linha por vez (nao executemany) para poder ler o
    `RETURNING (xmax = 0) AS inserted` de cada linha individualmente --
    executemany do psycopg3 nao expoe resultados por linha de forma
    conveniente quando cada execucao pode ter tomado o caminho INSERT ou
    UPDATE. Para o volume desta fase (uma usina/dia, ~96 pontos de 15 min
    no maximo) o custo por-linha e desprezivel.
    """
    inserted = 0
    updated = 0
    for reading in readings:
        cur.execute(UPSERT_READING_RETURNING_SQL, reading)
        (was_inserted,) = cur.fetchone()
        if was_inserted:
            inserted += 1
        else:
            updated += 1
    return inserted, updated


def get_watermark(
    cur, tenant_id: int, plant_id: int, manufacturer: str
) -> datetime | None:
    """Le o ultimo watermark gravado para (tenant, plant, manufacturer).

    Retorna None se ainda nao houver linha em `ingestion_state` (primeira
    execucao para essa usina).
    """
    cur.execute(
        "SELECT last_watermark FROM ingestion_state "
        "WHERE tenant_id = %s AND plant_id = %s AND manufacturer = %s",
        (tenant_id, plant_id, manufacturer),
    )
    row = cur.fetchone()
    return row[0] if row is not None else None


def update_watermark(
    cur,
    tenant_id: int,
    plant_id: int,
    manufacturer: str,
    watermark: datetime,
    status: str,
) -> None:
    """Avanca (ou cria) o watermark de ingestao para (tenant, plant, manufacturer).

    O CLI so deve chamar isto apos uma usina processar com sucesso (falha
    parcial de uma usina no loop manual nao avanca o watermark dela --
    ver CONTEXT.md "Claude's Discretion").
    """
    cur.execute(
        UPSERT_WATERMARK_SQL,
        dict(
            tenant_id=tenant_id,
            plant_id=plant_id,
            manufacturer=manufacturer,
            watermark=watermark,
            status=status,
        ),
    )
