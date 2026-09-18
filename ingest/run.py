"""ingest/run.py -- CLI manual de ingestao GoodWe (D-03/D-04/D-05, plano 02-05).

Fecha CONN-02/CONN-03/CONN-05 end-to-end: le usinas de `plants` filtradas por
`manufacturer`, autentica UMA vez (Pitfall D), descobre devices (D-06/D-07),
busca+normaliza (02-03/02-04) e grava idempotentemente via ingestion_writer
(02-02). Imprime um resumo legivel por humano (D-05) suficiente para o
usuario se convencer de CONN-05 (2a execucao -> 0 linhas novas).

Uso:
    python -m ingest.run --manufacturer=goodwe
    python -m ingest.run --manufacturer=goodwe --dry-run
    python -m ingest.run --manufacturer=goodwe --plant <external_plant_id>
    python -m ingest.run --manufacturer=goodwe --lookback-days=2
    python -m ingest.run --manufacturer=goodwe --no-whatsapp

Fase 3 adiciona a janela self-healing (--lookback-days) e passa a rodar este
mesmo CLI via GitHub Actions cron. Fase 4 (04-03, D-16) adiciona
`detect_and_apply_alarms()` como passo pos-ingestao por usina: deteccao +
abertura/fechamento idempotente dos alarmes `offline`/`zero_generation`
(METR-03), rodando logo apos o watermark de cada usina ser avancado com
sucesso, isolado por usina (ver ingest/alarms.py).

A Fase de notificacao (NOTI-01) pendura um passo a mais nesse mesmo ponto:
cada TRANSICAO de alarme (abriu/fechou) vira uma mensagem de WhatsApp, enviada
DEPOIS do commit e fora do try/except de alarme (ver main()). O canal e
opcional -- sem os secrets WHATSAPP_* o CLI roda exatamente como antes. Use
`--no-whatsapp` para silenciar um backfill manual. Ver ingest/notify.py.

SECURITY: nunca logar/imprimir token, senha ou connection string (ver notas
internas do projeto, "Secrets Handling"). Apenas nomes de usina,
external_plant_id, timezone, contagens e status sao impressos.
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from connectors.base import Connector
from connectors.common.db import (
    connect_as_ingestion_writer,
    get_plants_by_manufacturer,
    get_watermark,
    update_watermark,
    upsert_readings,
)
from connectors.goodwe.client import SemsClient
from connectors.goodwe.connector import GoodWeConnector
from connectors.sunweg.client import SunWegClient
from connectors.sunweg.connector import SunWegConnector
from ingest.alarms import (
    ZERO_GEN_MARGIN_HOURS,
    close_alarm,
    fetch_power_readings,
    is_offline,
    is_zero_generation,
    open_alarm,
    within_solar_window,
)
from ingest.notify import build_notifier_from_env
from ingest.solar_window import get_plant_coords, solar_window_utc
from notifiers.base import AlarmEvent


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ingest.run",
        description=(
            "Ingestao manual (D-03): le usinas de `plants`, autentica uma vez, "
            "descobre devices, busca+normaliza e grava idempotentemente."
        ),
    )
    parser.add_argument(
        "--manufacturer",
        default="goodwe",
        help="Fabricante a processar (default: goodwe)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Autentica, busca e normaliza, imprime o que gravaria -- SEM "
            "escrever no banco (D-04)."
        ),
    )
    parser.add_argument(
        "--plant",
        default=None,
        metavar="EXTERNAL_PLANT_ID",
        help="Roda so esta usina (external_plant_id) -- D-04.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=2,
        metavar="N",
        help=(
            "Quantos dias calendario buscar por usina, terminando hoje no fuso "
            "da usina (default: 2 = hoje + ontem). Janela self-healing contra "
            "atrasos/pulos do cron best-effort do GitHub Actions (INGE-02/D-02); "
            "a sobreposicao e deduplicada pelo upsert idempotente."
        ),
    )
    parser.add_argument(
        "--no-whatsapp",
        action="store_true",
        help=(
            "Desliga a notificacao WhatsApp nesta execucao, mesmo com os "
            "secrets presentes (a deteccao/gravacao de alarme continua "
            "normal). Util para backfill manual, que reprocessa dias antigos "
            "e nao deve reavisar ninguem."
        ),
    )
    return parser.parse_args(argv)


# D-03: o prefixo do secret e derivado do plants.manufacturer, nunca hardcoded.
# `credential_ref` continua significando apenas "de qual CLIENTE e esta
# credencial" (Fase 03.1 D-01) -- por isso uma usina pode ter credencial nos
# DOIS portais (SEMS_ACCOUNT_<REF> e SUNWEG_ACCOUNT_<REF>) sem ambiguidade
# nenhuma.
# Adicionar um fabricante novo aqui e no `if` de _build_connector abaixo; nao
# ha registry dinamico porque sao 2 fabricantes, e um dict + if explicito e mais
# facil de ler do que qualquer abstracao.
_MANUFACTURER_SECRET_PREFIX = {
    "goodwe": "SEMS",
    "weg": "SUNWEG",
}


def _build_connector(manufacturer: str, credential_ref: str) -> Connector:
    """Monta o conector do fabricante desta usina, lendo as credenciais de env
    (nunca hardcoded). O prefixo do secret vem de `manufacturer` (D-03):
    goodwe -> SEMS_ACCOUNT_<REF>/SEMS_PASSWORD_<REF>,
    weg    -> SUNWEG_ACCOUNT_<REF>/SUNWEG_PASSWORD_<REF>, ref em maiusculas.
    Nao imprime nenhum valor de account/password -- so o resultado da
    autenticacao (ver main()).

    Levanta ValueError (mensagem nomeando o fabricante) para fabricante sem
    conector, e KeyError com mensagem acionavel para secret ausente. Ambos sao
    capturados pelo try/except por grupo de credencial em main(), entao
    derrubam SO aquele grupo -- isolamento da Fase 03.1 D-03, agora tambem na
    dimensao fabricante."""
    prefix = _MANUFACTURER_SECRET_PREFIX.get(manufacturer)
    if prefix is None:
        raise ValueError(
            f"fabricante sem conector implementado: {manufacturer!r} "
            f"(conhecidos: {sorted(_MANUFACTURER_SECRET_PREFIX)})"
        )
    ref = credential_ref.upper()
    account_var = f"{prefix}_ACCOUNT_{ref}"
    password_var = f"{prefix}_PASSWORD_{ref}"
    try:
        account = os.environ[account_var]
        password = os.environ[password_var]
    except KeyError as exc:
        raise KeyError(
            f"secret ausente para manufacturer={manufacturer} "
            f"credential_ref={credential_ref}: {exc}. "
            f"Esperado {account_var} e {password_var} no ambiente/.env."
        ) from exc

    if manufacturer == "goodwe":
        return GoodWeConnector(SemsClient(account, password))
    if manufacturer == "weg":
        return SunWegConnector(SunWegClient(account, password))
    # Inalcancavel enquanto _MANUFACTURER_SECRET_PREFIX e os ifs estiverem em
    # sincronia -- guarda contra alguem adicionar so no dict.
    raise ValueError(f"fabricante sem conector implementado: {manufacturer!r}")


def _device_label(inv: dict | None, external_plant_id: str) -> tuple[str, str]:
    """Espelha a convencao de nomes de connectors/common/db.py
    `resolve_plant_device` (D-06/D-07) so para fins de exibicao -- nao
    reconsulta o banco. `inv is None` => device sintetico de nivel-usina."""
    if inv is None:
        return "plant", f"PLANT:{external_plant_id}"
    return "inverter", str(inv.get("sn"))


def _local_today(plant_tz: str) -> date:
    """Dia calendario CORRENTE no fuso IANA da usina (nunca UTC nem offset
    hardcoded)."""
    return datetime.now(ZoneInfo(plant_tz)).date()


def _window_days(plant_tz: str, lookback_days: int) -> list[date]:
    """Janela self-healing (INGE-02 / D-02): os `lookback_days` dias
    calendario terminando HOJE no fuso da usina, do mais recente para o mais
    antigo.

    O endpoint intradiario do SEMS e POR-DIA, entao "alargar a janela" =
    buscar N dias e deixar o upsert idempotente (chave natural
    (tenant_id, plant_id, device_id, ts)) deduplicar a sobreposicao.
    Com default 2 (hoje + ontem) qualquer atraso/pulo do cron best-effort
    dentro de ~24h e recuperado na run seguinte, inclusive a virada de
    meia-noite (run das 00:07 ainda pega a cauda de ontem).
    """
    if lookback_days < 1:
        raise ValueError("lookback_days deve ser >= 1")
    today = _local_today(plant_tz)
    return [today - timedelta(days=i) for i in range(lookback_days)]


def detect_and_apply_alarms(
    cur, plant: dict, now_utc: datetime, manufacturer: str
) -> dict:
    """Passo pos-ingestao (D-16): avalia offline (D-10) e zero_generation
    (D-11) para esta usina e abre/fecha os alarmes idempotentemente (D-12),
    ambos no nivel da usina (device_id=None). So deve ser chamado DEPOIS de
    `update_watermark(..., status="ok")` + `conn.commit()` desta usina, com
    dados/watermark frescos em maos.

    Sem coordenadas (`get_plant_coords` -> None): loga e retorna sem fazer
    nada (Pitfall 2 -- nunca usar (0,0), nunca lancar excecao). Fora da
    janela solar: nao faz nada em nenhum dos dois alarmes -- nunca fecha
    (nem abre) de noite.

    Retorna {"opened": N, "closed": N, "events": [AlarmEvent, ...]}. Os dois
    contadores continuam sendo linhas de fato afetadas (via cur.rowcount);
    `events` e a lista das TRANSICOES correspondentes, para notificacao.

    `cur.rowcount > 0` e o que torna a notificacao nao-repetitiva de graca:
    a abertura e `ON CONFLICT DO NOTHING` e o fechamento e `WHERE closed_at
    IS NULL`, entao um alarme que apenas PERMANECE aberto casa 0 linhas e
    nao gera evento nenhum. Uma usina offline por 6h notifica uma vez, nao a
    cada 15 min. Quem envia e `main()`, e so DEPOIS do commit -- daqui nao
    sai nenhuma chamada de rede.
    """
    counts: dict = {"opened": 0, "closed": 0, "events": []}

    tenant_id = plant["tenant_id"]
    plant_id = plant["id"]
    external_plant_id = plant["external_plant_id"]
    plant_tz = plant["timezone"]
    name = plant["name"]

    def _record(transition: str, alarm_type: str, severity: str) -> None:
        """Contabiliza a transicao e guarda o evento correspondente."""
        counts[transition] += 1
        counts["events"].append(
            AlarmEvent(
                transition=transition,
                plant_name=name,
                external_plant_id=external_plant_id,
                manufacturer=manufacturer,
                alarm_type=alarm_type,
                severity=severity,
                at_utc=now_utc,
                plant_tz=plant_tz,
            )
        )

    coords = get_plant_coords(external_plant_id)
    if coords is None:
        print(f"  (sem coordenadas para {name} -- pulando deteccao de alarme)")
        return counts
    lat, lon = coords

    local_today = _local_today(plant_tz)

    # OFFLINE (D-10): janela solar sem margem.
    sunrise0, sunset0 = solar_window_utc(lat, lon, plant_tz, local_today, margin_hours=0.0)
    if within_solar_window(now_utc, sunrise0, sunset0):
        last = get_watermark(cur, tenant_id, plant_id, manufacturer)
        if is_offline(last, now_utc):
            open_alarm(
                cur,
                tenant_id=tenant_id,
                plant_id=plant_id,
                device_id=None,
                manufacturer=manufacturer,
                alarm_type="offline",
                severity="warning",
                opened_at=now_utc,
            )
            if cur.rowcount:
                _record("opened", "offline", "warning")
        else:
            close_alarm(
                cur,
                tenant_id=tenant_id,
                plant_id=plant_id,
                device_id=None,
                alarm_type="offline",
                closed_at=now_utc,
            )
            if cur.rowcount:
                _record("closed", "offline", "warning")

    # ZERO_GENERATION (D-11): janela solar COM margem (exclui sol raso).
    sunriseM, sunsetM = solar_window_utc(
        lat, lon, plant_tz, local_today, margin_hours=ZERO_GEN_MARGIN_HOURS
    )
    if within_solar_window(now_utc, sunriseM, sunsetM):
        power_values = fetch_power_readings(cur, plant_id, sunriseM, sunsetM)
        if is_zero_generation(power_values):
            open_alarm(
                cur,
                tenant_id=tenant_id,
                plant_id=plant_id,
                device_id=None,
                manufacturer=manufacturer,
                alarm_type="zero_generation",
                severity="critical",
                opened_at=now_utc,
            )
            if cur.rowcount:
                _record("opened", "zero_generation", "critical")
        else:
            close_alarm(
                cur,
                tenant_id=tenant_id,
                plant_id=plant_id,
                device_id=None,
                alarm_type="zero_generation",
                closed_at=now_utc,
            )
            if cur.rowcount:
                _record("closed", "zero_generation", "critical")

    return counts


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _parse_args(argv)
    now_utc = datetime.now(timezone.utc)  # instante unico do ciclo (D-16)

    # Canal de notificacao (NOTI-01): construido UMA vez por run, fora do loop
    # -- e um httpx.Client, nao precisa de um por usina.
    # --dry-run nunca notifica: D-04 promete "nenhum efeito colateral", e uma
    # mensagem de WhatsApp e o efeito colateral mais visivel que existe.
    # Sem secrets configurados, build_notifier_from_env() devolve None e tudo
    # abaixo vira no-op -- o canal e estritamente opcional.
    if args.dry_run or args.no_whatsapp:
        notifier = None
        print("WhatsApp desativado nesta execucao (--dry-run/--no-whatsapp)")
    else:
        notifier = build_notifier_from_env()

    conn = connect_as_ingestion_writer(os.environ["SUPABASE_INGESTION_URL"])
    conn.autocommit = False
    cur = conn.cursor()

    try:
        plants = get_plants_by_manufacturer(
            cur, args.manufacturer, only_external_id=args.plant
        )
        print(f"Usinas ({args.manufacturer}): {len(plants)}")
        for plant in plants:
            print(
                f"  - {plant['name']} (external_plant_id={plant['external_plant_id']}, "
                f"tz={plant['timezone']}, credential_ref={plant['credential_ref']})"
            )

        # Agrupa por credential_ref (MTEN-02) preservando a ordem de chegada
        # do SELECT -- um login por grupo, corpo por-usina abaixo inalterado.
        plants_by_cred: dict[str, list[dict]] = {}
        for plant in plants:
            plants_by_cred.setdefault(plant["credential_ref"], []).append(plant)

        total_devices = 0
        total_normalized = 0
        total_inserted = 0
        total_updated = 0
        failures = 0
        # Alarmes (METR-03, D-16): abertos/fechados neste ciclo + falhas do
        # passo de alarme isoladas por usina (nao contam como falha de ingestao).
        total_alarms_opened = 0
        total_alarms_closed = 0
        alarm_failures = 0
        # Notificacoes (NOTI-01): mensagens que a Meta ACEITOU neste ciclo, e
        # falhas do passo de envio -- isoladas, nunca contam como falha de
        # ingestao nem de alarme.
        total_notified = 0
        notify_failures = 0
        # Status por conta (MTEN-03): consumido pelo alerta por-conta do
        # Plano 04. Nunca gravado no Postgres (D-06) -- so stdout/$GITHUB_OUTPUT.
        account_status: dict[str, str] = {}

        for credential_ref, group_plants in plants_by_cred.items():
            try:
                connector = _build_connector(args.manufacturer, credential_ref)
                connector.authenticate()
                print(f"login OK ({args.manufacturer}/{credential_ref})")
            except Exception as exc:  # falha de login isola so este grupo (D-03)
                failures += len(group_plants)
                account_status[credential_ref] = "error"
                print(
                    f"FALHA de login {args.manufacturer}/{credential_ref}: {exc} -- "
                    f"pulando {len(group_plants)} usina(s)"
                )
                for plant in group_plants:
                    tenant_id = plant["tenant_id"]
                    plant_id = plant["id"]
                    existing_watermark = get_watermark(
                        cur, tenant_id, plant_id, args.manufacturer
                    )
                    try:
                        update_watermark(
                            cur,
                            tenant_id,
                            plant_id,
                            args.manufacturer,
                            existing_watermark,
                            status="error",
                        )
                        conn.commit()
                    except Exception as inner_exc:
                        conn.rollback()
                        print(
                            f"  (nao foi possivel registrar status='error' no "
                            f"watermark de {plant['name']}: {inner_exc})"
                        )
                continue

            group_failures = 0

            for plant in group_plants:
                tenant_id = plant["tenant_id"]
                plant_id = plant["id"]
                external_plant_id = plant["external_plant_id"]
                plant_tz = plant["timezone"]
                name = plant["name"]

                # Lido ANTES do try para poder registrar status='error' (sem
                # avancar last_watermark) mesmo se a usina falhar no meio.
                existing_watermark = get_watermark(
                    cur, tenant_id, plant_id, args.manufacturer
                )

                try:
                    devices = connector.discover_devices(
                        cur, tenant_id, plant_id, external_plant_id
                    )
                    total_devices += len(devices)
                    for device_id, inv in devices:
                        kind, ext_id = _device_label(inv, external_plant_id)
                        print(
                            f"  device: kind={kind} external_device_id={ext_id} "
                            f"(device_id={device_id})"
                        )

                    days = _window_days(plant_tz, args.lookback_days)
                    print(
                        f"  janela buscada: {days[-1].isoformat()}..{days[0].isoformat()} "
                        f"({len(days)}d, local {plant_tz})"
                    )

                    raw: list[dict] = []
                    for day in days:
                        day_raw = connector.fetch_readings(external_plant_id, day)
                        print(f"    {day.isoformat()}: {len(day_raw)} pontos brutos")
                        raw.extend(day_raw)
                    print(f"  leituras brutas recebidas: {len(raw)}")

                    plant_normalized = 0
                    plant_inserted = 0
                    plant_updated = 0
                    max_ts = None

                    for device_id, inv in devices:
                        readings = connector.normalize(
                            raw,
                            tenant_id=tenant_id,
                            plant_id=plant_id,
                            device_id=device_id,
                            plant_tz=plant_tz,
                        )
                        plant_normalized += len(readings)
                        if readings:
                            device_max_ts = max(r.ts for r in readings)
                            max_ts = (
                                device_max_ts
                                if max_ts is None
                                else max(max_ts, device_max_ts)
                            )

                        if not args.dry_run:
                            inserted, updated = upsert_readings(
                                cur, [r.model_dump() for r in readings]
                            )
                            plant_inserted += inserted
                            plant_updated += updated

                    total_normalized += plant_normalized

                    if args.dry_run:
                        print(
                            f"  usina {name}: {plant_normalized} leituras normalizadas "
                            "(dry-run, nada gravado)"
                        )
                        # Desfaz QUALQUER escrita desta usina nesta transacao
                        # (inclusive o upsert idempotente de devices feito por
                        # discover_devices) -- D-04 exige zero escrita em
                        # --dry-run, nao so as leituras.
                        conn.rollback()
                    else:
                        new_watermark = (
                            max_ts if max_ts is not None else existing_watermark
                        )
                        update_watermark(
                            cur,
                            tenant_id,
                            plant_id,
                            args.manufacturer,
                            new_watermark,
                            status="ok",
                        )
                        conn.commit()
                        total_inserted += plant_inserted
                        total_updated += plant_updated
                        print(
                            f"  usina {name}: {plant_normalized} leituras normalizadas, "
                            f"inserted={plant_inserted} updated={plant_updated}"
                        )

                        # Passo pos-ingestao de alarmes (METR-03/D-16): roda
                        # com watermark ja fresco e commitado acima. Try/except
                        # PROPRIO (nao o da usina inteira) -- uma falha aqui so
                        # desfaz o trabalho de alarme desta usina, nunca a
                        # ingestao ja commitada nem o watermark ja avancado.
                        alarm_events = []
                        try:
                            alarm_counts = detect_and_apply_alarms(
                                cur, plant, now_utc, args.manufacturer
                            )
                            conn.commit()
                            total_alarms_opened += alarm_counts["opened"]
                            total_alarms_closed += alarm_counts["closed"]
                            alarm_events = alarm_counts["events"]
                        except Exception as alarm_exc:
                            alarm_failures += 1
                            conn.rollback()
                            print(
                                f"  (FALHA na deteccao de alarme de {name}: {alarm_exc})"
                            )

                        # Notificacao (NOTI-01) -- DEPOIS do commit acima e
                        # FORA do try/except de alarme, de proposito:
                        #  (a) depois do commit, para nunca avisar sobre um
                        #      alarme que acabou em rollback (mensagem enviada
                        #      nao tem desfazer);
                        #  (b) fora daquele try, para um erro de rede do
                        #      WhatsApp nao disparar o rollback() de la (a
                        #      transacao ja fechou) nem ser contado como falha
                        #      de deteccao de alarme.
                        # `alarm_events` so tem as BORDAS (rowcount > 0), entao
                        # um alarme que apenas continua aberto nao reenvia nada.
                        if notifier is not None and alarm_events:
                            try:
                                total_notified += notifier.send_all(alarm_events)
                            except Exception as notify_exc:
                                # send_all ja e a prova de falhas por
                                # destinatario; este except cobre so o
                                # imprevisto, e ainda assim nao derruba a usina.
                                notify_failures += 1
                                print(
                                    f"  (FALHA ao notificar alarmes de {name}: "
                                    f"{notify_exc})"
                                )

                except Exception as exc:  # falha parcial nao aborta as demais
                    failures += 1
                    group_failures += 1
                    conn.rollback()
                    print(f"  FALHA na usina {name}: {exc}")
                    try:
                        update_watermark(
                            cur,
                            tenant_id,
                            plant_id,
                            args.manufacturer,
                            existing_watermark,
                            status="error",
                        )
                        conn.commit()
                    except Exception as inner_exc:
                        conn.rollback()
                        print(
                            f"  (nao foi possivel registrar status='error' no "
                            f"watermark de {name}: {inner_exc})"
                        )
                    continue

            # Grupo so e "ok" se o login teve sucesso E nenhuma usina do
            # grupo caiu no except acima (MTEN-03 -- granularidade por-conta).
            account_status[credential_ref] = "ok" if group_failures == 0 else "error"

        print()
        print("=== RESUMO ===")
        # Marcador estavel de qual fabricante este processo rodou. Sob a matrix
        # do ingest.yml (D-05) ha dois jobs por run com logs separados; este
        # marcador torna impossivel confundir os logs ao investigar uma falha.
        # NAO substitui INGEST_ACCOUNT_STATUS: (esse continua no formato antigo,
        # porque o step de alerta do ingest.yml casa por substring exata).
        print(f"INGEST_MANUFACTURER:{args.manufacturer}")
        print(f"Usinas processadas: {len(plants)} (falhas: {failures})")
        print(f"Janela por usina: {args.lookback_days} dia(s) (INGE-02)")
        print(f"Devices descobertos: {total_devices}")
        print(f"Leituras normalizadas: {total_normalized}")
        if args.dry_run:
            print("(dry-run, nada gravado)")
        else:
            print(f"inserted={total_inserted} updated={total_updated}")
            if total_inserted == 0:
                print("IDEMPOTENCY OK: 0 new rows")
            else:
                print(f"FIRST RUN: {total_inserted} new rows")
            print(
                f"Alarmes (METR-03): opened={total_alarms_opened} "
                f"closed={total_alarms_closed} (falhas: {alarm_failures})"
            )
            print(
                f"Notificacoes WhatsApp (NOTI-01): enviadas={total_notified} "
                f"(falhas: {notify_failures})"
            )
        for ref, status in account_status.items():
            print(f"conta {ref}: {status}")

        # Marcador de status por conta (MTEN-03), fora do Postgres (D-06) --
        # sobrevive a uma queda do Supabase. (1) stdout, estavel e parseavel
        # por varredura de logs de runs historicos (Plano 04); (2) quando
        # rodando em CI, tambem grava em $GITHUB_OUTPUT para o step de alerta
        # consumir sem reabrir logs.
        for ref, status in account_status.items():
            print(f"INGEST_ACCOUNT_STATUS:{ref.upper()}:{status}")

        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as fh:
                for ref, status in account_status.items():
                    key = "acct_" + ref.lower()
                    fh.write(f"{key}={status}\n")
                fh.write(
                    "accounts=" + ",".join(r.lower() for r in account_status) + "\n"
                )
                # Cruzamento de conferencia para o step de alerta do plano
                # 06-08: steps.ingest.outputs.manufacturer deve bater com
                # matrix.manufacturer daquele job.
                fh.write(f"manufacturer={args.manufacturer}\n")

        # Falha parcial NAO derruba o run (visibilidade por-conta vem das
        # Issues do Plano 04). Exit 1 somente se HOUVE grupos e TODOS
        # terminaram "error" -- outage total merece run vermelho (INGE-04).
        if account_status and all(status == "error" for status in account_status.values()):
            return 1
        return 0
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
