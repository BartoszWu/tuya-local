"""Small Euroster adapter; the standard climate entity and IDs remain intact."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime

from homeassistant.exceptions import HomeAssistantError

from .climate import TuyaLocalClimate
from .euroster_schedule import schedule_for_device
from .number import TuyaLocalNumber

_LOGGER = logging.getLogger(__name__)


class EurosterClimate(TuyaLocalClimate):
    """Expose the device week on the existing climate entity."""

    def __init__(self, device, config):
        super().__init__(device, config)
        self.schedule = schedule_for_device(device)
        self._schedule_entity_added = False
        self._initial_schedule_started = False
        self._initial_schedule_task = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._schedule_entity_added = True
        self._start_initial_schedule_read()

    async def async_will_remove_from_hass(self):
        self._schedule_entity_added = False
        if self._initial_schedule_task is not None:
            self._initial_schedule_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._initial_schedule_task
        await super().async_will_remove_from_hass()

    def _start_initial_schedule_read(self):
        if (
            self._schedule_entity_added
            and not self._initial_schedule_started
            and self._device.has_returned_state
        ):
            self._initial_schedule_started = True
            # Never await replies inside on_receive: the receive loop must stay
            # free to deliver DP104 responses to this shared, serialized reader.
            self._initial_schedule_task = self.hass.async_create_background_task(
                self._async_read_initial_schedule(),
                "tuya_local initial Euroster schedule",
            )

    async def _async_read_initial_schedule(self):
        try:
            await self.async_refresh_schedule()
        except HomeAssistantError:
            # The reader already has bounded retries. Avoid starting another
            # batch on every report; the explicit refresh action can retry.
            _LOGGER.warning(
                "Initial Euroster schedule read failed; use tuya_local.refresh_schedule to retry"
            )

    def on_receive(self, dps, full_poll):
        self.schedule.receive(dps)
        self._start_initial_schedule_read()

    @property
    def extra_state_attributes(self):
        timezone = self.hass.config.time_zone if self.hass else "Europe/Warsaw"
        schedule = self.schedule.snapshot(datetime.now(UTC), timezone)
        transition = schedule["next_transition"]
        return {
            **super().extra_state_attributes,
            "weekly_schedule": schedule,
            "temporary_override_until": transition["at"]
            if self.preset_mode == "temporary_override" and transition
            else None,
            "temporary_override_until_source": "calculated_from_schedule",
        }

    async def async_refresh_schedule(self):
        """Request DP104 for each day. This never writes DP110 or presets."""
        try:
            await self.schedule.refresh()
        except (TimeoutError, ValueError, ConnectionError) as err:
            raise HomeAssistantError(
                "Unable to refresh Euroster schedule; no configuration was written"
            ) from err
        finally:
            self.async_write_ha_state()

    async def async_set_schedule(self, day, periods, expected_periods):
        await self._async_schedule_write(
            self.schedule.set_day(day, periods, expected_periods)
        )

    async def async_set_program_temperatures(
        self, day_temperature=None, night_temperature=None
    ):
        await self._async_schedule_write(
            self.schedule.set_temperatures(
                day_temperature=day_temperature, night_temperature=night_temperature
            )
        )

    async def async_copy_schedule(self, source_day, target_days):
        await self._async_schedule_write(
            self.schedule.copy_days(source_day, target_days)
        )

    async def _async_schedule_write(self, operation):
        try:
            await operation
        except (TimeoutError, ValueError, ConnectionError) as err:
            raise HomeAssistantError(
                "Euroster schedule write was not verified. Configuration may have changed; refresh and review before retrying."
            ) from err
        finally:
            self.async_write_ha_state()


class EurosterProgramTemperature(TuyaLocalNumber):
    """Keep existing number IDs but replace cached writes with verified IO."""

    def __init__(self, device, config):
        super().__init__(device, config)
        self.schedule = schedule_for_device(device)

    async def async_set_native_value(self, value):
        key = self._config.translation_key
        if key not in ("day_temperature", "night_temperature"):
            raise ValueError("Unsupported program temperature")
        try:
            await self.schedule.set_temperatures(**{key: value})
        except (TimeoutError, ValueError, ConnectionError) as err:
            raise HomeAssistantError(
                "Program temperature write was not verified; refresh and review the thermostat before retrying."
            ) from err
        finally:
            self.async_write_ha_state()
