"""Select platform for Tado X."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (DOMAIN, FEATURE_ENTITY_MAP, TERMINATION_MANUAL, TERMINATION_NEXT_TIME_BLOCK, TERMINATION_TIMER)
from .coordinator import TadoXDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Presence modes
PRESENCE_HOME = "home"
PRESENCE_AWAY = "away"
PRESENCE_AUTO = "auto"

PRESENCE_OPTIONS = [PRESENCE_HOME, PRESENCE_AWAY, PRESENCE_AUTO]

TERMINATION_OPTIONS = [
    TERMINATION_TIMER,
    TERMINATION_MANUAL,
    TERMINATION_NEXT_TIME_BLOCK,
]

# Domestic hot water modes
DHW_OFF = "OFF"
DHW_SCHEDULE = "SCHEDULE"
DHW_BOOST = "BOOST"

DHW_OPTIONS = [DHW_OFF, DHW_SCHEDULE, DHW_BOOST]


@dataclass(frozen=True, kw_only=True)
class TadoXSelectEntityDescription(SelectEntityDescription):
    """Describes a Tado X select entity."""

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tado X select entities."""
    coordinator: TadoXDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SelectEntity] = [
        TadoXPresenceSelect(coordinator, TadoXSelectEntityDescription(
            key="presence_mode",
            translation_key="presence_mode",
            icon="mdi:home-account",
            options=PRESENCE_OPTIONS,
        )),
        TadoXDhwModeSelect(coordinator, TadoXSelectEntityDescription(
            key="dhw_mode",
            translation_key="dhw_mode",
            icon="mdi:water-boiler",
            options=DHW_OPTIONS,
        )),
    ]

    if coordinator.data:
        for room_id in coordinator.data.rooms:
            entities.append(TadoXRoomTerminationType(coordinator, room_id))

    async_add_entities(entities)

    # Update entity enabled/disabled state based on feature flags
    entity_registry = er.async_get(hass)
    for entity_id, entity_entry in list(entity_registry.entities.items()):
        if entity_entry.platform != DOMAIN:
            continue

        if entity_entry.config_entry_id != entry.entry_id:
            continue

        # Preserve an explicit user decision to disable an entity.
        if entity_entry.disabled_by not in (None, er.RegistryEntryDisabler.INTEGRATION):
            continue

        if not entity_entry.unique_id:
            continue

        # Check if this entity matches a disabled feature
        should_be_disabled = False
        for feature_flag, entity_keys in FEATURE_ENTITY_MAP.items():
            feature_enabled = getattr(coordinator, feature_flag, False)

            # Check if entity_key matches any of the disabled feature's keys
            for entity_key in entity_keys:
                if entity_entry.unique_id.endswith(f"_{entity_key}"):
                    if not feature_enabled:
                        should_be_disabled = True
                    break

            if should_be_disabled:
                break

        # Update disabled state
        if should_be_disabled:
            entity_registry.async_update_entity(
                entity_id,
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            )
        else:
            # Make sure it's enabled if the feature is on
            if entity_entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION:
                entity_registry.async_update_entity(
                    entity_id,
                    disabled_by=None,
                )


class TadoXPresenceSelect(CoordinatorEntity[TadoXDataUpdateCoordinator], SelectEntity):
    """Select entity for Tado X home presence mode."""

    _attr_has_entity_name = True
    entity_description: TadoXSelectEntityDescription

    def __init__(
        self,
        coordinator: TadoXDataUpdateCoordinator,
        description: TadoXSelectEntityDescription,
    ) -> None:
        """Initialize the presence select entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.home_id}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for the home."""
        return DeviceInfo(
            identifiers={(DOMAIN, str(self.coordinator.home_id))},
            name=self.coordinator.home_name,
            manufacturer="Tado",
            model="Tado X Home",
        )

    @property
    def current_option(self) -> str | None:
        """Return the current presence mode."""
        data = self.coordinator.data
        if not data:
            return None

        # If presence is locked, user manually set home or away
        if data.presence_locked:
            if data.presence == "HOME":
                return PRESENCE_HOME
            elif data.presence == "AWAY":
                return PRESENCE_AWAY

        # Not locked = auto/geofencing mode
        return PRESENCE_AUTO

    async def async_select_option(self, option: str) -> None:
        """Change the presence mode."""
        try:
            if option == PRESENCE_HOME:
                await self.coordinator.api.set_presence_home()
            elif option == PRESENCE_AWAY:
                await self.coordinator.api.set_presence_away()
            elif option == PRESENCE_AUTO:
                await self.coordinator.api.set_presence_auto()

            # Refresh to get updated state
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set presence mode to %s: %s", option, err)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


class TadoXDhwModeSelect(CoordinatorEntity[TadoXDataUpdateCoordinator], SelectEntity):
    """Select entity for Tado X domestic hot water mode."""

    _attr_has_entity_name = True
    entity_description: TadoXSelectEntityDescription

    def __init__(
        self,
        coordinator: TadoXDataUpdateCoordinator,
        description: TadoXSelectEntityDescription,
    ) -> None:
        """Initialize the domestic hot water select entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.home_id}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for the home."""
        return DeviceInfo(
            identifiers={(DOMAIN, str(self.coordinator.home_id))},
            name=self.coordinator.home_name,
            manufacturer="Tado",
            model="Tado X Home",
        )

    @property
    def current_option(self) -> str | None:
        """Return the current domestic hot water mode."""
        data = self.coordinator.data
        if not data:
            return None

        if data.dhw_state in ("SCHEDULE_ON", "SCHEDULE_OFF", "AWAY_ON", "AWAY_OFF"):
            return DHW_SCHEDULE
        if data.dhw_state in DHW_OPTIONS:
            return data.dhw_state
        return None

    async def async_select_option(self, option: str) -> None:
        """Change the domestic hot water mode."""
        try:
            if option == DHW_BOOST:
                await self.coordinator.api.boost_domestic_hot_water()
            elif option == DHW_OFF:
                await self.coordinator.api.disable_domestic_hot_water()
            elif option == DHW_SCHEDULE:
                await self.coordinator.api.resume_domestic_hot_water_schedule()
            else:
                _LOGGER.error("Invalid domestic hot water mode selected: %s", option)
                return

            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set domestic hot water mode to %s: %s", option, err)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


class TadoXRoomTerminationType(
    CoordinatorEntity[TadoXDataUpdateCoordinator],
    RestoreEntity,
    SelectEntity,
):
    """Select entity for per-room termination type default."""

    _attr_has_entity_name = True
    _attr_translation_key = "termination_type"
    _attr_icon = "mdi:timer-cog-outline"
    _attr_options = TERMINATION_OPTIONS
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: TadoXDataUpdateCoordinator, room_id: int) -> None:
        """Initialize the room termination type entity."""
        super().__init__(coordinator)
        self._room_id = room_id
        self._attr_unique_id = f"{coordinator.home_id}_{room_id}_termination_type"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        room = self.coordinator.data.rooms.get(self._room_id) if self.coordinator.data else None
        room_name = room.name if room else f"Room {self._room_id}"
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.home_id}_{self._room_id}")},
            name=room_name,
            manufacturer="Tado",
            model="Tado X Room",
            via_device_id=self.coordinator.parent_device_ids[(DOMAIN, str(self.coordinator.home_id))],
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        data = self.coordinator.data
        return data is not None and self._room_id in data.rooms

    @property
    def current_option(self) -> str | None:
        """Return the current Tado termination type."""
        defaults = self.coordinator.get_room_control_defaults(self._room_id)
        return defaults.termination_type

    async def async_select_option(self, option: str) -> None:
        """Change the Tado manual-control termination type."""
        defaults = self.coordinator.get_room_control_defaults(self._room_id)

        # TIMER requires a duration.
        duration_seconds = None
        if option == TERMINATION_TIMER:
            duration_seconds = defaults.duration_minutes * 60

        await self.coordinator.api.set_room_manual_control_termination(
            self._room_id,
            option,
            duration_seconds=duration_seconds,
        )

        # Update locally immediately rather than waiting for the next poll.
        self.coordinator.set_room_control_defaults(
            self._room_id,
            termination_type=option,
        )

        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Initialize from the current Tado configuration."""
        await super().async_added_to_hass()
        self.async_write_ha_state()
