"""Services for Tuya Local integration."""

import asyncio
import logging

import voluptuous as vol
from homeassistant.components import infrared
from homeassistant.components.remote import (
    ATTR_DELAY_SECS,
    DEFAULT_DELAY_SECS,
)
from homeassistant.components.remote import DOMAIN as REMOTE_DOMAIN
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service

from .const import DOMAIN
from .infrared import TuyaRemoteCommand
from .remote import FLAG_SAVE_DELAY, TuyaLocalRemote

REMOTE_SEND_IR_COMMAND_SCHEMA = {
    vol.Required("emitter_entity_id"): cv.entity_id,
    vol.Required("command"): str,
    vol.Optional("device"): str,
}

PERIOD_LIST = vol.All([vol.In(("day", "night"))], vol.Length(min=48, max=48))
WEEKDAY = vol.All(int, vol.Range(min=1, max=7))
SCHEDULE_SCHEMA = {
    vol.Required("day"): WEEKDAY,
    vol.Required("periods"): PERIOD_LIST,
    vol.Required("expected_periods"): PERIOD_LIST,
}
TEMPERATURE_SCHEMA = {
    vol.Optional("day_temperature"): vol.Coerce(float),
    vol.Optional("night_temperature"): vol.Coerce(float),
}
COPY_SCHEMA = {
    vol.Required("source_day"): WEEKDAY,
    vol.Required("target_days"): vol.All([WEEKDAY], vol.Length(min=1, max=6)),
}

_LOGGER = logging.getLogger(__name__)


async def async_setup_services(hass: HomeAssistant, entities: list[str]):
    """Set up services for the Tuya Local integration."""
    if "climate" in entities:
        service.async_register_platform_entity_service(
            hass,
            DOMAIN,
            "refresh_schedule",
            entity_domain="climate",
            schema={},
            func=async_handle_refresh_schedule,
        )
        for name, schema, handler in (
            ("set_schedule", SCHEDULE_SCHEMA, async_handle_set_schedule),
            (
                "set_program_temperatures",
                TEMPERATURE_SCHEMA,
                async_handle_set_program_temperatures,
            ),
            ("copy_schedule", COPY_SCHEMA, async_handle_copy_schedule),
        ):
            service.async_register_platform_entity_service(
                hass,
                DOMAIN,
                name,
                entity_domain="climate",
                schema=schema,
                func=handler,
            )
    if "remote" in entities:
        service.async_register_platform_entity_service(
            hass,
            DOMAIN,
            "send_learned_ir_command",
            entity_domain=REMOTE_DOMAIN,
            schema=REMOTE_SEND_IR_COMMAND_SCHEMA,
            func=async_handle_send_ir_command,
        )
    return True


async def async_handle_send_ir_command(entity, call: ServiceCall):
    """Action to send a saved remote command."""
    _LOGGER.info("Sending saved remote command: %s", call.data)

    if not isinstance(entity, TuyaLocalRemote):
        raise ValueError("Entity must be a tuya-local remote")
    if not entity._storage_loaded:
        await entity._async_load_storage()

    emitter = call.data.get("emitter_entity_id")
    device = call.data.get("device")
    command = call.data.get("command")
    delay = call.data.get(ATTR_DELAY_SECS, DEFAULT_DELAY_SECS)
    code_list = entity._extract_codes(
        [command], subdevice=device
    )  # Validate command and get code
    at_least_one_sent = False
    for codes in code_list:
        if at_least_one_sent:
            await asyncio.sleep(delay)
        if len(codes) > 1:
            code = codes[entity._flags[device]]
            entity._flags[device] ^= 1
        else:
            code = codes[0]
        if code.startswith("rf:"):
            _LOGGER.error("RF emitters are not yet supported by this service")
            continue
        await infrared.async_send_command(
            entity.hass, emitter, TuyaRemoteCommand(code=code)
        )
        at_least_one_sent = True

        if at_least_one_sent:
            entity._flag_storage.async_delay_save(
                lambda: entity._flags, FLAG_SAVE_DELAY
            )


async def async_handle_refresh_schedule(entity, call: ServiceCall):
    """Read the week from a supported thermostat, never from a guessed DP."""
    from .euroster import EurosterClimate

    if not isinstance(entity, EurosterClimate):
        raise ValueError("Schedule is supported only by the Euroster 4040 profile")
    await entity.async_refresh_schedule()


async def async_handle_set_schedule(entity, call: ServiceCall):
    from .euroster import EurosterClimate

    if not isinstance(entity, EurosterClimate):
        raise ValueError("Schedule is supported only by the Euroster 4040 profile")
    await entity.async_set_schedule(
        **{key: call.data[key] for key in ("day", "periods", "expected_periods")}
    )


async def async_handle_set_program_temperatures(entity, call: ServiceCall):
    from .euroster import EurosterClimate

    if not isinstance(entity, EurosterClimate):
        raise ValueError("Schedule is supported only by the Euroster 4040 profile")
    await entity.async_set_program_temperatures(
        **{
            key: call.data[key]
            for key in ("day_temperature", "night_temperature")
            if key in call.data
        }
    )


async def async_handle_copy_schedule(entity, call: ServiceCall):
    from .euroster import EurosterClimate

    if not isinstance(entity, EurosterClimate):
        raise ValueError("Schedule is supported only by the Euroster 4040 profile")
    await entity.async_copy_schedule(call.data["source_day"], call.data["target_days"])
