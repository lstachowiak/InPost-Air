"""The InPost Air integration."""

from __future__ import annotations
from dataclasses import asdict, dataclass
import logging

from dacite import from_dict
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, ConfigEntryError
from homeassistant.helpers import device_registry as dr

from custom_components.inpost_air.coordinator import InPostAirDataCoordinator
from custom_components.inpost_air.models import ParcelLocker
from custom_components.inpost_air.utils import get_device_info, get_parcel_locker_url

from .api import InPostAirApiClientError, InPostAirPoint, InPostApi

_LOGGER = logging.getLogger(__name__)


@dataclass
class InPostAirData:
    """
    Represents data related to InPost Air service.
    """

    parcel_locker: ParcelLocker
    coordinator: InPostAirDataCoordinator


type InPostAirConfiEntry = ConfigEntry[InPostAirData]

PLATFORMS: list[Platform] = [Platform.SENSOR]


def get_configured_point(entry: InPostAirConfiEntry) -> InPostAirPoint:
    """Read parcel locker data stored in the config entry."""
    entry_data = entry.data.get("parcel_locker")

    if entry_data is None:
        raise ConfigEntryError(
            "Config entry does not contain any parcel locker data. "
            "Please remove the integration and set it up again."
        )

    if isinstance(entry_data, InPostAirPoint):
        return entry_data

    try:
        return from_dict(InPostAirPoint, entry_data)
    except Exception as ex:
        raise ConfigEntryError(
            f"Parcel locker data stored in the config entry is invalid: {ex}"
        ) from ex


async def resolve_parcel_locker(
    hass: HomeAssistant,
    entry: InPostAirConfiEntry,
    api_client: InPostApi,
    point: InPostAirPoint,
) -> tuple[InPostAirPoint, str]:
    """Find the parcel locker ID, refreshing outdated config entry data if needed."""
    refreshed_point = None

    try:
        if (locker_id := await api_client.find_parcel_locker_id(point)) is not None:
            return point, locker_id

        _LOGGER.info(
            "Refetching data of parcel locker %s - the one stored in the config entry might be outdated",
            point.n,
        )
        refreshed_point = await api_client.refresh_parcel_locker(point.n)

        if refreshed_point is not None and refreshed_point != point:
            locker_id = await api_client.find_parcel_locker_id(refreshed_point)
    except InPostAirApiClientError as err:
        # Transient problem on InPost side - let Home Assistant retry the setup.
        raise ConfigEntryNotReady(f"Error communicating with InPost: {err}") from err

    if locker_id is None:
        raise ConfigEntryNotReady(
            f"Could not find air quality data of parcel locker {point.n} on {get_parcel_locker_url(point)}"
        )

    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "parcel_locker": asdict(refreshed_point)},
    )

    return refreshed_point, locker_id


async def async_setup_entry(hass: HomeAssistant, entry: InPostAirConfiEntry) -> bool:
    """Set up InPost Air from a config entry."""
    api_client = InPostApi(hass)
    point = get_configured_point(entry)

    point, parcel_locker_id = await resolve_parcel_locker(
        hass, entry, api_client, point
    )

    parcel_locker = ParcelLocker(point.n, parcel_locker_id)
    coordinator = InPostAirDataCoordinator(hass, api_client, parcel_locker)

    entry.runtime_data = InPostAirData(parcel_locker, coordinator)

    await coordinator.async_config_entry_first_refresh()

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers=get_device_info(parcel_locker).get("identifiers"),
        name=f"Parcel locker {parcel_locker.locker_code}",
        manufacturer="InPost",
        configuration_url=get_parcel_locker_url(point),
        entry_type=dr.DeviceEntryType.SERVICE,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: InPostAirConfiEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, config_entry: InPostAirConfiEntry):
    """Migrate old entry."""
    _LOGGER.debug(
        "Migrating %s from version %s", config_entry.title, config_entry.version
    )

    if config_entry.version > 2:
        # This means the user has downgraded from a future version
        _LOGGER.error(
            "Cannot migrate %s from version %s - it was created by a newer version of the integration",
            config_entry.title,
            config_entry.version,
        )
        return False

    if config_entry.version == 1:
        hass.config_entries.async_update_entry(
            config_entry,
            data={
                "parcel_locker": asdict(from_dict(InPostAirPoint, config_entry.data))
            },
            version=2,
        )

    _LOGGER.debug(
        "Migrating %s to version %s completed", config_entry.title, config_entry.version
    )

    return True
