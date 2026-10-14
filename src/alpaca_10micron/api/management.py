"""Alpaca Management API and the setup pages."""

import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from alpaca_10micron.alpaca import read_request
from alpaca_10micron.api.common import package_version
from alpaca_10micron.models import (
    ConfiguredDevice,
    ConfiguredDevicesResponse,
    IntArrayResponse,
    ServerDescription,
    ServerDescriptionResponse,
)

router = APIRouter()


@router.get("/management/apiversions", response_model=IntArrayResponse)
async def get_apiversions(request: Request):
    alpaca = await read_request(request)
    return IntArrayResponse(Value=[1], **alpaca.ids())


@router.get("/management/v1/description", response_model=ServerDescriptionResponse)
async def get_description(request: Request):
    alpaca = await read_request(request)
    config = request.app.state.config
    return ServerDescriptionResponse(
        Value=ServerDescription(
            ServerName="10Micron Alpaca driver",
            Manufacturer="alpaca-10micron",
            ManufacturerVersion=package_version(),
            Location=config.server.location,
        ),
        **alpaca.ids(),
    )


@router.get("/management/v1/configureddevices", response_model=ConfiguredDevicesResponse)
async def get_configureddevices(request: Request):
    alpaca = await read_request(request)
    config = request.app.state.config
    device = ConfiguredDevice(
        DeviceName=config.device.name,
        DeviceType="Telescope",
        DeviceNumber=0,
        UniqueID=config.unique_id,
    )
    return ConfiguredDevicesResponse(Value=[device], **alpaca.ids())


def _status_page(request: Request) -> str:
    config = request.app.state.config
    mount = request.app.state.telescope.mount
    info = mount.info
    link = "up" if mount.link_up else ("connecting" if mount.running else "closed")
    rows = [
        ("Mount address", f"{config.mount.host}:{config.mount.port}"),
        ("Link to mount", link),
        ("Model", info.model if info else "unknown"),
        ("Firmware", ".".join(map(str, info.firmware)) if info else "unknown"),
        ("Links opened", str(mount.links_opened)),
        ("Last error", mount.last_error or "none"),
        ("Driver version", package_version()),
    ]
    body = "".join(
        f"<tr><th>{html.escape(name)}</th><td>{html.escape(value)}</td></tr>"
        for name, value in rows
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>10Micron Alpaca driver</title>"
        "<style>body{font-family:sans-serif;margin:2em}th{text-align:left;padding-right:2em}"
        "</style></head><body><h1>10Micron Alpaca driver</h1>"
        f"<table>{body}</table>"
        "<p>Settings are read from the configuration file when the driver starts.</p>"
        "</body></html>"
    )


@router.get("/setup", response_class=HTMLResponse)
async def setup(request: Request):
    return _status_page(request)


@router.get("/setup/v1/telescope/{device_number}/setup", response_class=HTMLResponse)
async def setup_telescope(request: Request, device_number: str):
    return _status_page(request)
