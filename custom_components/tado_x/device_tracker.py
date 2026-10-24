"""Device tracker platform for Tado X mobile devices."""
from __future__ import annotations

import logging

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TadoXDataUpdateCoordinator, TadoXMobileDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tado X device tracker entities."""
    coordinator: TadoXDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[TrackerEntity] = []

    # Add device trackers for each mobile device (only if feature is enabled)
    if coordinator.enable_mobile_devices:
        for device_id in coordinator.data.mobile_devices:
            entities.append(TadoXMobileDeviceTracker(coordinator, device_id))

    async_add_entities(entities)


class TadoXMobileDeviceTracker(
    CoordinatorEntity[TadoXDataUpdateCoordinator], TrackerEntity
):
    """Tado X mobile device tracker entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TadoXDataUpdateCoordinator,
        device_id: int,
    ) -> None:
        """Initialize the device tracker entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{coordinator.home_id}_mobile_{device_id}"

    @property
    def _mobile_device(self) -> TadoXMobileDevice | None:
        """Get the mobile device data."""
        return self.coordinator.data.mobile_devices.get(self._device_id)

    @property
    def name(self) -> str:
        """Return the name of the device."""
        mobile = self._mobile_device
        if mobile:
            return mobile.name
        return f"Mobile {self._device_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        mobile = self._mobile_device
        metadata = mobile.device_metadata if mobile else {}

        # Get device details from metadata
        platform = metadata.get("platform", "Unknown")
        os_version = metadata.get("osVersion", "")
        model = metadata.get("model", "")

        # Build model string
        model_str = "Mobile Device"
        if platform and model:
            model_str = f"{platform} - {model}"
        elif platform:
            model_str = platform

        return DeviceInfo(
            identifiers={(DOMAIN, f"mobile_{self._device_id}")},
            name=mobile.name if mobile else f"Mobile {self._device_id}",
            manufacturer="Tado",
            model=model_str,
            sw_version=os_version if os_version else None,
            via_device_id=self.coordinator.parent_device_ids[(DOMAIN, str(self.coordinator.home_id))],
        )

    @property
    def source_type(self) -> SourceType:
        """Return the source type of the device tracker."""
        return SourceType.GPS

    @property
    def in_zones(self) -> list[str] | None:
        """Return Home Assistant zone IDs without deprecated location_name.

        Tado X mobile geofencing reports HOME/AWAY rather than GPS
        coordinates.  None represents unknown/not tracking; an empty list
        represents a known location outside the Home zone.
        """
        mobile = self._mobile_device
        if mobile is None or not mobile.geofencing_enabled:
            return None

        location = mobile.location
        if location is None:
            return None
        if location.upper() == "HOME":
            return ["zone.home"]
        if location.upper() == "AWAY":
            return []
        # Do not fabricate a zone entity ID from an unknown API value.
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """Return extra state attributes."""
        mobile = self._mobile_device
        if not mobile:
            return {}

        metadata = mobile.device_metadata
        return {
            "geofencing_enabled": mobile.geofencing_enabled,
            "at_home": mobile.at_home,
            "tado_location": mobile.location,
            "platform": metadata.get("platform"),
            "os_version": metadata.get("osVersion"),
            "model": metadata.get("model"),
            "locale": metadata.get("locale"),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
