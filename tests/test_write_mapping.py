"""Command routing independent of a device model or reported DP type."""

from unittest.mock import Mock

import pytest

from custom_components.tuya_local.helpers.device_config import TuyaDpsConfig

from .helpers import mock_device


def attributes(mocker, rules, **target_options):
    entity = Mock()
    configs = {
        "command": {"id": 1, "write_mapping": rules},
        "source": {"id": 2, "readonly": True},
        "output": {"id": 3, **target_options},
    }
    dps = {
        name: TuyaDpsConfig(entity, {"type": "integer", "name": name, **cfg})
        for name, cfg in configs.items()
    }
    entity.find_dps.side_effect = dps.get
    device = mock_device({"1": 0, "2": 42}, mocker)
    return device, dps["command"]


@pytest.mark.parametrize(
    "rule,expected",
    [
        ({"target": "output"}, 7),
        ({"target": "output", "value_from": "source"}, 42),
        ({"target": "output", "write_value": 9}, 9),
    ],
)
def test_command_value_sources(mocker, rule, expected):
    device, dp = attributes(mocker, [rule])
    assert dp.get_values_to_set(device, 7) == {"3": expected}
    assert dp.get_value(device) == 0


def test_conditions_use_decoded_state_or_explicit_context(mocker):
    device, dp = attributes(mocker, [{"when": {"source": 43}, "target": "output"}])
    with pytest.raises(ValueError, match="No safe write"):
        dp.get_values_to_set(device, 7)
    assert dp.get_values_to_set(device, 7, context={"source": 43}) == {"3": 7}
    assert dp.get_value(device) == 0


async def test_noop_does_not_send(mocker):
    device, dp = attributes(mocker, [{"value": 7}])
    await dp.async_set_value(device, 7)
    device.async_set_properties.assert_not_called()
    with pytest.raises(ValueError):
        await dp.async_set_value(device, 8)
    device.async_set_properties.assert_not_called()


@pytest.mark.parametrize(
    "rule,options",
    [
        ({"target": "missing"}, {}),
        ({"target": "source"}, {}),
        ({"target": "command"}, {}),
        ({"target": "output", "value_from": "missing"}, {}),
        ({"target": "output"}, {"readonly": True}),
    ],
)
async def test_invalid_command_never_sends(mocker, rule, options):
    device, dp = attributes(mocker, [rule], **options)
    with pytest.raises(ValueError):
        await dp.async_set_value(device, 7)
    device.async_set_properties.assert_not_called()


def test_target_range_is_checked(mocker):
    device, dp = attributes(mocker, [{"target": "output"}], range={"min": 0, "max": 5})
    with pytest.raises(ValueError):
        dp.get_values_to_set(device, 7)


def test_missing_source_fails(mocker):
    device, dp = attributes(mocker, [{"target": "output", "value_from": "source"}])
    device.get_property.side_effect = {}.get
    with pytest.raises(ValueError, match="Missing command"):
        dp.get_values_to_set(device, 7)


def test_routed_masked_write_preserves_pending_bytes(mocker):
    device, dp = attributes(mocker, [{"target": "output"}], type="hex", mask="00ff")
    device.get_property.side_effect = {"3": "1200"}.get
    assert dp.get_values_to_set(device, 7, {"3": "3400"}) == {"3": "3407"}
