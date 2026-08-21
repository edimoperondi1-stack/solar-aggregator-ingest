"""ingest/run_test.py -- proves _window_days(), _parse_args() (plan 03-01,
Task 1) and _build_connector() (plan 06-07, Task 1, D-03).

Covers the 7 <behavior> cases from 03-01-PLAN.md (INGE-02/D-02: janela
self-healing hoje+ontem contra atrasos/pulos do cron best-effort do GitHub
Actions):
  1. _window_days("America/Cuiaba", 2) retorna [hoje_local, hoje_local - 1 dia]
  2. _window_days("America/Cuiaba", 1) retorna [hoje_local] (paridade Fase 2)
  3. _window_days("America/Cuiaba", 3) retorna 3 datas consecutivas decrescentes
  4. _window_days levanta ValueError se lookback_days < 1
  5. _parse_args([]) retorna lookback_days == 2 (default)
  6. _parse_args(["--lookback-days", "1"]) retorna lookback_days == 1
  7. O fuso importa: _window_days(tz, 1)[0] bate com
     datetime.now(ZoneInfo(tz)).date() do respectivo fuso, nunca com
     date.today() do runner

Mais 6 casos de 06-07-PLAN.md Task 1 (D-03: prefixo do secret e o conector
derivam de plants.manufacturer, nunca hardcoded). O credential_ref usado
nestes testes ("piloto") e um valor arbitrario de fixture -- nao precisa
bater com nenhuma credencial real, so exercita o mapeamento
manufacturer -> prefixo de env var:
  8. Com SUNWEG_ACCOUNT_PILOTO/SUNWEG_PASSWORD_PILOTO no ambiente,
     _build_connector("weg", "piloto") devolve SunWegConnector, manufacturer="weg"
  9. Com SEMS_ACCOUNT_PILOTO/SEMS_PASSWORD_PILOTO no ambiente,
     _build_connector("goodwe", "piloto") devolve GoodWeConnector, manufacturer="goodwe"
  10. _build_connector("weg", "piloto") le SUNWEG_ACCOUNT_PILOTO e NAO
      SEMS_ACCOUNT_PILOTO -- provado deixando SO a variavel do SEMS no
      ambiente
  11. _build_connector("solis", "piloto") levanta ValueError mencionando "solis"
  12. _build_connector("weg", "piloto") com o ambiente VAZIO levanta excecao
      nomeando SUNWEG_ACCOUNT_PILOTO (mensagem acionavel, nao KeyError cru)
  13. _MANUFACTURER_SECRET_PREFIX == {"goodwe": "SEMS", "weg": "SUNWEG"}

Usage: `python -m pytest ingest/run_test.py` or `python ingest/run_test.py`
(direct-script fallback, no pytest required).
"""

import os
import sys
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

# Allows running this file directly (`python ingest/run_test.py`) as well as
# via `python -m pytest` -- makes the `ingest` package importable when
# sys.path[0] would otherwise be ingest/, not the repo root. ingest/ is 1
# level below root (unlike connectors/goodwe/), so ONE fewer dirname than
# connectors/goodwe/connector_test.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connectors.goodwe.connector import GoodWeConnector
from connectors.sunweg.connector import SunWegConnector
from ingest.run import _MANUFACTURER_SECRET_PREFIX, _build_connector, _parse_args, _window_days

CUIABA = "America/Cuiaba"  # pilot plant -- UTC-4, no DST (Pitfall E)


def test_window_days_default_two_returns_today_and_yesterday():
    today = datetime.now(ZoneInfo(CUIABA)).date()
    days = _window_days(CUIABA, 2)
    assert days == [today, today - timedelta(days=1)]


def test_window_days_one_returns_only_today_parity_with_phase2():
    today = datetime.now(ZoneInfo(CUIABA)).date()
    days = _window_days(CUIABA, 1)
    assert days == [today]


def test_window_days_three_returns_three_consecutive_decreasing_no_repeat():
    today = datetime.now(ZoneInfo(CUIABA)).date()
    days = _window_days(CUIABA, 3)
    assert days == [today, today - timedelta(days=1), today - timedelta(days=2)]
    assert len(set(days)) == 3


def test_window_days_raises_valueerror_for_lookback_below_one():
    try:
        _window_days(CUIABA, 0)
        raise AssertionError("expected ValueError for lookback_days=0")
    except ValueError:
        pass

    try:
        _window_days(CUIABA, -1)
        raise AssertionError("expected ValueError for lookback_days=-1")
    except ValueError:
        pass


def test_parse_args_default_lookback_days_is_two():
    args = _parse_args([])
    assert args.lookback_days == 2


def test_parse_args_explicit_lookback_days_one():
    args = _parse_args(["--lookback-days", "1"])
    assert args.lookback_days == 1


def test_window_days_uses_plant_timezone_not_runner_local_date():
    # O fuso importa: cada janela deve bater com datetime.now(ZoneInfo(tz))
    # do RESPECTIVO fuso -- nunca com date.today() (fuso do runner/host).
    for tz in (CUIABA, "Pacific/Kiritimati"):
        expected = datetime.now(ZoneInfo(tz)).date()
        assert _window_days(tz, 1)[0] == expected


def _env_without_manufacturer_secrets(**add: str):
    """Contexto de ambiente para os testes de _build_connector: remove
    QUALQUER SEMS_*/SUNWEG_* pre-existente (para que nenhum teste vaze
    credencial real de um para o outro) e acrescenta so as chaves `add`,
    preservando o resto do ambiente do processo (PATH, SYSTEMROOT etc.).
    NAO usar `clear=True` com um dict minusculo aqui: no Windows isso
    derruba variaveis que o modulo `ssl` precisa para montar o contexto
    TLS (`httpx.Client()` levanta `ssl.SSLError` sem elas), o que e um
    efeito colateral do ambiente de teste, nao do comportamento provado."""
    base = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("SEMS_") and not k.startswith("SUNWEG_")
    }
    base.update(add)
    return mock.patch.dict(os.environ, base, clear=True)


def test_build_connector_weg_returns_sunwegconnector():
    # T8: com SUNWEG_ACCOUNT_PILOTO/SUNWEG_PASSWORD_PILOTO no ambiente,
    # _build_connector("weg", "piloto") devolve SunWegConnector.
    with _env_without_manufacturer_secrets(
        SUNWEG_ACCOUNT_PILOTO="user@example.com", SUNWEG_PASSWORD_PILOTO="secret"
    ):
        connector = _build_connector("weg", "piloto")
    assert isinstance(connector, SunWegConnector)
    assert connector.manufacturer == "weg"


def test_build_connector_goodwe_returns_goodweconnector():
    # T9: com SEMS_ACCOUNT_PILOTO/SEMS_PASSWORD_PILOTO no ambiente,
    # _build_connector("goodwe", "piloto") devolve GoodWeConnector.
    with _env_without_manufacturer_secrets(
        SEMS_ACCOUNT_PILOTO="user@example.com", SEMS_PASSWORD_PILOTO="secret"
    ):
        connector = _build_connector("goodwe", "piloto")
    assert isinstance(connector, GoodWeConnector)
    assert connector.manufacturer == "goodwe"


def test_build_connector_weg_reads_sunweg_prefix_not_sems():
    # T10: _build_connector("weg", "piloto") le SUNWEG_ACCOUNT_PILOTO e NAO
    # SEMS_ACCOUNT_PILOTO -- provado deixando SO a variavel do SEMS no
    # ambiente e esperando KeyError mencionando SUNWEG_ACCOUNT_PILOTO.
    with _env_without_manufacturer_secrets(
        SEMS_ACCOUNT_PILOTO="user@example.com", SEMS_PASSWORD_PILOTO="secret"
    ):
        try:
            _build_connector("weg", "piloto")
            raise AssertionError("expected KeyError for missing SUNWEG_ACCOUNT_PILOTO")
        except KeyError as exc:
            assert "SUNWEG_ACCOUNT_PILOTO" in str(exc)


def test_build_connector_unknown_manufacturer_raises_valueerror_naming_it():
    # T11: _build_connector("solis", "piloto") levanta ValueError cuja mensagem
    # contem "solis" -- fabricante conhecido pelo schema mas sem conector
    # ainda, nao pode cair silenciosamente no Goodwe.
    with _env_without_manufacturer_secrets():
        try:
            _build_connector("solis", "piloto")
            raise AssertionError("expected ValueError for unknown manufacturer 'solis'")
        except ValueError as exc:
            assert "solis" in str(exc)


def test_build_connector_weg_empty_env_raises_actionable_exception():
    # T12: _build_connector("weg", "piloto") com o ambiente sem nenhuma
    # credencial SEMS_*/SUNWEG_* levanta uma excecao cuja mensagem nomeia
    # SUNWEG_ACCOUNT_PILOTO (mensagem acionavel, nao um KeyError cru sem
    # contexto).
    with _env_without_manufacturer_secrets():
        try:
            _build_connector("weg", "piloto")
            raise AssertionError("expected exception for empty environment")
        except KeyError as exc:
            assert "SUNWEG_ACCOUNT_PILOTO" in str(exc)


def test_manufacturer_secret_prefix_maps_exactly_goodwe_and_weg():
    # T13: _MANUFACTURER_SECRET_PREFIX mapeia exatamente
    # {"goodwe": "SEMS", "weg": "SUNWEG"} -- nada mais, nada menos.
    assert _MANUFACTURER_SECRET_PREFIX == {"goodwe": "SEMS", "weg": "SUNWEG"}


def _run_all():
    tests = [
        test_window_days_default_two_returns_today_and_yesterday,
        test_window_days_one_returns_only_today_parity_with_phase2,
        test_window_days_three_returns_three_consecutive_decreasing_no_repeat,
        test_window_days_raises_valueerror_for_lookback_below_one,
        test_parse_args_default_lookback_days_is_two,
        test_parse_args_explicit_lookback_days_one,
        test_window_days_uses_plant_timezone_not_runner_local_date,
        test_build_connector_weg_returns_sunwegconnector,
        test_build_connector_goodwe_returns_goodweconnector,
        test_build_connector_weg_reads_sunweg_prefix_not_sems,
        test_build_connector_unknown_manufacturer_raises_valueerror_naming_it,
        test_build_connector_weg_empty_env_raises_actionable_exception,
        test_manufacturer_secret_prefix_maps_exactly_goodwe_and_weg,
    ]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print("WINDOW OK")


if __name__ == "__main__":
    try:
        _run_all()
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        sys.exit(1)
