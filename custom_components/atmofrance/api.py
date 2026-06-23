import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, quote
from zoneinfo import ZoneInfo
import aiohttp
from aiohttp.client import ClientTimeout, ClientError
from yarl import URL
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from .const import (
    AUTH_URL,
    DATA_URL,
    API_GOUV_URL,
    URL_CODE,
    OCCITANIE_POLLUTION_URL,
    OCCITANIE_POLLEN_URL,
    OCCITANIE_POLLEN_TAXON_MAP,
)

DEFAULT_TIMEOUT = 120
CLIENT_TIMEOUT = ClientTimeout(total=DEFAULT_TIMEOUT)

_LOGGER = logging.getLogger(__name__)


class TooManyRequestsError(Exception):
    """Exception to handle too many requests error."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AtmoFranceDataApi:
    """Api to get AirAtmo data"""

    def __init__(
        self,
        config,
        session: aiohttp.ClientSession = None,
        timeout=CLIENT_TIMEOUT,
        hass: HomeAssistant = None,
    ) -> None:
        self._timeout = timeout
        if session is not None:
            self._session = session
        else:
            self._session = aiohttp.ClientSession()
        self._config = config
        self._token = None
        self._data = None
        self._hass = hass

    async def async_get_token(self):
        """Get user token to allow request"""

        request = await self._session.post(
            AUTH_URL,
            json={
                "username": self._config[CONF_USERNAME],
                "password": self._config[CONF_PASSWORD],
            },
        )
        if request.status == 200:
            resp = await request.json()
            self._token = resp["token"]
            _LOGGER.debug("got response %s ", resp)
        elif request.status == 429:
            raise TooManyRequestsError(
                f"Too many requests response from the server, please retry in {request.headers.get("Retry-After")} seconds")
        else:
            raise ConnectionRefusedError(
                f"Failed to get authent token, with error status : {request.status}"
            )

    async def get_data(self, insee_code, type: URL_CODE) -> dict:
        """Get Data from AtmoFrance API"""
        try:
            await self.async_get_token()  # Always called to be sure to have a valid token
        except ConnectionRefusedError as err:
            _LOGGER.error(
                "Failed to get token with status error %s ", err),
            return None
        except TooManyRequestsError as err:
            _LOGGER.error(
                "Too many request error from the server. Try again in %s ", err),
            return None
        headers = {"Authorization": f"Bearer {self._token}"}
        today = datetime.now(
            ZoneInfo(self._hass.config.time_zone)).strftime("%Y-%m-%d")
        url = f'{DATA_URL}/{type.value}/{{"code_zone":{{"operator":"=","value":"{
            insee_code}"}},"date_ech":{{"operator":">=","value":"{today}"}}}}?withGeom=false'
        _LOGGER.debug("Getting data from %s", url)
        try:
            result = await self._session.get(url, headers=headers)
            json = await result.json()
            _LOGGER.debug("Got response %s ", json)
            _LOGGER.debug(
                "Extracting data for INSEE %s and date %s", insee_code, today)
            if len(json["features"]) > 0:  # At least one result
                self._data = json
                _LOGGER.debug(
                    "Got data for INSEE %s and date > %s: %s",
                    insee_code,
                    today,
                    self._data,
                )
            else:  # no result
                self._data = None
                _LOGGER.warning(
                    "No data for INSEE %s and date %s", insee_code, today)
            return json["features"]
        except ClientError as err:
            return err

    def get_key_value(self, key, shift: int = 0):
        """Get value for the given key in JSON Data for a given date based on shift from today"""
        extractDate = (datetime.now(
            ZoneInfo(self._hass.config.time_zone))+timedelta(days=shift)).strftime("%Y-%m-%d")
        if self._data is not None:
            extractedData = next(
                filter(
                    lambda feat: feat["properties"]["date_ech"] == extractDate,
                    self._data["features"],
                ), {"properties": {key: ""}})  # If not found return ''
            return extractedData.get("properties")[key]
        else:
            return ""

    @property
    def source(self):
        """Get value for source of data"""
        if self._data is not None:
            # Take the first value, we have multiple ones for forecast
            return self._data["features"][0]["properties"]["source"]
        return ""

    @property
    def last_update(self):
        """Get value of data update"""
        if self._data is not None:
            # Take the first value, we have multiple ones for forecast
            return self._data["features"][0]["properties"]["date_maj"]
        return ""

    @property
    def type_zone(self):
        """Get type of zone"""
        if self._data is not None:
            # Take the first value, we have multiple ones for forecast
            return self._data["features"][0]["properties"]["type_zone"]
        return ""

    @property
    def nom_zone(self):
        """Get Name of Zone"""
        if self._data is not None:
            # Take the first value, we have multiple ones for forecast
            return self._data["features"][0]["properties"]["lib_zone"]
        return ""


class AtmoOccitanieDataApi:
    """Api to get air quality / pollen data from the Atmo Occitanie ArcGIS open data portal.

    This is a regional source (Occitanie only) that requires no authentication.
    Records are normalized to the same shape as :class:`AtmoFranceDataApi`, so the
    coordinators and sensors can use either backend transparently.
    """

    def __init__(
        self,
        config,
        session: aiohttp.ClientSession = None,
        timeout=CLIENT_TIMEOUT,
        hass: HomeAssistant = None,
    ) -> None:
        self._timeout = timeout
        if session is not None:
            self._session = session
        else:
            self._session = aiohttp.ClientSession()
        self._config = config
        self._data = None
        self._hass = hass

    async def get_data(self, zone_code, type: URL_CODE) -> dict:
        """Get the most recent records for a zone from the ArcGIS FeatureServer."""
        if type == URL_CODE.POLLUTION:
            base = OCCITANIE_POLLUTION_URL
            # code_zone is a string field on the pollution layer (EPCI code)
            where = f"code_zone='{zone_code}'"
        else:
            base = OCCITANIE_POLLEN_URL
            # code_zone is an integer field on the pollen layer (département number)
            where = f"code_zone={zone_code}"
        params = {
            "where": where,
            "outFields": "*",
            "returnGeometry": "false",
            "orderByFields": "date_ech DESC",
            "resultRecordCount": "15",
            "f": "json",
        }
        # The service path is already percent-encoded; build the query ourselves and
        # mark the URL as encoded so aiohttp does not double-encode it.
        url = URL(f"{base}/query?{urlencode(params, quote_via=quote)}", encoded=True)
        _LOGGER.debug("Getting Atmo Occitanie data from %s", url)
        try:
            result = await self._session.get(url)
            json = await result.json()
        except ClientError as err:
            _LOGGER.error("Failed to get Atmo Occitanie data: %s", err)
            return None
        features = json.get("features")
        if not features:
            self._data = None
            _LOGGER.warning(
                "No Atmo Occitanie data for zone %s (type %s)", zone_code, type.name)
            return []
        normalized = [
            {"properties": self._normalize(feat.get("attributes", {}), type)}
            for feat in features
        ]
        self._data = {"features": normalized}
        _LOGGER.debug("Got %s Atmo Occitanie records for zone %s",
                      len(normalized), zone_code)
        return normalized

    def _normalize(self, attr: dict, type: URL_CODE) -> dict:
        """Translate an ArcGIS record into the national API property shape."""
        props = {
            "date_ech": self._epoch_to_date(attr.get("date_ech")),
            "source": attr.get("source") or "Atmo-Occitanie",
            "type_zone": attr.get("type_zone", ""),
            "lib_zone": attr.get("lib_zone", ""),
            "code_zone": str(attr.get("code_zone", "")),
        }
        if type == URL_CODE.POLLUTION:
            # The diffusion date acts as the "last update" timestamp.
            props["date_maj"] = self._epoch_to_iso(attr.get("date_dif"))
            props["lib_qual"] = attr.get("lib_qual", "")
            props["coul_qual"] = attr.get("coul_qual", "")
            for key in ("code_qual", "code_no2", "code_o3", "code_pm10", "code_pm25", "code_so2"):
                props[key] = attr.get(key)
        else:
            # The pollen layer has no diffusion date and exposes alert levels only.
            props["date_maj"] = ""
            for json_key, taxon in OCCITANIE_POLLEN_TAXON_MAP.items():
                props[json_key] = attr.get(taxon)
        return props

    def _tz(self) -> ZoneInfo:
        if self._hass is not None:
            return ZoneInfo(self._hass.config.time_zone)
        return ZoneInfo("Europe/Paris")

    def _epoch_to_date(self, value):
        """Convert an ArcGIS epoch-ms timestamp to a 'YYYY-MM-DD' string in local time."""
        if value is None:
            return ""
        return datetime.fromtimestamp(value / 1000, tz=self._tz()).strftime("%Y-%m-%d")

    def _epoch_to_iso(self, value):
        """Convert an ArcGIS epoch-ms timestamp to an ISO (UTC) string."""
        if value is None:
            return ""
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()

    def get_key_value(self, key, shift: int = 0):
        """Get value for the given key for a date based on shift from today."""
        extractDate = (datetime.now(self._tz()) +
                       timedelta(days=shift)).strftime("%Y-%m-%d")
        if self._data is not None:
            extractedData = next(
                filter(
                    lambda feat: feat["properties"]["date_ech"] == extractDate,
                    self._data["features"],
                ), {"properties": {key: ""}})  # If not found return ''
            value = extractedData.get("properties").get(key, "")
            return value if value is not None else ""
        else:
            return ""

    @property
    def source(self):
        """Get value for source of data"""
        if self._data is not None:
            return self._data["features"][0]["properties"]["source"]
        return ""

    @property
    def last_update(self):
        """Get value of data update"""
        if self._data is not None:
            return self._data["features"][0]["properties"].get("date_maj", "")
        return ""

    @property
    def type_zone(self):
        """Get type of zone"""
        if self._data is not None:
            return self._data["features"][0]["properties"]["type_zone"]
        return ""

    @property
    def nom_zone(self):
        """Get Name of Zone"""
        if self._data is not None:
            return self._data["features"][0]["properties"]["lib_zone"]
        return ""


class INSEEAPI:
    """Api to get INSEE data"""

    def __init__(
        self, session: aiohttp.ClientSession = None, timeout=CLIENT_TIMEOUT
    ) -> None:
        self._timeout = timeout
        if session is not None:
            self._session = session
        else:
            self._session = aiohttp.ClientSession()

    async def get_data(self, zipcode) -> dict:
        """Get INSEE code for a given zip code"""
        url = f"{API_GOUV_URL}codePostal={
            zipcode}&fields=code,nom,codeEpci&format=json&geometry=centre"
        result = await self._session.get(url)
        if result.status == 200:
            json = await result.json()
            _LOGGER.debug("Got response for INSEE Code %s ", json)
            if len(json) == 0:
                _LOGGER.error("No INSEE value fetched for %s ", zipcode)
                raise ValueError
            return json
        else:
            _LOGGER.error(
                "Failed to get INSEE data, with status %s ", result.status)
            raise ValueError
