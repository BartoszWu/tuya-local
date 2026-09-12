"""Euroster protocol regressions using public, model-level payloads."""

from base64 import b64encode

import pytest

from custom_components.tuya_local.button import TuyaLocalButton
from custom_components.tuya_local.climate import TuyaLocalClimate
from custom_components.tuya_local.helpers.device_config import get_config

from .helpers import assert_device_properties_set, mock_device


def thermostat(mocker, action=9):
    payload = bytearray.fromhex("690200000000000113021606800080000802")
    payload[0] = 0x60 | action
    device = mock_device({"1": True, "103": b64encode(payload).decode()}, mocker)
    config = get_config("euroster4040_thermostat")
    entity = next(e for e in config.all_entities() if e.entity == "climate")
    return device, TuyaLocalClimate(device, entity), config


@pytest.mark.parametrize(
    "temperature,payload", [(19.1, "EwE="), (19.2, "EwI="), (19.6, "EwY=")]
)
async def test_temperature_write(mocker, temperature, payload):
    """Encode raw temperature bytes for LAN transport; never write DP103."""
    device, climate, _ = thermostat(mocker)
    async with assert_device_properties_set(device, {"108": payload}):
        await climate.async_set_temperature(temperature=temperature)


@pytest.mark.parametrize(
    "action,expected", [(1, "preheating"), (5, "heating"), (9, "idle"), (13, "heating")]
)
def test_readback(mocker, action, expected):
    _, climate, _ = thermostat(mocker, action)
    assert climate.target_temperature == 19.2
    assert climate.current_temperature == 22.6
    assert "current_temperature_decimal" not in climate.extra_state_attributes
    assert climate.hvac_action == expected
    assert climate.target_temperature_step == 0.1
    assert climate.precision == 0.1
    assert (climate.min_temp, climate.max_temp) == (5, 35)


def test_hardware_report_with_fractional_temperatures(mocker):
    device, climate, _ = thermostat(mocker)
    device.get_property.side_effect = {
        "1": True,
        "103": "aQIAAAAAAAITBhcEgACAAAgB",
    }.get
    assert climate.target_temperature == 19.6
    assert climate.current_temperature == 23.4
    assert climate.precision == 0.1


async def test_resume_schedule(mocker):
    device, _, config = thermostat(mocker)
    entity = next(e for e in config.all_entities() if e.name == "Resume schedule")
    dp = entity.find_dps("button")
    assert dp.encode_value(bytes.fromhex("ffff")) == "//8="
    assert dp.decode_value("//8=", device) == bytes.fromhex("ffff")
    async with assert_device_properties_set(device, {"108": "//8="}):
        await TuyaLocalButton(device, entity).async_press()


@pytest.mark.parametrize("tenths", range(50, 351))
async def test_inferred_temperature_range(mocker, tenths):
    """Exhaustive inferred encoding, not additional hardware measurements."""
    device, climate, _ = thermostat(mocker)
    whole, fraction = divmod(tenths, 10)
    expected = b64encode(bytes((whole, fraction))).decode()
    async with assert_device_properties_set(device, {"108": expected}):
        await climate.async_set_temperature(temperature=tenths / 10)


@pytest.mark.parametrize(
    "value", [4.9, 35.1, float("nan"), float("inf"), -float("inf")]
)
async def test_invalid_temperature(mocker, value):
    device, climate, _ = thermostat(mocker)
    with pytest.raises(ValueError):
        await climate.async_set_temperature(temperature=value)
    device.async_set_properties.assert_not_called()


def test_report_is_authoritative(mocker):
    device, climate, _ = thermostat(mocker)
    reports = {"108": "//8="}
    device.get_property.side_effect = reports.get
    assert climate.target_temperature is None
    reports["103"] = b64encode(
        bytes.fromhex("290200000000000112091606800080000802")
    ).decode()
    assert climate.target_temperature == 18.9
    reports["108"] = "EwI="
    assert climate.target_temperature == 18.9


PRESETS = [(0x20, "program"), (0x30, "temporary_override"), (0x60, "hold")]


def report_preset(device, flags, action=9):
    payload = bytearray.fromhex("290200000000000216001a09800080000801")
    payload[0] = flags | action
    device.get_property.side_effect = {
        "1": True,
        "103": b64encode(payload).decode(),
    }.get


@pytest.mark.parametrize("flags,preset", PRESETS)
@pytest.mark.parametrize(
    "action,expected", [(1, "preheating"), (5, "heating"), (9, "idle"), (13, "heating")]
)
def test_preset_and_action_independent(mocker, flags, preset, action, expected):
    """Actions 9 and 13 are hardware-confirmed; 1 and 5 are synthetic combinations."""
    device, climate, _ = thermostat(mocker)
    report_preset(device, flags, action)
    assert climate.preset_mode == preset
    assert climate.hvac_action == expected
    assert climate.target_temperature == 22.0
    assert climate.current_temperature == 26.9
    assert set(climate.preset_modes) == {p for _, p in PRESETS}


@pytest.mark.parametrize("flags,dp", [(0x20, "109"), (0x30, "109"), (0x60, "108")])
async def test_temperature_routes_by_report(mocker, flags, dp):
    device, climate, _ = thermostat(mocker)
    report_preset(device, flags)
    async with assert_device_properties_set(device, {dp: "FgA="}):
        await climate.async_set_temperature(temperature=22.0)


@pytest.mark.parametrize("flags,current", PRESETS)
@pytest.mark.parametrize("desired,dp", [("temporary_override", "109"), ("hold", "108")])
async def test_select_override_preserves_target(mocker, flags, current, desired, dp):
    device, climate, _ = thermostat(mocker)
    report_preset(device, flags)
    if current == desired:
        await climate.async_set_preset_mode(desired)
        device.async_set_properties.assert_not_called()
    else:
        async with assert_device_properties_set(device, {dp: "FgA="}):
            await climate.async_set_preset_mode(desired)
    assert climate.preset_mode == current  # No optimistic state from a command.


@pytest.mark.parametrize("flags,preset", PRESETS)
@pytest.mark.parametrize("button", [False, True])
async def test_resume_routes_by_report(mocker, flags, preset, button):
    device, climate, config = thermostat(mocker)
    report_preset(device, flags)
    entity = next(e for e in config.all_entities() if e.name == "Resume schedule")

    async def resume():
        if button:
            await TuyaLocalButton(device, entity).async_press()
        else:
            await climate.async_set_preset_mode("program")

    if preset == "program":
        await resume()
        device.async_set_properties.assert_not_called()
    else:
        dp = "109" if preset == "temporary_override" else "108"
        async with assert_device_properties_set(device, {dp: "//8="}):
            await resume()


@pytest.mark.parametrize("flags,current", PRESETS)
@pytest.mark.parametrize("desired,dp", [("temporary_override", "109"), ("hold", "108")])
async def test_combined_command_uses_requested_preset(
    mocker, flags, current, desired, dp
):
    device, climate, _ = thermostat(mocker)
    report_preset(device, flags)
    async with assert_device_properties_set(device, {dp: "EwI="}):
        await climate.async_set_temperature(temperature=19.2, preset_mode=desired)
    assert climate.preset_mode == current
    assert climate.target_temperature == 22.0


async def test_program_and_manual_temperature_conflict(mocker):
    device, climate, _ = thermostat(mocker)
    with pytest.raises(ValueError):
        await climate.async_set_temperature(temperature=19.2, preset_mode="program")
    device.async_set_properties.assert_not_called()


@pytest.mark.parametrize("flags", [None, 0x70])
async def test_unknown_preset_never_guesses_command(mocker, flags):
    device, climate, config = thermostat(mocker)
    if flags is None:
        device.get_property.side_effect = {"1": True}.get
    else:
        report_preset(device, flags)
    assert climate.preset_mode is None
    with pytest.raises(ValueError):
        await climate.async_set_temperature(temperature=22.0)
    with pytest.raises(ValueError):
        await climate.async_set_preset_mode("program")
    entity = next(e for e in config.all_entities() if e.name == "Resume schedule")
    with pytest.raises(ValueError):
        await TuyaLocalButton(device, entity).async_press()
    device.async_set_properties.assert_not_called()


async def test_missing_target_cannot_activate_override(mocker):
    device, climate, _ = thermostat(mocker)
    device.get_property.side_effect = {"1": True}.get
    for preset in ("temporary_override", "hold", "not_a_preset"):
        with pytest.raises(ValueError):
            await climate.async_set_preset_mode(preset)
    device.async_set_properties.assert_not_called()


@pytest.mark.parametrize("tenths", range(50, 351))
async def test_temporary_range(mocker, tenths):
    """Exhaustive inferred DP109 temperatures; only 19.0 was verified over LAN."""
    device, climate, _ = thermostat(mocker)
    report_preset(device, 0x30)
    expected = b64encode(bytes(divmod(tenths, 10))).decode()
    async with assert_device_properties_set(device, {"109": expected}):
        await climate.async_set_temperature(temperature=tenths / 10)


@pytest.mark.parametrize(
    "hex_report,preset,target,current",
    [
        ("690200000000000213021a09800080000801", "hold", 19.2, 26.9),
        ("390200000000000216001a09800080000801", "temporary_override", 22.0, 26.9),
        ("690200000000000215001a09800080000801", "hold", 21.0, 26.9),
        ("690200000000000216001a09800080000801", "hold", 22.0, 26.9),
        ("290200000000000213001a09800080000801", "program", 19.0, 26.9),
        ("290200000000000213001905800080000801", "program", 19.0, 25.5),
    ],
)
def test_app_captures(mocker, hex_report, preset, target, current):
    device, climate, _ = thermostat(mocker)
    device.get_property.side_effect = {
        "1": True,
        "103": b64encode(bytes.fromhex(hex_report)).decode(),
    }.get
    assert climate.preset_mode == preset
    assert climate.target_temperature == target
    assert climate.current_temperature == current
    assert climate.hvac_action == "idle"


@pytest.mark.parametrize(
    "idle,heating,preset",
    [(0x29, 0x2D, "program"), (0x39, 0x3D, "temporary_override"), (0x69, 0x6D, "hold")],
)
def test_hardware_preset_transition(mocker, idle, heating, preset):
    """Confirmed first-byte transitions; other bytes are a shared test fixture."""
    device, climate, _ = thermostat(mocker)
    for first_byte, action in ((idle, "idle"), (heating, "heating")):
        report_preset(device, first_byte & 0xF0, first_byte & 0x0F)
        assert climate.preset_mode == preset
        assert climate.hvac_action == action
    assert idle & 0x50 == heating & 0x50


@pytest.mark.parametrize("first_byte,action", [("69", "idle"), ("6d", "heating")])
def test_complete_hold_hardware_transition(mocker, first_byte, action):
    """Complete raw reports captured during Hold idle to heating."""
    device, climate, _ = thermostat(mocker)
    raw = bytes.fromhex(first_byte + "0200000000000219001706800080000801")
    device.get_property.side_effect = {"1": True, "103": b64encode(raw).decode()}.get
    assert climate.preset_mode == "hold"
    assert climate.hvac_action == action
    assert climate.target_temperature == 25.0
    assert climate.current_temperature == 23.6
