"""Historical sanitized app captures and synthetic IO/time scenarios."""

import asyncio
from base64 import b64encode
from datetime import datetime
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import ServiceCall
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_local.climate import async_setup_entry, climate_for_config
from custom_components.tuya_local.const import DOMAIN
from custom_components.tuya_local.euroster import EurosterClimate
from custom_components.tuya_local.euroster_schedule import (
    ScheduleDay,
    ScheduleReader,
    next_transition,
    program_at,
)
from custom_components.tuya_local.helpers.device_config import get_config
from custom_components.tuya_local.services import (
    async_handle_refresh_schedule,
    async_setup_services,
)

from .helpers import mock_device

CAPTURES = [
    "015555555aaaaaaaaaaaaaa955130214060000",
    "02555556aaaaaaaaaaaaaaa955130214060000",
    "03555556aaaaaaaaaaaaaa5555130214060000",
    "06555556aaaaaaaaaaaaaa5555130214060000",
    "07555556aaaaaa9aaaaaaa5555130214060000",
]


def transport(raw):
    return b64encode(raw).decode()


def frame(day, periods=None, tail=b"\x00\x00"):
    periods = periods or ["night"] * 11 + ["day"] * 29 + ["night"] * 8
    encoded = bytes(
        sum(
            (1 if p == "night" else 2) << (6 - 2 * j)
            for j, p in enumerate(periods[i : i + 4])
        )
        for i in range(0, 48, 4)
    )
    return bytes([day]) + encoded + bytes.fromhex("13021406") + tail


def week(periods=None):
    return {
        day: ScheduleDay.decode(transport(frame(day, periods))) for day in range(1, 8)
    }


@pytest.mark.parametrize("raw", CAPTURES)
def test_decode_captured_days(raw):
    payload = bytes.fromhex(raw)
    day = ScheduleDay.decode(transport(payload))
    assert day.payload == payload
    assert day.day == payload[0]
    assert len(day.periods) == 48
    assert day.periods[0] == "night"
    assert day.day_temperature == 20.6
    assert day.night_temperature == 19.2


def test_captured_single_slot_change_is_noon_only():
    before = ScheduleDay.decode(
        transport(bytes.fromhex("07555556aaaaaa5aaaaaaa5555130214060000"))
    )
    after = ScheduleDay.decode(transport(bytes.fromhex(CAPTURES[-1])))
    assert [i for i in range(48) if before.periods[i] != after.periods[i]] == [24]
    assert before.periods[24] == "night"
    assert after.periods[24] == "day"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        frame(1)[:-1],
        frame(1) + b"\x00",
        bytes([0]) + frame(1)[1:],
        bytes([8]) + frame(1)[1:],
        frame(1)[:1] + b"\x00" + frame(1)[2:],
        frame(1)[:13] + b"\x13\x0a" + frame(1)[15:],
        frame(1)[:15] + b"\x24\x00" + frame(1)[17:],
    ],
)
def test_invalid_frames(raw):
    with pytest.raises(ValueError):
        ScheduleDay.decode(transport(raw))


@pytest.mark.parametrize(
    "value", [None, b"binary", "not_base64!", "06555556aaaaaaaaaaaaaa5555130214060000"]
)
def test_not_raw_transport(value):
    with pytest.raises(ValueError):
        ScheduleDay.decode(value)


@pytest.mark.parametrize(
    "time,expected",
    [
        ("2026-10-19T18:00:00+02:00", "2026-10-19T20:00:00+02:00"),
        ("2026-10-19T21:00:00+02:00", "2026-10-20T05:30:00+02:00"),
        ("2026-10-18T21:00:00+02:00", "2026-10-19T05:30:00+02:00"),
        ("2026-10-19T05:30:00+02:00", "2026-10-19T20:00:00+02:00"),
    ],
)
def test_actual_changes_and_week_boundary(time, expected):
    now = datetime.fromisoformat(time).astimezone(ZoneInfo("Europe/Warsaw"))
    assert next_transition(week(), now)["at"] == expected


def test_no_change_and_incomplete_week():
    now = datetime(2026, 10, 19, 18, tzinfo=ZoneInfo("Europe/Warsaw"))
    assert next_transition(week(["day"] * 48), now) is None
    assert next_transition({1: week()[1]}, now) is None
    with pytest.raises(ValueError):
        program_at(week(), datetime(2026, 10, 19))


def test_spring_dst_skips_missing_slots():
    days = week(["night"] * 48)
    # The only day period on Sunday is in the nonexistent 02:00-03:00 hour.
    days[7] = ScheduleDay.decode(
        transport(frame(7, ["night"] * 4 + ["day"] * 2 + ["night"] * 42))
    )
    now = datetime(2026, 3, 29, 1, 45, tzinfo=ZoneInfo("Europe/Warsaw"))
    result = next_transition(days, now)
    # It cannot invent the missing hour; next valid occurrence is next week.
    assert result["at"] == "2026-04-05T02:00:00+02:00"


def test_autumn_dst_visits_repeated_hour_in_real_time_order():
    days = week(["night"] * 48)
    days[7] = ScheduleDay.decode(transport(frame(7, ["night"] * 5 + ["day"] * 43)))
    now = datetime(2026, 10, 25, 2, 45, tzinfo=ZoneInfo("Europe/Warsaw"), fold=0)
    # After the first 02:45 comes the second 02:00, whose period is night.
    assert next_transition(days, now) == {
        "at": "2026-10-25T02:00:00+01:00",
        "period": "night",
    }


async def test_sequential_week_ignores_wrong_day_and_publishes_safe_contract():
    sent = []

    async def send(dp, day):
        sent.append((dp, day))
        reader.receive({"104": transport(frame(day % 7 + 1, tail=b"\x9a\xbc"))})
        assert not reader._waiting.done()
        reader.receive({"104": transport(frame(day, tail=b"\x9a\xbc"))})

    reader = ScheduleReader(send, lambda: True)
    await reader.refresh()
    assert sent == [("102", i) for i in range(1, 8)]
    snapshot = reader.snapshot(datetime.now(ZoneInfo("Europe/Warsaw")), "Europe/Warsaw")
    assert snapshot["status"] == "ready"
    assert len(snapshot["days"]) == 7
    assert snapshot["last_synced_at"]
    assert snapshot["day_temperature"] == 20.6
    assert "payload" not in str(snapshot)
    assert "9abc" not in str(snapshot)


async def test_reads_are_serialized_across_concurrent_refreshes():
    sent = []

    async def send(dp, day):
        sent.append(day)
        await asyncio.sleep(0)
        reader.receive({"104": transport(frame(day))})

    reader = ScheduleReader(send, lambda: True)
    await asyncio.gather(reader.refresh(), reader.refresh())
    assert sent == list(range(1, 8)) * 2


async def test_timeout_retries_are_bounded_and_do_not_publish_partial_week():
    sent = []

    async def send(dp, day):
        sent.append(day)
        if day == 1:
            reader.receive({"104": transport(frame(1))})

    reader = ScheduleReader(send, lambda: True, timeout=0.001)
    with pytest.raises(TimeoutError):
        await reader.refresh()
    assert sent == [1, 2, 2]
    snapshot = reader.snapshot(datetime.now(ZoneInfo("Europe/Warsaw")), "Europe/Warsaw")
    assert snapshot["status"] == "not_loaded"
    assert snapshot["next_transition"] is None
    assert snapshot["days"] == []


async def test_stale_external_change_and_disconnect_suppress_deadlines():
    async def send(dp, day):
        reader.receive({"104": transport(frame(day))})

    connected = True
    reader = ScheduleReader(send, lambda: connected)
    await reader.refresh()
    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    reader.max_age = 0
    assert reader.snapshot(now, "Europe/Warsaw")["next_transition"] is None
    reader.max_age = 3600
    reader.receive({"104": transport(frame(3, ["night"] * 48))})
    assert reader.snapshot(now, "Europe/Warsaw")["status"] == "stale"
    await reader.refresh()
    connected = False
    assert reader.snapshot(now, "Europe/Warsaw")["status"] == "unavailable"
    connected = True
    assert reader.snapshot(now, "Europe/Warsaw")["status"] == "stale"


async def test_invalid_and_inconsistent_reports_fail_without_replacing_last_good_week():
    bad = False

    async def send(dp, day):
        raw = frame(day)
        if bad and day == 4:
            raw = raw[:13] + bytes.fromhex("13021407") + raw[17:]
        reader.receive({"104": transport(raw)})

    reader = ScheduleReader(send, lambda: True)
    await reader.refresh()
    previous = reader._days
    bad = True
    with pytest.raises(ValueError, match="global"):
        await reader.refresh()
    assert reader._days is previous
    assert (
        reader.snapshot(datetime.now(ZoneInfo("Europe/Warsaw")), "Europe/Warsaw")[
            "status"
        ]
        == "stale"
    )
    reader.receive({"104": "bad!"})
    assert reader._error == "invalid_report"


async def test_cancellation_cleans_up_and_connection_error_never_sends():
    send = AsyncMock()
    reader = ScheduleReader(send, lambda: False)
    with pytest.raises(ConnectionError):
        await reader.refresh()
    send.assert_not_awaited()
    reader = ScheduleReader(send, lambda: True)
    task = asyncio.create_task(reader.refresh())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader._waiting is None
    assert not reader.lock.locked()


def make_climate(mocker):
    device = mock_device(
        {
            "1": True,
            "103": transport(bytes.fromhex("390200000000000214051407800080000804")),
        },
        mocker,
    )
    config = next(
        c
        for c in get_config("euroster4040_thermostat").all_entities()
        if c.entity == "climate"
    )
    device.async_set_property = AsyncMock()
    return device, climate_for_config(device, config)


def test_factory_keeps_ids_and_heating_only_with_no_startup_writes(mocker):
    device, climate = make_climate(mocker)
    assert isinstance(climate, EurosterClimate)
    assert climate.hvac_modes == ["off", "heat"]
    assert climate.hvac_mode == "heat"
    assert climate.hvac_action == "idle"
    assert climate.preset_mode == "temporary_override"
    assert climate.unique_id == "test_device_id-climate_thermostat"
    assert climate.extra_state_attributes["weekly_schedule"]["status"] == "not_loaded"
    assert climate.extra_state_attributes["temporary_override_until"] is None
    device.async_set_property.assert_not_called()
    device.async_set_properties.assert_not_called()


async def test_climate_wires_real_receive_hook_and_refresh_service(hass, mocker):
    device, climate = make_climate(mocker)
    climate.hass = hass
    mocker.patch.object(climate, "async_write_ha_state")

    async def send(dp, day):
        climate.on_receive({"104": transport(frame(day))}, False)

    device.async_set_property.side_effect = send
    # Rebind captured mock method is the same mock object.
    await async_handle_refresh_schedule(
        climate, ServiceCall(hass, DOMAIN, "refresh_schedule", {})
    )
    attrs = climate.extra_state_attributes
    assert attrs["weekly_schedule"]["status"] == "ready"
    assert attrs["temporary_override_until"]
    assert [call.args[0] for call in device.async_set_property.await_args_list] == [
        "102"
    ] * 7
    with pytest.raises(ValueError):
        await async_handle_refresh_schedule(
            Mock(), ServiceCall(hass, DOMAIN, "refresh_schedule", {})
        )
    device.has_returned_state = False
    with pytest.raises(HomeAssistantError):
        await climate.async_refresh_schedule()


async def test_actual_platform_setup_selects_adapter(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "type": "euroster4040_thermostat",
            "device_id": "dummy",
            "protocol_version": "auto",
        },
    )
    device = Mock(get_property=Mock(return_value=None))
    hass.data[DOMAIN] = {"dummy": {"device": device}}
    add = Mock()
    await async_setup_entry(hass, entry, add)
    assert isinstance(add.call_args.args[0][0], EurosterClimate)
    await async_setup_services(hass, ["climate"])
    assert hass.services.has_service(DOMAIN, "refresh_schedule")


async def test_external_change_to_already_read_day_aborts_week_commit():
    async def send(dp, day):
        if day == 7:
            reader.receive({"104": transport(frame(1, ["night"] * 48))})
        reader.receive({"104": transport(frame(day))})

    reader = ScheduleReader(send, lambda: True)
    with pytest.raises(ValueError, match="during refresh"):
        await reader.refresh()
    assert reader._days == {}


async def test_legacy_fetch_button_sends_integer_not_boolean(mocker):
    from custom_components.tuya_local.button import TuyaLocalButton

    device, _ = make_climate(mocker)
    config = next(
        e
        for e in get_config("euroster4040_thermostat").all_entities()
        if e.name == "Fetch schedule"
    )
    device.async_set_properties = AsyncMock()
    await TuyaLocalButton(device, config).async_press()
    value = device.async_set_properties.call_args.args[0]["102"]
    assert type(value) is int
    assert value == 1


@pytest.mark.parametrize("connected_at_add", [False, True])
async def test_initial_schedule_loads_once_after_connection_without_configuration_writes(
    hass, mocker, connected_at_add
):
    device, climate = make_climate(mocker)
    climate.hass = hass
    device.has_returned_state = connected_at_add
    device.async_unregister_entity = AsyncMock()
    mocker.patch.object(climate, "async_write_ha_state")

    async def send(dp, day):
        assert dp == "102"
        assert type(day) is int
        climate.on_receive({"104": transport(frame(day))}, False)

    device.async_set_property.side_effect = send
    # Reports before HA adds the entity must not start unmanaged IO.
    climate.on_receive({}, True)
    device.async_set_property.assert_not_called()
    await climate.async_added_to_hass()
    if not connected_at_add:
        await hass.async_block_till_done(wait_background_tasks=True)
        device.async_set_property.assert_not_called()
        device.has_returned_state = True
        climate.on_receive({"1": True}, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert climate.extra_state_attributes["weekly_schedule"]["status"] == "ready"
    assert [call.args for call in device.async_set_property.await_args_list] == [
        ("102", day) for day in range(1, 8)
    ]
    climate.on_receive({"1": True}, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert device.async_set_property.await_count == 7
    device.async_set_properties.assert_not_called()
    await climate.async_will_remove_from_hass()


async def test_initial_schedule_failure_does_not_retry_on_every_report(hass, mocker):
    device, climate = make_climate(mocker)
    climate.hass = hass
    device.async_unregister_entity = AsyncMock()
    mocker.patch.object(climate, "async_write_ha_state")
    refresh = mocker.patch.object(
        climate.schedule, "refresh", side_effect=ConnectionError("Disconnected")
    )
    await climate.async_added_to_hass()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert refresh.await_count == 1
    climate.on_receive({"1": True}, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert refresh.await_count == 1
    # Explicit refresh remains usable after a failed automatic read.
    refresh.side_effect = None
    await climate.async_refresh_schedule()
    assert refresh.await_count == 2
    await climate.async_will_remove_from_hass()


async def test_remove_cancels_initial_read_and_cleans_waiter(hass, mocker):
    device, climate = make_climate(mocker)
    climate.hass = hass
    device.async_unregister_entity = AsyncMock()
    mocker.patch.object(climate, "async_write_ha_state")
    sent = asyncio.Event()
    device.async_set_property.side_effect = lambda *args: sent.set()
    await climate.async_added_to_hass()
    await asyncio.wait_for(sent.wait(), timeout=1)
    await climate.async_will_remove_from_hass()
    assert climate.schedule._waiting is None
    assert not climate.schedule.lock.locked()
    device.async_unregister_entity.assert_awaited_once_with(climate)


async def test_initial_read_requests_missing_dp103_before_week(hass, mocker):
    device, climate = make_climate(mocker)
    climate.hass = hass
    mocker.patch.object(climate, "async_write_ha_state")
    device.async_unregister_entity = AsyncMock()
    # status() can return power/schedule while omitting the optional DP103.
    cache = {"1": True}
    device.get_property.side_effect = cache.get
    sent = asyncio.Event()

    async def send(dp, value):
        if dp == "101":
            assert value is True  # Existing Update sensors action, not a preset.
            sent.set()
        elif dp == "102":
            assert "103" in cache, "Wait for a real sensor report before the week"
            cache["104"] = transport(frame(value))
            climate.on_receive({"104": cache["104"]}, False)
        else:
            raise AssertionError("Unexpected configuration write")

    device.async_set_property.side_effect = send
    await climate.async_added_to_hass()
    await asyncio.wait_for(sent.wait(), timeout=1)
    assert climate.current_temperature is None
    assert climate.preset_mode is None
    # Command acknowledgment alone must not count as sensor state.
    assert device.async_set_property.await_count == 1
    cache["103"] = transport(bytes.fromhex("290200000000000113021406800080000804"))
    climate.on_receive({"103": cache["103"]}, False)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert climate.current_temperature == 20.6
    assert climate.target_temperature == 19.2
    assert climate.preset_mode == "program"
    assert climate.extra_state_attributes["weekly_schedule"]["status"] == "ready"
    assert [c.args for c in device.async_set_property.await_args_list] == [
        ("101", True),
        *[("102", day) for day in range(1, 8)],
    ]
    await climate.async_will_remove_from_hass()


async def test_missing_dp103_timeout_still_reads_week_without_retry_storm(hass, mocker):
    device, climate = make_climate(mocker)
    climate.hass = hass
    mocker.patch.object(climate, "async_write_ha_state")
    device.async_unregister_entity = AsyncMock()
    device.get_property.side_effect = lambda dp: True if dp == "1" else None
    climate.schedule.timeout = 0.01

    async def send(dp, value):
        if dp == "102":
            climate.on_receive({"104": transport(frame(value))}, False)
        else:
            assert (dp, value) == ("101", True)

    device.async_set_property.side_effect = send
    await climate.async_added_to_hass()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert climate.current_temperature is None
    assert climate.extra_state_attributes["weekly_schedule"]["status"] == "ready"
    assert [c.args for c in device.async_set_property.await_args_list].count(
        ("101", True)
    ) == 1
    climate.on_receive({"1": True}, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert device.async_set_property.await_count == 8
    await climate.async_will_remove_from_hass()
