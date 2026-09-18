"""notifiers/base_test.py -- prova o DTO de transicao e o mascaramento.

Cobre:
  1. mask_phone deixa so os 4 ultimos digitos
  2. mask_phone com numero curto nao vaza digito nenhum
  3. mask_phone ignora separadores (+, espaco, hifen, parenteses)
  4. at_utc sem tzinfo e rejeitado (mesma regra de NormalizedReading.ts)
  5. local_time_str converte de UTC para o fuso da usina
  6. template_params devolve os 5 campos na ordem documentada
  7. template_params colapsa quebra de linha (a Cloud API rejeita)
  8. template_params cai no external_plant_id quando o nome e vazio
  9. status_word: opened -> ABERTO, closed -> RESOLVIDO
 10. text_body cita usina, problema e severidade
 11. alarm_label traduz os dois tipos e nao levanta em tipo desconhecido

Usage: `python -m pytest notifiers/base_test.py` ou `python notifiers/base_test.py`.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from notifiers.base import AlarmEvent, alarm_label, mask_phone, normalize_phone

# 15:05 UTC = 11:05 em America/Cuiaba (UTC-4, sem horario de verao).
AT = datetime(2026, 7, 21, 15, 5, 0, tzinfo=timezone.utc)


def _event(**overrides) -> AlarmEvent:
    base = dict(
        transition="opened",
        plant_name="Usina Piloto",
        external_plant_id="147493",
        manufacturer="goodwe",
        alarm_type="zero_generation",
        severity="critical",
        at_utc=AT,
        plant_tz="America/Cuiaba",
    )
    base.update(overrides)
    return AlarmEvent(**base)


def test_mask_phone_keeps_only_last_four_digits():
    assert mask_phone("5565999998888") == "*********8888"


def test_mask_phone_short_number_leaks_nothing():
    assert mask_phone("123") == "***"


def test_mask_phone_ignores_separators():
    assert mask_phone("+55 (65) 99999-8888") == "*********8888"


def test_normalize_phone_strips_everything_but_digits():
    assert normalize_phone("+55 (65) 99999-8888") == "5565999998888"
    assert normalize_phone("5565999998888") == "5565999998888"


def test_at_utc_must_be_tz_aware():
    try:
        _event(at_utc=datetime(2026, 7, 21, 15, 5, 0))
        raise AssertionError("esperado erro de validacao para at_utc naive")
    except Exception as exc:
        assert "tz-aware" in str(exc)


def test_local_time_str_converts_utc_to_plant_timezone():
    # 15:05 UTC -> 11:05 Cuiaba, nunca 15:05.
    assert _event().local_time_str() == "21/07/2026 11:05"


def test_template_params_documented_order():
    assert _event().template_params() == [
        "ABERTO",
        "Usina Piloto",
        "Geracao zero durante o dia",
        "critical",
        "21/07/2026 11:05",
    ]


def test_template_params_collapses_newlines():
    params = _event(plant_name="Usina\nDuas Linhas").template_params()
    assert params[1] == "Usina Duas Linhas"
    assert all("\n" not in p for p in params)


def test_template_params_falls_back_to_external_id_when_name_blank():
    assert _event(plant_name="   ").template_params()[1] == "147493"


def test_status_word_per_transition():
    assert _event(transition="opened").status_word() == "ABERTO"
    assert _event(transition="closed").status_word() == "RESOLVIDO"


def test_text_body_mentions_plant_problem_and_severity():
    body = _event().text_body()
    assert "Usina Piloto" in body
    assert "Geracao zero durante o dia" in body
    assert "critical" in body
    assert "21/07/2026 11:05" in body


def test_alarm_label_translates_known_and_passes_through_unknown():
    assert alarm_label("offline") == "Usina offline (sem leitura nova)"
    assert alarm_label("zero_generation") == "Geracao zero durante o dia"
    assert alarm_label("tipo_novo_do_futuro") == "tipo_novo_do_futuro"


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("todos os testes passaram")
