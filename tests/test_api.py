import asyncio
import os
from unittest.mock import patch

import pytest
import pytest_socket

from custom_components.inpost_air.api import (
    ApiResponse,
    InPostAirApiClientError,
    InPostApi,
)
from custom_components.inpost_air.models import (
    InPostAirPoint,
    InPostAirPointCoordinates,
)


@pytest.fixture()
def _allow_inpost_requests():
    pytest_socket.enable_socket()
    pytest_socket.socket_allow_hosts(["inpost.pl"])


@pytest.mark.parametrize("expected_lingering_timers", [True])
@pytest.mark.api
async def test_parcel_lockers_list(hass, _allow_inpost_requests):
    response = await InPostApi(hass).get_parcel_lockers_list()
    assert response is not None


@pytest.mark.parametrize("expected_lingering_timers", [True])
@pytest.mark.api
async def test_parcel_locker_search(hass, _allow_inpost_requests):
    response = await InPostApi(hass).search_parcel_locker("AJE01BAPP")
    assert response is not None


@pytest.mark.skipif(
    os.environ.get("CI") == "true", reason="InPost blocks Github IP address"
)
@pytest.mark.parametrize("expected_lingering_timers", [True])
@pytest.mark.api
async def test_find_parcel_locker_id(hass, _allow_inpost_requests):
    response = await InPostApi(hass).find_parcel_locker_id(
        InPostAirPoint(
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
    )
    assert response is not None


@pytest.mark.skipif(
    os.environ.get("CI") == "true", reason="InPost blocks Github IP address"
)
@pytest.mark.parametrize("expected_lingering_timers", [True])
@pytest.mark.api
async def test_air_data(hass, _allow_inpost_requests):
    response = await InPostApi(hass).get_parcel_locker_air_data("AJE01BAPP", "56311")
    assert response is not None


class _StallingResponse:
    """Response that never finishes sending its body."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.status = 200

    def raise_for_status(self) -> None:
        pass

    async def text(self) -> str:
        await asyncio.sleep(10)
        return ""


class _StallingSession:
    """Session returning responses with a stalled body."""

    async def request(self, method: str, url: str, headers=None) -> _StallingResponse:
        return _StallingResponse(url)


PAGE_WITH_AIR_DATA = (
    '<div data-shipx-url="/shipx-point-data/56311/AJE01BAPP/air_index_level"></div>'
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

mocked_point_url = (
    "https://inpost.pl/paczkomat-andrzejewo-aje01bapp-warszawska-paczkomaty-mazowieckie"
)


async def test_request_times_out_while_reading_body(hass):
    """Reading the body is covered by the request timeout."""
    api = InPostApi(hass)
    api.session = _StallingSession()

    with patch("custom_components.inpost_air.api.REQUEST_TIMEOUT", 0.05):
        with pytest.raises(InPostAirApiClientError, match="Request timed out"):
            await api._request(method="get", url="https://inpost.pl/whatever")


async def test_find_parcel_locker_id_from_page(hass):
    """Parcel locker ID is scraped from its page."""
    api = InPostApi(hass)

    with patch.object(
        InPostApi,
        "_request",
        return_value=ApiResponse(url=mocked_point_url, body=PAGE_WITH_AIR_DATA),
    ):
        assert await api.find_parcel_locker_id(mocked_point) == "56311"


async def test_find_parcel_locker_id_without_air_data(hass, caplog):
    """Missing air data on the page is reported instead of failing silently."""
    api = InPostApi(hass)

    with patch.object(
        InPostApi,
        "_request",
        return_value=ApiResponse(url=mocked_point_url, body="<div></div>"),
    ):
        assert await api.find_parcel_locker_id(mocked_point) is None

    assert "Could not find air quality data" in caplog.text


async def test_find_parcel_locker_id_when_page_redirects(hass, caplog):
    """Redirect away from the parcel locker page means outdated entry data."""
    api = InPostApi(hass)

    with patch.object(
        InPostApi,
        "_request",
        return_value=ApiResponse(
            url="https://inpost.pl/znajdz-paczkomat", body="<div></div>"
        ),
    ):
        assert await api.find_parcel_locker_id(mocked_point) is None

    assert "redirected to" in caplog.text
