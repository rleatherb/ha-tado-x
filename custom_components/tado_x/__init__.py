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
from .schedule_profiles import ProfileManager, DEFAULT_PROFILES, base_default_name
from .const import CONF_ENABLE_SCHEDULE_POLLING, CONF_SCHEDULE_POLL_INTERVAL, DEFAULT_SCHEDULE_POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)

# Service constants
SERVICE_SET_TEMPERATURE_OFFSET: Final = "set_temperature_offset"
ATTR_OFFSET: Final = "offset"

SERVICE_ADD_METER_READING: Final = "add_meter_reading"
ATTR_HOME_DEVICE_ID: Final = "device_id"
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
SERVICE_GET_AWAY_SCHEDULE: Final = "get_away_schedule"
SERVICE_SET_AWAY_SCHEDULE: Final = "set_away_schedule"
SERVICE_SAVE_PROFILE = "save_schedule_profile"
SERVICE_RESTORE_PROFILE = "restore_schedule_profile"
SERVICE_LIST_PROFILES = "list_schedule_profiles"
SERVICE_DELETE_PROFILE = "delete_schedule_profile"
SERVICE_RESET_DEFAULT = "reset_schedule_default"
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
        vol.Optional(ATTR_HOME_DEVICE_ID): cv.string,
    }
)

SERVICE_SET_EIQ_TARIFF_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TARIFF): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Required(ATTR_UNIT): vol.In(["m3", "kWh"]),
        vol.Optional(ATTR_START_DATE): cv.string,
        vol.Optional(ATTR_END_DATE): cv.string,
        vol.Optional(ATTR_HOME_DEVICE_ID): cv.string,
    }
)

SERVICE_SET_CLIMATE_TIMER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_TEMPERATURE): vol.All(
            vol.Coerce(float),
            vol.Range(min=5.0, max=30.0),
        ),
        vol.Optional(ATTR_DURATION): vol.All(
            vol.Coerce(int),
            vol.Range(min=1, max=1440),  # 1 minute to 24 hours
        ),
        vol.Optional(ATTR_TERMINATION_TYPE, default=TERMINATION_TIMER): vol.In(
            [TERMINATION_TIMER, TERMINATION_MANUAL, TERMINATION_NEXT_TIME_BLOCK]
        ),
    }
)


RESET_DEFAULT_SCHEMA = vol.Schema({vol.Optional(ATTR_HOME_DEVICE_ID): cv.string, vol.Required("profile"): vol.In(DEFAULT_PROFILES)})

PROFILE_SCHEMA = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id, vol.Required("profile"): vol.All(cv.string, vol.Length(min=1, max=80))})

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

SERVICE_SET_AWAY_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required("mode"): vol.In(["ON", "OFF"]),
        vol.Optional("away_temperature"): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=49.9)),
        vol.Optional("preheating_level"): vol.In(["OFF", "ECO", "BALANCE", "COMFORT"]),
    }
)

SERVICE_GET_DAY_REPORT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_DATE): cv.string,
    }
)

SERVICE_GET_HOLIDAY_MODE_SCHEMA = vol.Schema({vol.Optional(ATTR_HOME_DEVICE_ID): cv.string})

SERVICE_SET_HOLIDAY_MODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENABLED): cv.boolean,
        vol.Optional(ATTR_HOME_DEVICE_ID): cv.string,
        vol.Optional(ATTR_START_DATE): cv.string,
        vol.Optional(ATTR_END_DATE): cv.string,
    }
)


def _validate_schedule_blocks(day_type: str, blocks: list[Any]) -> None:
    """Validate a complete, contiguous 24-hour Tado day schedule."""
    if not blocks:
        raise HomeAssistantError("Schedule must contain at least one block")

    def minutes(value: Any, field: str, index: int) -> int:
        if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
            raise HomeAssistantError(
                f"Block {index}: {field} must be HH:MM (00:00 to 24:00)"
            )
        hh, mm = value[:2], value[3:]
        if not (hh.isdigit() and mm.isdigit()):
            raise HomeAssistantError(f"Block {index}: invalid {field} time {value!r}")
        hour, minute = int(hh), int(mm)
        if hour > 24 or minute > 59 or (hour == 24 and minute != 0):
            raise HomeAssistantError(f"Block {index}: invalid {field} time {value!r}")
        return hour * 60 + minute

    previous_end = 0
    for index, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            raise HomeAssistantError(f"Block {index}: must be a JSON object")
        if block.get("dayType") != day_type:
            raise HomeAssistantError(
                f"Block {index}: dayType must match selected day type {day_type}"
            )
        start = minutes(block.get("start"), "start", index)
        end = minutes(block.get("end"), "end", index)
        if index == 1 and start != 0:
            raise HomeAssistantError("Day schedule must start at 00:00")
        if start != previous_end:
            raise HomeAssistantError(
                f"Block {index}: start must equal previous block end "
                f"({previous_end // 60:02d}:{previous_end % 60:02d}); "
                "gaps and overlaps are not allowed"
            )
        if end <= start:
            raise HomeAssistantError(f"Block {index}: end must be after start")
        setting = block.get("setting")
        if not isinstance(setting, dict):
            raise HomeAssistantError(f"Block {index}: setting must be an object")
        if setting.get("power") not in ("ON", "OFF"):
            raise HomeAssistantError(f"Block {index}: setting.power must be ON or OFF")
        temperature = setting.get("temperature")
        if setting["power"] == "ON":
            if not isinstance(temperature, dict) or not isinstance(temperature.get("value"), (int, float)) or isinstance(temperature.get("value"), bool):
                raise HomeAssistantError(
                    f"Block {index}: ON requires setting.temperature.value as a number"
                )
        elif temperature is not None and not isinstance(temperature, dict):
            raise HomeAssistantError(f"Block {index}: temperature must be an object or null")
        previous_end = end
    if previous_end != 1440:
        raise HomeAssistantError("Day schedule must end at 24:00")


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

    # Learned reset observations are independent of the obsolete fixed-clock
    # api_reset_time persisted by older versions.
    last_reset = None
    if entry.data.get("api_last_observed_reset"):
        try:
            last_reset = datetime.fromisoformat(entry.data["api_last_observed_reset"])
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
        api_reset_time=None,  # Do not migrate the old hard-coded reset time.
        api_last_reset=last_reset,
        api_previous_used=entry.data.get("api_previous_server_used"),
        api_previous_quota=entry.data.get("api_previous_server_quota"),
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
                CONF_API_RESET_TIME: api.api_reset_time.isoformat() if api.api_reset_time else None,
                "api_last_observed_reset": api.api_last_reset.isoformat() if api.api_last_reset else None,
                "api_previous_server_used": api.api_previous_used,
                "api_previous_server_quota": api.api_previous_quota,
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
                CONF_API_RESET_TIME: api.api_reset_time.isoformat() if api.api_reset_time else None,
                "api_last_observed_reset": api.api_last_reset.isoformat() if api.api_last_reset else None,
                "api_previous_server_used": api.api_previous_used,
                "api_previous_server_quota": api.api_previous_quota,
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

    profile_manager = ProfileManager(hass, coordinator, _validate_schedule_blocks)
    await profile_manager.load()
    # Capture actual Tado Home and Away settings before the selectors appear.
    # Only rooms without a persisted Normal profile are read and saved.
    await profile_manager.initialize_normal_profiles()
    coordinator.schedule_profile_manager = profile_manager
    profile_manager.configure_polling(
        entry.data.get(CONF_ENABLE_SCHEDULE_POLLING, has_auto_assist),
        entry.data.get(CONF_SCHEDULE_POLL_INTERVAL, DEFAULT_SCHEDULE_POLL_INTERVAL),
    )
    # Only perform automatic startup comparison when periodic checks are enabled.
    if profile_manager.poll_enabled:
        entry.async_create_background_task(hass, profile_manager.poll(), "Tado X initial schedule profile comparison")

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
                "Multiple Tado X homes are configured. Select a Tado X Home device."
            )
        return next(iter(coordinators.values()))

    def get_home_coordinator(call: ServiceCall) -> TadoXDataUpdateCoordinator:
        """Resolve an optional Tado X Home device for a home-level action."""
        selected_device_id = call.data.get(ATTR_HOME_DEVICE_ID)
        if not selected_device_id:
            return get_default_coordinator()

        device = dr.async_get(hass).async_get(selected_device_id)
        if device is None:
            raise HomeAssistantError("Selected Tado X Home device was not found")

        # The Home device has the (DOMAIN, home_id) identifier. Room
        # devices have a different identifier and cannot be selected.
        matches = [
            coordinator
            for entry_id, coordinator in hass.data.get(DOMAIN, {}).items()
            if entry_id in device.config_entries
            and (DOMAIN, str(coordinator.home_id)) in device.identifiers
        ]
        if len(matches) != 1:
            raise HomeAssistantError(
                "Select the Tado X Home device (not a room, thermostat or valve device)"
            )
        return matches[0]

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
        termination_type = call.data.get(ATTR_TERMINATION_TYPE, TERMINATION_TIMER)
        temperature = call.data.get(ATTR_TEMPERATURE)
        duration_minutes = call.data.get(ATTR_DURATION)

        if temperature is None:
            raise HomeAssistantError(
                "Temperature is required for Timer, Manual and Next time block termination types"
            )
        if termination_type == TERMINATION_TIMER and duration_minutes is None:
            raise HomeAssistantError("Duration is required for Timer termination type")

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

        duration_seconds = duration_minutes * 60 if termination_type == TERMINATION_TIMER else None

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
                "Set %s to %.1f°C with termination %s (duration %s minutes, home %s, room %s)",
                entity_id,
                temperature,
                termination_type,
                duration_minutes if termination_type == TERMINATION_TIMER else "n/a",
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

    async def async_get_away_schedule(call: ServiceCall) -> dict[str, Any]:
        """Return the stored per-room Away configuration, without changing it."""
        service_coordinator, room_id = resolve_room(call.data[ATTR_ENTITY_ID])
        try:
            return await service_coordinator.api.get_zone_away_configuration(room_id)
        except TadoXApiError as err:
            raise HomeAssistantError(f"Failed to get Away schedule: {err}") from err

    async def async_set_away_schedule(call: ServiceCall) -> None:
        """Update a room's stored Away settings without changing saved profiles."""
        service_coordinator, room_id = resolve_room(call.data[ATTR_ENTITY_ID])
        try:
            previous = await service_coordinator.api.get_zone_away_configuration(room_id)
            mode = call.data["mode"]
            temperature = (
                call.data.get("away_temperature", previous.get("awayTemperatureCelsius"))
                if mode == "ON" else None
            )
            if mode == "ON" and temperature is None:
                raise HomeAssistantError(
                    "away_temperature is required when switching Away mode ON "
                    "from a configuration without an Away temperature"
                )
            configuration = {
                "mode": mode,
                "awayTemperatureCelsius": temperature,
                "preheatingLevel": call.data.get(
                    "preheating_level", previous["preheatingLevel"]
                ),
            }
            await service_coordinator.api.set_zone_away_configuration(room_id, configuration)
            manager = getattr(service_coordinator, "schedule_profile_manager", None)
            if manager is not None:
                await manager.check(room_id)
            _LOGGER.info("Away schedule updated for room %s (home %s)", room_id, service_coordinator.home_id)
        except TadoXApiError as err:
            raise HomeAssistantError(f"Failed to set Away schedule: {err}") from err

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
        _validate_schedule_blocks(day_type, day_schedule)
        try:
            await service_coordinator.api.set_zone_schedule(room_id, day_type, day_schedule)
            await service_coordinator.async_request_refresh()
            _LOGGER.info("Schedule updated for room %s (%s)", room_id, day_type)
        except TadoXApiError as err:
            raise HomeAssistantError(f"Failed to set schedule: {err}") from err

    def profile_target(call):
        service_coordinator, room_id = resolve_room(call.data[ATTR_ENTITY_ID])
        return service_coordinator.schedule_profile_manager, room_id

    async def async_save_profile(call):
        manager, room_id = profile_target(call)
        try:
            profile = call.data["profile"].strip()
            default = base_default_name(profile)
            if default is not None:
                await manager.save_home_default(room_id, default)
            else:
                await manager.save(room_id, profile)
        except Exception as err:
            raise HomeAssistantError(f"Failed to save schedule profile: {err}") from err

    async def async_restore_profile(call):
        manager, room_id = profile_target(call)
        try:
            await manager.restore(room_id, call.data["profile"].strip())
        except Exception as err:
            raise HomeAssistantError(f"Failed to restore schedule profile: {err}") from err

    async def async_list_profiles(call):
        manager, room_id = profile_target(call)
        key = str(room_id)
        return {"profiles": [{"name": n, "saved": n in manager.profiles.get(key, {})} for n in manager.names(room_id)],
                "home_defaults": [{"name": f"{n} default", "customized": n in manager.home_defaults}
                                  for n in DEFAULT_PROFILES],
                "current": manager.selected(room_id), "status": manager.status.get(key, "Unknown"),
                "matching_profiles": manager.matches.get(key, [])}

    async def async_delete_profile(call):
        manager, room_id = profile_target(call)
        await manager.delete(room_id, call.data["profile"].strip())

    async def async_reset_schedule_default(call):
        service_coordinator = get_home_coordinator(call)
        manager = service_coordinator.schedule_profile_manager
        name = call.data["profile"].strip()
        profile = base_default_name(name) or name
        await manager.reset_home_default(profile)

    for service_name, handler, schema, response in (
        (SERVICE_RESET_DEFAULT, async_reset_schedule_default, RESET_DEFAULT_SCHEMA, SupportsResponse.NONE),
        (SERVICE_SAVE_PROFILE, async_save_profile, PROFILE_SCHEMA, SupportsResponse.NONE),
        (SERVICE_RESTORE_PROFILE, async_restore_profile, PROFILE_SCHEMA, SupportsResponse.NONE),
        (SERVICE_LIST_PROFILES, async_list_profiles, SERVICE_GET_SCHEDULE_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_DELETE_PROFILE, async_delete_profile, PROFILE_SCHEMA, SupportsResponse.NONE),
    ):
        if not hass.services.has_service(DOMAIN, service_name):
            hass.services.async_register(DOMAIN, service_name, handler, schema=schema, supports_response=response)

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

    if not hass.services.has_service(DOMAIN, SERVICE_GET_AWAY_SCHEDULE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_AWAY_SCHEDULE,
            async_get_away_schedule,
            schema=SERVICE_GET_SCHEDULE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_AWAY_SCHEDULE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_AWAY_SCHEDULE,
            async_set_away_schedule,
            schema=SERVICE_SET_AWAY_SCHEDULE_SCHEMA,
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
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        if hasattr(coordinator, "schedule_profile_manager"):
            coordinator.schedule_profile_manager.stop()

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
