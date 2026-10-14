"""Members common to all ASCOM device types (here: only the telescope)."""

from importlib.metadata import version as _pkg_version

from fastapi import APIRouter, Request

from alpaca_10micron.alpaca import AlpacaRequest, read_request
from alpaca_10micron.device import TelescopeDevice
from alpaca_10micron.errors import (
    ACTION_NOT_IMPLEMENTED,
    AlpacaError,
    BadRequest,
    not_implemented,
)
from alpaca_10micron.models import (
    AlpacaResponse,
    BoolResponse,
    DeviceStateItem,
    DeviceStateResponse,
    IntResponse,
    StringArrayResponse,
    StringResponse,
)

router = APIRouter(prefix="/telescope/{device_number}")

INTERFACE_VERSION = 4  # ITelescopeV4 (ASCOM Platform 7)


def get_telescope(request: Request, device_number: str) -> TelescopeDevice:
    if device_number != "0":
        raise BadRequest(f"No telescope with device number {device_number!r}; only 0 exists")
    return request.app.state.telescope


async def device_request(
    request: Request, device_number: str
) -> tuple[AlpacaRequest, TelescopeDevice]:
    """For members that work without a connection."""
    alpaca = await read_request(request)
    return alpaca, get_telescope(request, device_number)


async def connected_request(
    request: Request, device_number: str
) -> tuple[AlpacaRequest, TelescopeDevice]:
    """For all other members: NotConnected (0x407) unless this client is connected."""
    alpaca, telescope = await device_request(request, device_number)
    telescope.require_connected(alpaca.client_id)
    return alpaca, telescope


def package_version() -> str:
    return _pkg_version("alpaca-10micron")


def driver_version() -> str:
    """ASCOM DriverVersion must be "major.minor"."""
    major, minor, *_ = package_version().split(".")
    return f"{major}.{minor}"


@router.put("/action", response_model=StringResponse)
async def action(request: Request, device_number: str):
    alpaca, _ = await connected_request(request, device_number)
    name = alpaca.get("Action")
    alpaca.get("Parameters")
    raise AlpacaError(ACTION_NOT_IMPLEMENTED, f"Action {name!r} is not supported")


@router.put("/commandblind", response_model=AlpacaResponse)
async def commandblind(request: Request, device_number: str):
    await connected_request(request, device_number)
    raise not_implemented("CommandBlind")


@router.put("/commandbool", response_model=BoolResponse)
async def commandbool(request: Request, device_number: str):
    await connected_request(request, device_number)
    raise not_implemented("CommandBool")


@router.put("/commandstring", response_model=StringResponse)
async def commandstring(request: Request, device_number: str):
    await connected_request(request, device_number)
    raise not_implemented("CommandString")


@router.put("/connect", response_model=AlpacaResponse)
async def connect(request: Request, device_number: str):
    alpaca, telescope = await device_request(request, device_number)
    telescope.connect(alpaca.client_id)
    return AlpacaResponse(**alpaca.ids())


@router.get("/connected", response_model=BoolResponse)
async def get_connected(request: Request, device_number: str):
    alpaca, telescope = await device_request(request, device_number)
    return BoolResponse(Value=telescope.is_connected(alpaca.client_id), **alpaca.ids())


@router.put("/connected", response_model=AlpacaResponse)
async def set_connected(request: Request, device_number: str):
    alpaca, telescope = await device_request(request, device_number)
    await telescope.set_connected(alpaca.client_id, alpaca.get_bool("Connected"))
    return AlpacaResponse(**alpaca.ids())


@router.get("/connecting", response_model=BoolResponse)
async def get_connecting(request: Request, device_number: str):
    alpaca, telescope = await device_request(request, device_number)
    return BoolResponse(Value=telescope.is_connecting(alpaca.client_id), **alpaca.ids())


@router.put("/disconnect", response_model=AlpacaResponse)
async def disconnect(request: Request, device_number: str):
    alpaca, telescope = await device_request(request, device_number)
    await telescope.disconnect(alpaca.client_id)
    return AlpacaResponse(**alpaca.ids())


@router.get("/description", response_model=StringResponse)
async def get_description(request: Request, device_number: str):
    alpaca, telescope = await device_request(request, device_number)
    info = telescope.mount.info
    model = f"10Micron {info.model}" if info else "10Micron mount"
    return StringResponse(Value=model, **alpaca.ids())


@router.get("/devicestate", response_model=DeviceStateResponse)
async def get_devicestate(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    items = [DeviceStateItem(Name=name, Value=value) for name, value in telescope.device_state()]
    return DeviceStateResponse(Value=items, **alpaca.ids())


@router.get("/driverinfo", response_model=StringResponse)
async def get_driverinfo(request: Request, device_number: str):
    alpaca, _ = await device_request(request, device_number)
    return StringResponse(
        Value=f"ASCOM Alpaca driver for 10Micron mounts, version {package_version()}",
        **alpaca.ids(),
    )


@router.get("/driverversion", response_model=StringResponse)
async def get_driverversion(request: Request, device_number: str):
    alpaca, _ = await device_request(request, device_number)
    return StringResponse(Value=driver_version(), **alpaca.ids())


@router.get("/interfaceversion", response_model=IntResponse)
async def get_interfaceversion(request: Request, device_number: str):
    alpaca, _ = await device_request(request, device_number)
    return IntResponse(Value=INTERFACE_VERSION, **alpaca.ids())


@router.get("/name", response_model=StringResponse)
async def get_name(request: Request, device_number: str):
    alpaca, telescope = await device_request(request, device_number)
    return StringResponse(Value=telescope.config.name, **alpaca.ids())


@router.get("/supportedactions", response_model=StringArrayResponse)
async def get_supportedactions(request: Request, device_number: str):
    alpaca, _ = await device_request(request, device_number)
    return StringArrayResponse(Value=[], **alpaca.ids())
