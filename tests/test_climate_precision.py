"""Climate display precision for fractional configuration steps."""

from unittest.mock import Mock

import pytest

from custom_components.tuya_local.climate import TuyaLocalClimate
from custom_components.tuya_local.helpers.device_config import TuyaEntityConfig

from .helpers import mock_device


@pytest.mark.parametrize(
    "scale,step,expected",
    [(1, 1, 1), (1, 0.1, 0.1), (1, 0.5, 0.1), (10, 1, 0.1)],
)
def test_precision_respects_fractional_step(mocker, scale, step, expected):
    device = mock_device({"42": 20}, mocker)
    config = TuyaEntityConfig(
        Mock(),
        {
            "entity": "climate",
            "dps": [
                {
                    "id": 42,
                    "type": "float",
                    "name": "temperature",
                    "mapping": [{"scale": scale, "step": step}],
                }
            ],
        },
    )
    assert TuyaLocalClimate(device, config).precision == expected
