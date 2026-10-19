"""Small Euroster adapter; the standard climate entity and IDs remain intact."""

from datetime import UTC, datetime

from homeassistant.exceptions import HomeAssistantError

from .climate import TuyaLocalClimate
from .euroster_schedule import ScheduleReader


class EurosterClimate(TuyaLocalClimate):
    """Expose an explicitly refreshed week on the existing climate entity."""

    def __init__(self, device, config):
        super().__init__(device, config)
        self.schedule = ScheduleReader(
            device.async_set_property, lambda: device.has_returned_state
        )

    def on_receive(self, dps, full_poll):
        self.schedule.receive(dps)

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
