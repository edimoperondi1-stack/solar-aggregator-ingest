"""connectors/sunweg/connector_test.py -- prova SunWegConnector.normalize()
(plano 06-04, Task 1) sem rede e sem banco.

Cobre os casos de <behavior> do 06-04-PLAN.md:
  T1 (fuso): payload com 2 pontos normaliza para `ts` tz-aware UTC, com
     diferenca de EXATAMENTE 4h em relacao ao valor local naive para
     America/Cuiaba (UTC-4) -- prova negativa explicita contra 3h (a usina
     piloto e Cuiaba, nao Sao_Paulo; trocar isso corromperia toda leitura).
  T2 (ausencia != zero): um ponto com `y=None` (o formato REAL que a API usa
     para pontos futuros da curva, 06-SPIKE-FINDINGS.md) normaliza para
     `power_w is None`, `energy_day_kwh is None`, `energy_total_kwh is None`
     -- nunca 0/0.0.
  T3 (vazio): `normalize([])` devolve `[]` sem levantar.
  T4 (status bruto, D-08): um codigo de status do vendor e preservado como
     TEXTO em `status_raw`, sem mapeamento/interpretacao.
  T5 (manufacturer): toda `NormalizedReading` produzida tem
     `manufacturer == "weg"`.
  T6 (ts sempre aware): toda `NormalizedReading` produzida tem
     `ts.tzinfo is not None` e `ts.utcoffset() == timedelta(0)` (UTC).

Nao ha T7 (variante pt-BR de decimal com virgula): o spike observou `y` como
string com PONTO decimal ("18.48", "0.07"), nunca virgula -- ver
connector.py::_to_float. T7 so se aplicaria se o spike tivesse registrado
formato pt-BR, o que nao ocorreu.

Fixtures inline montadas a partir do PRIMEIRO ITEM BRUTO real colado em
06-SPIKE-FINDINGS.md ## ENDPOINTS ("getenergiadia"): `{'x': 'Wed, 22 Jul 2026
00:00:00 GMT', 'y': '0.00'}` -- e dos pontos subsequentes citados na mesma
secao.

Uso: `python -m pytest connectors/sunweg/connector_test.py` ou
`python connectors/sunweg/connector_test.py` (execucao direta, fallback sem
pytest).
"""

import os
import sys
from datetime import datetime, timedelta, timezone

# Permite rodar este arquivo diretamente
# (`python connectors/sunweg/connector_test.py`) alem de via `python -m
# pytest` -- torna o pacote `connectors` importavel quando sys.path[0] seria
# de outra forma connectors/sunweg/, nao a raiz do repo.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from connectors.sunweg.connector import SunWegConnector

PLANT_TZ = "America/Cuiaba"  # usina piloto -- UTC-4, sem DST


def _connector() -> SunWegConnector:
    # normalize() nunca toca em self._client -- um client None serve aqui.
    return SunWegConnector(client=None)


def test_normalize_localizes_naive_local_to_utc_plus_4h_for_cuiaba():
    raw = [
        {"x": "Wed, 22 Jul 2026 09:30:00 GMT", "y": "18.48"},
        {"x": "Wed, 22 Jul 2026 09:45:00 GMT", "y": "19.10"},
    ]
    readings = _connector().normalize(
        raw, tenant_id=1, plant_id=1, device_id=1, plant_tz=PLANT_TZ
    )

    assert len(readings) == 2
    assert readings[0].ts.tzinfo is not None, "ts deve ser tz-aware"
    assert readings[0].ts == datetime(2026, 7, 22, 13, 30, tzinfo=timezone.utc)
    assert readings[1].ts == datetime(2026, 7, 22, 13, 45, tzinfo=timezone.utc)

    # Prova explicita: naive local + 4h == UTC, NAO +3h (America/Cuiaba,
    # nao America/Sao_Paulo).
    naive_local = datetime(2026, 7, 22, 9, 30)
    assert readings[0].ts.replace(tzinfo=None) - naive_local == timedelta(hours=4)
    assert readings[0].ts.replace(tzinfo=None) - naive_local != timedelta(hours=3)

    # y (kW string) -> power_w (W): x1000.
    assert readings[0].power_w == 18480.0
    assert readings[1].power_w == 19100.0


def test_normalize_null_future_point_becomes_none_never_zero():
    # Formato REAL observado ao vivo para um ponto futuro da curva
    # (06-SPIKE-FINDINGS.md ## ENDPOINTS): y=None, nao "0.00".
    raw = [{"x": "Wed, 22 Jul 2026 23:45:00 GMT", "y": None}]
    readings = _connector().normalize(
        raw, tenant_id=1, plant_id=1, device_id=1, plant_tz=PLANT_TZ
    )

    assert len(readings) == 1
    assert readings[0].power_w is None
    assert readings[0].energy_day_kwh is None
    assert readings[0].energy_total_kwh is None


def test_normalize_empty_payload_returns_empty_list_without_raising():
    readings = _connector().normalize(
        [], tenant_id=1, plant_id=1, device_id=1, plant_tz=PLANT_TZ
    )
    assert readings == []


def test_normalize_preserves_raw_status_as_text_unmapped():
    raw = [{"x": "Wed, 22 Jul 2026 09:30:00 GMT", "y": "18.48", "status": 2}]
    readings = _connector().normalize(
        raw, tenant_id=1, plant_id=1, device_id=1, plant_tz=PLANT_TZ
    )

    assert len(readings) == 1
    assert readings[0].status_raw == "2"
    assert isinstance(readings[0].status_raw, str)


def test_normalize_manufacturer_is_always_weg():
    raw = [
        {"x": "Wed, 22 Jul 2026 07:00:00 GMT", "y": "0.07"},
        {"x": "Wed, 22 Jul 2026 08:00:00 GMT", "y": "6.68"},
    ]
    readings = _connector().normalize(
        raw, tenant_id=1, plant_id=1, device_id=1, plant_tz=PLANT_TZ
    )

    assert len(readings) == 2
    assert all(r.manufacturer == "weg" for r in readings)


def test_normalize_ts_always_tz_aware_utc():
    raw = [{"x": "Wed, 22 Jul 2026 00:00:00 GMT", "y": "0.00"}]
    readings = _connector().normalize(
        raw, tenant_id=1, plant_id=1, device_id=1, plant_tz=PLANT_TZ
    )

    assert len(readings) == 1
    assert readings[0].ts.tzinfo is not None
    assert readings[0].ts.utcoffset() == timedelta(0)


def _run_all():
    tests = [
        test_normalize_localizes_naive_local_to_utc_plus_4h_for_cuiaba,
        test_normalize_null_future_point_becomes_none_never_zero,
        test_normalize_empty_payload_returns_empty_list_without_raising,
        test_normalize_preserves_raw_status_as_text_unmapped,
        test_normalize_manufacturer_is_always_weg,
        test_normalize_ts_always_tz_aware_utc,
    ]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    try:
        _run_all()
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        sys.exit(1)
