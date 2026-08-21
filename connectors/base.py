"""connectors/base.py -- o seam CONN-04.

`NormalizedReading` e o DTO pydantic unico para o qual QUALQUER conector de
fabricante (goodwe, e futuramente solis/deye/sungrow/huawei) normaliza seus
dados brutos antes da escrita no Postgres. Os nomes de campo mapeiam EXATAMENTE
as colunas reais de `readings` em db/schema.sql -- power_w, energy_day_kwh,
energy_total_kwh, status_raw, ts -- e nao os nomes antigos (ja descontinuados)
do exemplo em ARCHITECTURE.md. connectors/common/db.py faz bind
destes campos por nome (model_dump()) no upsert; nao renomear sem atualizar
os dois lados.

`Connector` e o Protocol que cada implementacao de fabricante (ex.:
connectors/goodwe/connector.py) deve satisfazer -- o contrato que o CLI de
ingestao (Fase 2, plano 05) usa para tratar qualquer fabricante de forma
uniforme.
"""

from datetime import date, datetime
from typing import Protocol

from pydantic import BaseModel, field_validator


class NormalizedReading(BaseModel):
    """Uma leitura normalizada, pronta para upsert em `readings`.

    Campos ausentes na fonte do fabricante devem virar `None`, NUNCA zero --
    isso importa para o estado offline/0-geracao real de uma usina piloto (sem
    inversor vinculado): "sem dado" e semanticamente diferente de "0 W
    medidos", e inventar zero corromperia o dashboard/calculo de PR futuro.
    """

    tenant_id: int
    plant_id: int
    device_id: int  # obrigatorio, NUNCA None -- device sintetico (D-07) se a usina nao tiver inversor
    manufacturer: str = "goodwe"
    ts: datetime  # tz-aware, UTC (validado abaixo)
    power_w: float | None = None
    energy_day_kwh: float | None = None
    energy_total_kwh: float | None = None
    status_raw: str | None = None  # status bruto do vendor; Fase 4 interpreta, esta fase so preserva (D-08)

    @field_validator("ts")
    @classmethod
    def ts_must_be_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError(
                "ts must be tz-aware (localize with plant's IANA zone, then convert to UTC)"
            )
        return v


class Connector(Protocol):
    """Contrato que cada conector de fabricante implementa.

    O CLI de ingestao (plano 02-05) so conhece este Protocol -- nao importa
    detalhes de nenhum fabricante especifico. `normalize()` e a fronteira
    onde o payload cru do fabricante vira `NormalizedReading` (o seam CONN-04).
    """

    manufacturer: str

    def authenticate(self) -> None: ...

    def list_plants(self) -> list[dict]: ...

    def list_devices(self, plant_ext_id: str) -> list[dict]: ...

    def fetch_readings(self, plant_ext_id: str, day: date) -> list[dict]: ...

    def normalize(
        self,
        raw: list[dict],
        tenant_id: int,
        plant_id: int,
        device_id: int,
        plant_tz: str,
    ) -> list[NormalizedReading]: ...
