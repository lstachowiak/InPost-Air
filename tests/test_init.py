"""Define tests for setting up a config entry."""

from dataclasses import asdict
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inpost_air.api import (
    InPostAirApiClientError,
    InPostAirApiClientSensorsMissingError,
    InPostApi,
    ParcelLockerAirDataResponse,
)
from custom_components.inpost_air.const import DOMAIN
from custom_components.inpost_air.models import (
    InPostAirPoint,
    InPostAirPointCoordinates,
)

mocked_point = InPostAirPoint(
    "AJE01BAPP",
    1,
    "Market Dino",
    "",
    "",
    "006",
    "Andrzejewo",
    "andrzejewo",
    "Warszawska",
    "mazowieckie",
    "07-305",
    "62A",
    "24/7",
    "[]",
    InPostAirPointCoordinates(52.83679, 22.20968),
    0,
    1,
)

mocked_air_data = ParcelLockerAirDataResponse(
    message="",
    air_index_level="good",
    air_sensors=["PM25:10.0:20.0", "TEMPERATURE:21.5:", "HUMIDITY:50.0:"],
)


def create_entry(hass, data=None) -> MockConfigEntry:
    """Add a config entry to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        unique_id=mocked_point.n,
        title=f"Parcel locker {mocked_point.n}",
        data={"parcel_locker": asdict(mocked_point)} if data is None else data,
    )
    entry.add_to_hass(hass)
    return entry


async def setup_entry(hass, entry: MockConfigEntry) -> None:
    """Run setup of the given config entry."""
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_entry(hass):
    """Entry is loaded when parcel locker data can be fetched."""
    entry = create_entry(hass)

    with (
        patch.object(InPostApi, "find_parcel_locker_id", return_value="56311"),
        patch.object(
            InPostApi, "get_parcel_locker_air_data", return_value=mocked_air_data
        ),
    ):
        await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.parcel_locker.locker_id == "56311"
    assert hass.states.get("sensor.parcel_locker_aje01bapp_temperature") is not None


async def test_setup_entry_without_parcel_locker_data(hass, caplog):
    """Entry with no parcel locker data fails with a logged error, not silently."""
    entry = create_entry(hass, data={})

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None and entry.reason != "Unknown error"
    assert "does not contain any parcel locker data" in caplog.text


async def test_setup_entry_with_invalid_parcel_locker_data(hass, caplog):
    """Entry with broken parcel locker data fails with a logged error."""
    entry = create_entry(hass, data={"parcel_locker": {"n": "AJE01BAPP"}})

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "is invalid" in caplog.text


async def test_setup_entry_retries_when_parcel_locker_id_not_found(hass):
    """Missing parcel locker ID schedules a retry instead of killing the entry."""
    entry = create_entry(hass)

    with (
        patch.object(InPostApi, "find_parcel_locker_id", return_value=None),
        patch.object(InPostApi, "refresh_parcel_locker", return_value=mocked_point),
    ):
        await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.reason is not None and "AJE01BAPP" in entry.reason


async def test_setup_entry_retries_on_api_error(hass):
    """API errors schedule a retry instead of killing the entry."""
    entry = create_entry(hass)

    with patch.object(
        InPostApi,
        "find_parcel_locker_id",
        side_effect=InPostAirApiClientError("Request timed out"),
    ):
        await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_entry_refreshes_outdated_parcel_locker_data(hass):
    """Outdated data stored in the entry is refetched and saved."""
    entry = create_entry(hass)
    refreshed_point = InPostAirPoint(**{**asdict(mocked_point), "e": "Nowa Warszawska"})
    refreshed_point.l = InPostAirPointCoordinates(**asdict(mocked_point).get("l"))

    def find_parcel_locker_id(_self, point: InPostAirPoint):
        return "56311" if point.e == "Nowa Warszawska" else None

    with (
        patch.object(
            InPostApi,
            "find_parcel_locker_id",
            autospec=True,
            side_effect=find_parcel_locker_id,
        ),
        patch.object(InPostApi, "refresh_parcel_locker", return_value=refreshed_point),
        patch.object(
            InPostApi, "get_parcel_locker_air_data", return_value=mocked_air_data
        ),
    ):
        await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["parcel_locker"] == asdict(refreshed_point)


async def test_setup_entry_without_air_sensors(hass):
    """Parcel locker without air sensors fails permanently, without retrying."""
    entry = create_entry(hass)

    with (
        patch.object(InPostApi, "find_parcel_locker_id", return_value="56311"),
        patch.object(
            InPostApi,
            "get_parcel_locker_air_data",
            side_effect=InPostAirApiClientSensorsMissingError(
                "Air sensors are not available"
            ),
        ),
    ):
        await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason == "Air sensors are not available"


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_unload_entry(hass):
    """Entry can be unloaded."""
    entry = create_entry(hass)

    with (
        patch.object(InPostApi, "find_parcel_locker_id", return_value="56311"),
        patch.object(
            InPostApi, "get_parcel_locker_air_data", return_value=mocked_air_data
        ),
    ):
        await setup_entry(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_migrate_entry_from_version_1(hass):
    """Version 1 entry data is migrated into a serializable dict."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        unique_id=mocked_point.n,
        data=asdict(mocked_point),
    )
    entry.add_to_hass(hass)

    with (
        patch.object(InPostApi, "find_parcel_locker_id", return_value="56311"),
        patch.object(
            InPostApi, "get_parcel_locker_air_data", return_value=mocked_air_data
        ),
    ):
        await setup_entry(hass, entry)

    assert entry.version == 2
    assert entry.data == {"parcel_locker": asdict(mocked_point)}
    assert entry.state is ConfigEntryState.LOADED
