"""Pan/tilt and zoom buttons for eufy NVR cameras with a PTZ head (S4 Max).

The add-on's control API (port 1986, same login as go2rtc) sends the commands inside the
open live session, as eufy's web viewer does, so the camera's live view must be open.
A PTZ camera is recognised by its fixed-lens sibling stream (``<stream>_lens0``); the
fixed lens itself has no pan/tilt or zoom.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aiohttp import BasicAuth, ClientError, ClientTimeout

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EufyNvrConfigEntry
from .const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, DEVICE_NAME, DOMAIN, MANUFACTURER, MODEL
from .coordinator import EufyNvrCoordinator
from .go2rtc_api import STREAM_PREFIX, api_base_url

_LOGGER = logging.getLogger(__name__)

CONTROL_PORT = 1986
FIXED_LENS_SUFFIX = "_lens0"
ZOOM_STEPS = (1, 2, 4, 8)


@dataclass(frozen=True)
class ControlButton:
    key: str
    name: str
    icon: str
    request: dict[str, Any] | None = None   # None: a zoom step, resolved per press
    zoom: str | None = None                 # "in" | "out" | "reset"


BUTTONS = (
    ControlButton("pan_left", "Pan left", "mdi:arrow-left-bold", {"action": "ptz", "direction": "left"}),
    ControlButton("pan_right", "Pan right", "mdi:arrow-right-bold", {"action": "ptz", "direction": "right"}),
    ControlButton("tilt_up", "Tilt up", "mdi:arrow-up-bold", {"action": "ptz", "direction": "up"}),
    ControlButton("tilt_down", "Tilt down", "mdi:arrow-down-bold", {"action": "ptz", "direction": "down"}),
    ControlButton("zoom_in", "Zoom in", "mdi:magnify-plus", zoom="in"),
    ControlButton("zoom_out", "Zoom out", "mdi:magnify-minus", zoom="out"),
    ControlButton("zoom_reset", "Zoom 1x", "mdi:magnify-close", zoom="reset"),
)


def ptz_streams(streams) -> list[str]:
    """Main streams of cameras that also publish a fixed-lens stream."""
    names = set(streams or ())
    return sorted(
        name for name in names
        if not name.endswith(FIXED_LENS_SUFFIX) and f"{name}{FIXED_LENS_SUFFIX}" in names
    )


def next_zoom(current: int, direction: str) -> int:
    if direction == "reset":
        return ZOOM_STEPS[0]
    if direction == "in":
        return next((z for z in ZOOM_STEPS if z > current), ZOOM_STEPS[-1])
    return next((z for z in reversed(ZOOM_STEPS) if z < current), ZOOM_STEPS[0])


def _friendly_name(stream: str) -> str:
    base = stream[len(STREAM_PREFIX):] if stream.startswith(STREAM_PREFIX) else stream
    return base.replace("_", " ").strip().title() or stream


class ControlClient:
    """Posts control requests to the add-on and tracks each camera's zoom level."""

    def __init__(self, hass: HomeAssistant, host: str, username: str, password: str) -> None:
        self._session = async_get_clientsession(hass)
        self._url = f"{api_base_url(host, CONTROL_PORT)}/api/control"
        self._auth = BasicAuth(username, password)
        self.zoom: dict[str, int] = {}

    async def send(self, stream: str, request: dict[str, Any]) -> None:
        body = {"stream": stream, **request}
        try:
            async with self._session.post(self._url, json=body, auth=self._auth,
                                          timeout=ClientTimeout(total=8)) as resp:
                reply = await resp.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            raise HomeAssistantError(
                "Camera controls are unavailable; turn on camera_controls in the Eufy NVR add-on"
            ) from err
        if resp.status == 409 and reply.get("error") in ("camera not live", "no live stream",
                                                         "that camera is not the live one"):
            raise HomeAssistantError(f"Open {_friendly_name(stream)}'s live view first")
        if resp.status != 200 or not reply.get("ok"):
            raise HomeAssistantError(f"Camera control failed: {reply.get('error', resp.status)}")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyNvrConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add control buttons for every PTZ camera, now and as streams appear."""
    coordinator = entry.runtime_data
    client = ControlClient(hass, entry.data[CONF_HOST], entry.data[CONF_USERNAME],
                           entry.data[CONF_PASSWORD])
    known: set[str] = set()

    @callback
    def _async_add_new() -> None:
        new = [s for s in ptz_streams(coordinator.data) if s not in known]
        if not new:
            return
        known.update(new)
        async_add_entities(
            EufyNvrControlButton(coordinator, entry.entry_id, client, stream, button)
            for stream in new for button in BUTTONS
        )

    _async_add_new()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))


class EufyNvrControlButton(CoordinatorEntity[EufyNvrCoordinator], ButtonEntity):
    """One pan/tilt or zoom action for one camera."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EufyNvrCoordinator, entry_id: str, client: ControlClient,
                 stream: str, button: ControlButton) -> None:
        super().__init__(coordinator)
        self._client = client
        self._stream = stream
        self._button = button
        self._attr_name = f"{_friendly_name(stream)} {button.name}"
        self._attr_icon = button.icon
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{stream}_{button.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=DEVICE_NAME,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def available(self) -> bool:
        return super().available and self._stream in (self.coordinator.data or {})

    async def async_press(self) -> None:
        if self._button.zoom is None:
            await self._client.send(self._stream, self._button.request)
            return
        level = next_zoom(self._client.zoom.get(self._stream, 1), self._button.zoom)
        await self._client.send(self._stream, {"action": "zoom", "level": level})
        self._client.zoom[self._stream] = level
