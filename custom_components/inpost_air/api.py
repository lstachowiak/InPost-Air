"""Functions to connect to InPost APIs."""

import asyncio
from dataclasses import dataclass
import json
import logging
import re
from typing import Any
from aiohttp import ClientResponseError
from dacite import from_dict
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from custom_components.inpost_air.models import InPostAirPoint
from custom_components.inpost_air.utils import get_parcel_locker_url

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30

AIR_DATA_PATTERN = re.compile(
    r"data-shipx-url=\"/shipx-point-data/(.*?)/(.*?)/air_index_level\""
)


@dataclass
class ApiResponse:
    """API response with an already read body."""

    url: str
    body: str

    def json(self) -> Any:
        """Parse the body as JSON."""
        return json.loads(self.body)


@dataclass
class ParcelLockerListResponse:
    date: str
    page: int
    total_pages: int
    items: list[InPostAirPoint]


@dataclass
class ParcelLockerAirDataResponse:
    message: str
    air_index_level: str
    air_sensors: list[str]


class InPostApi:
    """Helper functions for the Air integration."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Init class."""
        self.hass = hass
        self.session = async_create_clientsession(hass)

    async def _request(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        raise_client_response_error: bool = False,
    ) -> ApiResponse:
        """Get information from the API.

        The body is read inside the timeout on purpose - `session.request`
        returns as soon as the response headers arrive, so reading the body
        outside of it would leave that part of the request without any limit.
        """
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                response = await self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                )
                response.raise_for_status()

                return ApiResponse(url=str(response.url), body=await response.text())

        except TimeoutError as e:
            _LOGGER.warning("Request to %s timed out", url)
            raise InPostAirApiClientError("Request timed out") from e
        except ClientResponseError as e:
            if raise_client_response_error:
                raise
            _LOGGER.warning("Request to %s failed with status %s", url, e.status)
            raise InPostAirApiClientError(
                f"Request to {url} failed with status {e.status}"
            ) from e
        except Exception as exception:  # pylint: disable=broad-except
            _LOGGER.warning("Request to %s failed: %s", url, exception)
            raise InPostAirApiClientError(
                "Something really wrong happened!"
            ) from exception

    async def _search_easypack24_locker(self, locker_code: str) -> dict | None:
        """Find info about given parcel locker."""
        if not locker_code or locker_code == "":
            return None

        response = await self._request(
            method="get",
            url="https://api-shipx-pl.easypack24.net/v1/points/" + locker_code,
        )
        resp = response.json()

        error = resp.get("error")
        if error:
            _LOGGER.warning(f"easypack24.net for {locker_code} returned error: {resp}")
            return None

        resp_address = resp.get("address_details", {})
        location = resp.get("location", {"latitude": "0", "longitude": "0"})
        city = resp_address.get("city") or ""

        parcel_locker = {
            "n": resp["name"],
            "t": 1,
            "d": resp["location_description"],
            "m": resp.get("apm_doubled") or "",
            "q": resp.get("partner_id") or "",
            "f": resp.get("physical_type_mapped") or "",
            "c": city,
            "g": city.lower(),
            "e": resp_address.get("street") or "",
            "r": resp_address.get("province") or "",
            "o": resp_address.get("post_code") or "",
            "b": resp_address.get("building_number") or "",
            "h": resp.get("opening_hours") or "",
            "i": "[]",  # Unknown
            "l": {"a": location["latitude"], "o": location["longitude"]},
            "p": 1 if resp.get("payment_type", {"0": ""}) == "0" else 0,
            "s": 1,  # Unkown - most lockers have 1 here
        }
        return parcel_locker

    async def search_parcel_locker(self, locker_code: str) -> InPostAirPoint | None:
        """Find info about given parcel locker."""
        if not locker_code or locker_code == "":
            return None

        response = await self._request(
            method="get", url="https://inpost.pl/sites/default/files/points.json"
        )
        parcel_locker = next(
            (x for x in response.json().get("items") if x.get("n") == locker_code),
            None,
        )

        if not parcel_locker:
            parcel_locker = await self._search_easypack24_locker(locker_code)

        return from_dict(InPostAirPoint, parcel_locker) if parcel_locker else None

    async def refresh_parcel_locker(self, locker_code: str) -> InPostAirPoint | None:
        """Get current data of an already known parcel locker.

        Data stored in a config entry gets outdated when InPost changes details
        of a parcel locker, so it has to be refetched. The single point endpoint
        is tried first - the full list is a few megabytes of JSON.
        """
        parcel_locker = await self._search_easypack24_locker(locker_code)

        if parcel_locker is None:
            return await self.search_parcel_locker(locker_code)

        return from_dict(InPostAirPoint, parcel_locker)

    async def get_parcel_lockers_list(self) -> list[InPostAirPoint]:
        """Get parcel lockers list."""
        response = await self._request(
            method="get", url="https://inpost.pl/sites/default/files/points.json"
        )
        response_data = from_dict(ParcelLockerListResponse, response.json())

        return response_data.items

    async def find_parcel_locker_id(self, point: InPostAirPoint) -> str | None:
        """Find parcel locker ID by its code."""
        url = get_parcel_locker_url(point)
        response = await self._request(method="get", url=url)

        if response.url.rstrip("/") != url.rstrip("/"):
            _LOGGER.warning(
                "Page of parcel locker %s (%s) redirected to %s - data stored for that parcel locker is outdated",
                point.n,
                url,
                response.url,
            )
            return None

        match = AIR_DATA_PATTERN.search(response.body)

        if match is None:
            _LOGGER.warning(
                "Could not find air quality data of parcel locker %s on %s",
                point.n,
                url,
            )
            return None

        return match.group(1)

    async def get_parcel_locker_air_data(
        self, locker_code: str, locker_id: str
    ) -> ParcelLockerAirDataResponse:
        """Get air data from parcel locker."""
        try:
            response = await self._request(
                method="post",
                url=f"https://inpost.pl/shipx-point-data/{locker_id}/{locker_code}/air_index_level",
                headers={"X-Requested-With": "XMLHttpRequest"},
                raise_client_response_error=True,
            )
        except ClientResponseError as e:
            if e.status == 404:
                raise InPostAirApiClientSensorsMissingError(
                    "Air sensors are not available"
                ) from e
            raise InPostAirApiClientError(
                f"Request for air data of parcel locker {locker_code} failed with status {e.status}"
            ) from e

        return from_dict(ParcelLockerAirDataResponse, response.json())


class InPostAirApiClientError(Exception):
    """Exception to indicate a general API error."""


class InPostAirApiClientSensorsMissingError(InPostAirApiClientError):
    """Exception to indicate missing air sensors error"""
