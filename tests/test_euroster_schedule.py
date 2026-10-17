"""Program temperatures: public app captures, transported as RAW over LAN."""

from base64 import b64encode

import pytest

from custom_components.tuya_local.helpers.device_config import get_config
from custom_components.tuya_local.number import TuyaLocalNumber

from .helpers import assert_device_properties_set, mock_device

BASE = bytes.fromhex("06555556aaaaaaaaaaaaaa5555130114050000")
DAY = bytes.fromhex("06555556aaaaaaaaaaaaaa5555130114060000")
NIGHT = bytes.fromhex("06555556aaaaaaaaaaaaaa5555130214060000")


def setup_number(mocker, key, report=BASE, first_byte=0x29):
    state = bytearray.fromhex("290200000000000113011404800080000804")
    state[0] = first_byte
    reports = {"1": True, "103": b64encode(state).decode()}
    if report is not None:
        reports["104"] = b64encode(report).decode()
    device = mock_device(reports, mocker)
    config = get_config("euroster4040_thermostat")
    entity = next(e for e in config.all_entities() if e.translation_key == key)
    return device, TuyaLocalNumber(device, entity), reports


@pytest.mark.parametrize(
    "key,value,raw,expected",
    [
        ("day_temperature", 20.6, BASE, DAY),
        ("night_temperature", 19.2, DAY, NIGHT),
    ],
)
@pytest.mark.parametrize("first_byte", [0x29, 0x2D, 0x39, 0x3D, 0x69, 0x6D])
async def test_captured_program_edits(mocker, key, value, raw, expected, first_byte):
    device, entity, _ = setup_number(mocker, key, raw, first_byte)
    async with assert_device_properties_set(
        device, {"110": b64encode(expected).decode()}
    ):
        await entity.async_set_native_value(value)
    # Exact equality protects the other temperature, timetable and unknown bytes.
    assert entity.native_step == 0.1
    assert entity.native_unit_of_measurement == "°C"


def test_report_104_is_authoritative(mocker):
    device, entity, reports = setup_number(mocker, "day_temperature", DAY)
    assert entity.native_value == 20.6
    reports["110"] = b64encode(NIGHT).decode()
    device.get_property.side_effect = reports.get
    assert entity.native_value == 20.6
    reports["104"] = b64encode(BASE).decode()
    assert entity.native_value == 20.5


@pytest.mark.parametrize("raw", [None, b"", BASE[:-1], BASE + b"\x00"])
@pytest.mark.parametrize("key", ["day_temperature", "night_temperature"])
async def test_unknown_or_wrong_length_report_cannot_be_written(mocker, raw, key):
    device, entity, _ = setup_number(mocker, key, raw)
    with pytest.raises(ValueError):
        await entity.async_set_native_value(20.6)
    device.async_set_properties.assert_not_called()


@pytest.mark.parametrize(
    "key,offset", [("day_temperature", 15), ("night_temperature", 13)]
)
@pytest.mark.parametrize("value", [5.0, 19.1, 19.2, 20.6, 35.0, 0.1 + 19.1])
async def test_only_selected_bytes_change(mocker, key, offset, value):
    # Synthetic sentinel data checks preservation, not a measured timetable.
    raw = bytearray(range(19))
    raw[13:17] = bytes.fromhex("13021406")
    device, entity, _ = setup_number(mocker, key, bytes(raw))
    expected = bytearray(raw)
    expected[offset : offset + 2] = bytes(divmod(round(value * 10), 10))
    async with assert_device_properties_set(
        device, {"110": b64encode(expected).decode()}
    ):
        await entity.async_set_native_value(value)


@pytest.mark.parametrize("value", [4.9, 35.1, float("nan"), float("inf")])
async def test_invalid_program_temperature(mocker, value):
    device, entity, _ = setup_number(mocker, "day_temperature")
    with pytest.raises(ValueError):
        await entity.async_set_native_value(value)
    device.async_set_properties.assert_not_called()
