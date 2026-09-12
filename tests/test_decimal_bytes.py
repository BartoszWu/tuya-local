"""Generic binary decimal codec, independent of any device profile."""

from base64 import b64encode
from unittest.mock import Mock

import pytest

from custom_components.tuya_local.helpers.device_config import TuyaDpsConfig

from .helpers import mock_device


def dp(**options):
    return TuyaDpsConfig(
        Mock(),
        {"id": 42, "type": "hex", "name": "value", "decimal_bytes": 1, **options},
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        (18.5, "1205"),
        (20, "1400"),
        (21.5, "1505"),
        (19.15, "1302"),
        (19.25, "1302"),
        (19.95, "1400"),
        (0, "0000"),
        (255.9, "ff09"),
        (0.1 + 0.2, "0003"),
    ],
)
def test_encode_without_prior_state(mocker, value, expected):
    device = mock_device({}, mocker)
    assert dp().get_values_to_set(device, value) == {"42": expected}


@pytest.mark.parametrize("rawtype", ["hex", "base64"])
@pytest.mark.parametrize("endianness", ["big", "little"])
@pytest.mark.parametrize(
    "digits,value,raw", [(1, 19.2, b"\x13\x02"), (2, 19.25, b"\x13\x19")]
)
def test_round_trip(mocker, rawtype, endianness, digits, value, raw):
    if endianness == "little":
        raw = raw[::-1]
    payload = raw.hex() if rawtype == "hex" else b64encode(raw).decode()
    config = dp(type=rawtype, endianness=endianness, decimal_bytes=digits)
    device = mock_device({"42": payload}, mocker)
    assert config.get_value(device) == value
    assert config.get_values_to_set(device, value) == {"42": payload}


def test_mask_preserves_other_bytes_and_pending_updates(mocker):
    config = dp(mask="00ffff00")
    device = mock_device({"42": "aa1302bb"}, mocker)
    assert config.get_value(device) == 19.2
    assert config.get_values_to_set(device, 20.5) == {"42": "aa1405bb"}
    assert config.get_values_to_set(device, 20.5, {"42": "cc1302dd"}) == {
        "42": "cc1405dd"
    }
    device.get_property.return_value = None
    device.get_property.side_effect = None
    with pytest.raises(ValueError, match="unknown current"):
        config.get_values_to_set(device, 20.5)


@pytest.mark.parametrize("payload", [None, "ffff", "130a", "13", "001302", "not hex"])
def test_invalid_report(mocker, payload):
    assert dp().get_value(mock_device({"42": payload}, mocker)) is None


@pytest.mark.parametrize("value", [-0.1, 256, 255.99, float("nan"), float("inf")])
def test_invalid_write(mocker, value):
    with pytest.raises(ValueError):
        dp().get_values_to_set(mock_device({}, mocker), value)


def test_mapping_scale_and_step(mocker):
    config = dp(mapping=[{"scale": 10, "step": 0.1}])
    device = mock_device({"42": "1302"}, mocker)
    assert config.get_value(device) == 1.92
    assert config.get_values_to_set(device, 1.915) == {"42": "1302"}
