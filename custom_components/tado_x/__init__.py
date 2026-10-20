"""The Tado X integration."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Final

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TadoXApi, TadoXApiError, TadoXAuthError, summarize_day_report
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_API_CALLS_TODAY,
    CONF_API_RESET_TIME,
    CONF_ENABLE_AIR_COMFORT,
    CONF_ENABLE_FLOW_TEMP,
    CONF_ENABLE_HOT_WATER,
    CONF_ENABLE_MOBILE_DEVICES,
    CONF_ENABLE_RUNNING_TIMES,
    CONF_ENABLE_WEATHER,
    CONF_HAS_AUTO_ASSIST,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN_EXPIRY,
    DOMAIN,
    PLATFORMS,
    TERMINATION_MANUAL,
    TERMINATION_NEXT_TIME_BLOCK,
    TERMINATION_TIMER,
)
from .coordinator import TadoXDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Service constants
SERVICE_SET_TEMPERATURE_OFFSET: Final = "set_temperature_offset"
ATTR_OFFSET: Final = "offset"

SERVICE_ADD_METER_READING: Final = "add_meter_reading"
ATTR_HOME_ID: Final = "home_id"
ATTR_READING: Final = "reading"
ATTR_DATE: Final = "date"

SERVICE_SET_EIQ_TARIFF: Final = "set_eiq_tariff"
ATTR_TARIFF: Final = "tariff"
ATTR_UNIT: Final = "unit"
ATTR_START_DATE: Final = "start_date"
ATTR_END_DATE: Final = "end_date"

SERVICE_SET_CLIMATE_TIMER: Final = "set_climate_timer"
ATTR_ENTITY_ID: Final = "entity_id"
ATTR_TEMPERATURE: Final = "temperature"
ATTR_DURATION: Final = "duration"
ATTR_TERMINATION_TYPE: Final = "termination_type"

SERVICE_GET_SCHEDULE: Final = "get_schedule"
SERVICE_SET_SCHEDULE: Final = "set_schedule"
SERVICE_GET_DAY_REPORT: Final = "get_day_report"
ATTR_DAY_TYPE: Final = "day_type"
ATTR_SCHEDULE: Final = "schedule"

SERVICE_GET_HOLIDAY_MODE: Final = "get_holiday_mode"
SERVICE_SET_HOLIDAY_MODE: Final = "set_holiday_mode"
ATTR_ENABLED: Final = "enabled"

DAY_TYPE_OPTIONS: Final = [
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
    "MONDAY_TO_FRIDAY",
    "MONDAY_TO_SUNDAY",
]

# Service schemas
SERVICE_SET_TEMPERATURE_OFFSET_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_OFFSET): vol.All(
            vol.Coerce(float),
            vol.Range(min=-9.9, max=9.9),
        ),
    }
)

SERVICE_ADD_METER_READING_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_READING): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional(ATTR_DATE): cv.string,
        vol.Optional(ATTR_HOME_ID): cv.string,
    }
)

SERVICE_SET_EIQ_TARIFF_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TARIFF): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Required(ATTR_UNIT): vol.In(["m3", "kWh"]),
        vol.Optional(ATTR_START_DATE): cv.string,
        vol.Optional(ATTR_END_DATE): cv.string,
        vol.Optional(ATTR_HOME_ID): cv.string,
    }
)

SERVICE_SET_CLIMATE_TIMER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_TEMPERATURE): vol.All(
            vol.Coerce(float),
            vol.Range(min=5.0, max=30.0),
        ),
        vol.Required(ATTR_DURATION): vol.All(
            vol.Coerce(int),
            vol.Range(min=1, max=1440),  # 1 minute to 24 hours
        ),
        vol.Optional(ATTR_TERMINATION_TYPE, default=TERMINATION_TIMER): vol.In(
            [TERMINATION_TIMER, TERMINATION_MANUAL, TERMINATION_NEXT_TIME_BLOCK]
        ),
    }
)


SERVICE_GET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
    }
)

SERVICE_SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_DAY_TYPE): vol.In(DAY_TYPE_OPTIONS),
        vol.Required(ATTR_SCHEDULE): cv.string,
    }
)

SERVICE_GET_DAY_REPORT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_DATE): cv.string,
    }
)

SERVICE_GET_HOLIDAY_MODE_SCHEMA = vol.Schema({vol.Optional(ATTR_HOME_ID): cv.string})

SERVICE_SET_HOLIDAY_MODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENABLED): cv.boolean,
        vol.Optional(ATTR_HOME_ID): cv.string,
        vol.Optional(ATTR_START_DATE): cv.string,
        vol.Optional(ATTR_END_DATE): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tado X from a config entry."""
    session = async_get_clientsession(hass)

    # Parse token expiry
    token_expiry = None
    if entry.data.get(CONF_TOKEN_EXPIRY):
        try:
            token_expiry = datetime.fromisoformat(entry.data[CONF_TOKEN_EXPIRY])
        except (ValueError, TypeError):
            pass

    # Parse API reset time for persistence
    api_reset_time = None
    if entry.data.get(CONF_API_RESET_TIME):
        try:
            api_reset_time = datetime.fromisoformat(entry.data[CONF_API_RESET_TIME])
        except (ValueError, TypeError):
            pass

    # Create a mutable container for the API reference (needed for callback closure)
    api_container: dict[str, TadoXApi] = {}

    def save_tokens() -> None:
        """Save tokens to config entry after refresh to prevent auth loss on restart."""
        if "api" not in api_container:
            return
        api = api_container["api"]
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: api.access_token,
                CONF_REFRESH_TOKEN: api.refresh_token,
                CONF_TOKEN_EXPIRY: api.token_expiry.isoformat() if api.token_expiry else None,
            },
        )
        _LOGGER.debug("Tokens persisted to config entry")

    api = TadoXApi(
        session=session,
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        token_expiry=token_expiry,
        api_calls_today=entry.data.get(CONF_API_CALLS_TODAY, 0),
        api_reset_time=api_reset_time,
        has_auto_assist=entry.data.get(CONF_HAS_AUTO_ASSIST, False),
        on_token_refresh=save_tokens,
    )
    api_container["api"] = api

    home_id = entry.data[CONF_HOME_ID]
    home_name = entry.data.get(CONF_HOME_NAME, f"Tado Home {home_id}")

    # Test the connection and refresh token if needed
    try:
        await api.refresh_access_token()

        # Update stored tokens and API call stats
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: api.access_token,
                CONF_REFRESH_TOKEN: api.refresh_token,
                CONF_TOKEN_EXPIRY: api.token_expiry.isoformat() if api.token_expiry else None,
                CONF_API_CALLS_TODAY: api.api_calls_today,
                CONF_API_RESET_TIME: api.api_reset_time.isoformat(),
                CONF_HAS_AUTO_ASSIST: api.has_auto_assist,
            },
        )
    except TadoXAuthError as err:
        _LOGGER.error("Authentication failed: %s", err)
        raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err

    # Create callback to save API stats periodically
    def save_api_stats() -> None:
        """Save API call statistics to config entry."""
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_API_CALLS_TODAY: api.api_calls_today,
                CONF_API_RESET_TIME: api.api_reset_time.isoformat(),
            },
        )

    # Get configured scan interval (or None to use auto-detection based on tier)
    configured_scan_interval = entry.data.get(CONF_SCAN_INTERVAL)

    # Get feature toggles - default based on subscription tier
    # Auto-Assist users get all features enabled, free tier users get them disabled
    has_auto_assist = entry.data.get(CONF_HAS_AUTO_ASSIST, False)
    default_features = has_auto_assist
    enable_weather = entry.data.get(CONF_ENABLE_WEATHER, default_features)
    enable_mobile_devices = entry.data.get(CONF_ENABLE_MOBILE_DEVICES, default_features)
    enable_air_comfort = entry.data.get(CONF_ENABLE_AIR_COMFORT, default_features)
    enable_running_times = entry.data.get(CONF_ENABLE_RUNNING_TIMES, default_features)
    enable_flow_temp = entry.data.get(CONF_ENABLE_FLOW_TEMP, default_features)
    enable_hot_water = entry.data.get(CONF_ENABLE_HOT_WATER, default_features)

    # Create coordinator
    coordinator = TadoXDataUpdateCoordinator(
        hass=hass,
        api=api,
        home_id=home_id,
        home_name=home_name,
        save_api_stats_callback=save_api_stats,
        scan_interval=configured_scan_interval if configured_scan_interval else None,
        enable_weather=enable_weather,
        enable_mobile_devices=enable_mobile_devices,
        enable_air_comfort=enable_air_comfort,
        enable_running_times=enable_running_times,
        enable_flow_temp=enable_flow_temp,
        enable_hot_water=enable_hot_water,
    )

    # Fetch initial data
    try:
        await coordinator.async_config_entry_first_refresh()
    except TadoXApiError as err:
        raise ConfigEntryNotReady(f"Failed to fetch data: {err}") from err

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Register services
    #
    # Services are global to the integration domain, while coordinators are
    # specific to config entries. Service handlers must therefore resolve the
    # correct coordinator at call time rather than using the coordinator from
    # whichever config entry happened to register the service first.

    def get_coordinator_for_config_entry(
        config_entry_id: str,
    ) -> TadoXDataUpdateCoordinator:
        """Return the coordinator belonging to a config entry."""
        service_coordinator = hass.data.get(DOMAIN, {}).get(config_entry_id)
        if service_coordinator is None:
            raise HomeAssistantError(
                f"No Tado X coordinator found for config entry {config_entry_id}"
            )
        return service_coordinator

    def get_coordinator_for_home(home_id: str) -> TadoXDataUpdateCoordinator:
        """Return the coordinator belonging to a Tado home."""
        for service_coordinator in hass.data.get(DOMAIN, {}).values():
            if str(service_coordinator.home_id) == str(home_id):
                return service_coordinator
        raise HomeAssistantError(f"No Tado X coordinator found for home {home_id}")

    def get_default_coordinator() -> TadoXDataUpdateCoordinator:
        """Return the coordinator when exactly one Tado home is configured."""
        coordinators = hass.data.get(DOMAIN, {})
        if not coordinators:
            raise HomeAssistantError("No Tado X homes are configured")
        if len(coordinators) > 1:
            raise HomeAssistantError(
                "Multiple Tado X homes are configured. A home_id must be specified."
            )
        return next(iter(coordinators.values()))

    def get_home_coordinator(call: ServiceCall) -> TadoXDataUpdateCoordinator:
        """Resolve coordinator for a home-level service."""
        requested_home_id = call.data.get(ATTR_HOME_ID)
        if requested_home_id is not None:
            return get_coordinator_for_home(requested_home_id)
        return get_default_coordinator()

    async def async_set_temperature_offset(call: ServiceCall) -> None:
        """Handle set_temperature_offset service call."""
        device_id = call.data[ATTR_DEVICE_ID]
        offset = call.data[ATTR_OFFSET]

        device_registry_instance = dr.async_get(hass)
        device = device_registry_instance.async_get(device_id)
        if not device:
            raise HomeAssistantError(f"Device {device_id} not found")

        config_entry_id = device.config_entry_id
        if config_entry_id not in hass.data.get(DOMAIN, {}):
            raise HomeAssistantError(
                f"Could not determine Tado X config entry for device {device_id}"
            )

        service_coordinator = get_coordinator_for_config_entry(config_entry_id)

        device_serial = None
        for identifier in device.identifiers:
            if identifier[0] == DOMAIN:
                device_serial = identifier[1]
                break

        if not device_serial:
            raise HomeAssistantError(
                f"Could not find serial number for device {device_id}"
            )

        if "_" in device_serial:
            raise HomeAssistantError(
                f"Cannot set temperature offset for room device {device_id}. "
                "Please select a specific valve or sensor device."
            )

        try:
            await service_coordinator.api.set_temperature_offset(device_serial, offset)
            await service_coordinator.async_request_refresh()
            _LOGGER.info(
                "Set temperature offset for device %s to %.1f°C",
                device_serial,
                offset,
            )
        except TadoXApiError as err:
            _LOGGER.error(
                "Failed to set temperature offset for device %s: %s",
                device_serial,
                err,
            )
            raise HomeAssistantError(
                f"Failed to set temperature offset: {err}"
            ) from err

    if not hass.services.has_service(DOMAIN, SERVICE_SET_TEMPERATURE_OFFSET):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_TEMPERATURE_OFFSET,
            async_set_temperature_offset,
            schema=SERVICE_SET_TEMPERATURE_OFFSET_SCHEMA,
        )

    async def async_add_meter_reading(call: ServiceCall) -> None:
        """Handle add_meter_reading service call."""
        reading = call.data[ATTR_READING]
        date = call.data.get(ATTR_DATE)
        service_coordinator = get_home_coordinator(call)

        try:
            await service_coordinator.api.add_meter_reading(reading, date)
            _LOGGER.info(
                "Meter reading %s added successfully for home %s",
                reading,
                service_coordinator.home_id,
            )
        except TadoXApiError as err:
            _LOGGER.error("Failed to add meter reading: %s", err)
            raise HomeAssistantError(f"Failed to add meter reading: {err}") from err

    if not hass.services.has_service(DOMAIN, SERVICE_ADD_METER_READING):
        hass.services.async_register(
            DOMAIN,
            SERVICE_ADD_METER_READING,
            async_add_meter_reading,
            schema=SERVICE_ADD_METER_READING_SCHEMA,
        )

    async def async_set_eiq_tariff(call: ServiceCall) -> None:
        """Handle set_eiq_tariff service call."""
        tariff = call.data[ATTR_TARIFF]
        unit = call.data[ATTR_UNIT]
        start_date = call.data.get(ATTR_START_DATE)
        end_date = call.data.get(ATTR_END_DATE)
        service_coordinator = get_home_coordinator(call)

        try:
            await service_coordinator.api.set_eiq_tariff(
                tariff, unit, start_date, end_date
            )
            _LOGGER.info(
                "EIQ tariff %.2f %s set successfully for home %s",
                tariff,
                unit,
                service_coordinator.home_id,
            )
        except TadoXApiError as err:
            _LOGGER.error("Failed to set EIQ tariff: %s", err)
            raise HomeAssistantError(f"Failed to set EIQ tariff: {err}") from err

    if not hass.services.has_service(DOMAIN, SERVICE_SET_EIQ_TARIFF):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_EIQ_TARIFF,
            async_set_eiq_tariff,
            schema=SERVICE_SET_EIQ_TARIFF_SCHEMA,
        )

    async def async_set_climate_timer(call: ServiceCall) -> None:
        """Handle set_climate_timer service call."""
        from homeassistant.helpers import entity_registry as er

        entity_id = call.data[ATTR_ENTITY_ID]
        temperature = call.data[ATTR_TEMPERATURE]
        duration_minutes = call.data[ATTR_DURATION]
        termination_type = call.data.get(ATTR_TERMINATION_TYPE, TERMINATION_TIMER)

        entity_registry = er.async_get(hass)
        entity_entry = entity_registry.async_get(entity_id)
        if not entity_entry:
            raise HomeAssistantError(f"Entity {entity_id} not found in registry")

        if entity_entry.platform != DOMAIN:
            raise HomeAssistantError(
                f"Entity {entity_id} is not a Tado X entity "
                f"(platform: {entity_entry.platform}). "
                "The set_climate_timer service only works with native "
                "Tado X climate entities."
            )

        if not entity_entry.config_entry_id:
            raise HomeAssistantError(f"Entity {entity_id} has no config entry")
        if not entity_entry.unique_id:
            raise HomeAssistantError(f"Entity {entity_id} has no unique_id")

        service_coordinator = get_coordinator_for_config_entry(
            entity_entry.config_entry_id
        )

        try:
            parts = entity_entry.unique_id.split("_")
            if len(parts) == 3 and parts[2] == "climate":
                entity_home_id = parts[0]
                room_id = int(parts[1])
            elif len(parts) == 2:
                entity_home_id = parts[0]
                room_id = int(parts[1])
            else:
                raise ValueError(
                    f"Unexpected unique_id format: {entity_entry.unique_id}"
                )
        except (ValueError, IndexError) as err:
            raise HomeAssistantError(
                f"Could not extract room_id from entity {entity_id}: {err}"
            ) from err

        if str(service_coordinator.home_id) != str(entity_home_id):
            raise HomeAssistantError(
                f"Entity {entity_id} belongs to Tado home {entity_home_id}, "
                f"but its config entry belongs to home {service_coordinator.home_id}"
            )

        duration_seconds = duration_minutes * 60

        try:
            await service_coordinator.api.set_room_temperature(
                room_id=room_id,
                temperature=temperature,
                power="ON",
                termination_type=termination_type,
                duration_seconds=duration_seconds,
            )
            await service_coordinator.async_request_refresh()
            _LOGGER.info(
                "Set %s to %.1f°C for %d minutes (home %s, room %s)",
                entity_id,
                temperature,
                duration_minutes,
                service_coordinator.home_id,
                room_id,
            )
        except TadoXApiError as err:
            _LOGGER.error("Failed to set climate timer for %s: %s", entity_id, err)
            raise HomeAssistantError(f"Failed to set climate timer: {err}") from err

    if not hass.services.has_service(DOMAIN, SERVICE_SET_CLIMATE_TIMER):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_CLIMATE_TIMER,
            async_set_climate_timer,
            schema=SERVICE_SET_CLIMATE_TIMER_SCHEMA,
        )

    def resolve_room(entity_id: str) -> tuple[TadoXDataUpdateCoordinator, int]:
        """Resolve a room using its owning config entry, never a global coordinator."""
        entity_entry = er.async_get(hass).async_get(entity_id)
        if not entity_entry or entity_entry.platform != DOMAIN or entity_entry.domain != "climate" or not entity_entry.config_entry_id:
            raise HomeAssistantError(f"{entity_id} is not a registered Tado X entity")
        service_coordinator = get_coordinator_for_config_entry(entity_entry.config_entry_id)
        unique_id = entity_entry.unique_id or ""
        parts = unique_id.split("_")
        if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] != "climate"):
            raise HomeAssistantError(f"Invalid Tado X room entity: {entity_id}")
        try:
            room_id = int(parts[1])
        except ValueError as err:
            raise HomeAssistantError(f"Invalid room ID for {entity_id}") from err
        if str(parts[0]) != str(service_coordinator.home_id):
            raise HomeAssistantError(f"Home ID mismatch for {entity_id}")
        if room_id not in service_coordinator.data.rooms:
            raise HomeAssistantError(f"Room {room_id} is not part of home {service_coordinator.home_id}")
        return service_coordinator, room_id

    async def async_get_schedule(call: ServiceCall) -> dict[str, Any]:
        """Return the current smart schedule for a room."""
        service_coordinator, room_id = resolve_room(call.data[ATTR_ENTITY_ID])
        try:
            return await service_coordinator.api.get_zone_schedule(room_id)
        except TadoXApiError as err:
            raise HomeAssistantError(f"Failed to get schedule: {err}") from err

    async def async_set_schedule(call: ServiceCall) -> None:
        """Set the smart schedule blocks for a room."""
        service_coordinator, room_id = resolve_room(call.data[ATTR_ENTITY_ID])
        day_type = call.data[ATTR_DAY_TYPE]
        try:
            day_schedule = json.loads(call.data[ATTR_SCHEDULE])
        except json.JSONDecodeError as err:
            raise HomeAssistantError(f"Invalid schedule JSON: {err}") from err
        if not isinstance(day_schedule, list):
            raise HomeAssistantError("schedule must be a JSON list of blocks")
        try:
            await service_coordinator.api.set_zone_schedule(room_id, day_type, day_schedule)
            room = service_coordinator.data.rooms.get(room_id)
            if room:
                room.schedule = [
                    block
                    for block in room.schedule
                    if block.get("dayType") != day_type
                ] + [{**block, "dayType": day_type} for block in day_schedule]
            await service_coordinator.async_request_refresh()
            _LOGGER.info("Schedule updated for room %s (%s)", room_id, day_type)
        except TadoXApiError as err:
            raise HomeAssistantError(f"Failed to set schedule: {err}") from err

    async def async_get_day_report(call: ServiceCall) -> dict[str, Any]:
        """Return the historical day report for a room."""
        service_coordinator, room_id = resolve_room(call.data[ATTR_ENTITY_ID])
        report_date = call.data.get(ATTR_DATE) or datetime.now().date().isoformat()
        try:
            report = await service_coordinator.api.get_zone_day_report(room_id, report_date)
        except TadoXApiError as err:
            raise HomeAssistantError(f"Failed to get day report: {err}") from err
        return {
            "date": report_date,
            "report": report,
            "summary": summarize_day_report(report),
        }

    async def async_get_holiday_mode(call: ServiceCall) -> dict[str, Any]:
        """Return the current holiday mode settings."""
        service_coordinator = get_home_coordinator(call)
        try:
            return await service_coordinator.api.get_holiday_mode()
        except TadoXApiError as err:
            raise HomeAssistantError(f"Failed to get holiday mode: {err}") from err

    async def async_set_holiday_mode(call: ServiceCall) -> None:
        """Enable/disable holiday mode and optionally set its date range."""
        service_coordinator = get_home_coordinator(call)
        try:
            await service_coordinator.api.set_holiday_mode(
                enabled=call.data[ATTR_ENABLED],
                start_date=call.data.get(ATTR_START_DATE),
                end_date=call.data.get(ATTR_END_DATE),
            )
            await service_coordinator.async_request_refresh()
            _LOGGER.info("Holiday mode set to %s", call.data[ATTR_ENABLED])
        except TadoXApiError as err:
            raise HomeAssistantError(f"Failed to set holiday mode: {err}") from err

    # Register schedule / historic data services (only once per integration)
    if not hass.services.has_service(DOMAIN, SERVICE_GET_SCHEDULE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_SCHEDULE,
            async_get_schedule,
            schema=SERVICE_GET_SCHEDULE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_SCHEDULE,
            async_set_schedule,
            schema=SERVICE_SET_SCHEDULE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_GET_DAY_REPORT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_DAY_REPORT,
            async_get_day_report,
            schema=SERVICE_GET_DAY_REPORT_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_GET_HOLIDAY_MODE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_HOLIDAY_MODE,
            async_get_holiday_mode,
            schema=SERVICE_GET_HOLIDAY_MODE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_HOLIDAY_MODE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_HOLIDAY_MODE,
            async_set_holiday_mode,
            schema=SERVICE_SET_HOLIDAY_MODE_SCHEMA,
        )

    # Register parent devices before entities so links use actual registry IDs.
    device_registry_instance = dr.async_get(hass)
    home_device = device_registry_instance.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, str(home_id))},
        name=f"{home_name} Home",
        manufacturer="Tado",
        model="Tado X Home",
    )
    coordinator.parent_device_ids[(DOMAIN, str(home_id))] = home_device.id

    for room_id, room in coordinator.data.rooms.items():
        room_identifier = (DOMAIN, f"{home_id}_{room_id}")
        room_device = device_registry_instance.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={room_identifier},
            name=room.name,
            manufacturer="Tado",
            model="Tado X Room",
            via_device_id=home_device.id,
        )
        coordinator.parent_device_ids[room_identifier] = room_device.id

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
