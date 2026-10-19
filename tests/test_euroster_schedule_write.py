"""Verified-write protocol with synthetic transport; never a real thermostat."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol
from homeassistant.core import ServiceCall
from homeassistant.exceptions import HomeAssistantError

from custom_components.tuya_local.euroster import EurosterProgramTemperature
from custom_components.tuya_local.euroster_schedule import ScheduleDay, ScheduleReader
from custom_components.tuya_local.helpers.device_config import get_config
from custom_components.tuya_local.number import number_for_config
from custom_components.tuya_local.services import (
    COPY_SCHEMA,
    SCHEDULE_SCHEMA,
    async_handle_copy_schedule,
    async_handle_set_program_temperatures,
    async_handle_set_schedule,
    async_setup_services,
)

from .test_euroster_weekly import CAPTURES, frame, make_climate, transport, week


class SimulatedThermostat:
    """Separate reported data from sent commands and optimistic overlays."""

    def __init__(self, *, global_scope=True, acknowledgment=True, readback=True):
        self.days = {
            day: frame(day, tail=bytes([day, 255 - day])) for day in range(1, 8)
        }
        self.calls = []
        self.global_scope = global_scope
        self.acknowledgment = acknowledgment
        self.readback = readback
        self.connected = True
        self.before_write = None
        self.reader = ScheduleReader(
            self.send, lambda: self.connected, timeout=0.01, attempts=1
        )

    async def send(self, dp, value):
        self.calls.append((dp, value))
        await asyncio.sleep(0)
        if dp == "102":
            self.reader.receive({"104": transport(self.days[value])})
        elif dp == "110":
            decoded = ScheduleDay.decode(value)
            self.before_write = self.days[decoded.day]
            if self.readback:
                self.days[decoded.day] = decoded.payload
                if self.global_scope:
                    for day, raw in self.days.items():
                        self.days[day] = raw[:13] + decoded.payload[13:17] + raw[17:]
            if self.acknowledgment:
                self.reader.receive({"104": value})
        else:
            raise AssertionError("Unexpected command")

    @property
    def writes(self):
        return [ScheduleDay.decode(value) for dp, value in self.calls if dp == "110"]


@pytest.mark.parametrize("raw", CAPTURES)
def test_encode_decode_roundtrips_preserve_all_bytes(raw):
    payload = bytes.fromhex(raw)
    day = ScheduleDay.decode(transport(payload))
    assert day.encode() == transport(payload)
    assert ScheduleDay.decode(day.edited(periods=day.periods).encode()) == day
    assert (
        day.edited(
            day_temperature=day.day_temperature, night_temperature=day.night_temperature
        )
        == day
    )


def test_encode_matches_captured_noon_change_and_keeps_tail():
    before = ScheduleDay.decode(
        transport(bytes.fromhex("07555556aaaaaa5aaaaaaa5555130214061234"))
    )
    periods = list(before.periods)
    periods[24] = "day"
    result = before.edited(periods=periods)
    assert result.payload.hex() == "07555556aaaaaa9aaaaaaa5555130214061234"
    assert result.periods == tuple(periods)


@pytest.mark.parametrize(
    "value", [4.9, 35.1, 20.15, float("nan"), float("inf"), True, "bad"]
)
async def test_invalid_temperature_never_sends_anything(value):
    device = SimulatedThermostat()
    with pytest.raises(ValueError):
        await device.reader.set_temperatures(day_temperature=value)
    assert device.calls == []


@pytest.mark.parametrize(
    "periods", [[], ["day"] * 47, ["day"] * 49, ["x"] * 48, "day", None]
)
async def test_invalid_periods_never_send(periods):
    device = SimulatedThermostat()
    with pytest.raises(ValueError):
        await device.reader.set_day(1, periods, ["night"] * 48)
    assert device.calls == []


async def test_single_slot_fresh_read_preserves_latest_unknown_bytes():
    device = SimulatedThermostat()
    await device.reader.refresh()
    # An app edit to unknown metadata is newer than the week cache.
    device.days[7] = device.days[7][:-2] + b"\x9a\xbc"
    baseline = ScheduleDay.decode(transport(device.days[7])).periods
    changed = list(baseline)
    changed[24] = "night"
    await device.reader.set_day(7, changed, baseline)
    assert len(device.writes) == 1
    written = device.writes[0]
    assert written.payload[-2:] == b"\x9a\xbc"
    assert [
        i
        for i, (a, b) in enumerate(
            zip(device.before_write, written.payload, strict=True)
        )
        if a != b
    ] == [7]
    # Ack alone is insufficient: the next command must be an explicit DP102.
    write_index = next(i for i, call in enumerate(device.calls) if call[0] == "110")
    assert device.calls[write_index + 1] == ("102", 7)
    assert device.reader.last_write["status"] == "verified"
    assert device.reader.last_write["confirmed_days"] == [7]


async def test_conflicting_edit_baseline_never_writes():
    device = SimulatedThermostat()
    with pytest.raises(ValueError, match="changed since editing"):
        await device.reader.set_day(1, ["day"] * 48, ["night"] * 48)
    assert device.writes == []
    assert device.reader.last_write["status"] == "unconfirmed"


async def test_globals_use_one_write_and_prove_all_seven_days():
    device = SimulatedThermostat()
    original = device.days.copy()
    await device.reader.set_temperatures(day_temperature=21.3, night_temperature=18.9)
    assert len(device.writes) == 1
    assert device.writes[0].day == 1
    for day, raw in device.days.items():
        assert raw[1:13] == original[day][1:13]
        assert raw[17:] == original[day][17:]
        report = ScheduleDay.decode(transport(raw))
        assert (report.day_temperature, report.night_temperature) == (21.3, 18.9)
    assert device.calls[-7:] == [("102", day) for day in range(1, 8)]
    assert device.reader.last_write["status"] == "verified"


async def test_unconfirmed_global_scope_fails_without_seven_writes():
    device = SimulatedThermostat(global_scope=False)
    with pytest.raises(ValueError, match="global"):
        await device.reader.set_temperatures(day_temperature=21.3)
    assert len(device.writes) == 1
    assert device.reader.last_write["status"] == "unconfirmed"
    snapshot = device.reader.snapshot(datetime.now(UTC), "Europe/Warsaw")
    assert snapshot["next_transition"] is None


@pytest.mark.parametrize(
    "ack,readback,match", [(False, True, None), (True, False, "readback")]
)
async def test_optimistic_send_or_ack_is_never_success(ack, readback, match):
    device = SimulatedThermostat(acknowledgment=ack, readback=readback)
    with pytest.raises((TimeoutError, ValueError), match=match):
        await device.reader.set_temperatures(day_temperature=21.3)
    assert len(device.writes) == 1
    assert device.reader.last_write["status"] == "unconfirmed"
    assert device.reader._waiting is None
    assert not device.reader.lock.locked()


async def test_noop_is_verified_without_a_write():
    device = SimulatedThermostat()
    await device.reader.set_temperatures(day_temperature=20.6)
    assert device.writes == []
    assert device.reader.last_write["status"] == "verified"


async def test_copy_only_periods_preserves_target_tails_and_global_levels():
    device = SimulatedThermostat()
    device.days[1] = frame(1, ["night"] * 48, tail=b"\x11\x22")
    before = device.days.copy()
    await device.reader.copy_days(1, [3, 7])
    assert [day.day for day in device.writes] == [3, 7]
    for day in (3, 7):
        assert device.days[day][1:13] == device.days[1][1:13]
        assert device.days[day][13:] == before[day][13:]
    for day in (1, 2, 4, 5, 6):
        assert device.days[day] == before[day]


@pytest.mark.parametrize(
    "source,targets",
    [(0, [1]), (True, [2]), (1, []), (1, [1]), (1, [2, 2]), (1, [8]), (1, [1.2])],
)
async def test_invalid_copy_targets_never_send(source, targets):
    device = SimulatedThermostat()
    with pytest.raises(ValueError):
        await device.reader.copy_days(source, targets)
    assert device.calls == []


async def test_partial_copy_exposes_confirmed_days_and_never_rolls_back():
    device = SimulatedThermostat()
    device.days[1] = frame(1, ["night"] * 48)
    base_send = device.reader._send

    async def send(dp, value):
        if dp == "110" and ScheduleDay.decode(value).day == 7:
            device.calls.append((dp, value))
            return  # transport success without a device report
        await base_send(dp, value)

    device.reader._send = send
    with pytest.raises(TimeoutError):
        await device.reader.copy_days(1, [3, 7])
    assert device.reader.last_write["confirmed_days"] == [3]
    assert device.reader.last_write["status"] == "unconfirmed"
    assert [report.day for report in device.writes] == [3, 7]


async def test_cancellation_after_send_never_retries_or_claims_success():
    device = SimulatedThermostat()
    sent = asyncio.Event()

    async def send(dp, value):
        await device.send(dp, value)
        if dp == "110":
            sent.set()
            await asyncio.Event().wait()

    device.reader._send = send
    task = asyncio.create_task(device.reader.set_temperatures(day_temperature=21.3))
    await sent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(device.writes) == 1
    assert device.reader.last_write["status"] == "unconfirmed"
    assert device.reader._waiting is None
    assert not device.reader.lock.locked()


async def test_refresh_and_write_share_a_lane():
    device = SimulatedThermostat()
    await asyncio.gather(
        device.reader.refresh(), device.reader.set_temperatures(day_temperature=21.3)
    )
    assert device.calls[:7] == [("102", day) for day in range(1, 8)]
    assert device.reader.last_write["status"] == "verified"
    snapshot = device.reader.snapshot(datetime.now(UTC), "Europe/Warsaw")
    snapshot["last_write"]["confirmed_days"].append(7)
    assert device.reader.last_write["confirmed_days"] == [1]


async def test_number_factory_shares_climate_reader_and_verifies_existing_numbers(
    hass, mocker
):
    device, climate = make_climate(mocker)
    simulated = SimulatedThermostat()
    climate.schedule._send = simulated.send
    simulated.reader = climate.schedule
    config = next(
        e
        for e in get_config("euroster4040_thermostat").all_entities()
        if e.translation_key == "day_temperature"
    )
    number = number_for_config(device, config)
    assert isinstance(number, EurosterProgramTemperature)
    assert number.schedule is climate.schedule
    assert number.unique_id == config.unique_id(device.unique_id)
    mocker.patch.object(number, "async_write_ha_state")
    await number.async_set_native_value(21.3)
    assert len(simulated.writes) == 1
    simulated.connected = False
    device.has_returned_state = False
    with pytest.raises(HomeAssistantError):
        await number.async_set_native_value(21.4)


async def test_new_services_registered_and_dispatch_to_adapter(hass, mocker):
    _, climate = make_climate(mocker)
    await async_setup_services(hass, ["climate"])
    for name in ("set_schedule", "set_program_temperatures", "copy_schedule"):
        assert hass.services.has_service("tuya_local", name)
    climate.async_set_schedule = AsyncMock()
    climate.async_set_program_temperatures = AsyncMock()
    climate.async_copy_schedule = AsyncMock()
    periods = list(week()[1].periods)
    data = {
        "day": 1,
        "periods": periods,
        "expected_periods": periods,
        "entity_id": "climate.demo",
    }
    await async_handle_set_schedule(
        climate, ServiceCall(hass, "tuya_local", "set_schedule", data)
    )
    climate.async_set_schedule.assert_awaited_once_with(
        day=1, periods=periods, expected_periods=periods
    )
    await async_handle_set_program_temperatures(
        climate,
        ServiceCall(
            hass, "tuya_local", "set_program_temperatures", {"day_temperature": 21.3}
        ),
    )
    climate.async_set_program_temperatures.assert_awaited_once_with(
        day_temperature=21.3
    )
    await async_handle_copy_schedule(
        climate,
        ServiceCall(
            hass,
            "tuya_local",
            "copy_schedule",
            {"source_day": 1, "target_days": [2, 3]},
        ),
    )
    climate.async_copy_schedule.assert_awaited_once_with(1, [2, 3])
    with pytest.raises(vol.Invalid):
        vol.Schema(SCHEDULE_SCHEMA)({"day": 1, "periods": periods})
    with pytest.raises(vol.Invalid):
        vol.Schema(COPY_SCHEMA)({"source_day": 1, "target_days": [8]})
    with pytest.raises(ValueError):
        await async_handle_set_schedule(
            object(), ServiceCall(hass, "tuya_local", "set_schedule", {})
        )
