"""ingest/solar_window.py -- ponte compartilhada lat/lon + janela solar pvlib
(plano 04-01).

`plants` NAO tem colunas latitude/longitude (fora de escopo adicionar via DDL
nesta fase -- so codigo). pvlib precisa de lat/lon para calcular
sunrise/sunset. Este modulo bridgeia essa lacuna lendo
`config/plant_coordinates.toml` (nao-secreto, keyed por
`plants.external_plant_id`) e expondo um wrapper fino sobre
`pvlib.location.Location.get_sun_rise_set_transit` que os planos 04-02
(agregacao diaria) e 04-03 (passo de alarme) consomem.

SECURITY: nada aqui e secreto (apenas coordenadas geograficas publicas de
cidade) -- ainda assim, seguindo a convencao do projeto, nunca logar/imprimir
credenciais ou connection strings neste modulo ou em quem o chama.
"""

import tomllib
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from pvlib.location import Location

_COORDS_PATH = Path(__file__).resolve().parent.parent / "config" / "plant_coordinates.toml"


def load_plant_coords() -> dict[str, tuple[float, float]]:
    """Todas as coordenadas de config/plant_coordinates.toml, keyed por
    external_plant_id."""
    with _COORDS_PATH.open("rb") as f:
        data = tomllib.load(f)
    return {ext_id: (row["lat"], row["lon"]) for ext_id, row in data.items()}


def get_plant_coords(external_plant_id: str) -> tuple[float, float] | None:
    """(lat, lon) para uma usina, ou None se a usina nao tiver entrada (quem
    chama deve logar + pular essa usina graciosamente -- nunca usar a origem
    geografica (equador/meridiano de Greenwich) como default nem lancar
    excecao)."""
    return load_plant_coords().get(external_plant_id)


def solar_window_utc(
    lat: float,
    lon: float,
    tz: str,
    local_day: date,
    margin_hours: float = 0.0,
) -> tuple[datetime, datetime]:
    """(sunrise_utc, sunset_utc) tz-aware UTC para o dia calendario LOCAL da
    usina. margin_hours estreita a janela simetricamente (0 para
    disponibilidade, ~1 para exclusao de geracao-zero com sol raso)."""
    loc = Location(latitude=lat, longitude=lon, tz=tz)
    # Ancora ao meio-dia LOCAL (nao meia-noite) para evitar ambiguidade de
    # fronteira de dia no algoritmo SPA. method="spa" e o default, sem
    # dependencias extras (ao contrario de 'pyephem').
    midday_local = pd.Timestamp(local_day, tz=tz) + pd.Timedelta(hours=12)
    times = pd.DatetimeIndex([midday_local])
    rst = loc.get_sun_rise_set_transit(times, method="spa")
    sunrise = rst["sunrise"].iloc[0] + pd.Timedelta(hours=margin_hours)
    sunset = rst["sunset"].iloc[0] - pd.Timedelta(hours=margin_hours)
    return (
        sunrise.tz_convert("UTC").to_pydatetime(),
        sunset.tz_convert("UTC").to_pydatetime(),
    )
