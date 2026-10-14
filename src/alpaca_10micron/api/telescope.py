"""Telescope-specific members (ITelescopeV4). The logic is in device.TelescopeDevice."""

from datetime import UTC, datetime

from fastapi import APIRouter, Request

from alpaca_10micron.api.common import connected_request
from alpaca_10micron.device import TelescopeDevice
from alpaca_10micron.errors import INVALID_VALUE, VALUE_NOT_SET, AlpacaError, not_implemented
from alpaca_10micron.models import (
    AlpacaResponse,
    BoolResponse,
    DoubleResponse,
    DriveRates,
    EquatorialCoordinateType,
    GuideDirections,
    IntArrayResponse,
    IntResponse,
    PierSide,
    Rate,
    RateArrayResponse,
    StringResponse,
    TelescopeAxes,
)
from alpaca_10micron.protocol import parse_float

router = APIRouter(prefix="/telescope/{device_number}")


def _enum(alpaca, name: str, enum_type):
    value = alpaca.get_int(name)
    try:
        return enum_type(value)
    except ValueError:
        raise AlpacaError(INVALID_VALUE, f"{name} {value} is not a valid value") from None


# --- Capabilities ---

_CAPABILITIES = {
    "canfindhome": TelescopeDevice.can_find_home,
    "canpark": lambda telescope: True,
    "canpulseguide": lambda telescope: True,
    "cansetdeclinationrate": lambda telescope: True,
    "cansetguiderates": lambda telescope: True,
    "cansetpark": TelescopeDevice.can_set_park,
    "cansetpierside": TelescopeDevice.can_set_pier_side,
    "cansetrightascensionrate": lambda telescope: True,
    "cansettracking": lambda telescope: True,
    "canslew": lambda telescope: True,
    "canslewaltaz": lambda telescope: True,
    "canslewaltazasync": lambda telescope: True,
    "canslewasync": lambda telescope: True,
    "cansync": TelescopeDevice.can_sync,
    "cansyncaltaz": lambda telescope: False,
    "canunpark": lambda telescope: True,
}


def _add_capability(name: str, value) -> None:
    async def endpoint(request: Request, device_number: str):
        alpaca, telescope = await connected_request(request, device_number)
        return BoolResponse(Value=value(telescope), **alpaca.ids())

    router.add_api_route(
        f"/{name}", endpoint, methods=["GET"], response_model=BoolResponse, name=f"get_{name}"
    )


for _name, _value in _CAPABILITIES.items():
    _add_capability(_name, _value)


# --- Position, status and site ---


@router.get("/alignmentmode", response_model=IntResponse)
async def get_alignmentmode(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return IntResponse(Value=telescope.alignment_mode(), **alpaca.ids())


@router.get("/altitude", response_model=DoubleResponse)
async def get_altitude(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return DoubleResponse(Value=telescope.state().ginfo.altitude, **alpaca.ids())


@router.get("/aperturearea", response_model=DoubleResponse)
async def get_aperturearea(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    if telescope.config.aperture_area is None:
        raise not_implemented("ApertureArea (not set in the configuration)")
    return DoubleResponse(Value=telescope.config.aperture_area, **alpaca.ids())


@router.get("/aperturediameter", response_model=DoubleResponse)
async def get_aperturediameter(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    if telescope.config.aperture_diameter is None:
        raise not_implemented("ApertureDiameter (not set in the configuration)")
    return DoubleResponse(Value=telescope.config.aperture_diameter, **alpaca.ids())


@router.get("/athome", response_model=BoolResponse)
async def get_athome(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return BoolResponse(Value=telescope.at_home(), **alpaca.ids())


@router.get("/atpark", response_model=BoolResponse)
async def get_atpark(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return BoolResponse(Value=telescope.at_park(), **alpaca.ids())


@router.get("/azimuth", response_model=DoubleResponse)
async def get_azimuth(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return DoubleResponse(Value=telescope.state().ginfo.azimuth, **alpaca.ids())


@router.get("/declination", response_model=DoubleResponse)
async def get_declination(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return DoubleResponse(Value=telescope.state().ginfo.declination, **alpaca.ids())


@router.get("/equatorialsystem", response_model=IntResponse)
async def get_equatorialsystem(request: Request, device_number: str):
    alpaca, _ = await connected_request(request, device_number)
    # The mount works in local topocentric coordinates, equinox of date (JNow).
    return IntResponse(Value=EquatorialCoordinateType.TOPOCENTRIC, **alpaca.ids())


@router.get("/focallength", response_model=DoubleResponse)
async def get_focallength(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    if telescope.config.focal_length is None:
        raise not_implemented("FocalLength (not set in the configuration)")
    return DoubleResponse(Value=telescope.config.focal_length, **alpaca.ids())


@router.get("/rightascension", response_model=DoubleResponse)
async def get_rightascension(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return DoubleResponse(Value=telescope.state().ginfo.right_ascension, **alpaca.ids())


@router.get("/siderealtime", response_model=DoubleResponse)
async def get_siderealtime(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return DoubleResponse(Value=telescope.state().sidereal_time, **alpaca.ids())


@router.get("/slewing", response_model=BoolResponse)
async def get_slewing(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return BoolResponse(Value=telescope.slewing(), **alpaca.ids())


@router.get("/siteelevation", response_model=DoubleResponse)
async def get_siteelevation(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return DoubleResponse(Value=telescope.site().elevation, **alpaca.ids())


@router.put("/siteelevation", response_model=AlpacaResponse)
async def set_siteelevation(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.set_site_elevation(alpaca.get_float("SiteElevation"))
    return AlpacaResponse(**alpaca.ids())


@router.get("/sitelatitude", response_model=DoubleResponse)
async def get_sitelatitude(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return DoubleResponse(Value=telescope.site().latitude, **alpaca.ids())


@router.put("/sitelatitude", response_model=AlpacaResponse)
async def set_sitelatitude(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.set_site_latitude(alpaca.get_float("SiteLatitude"))
    return AlpacaResponse(**alpaca.ids())


@router.get("/sitelongitude", response_model=DoubleResponse)
async def get_sitelongitude(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return DoubleResponse(Value=telescope.site().longitude, **alpaca.ids())


@router.put("/sitelongitude", response_model=AlpacaResponse)
async def set_sitelongitude(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.set_site_longitude(alpaca.get_float("SiteLongitude"))
    return AlpacaResponse(**alpaca.ids())


@router.get("/utcdate", response_model=StringResponse)
async def get_utcdate(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return StringResponse(Value=telescope.utc_date(), **alpaca.ids())


@router.put("/utcdate", response_model=AlpacaResponse)
async def set_utcdate(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    text = alpaca.get("UTCDate")
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        raise AlpacaError(INVALID_VALUE, f"UTCDate {text!r} is not an ISO 8601 date") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    await telescope.set_utc_date(moment)
    return AlpacaResponse(**alpaca.ids())


@router.get("/doesrefraction", response_model=BoolResponse)
async def get_doesrefraction(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return BoolResponse(Value=telescope.site().refraction, **alpaca.ids())


@router.put("/doesrefraction", response_model=AlpacaResponse)
async def set_doesrefraction(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.set_does_refraction(alpaca.get_bool("DoesRefraction"))
    return AlpacaResponse(**alpaca.ids())


@router.get("/slewsettletime", response_model=IntResponse)
async def get_slewsettletime(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    seconds = await telescope.query(":Gstm#", parse_float)
    # ASCOM SlewSettleTime is a 16-bit integer; the mount allows up to 99999 s.
    return IntResponse(Value=min(round(seconds), 32767), **alpaca.ids())


@router.put("/slewsettletime", response_model=AlpacaResponse)
async def set_slewsettletime(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.set_slew_settle_time(alpaca.get_int("SlewSettleTime"))
    return AlpacaResponse(**alpaca.ids())


# --- Pointing state ---


@router.get("/sideofpier", response_model=IntResponse)
async def get_sideofpier(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return IntResponse(Value=telescope.side_of_pier(), **alpaca.ids())


@router.put("/sideofpier", response_model=AlpacaResponse)
async def set_sideofpier(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.set_side_of_pier(_enum(alpaca, "SideOfPier", PierSide))
    return AlpacaResponse(**alpaca.ids())


@router.get("/destinationsideofpier", response_model=IntResponse)
async def get_destinationsideofpier(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    side = await telescope.destination_side_of_pier(
        alpaca.get_float("RightAscension"), alpaca.get_float("Declination")
    )
    return IntResponse(Value=side, **alpaca.ids())


# --- Targets ---


@router.get("/targetdeclination", response_model=DoubleResponse)
async def get_targetdeclination(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    if telescope.target_declination is None:
        raise AlpacaError(VALUE_NOT_SET, "TargetDeclination has not been set")
    return DoubleResponse(Value=telescope.target_declination, **alpaca.ids())


@router.put("/targetdeclination", response_model=AlpacaResponse)
async def set_targetdeclination(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    value = alpaca.get_float("TargetDeclination")
    if not -90 <= value <= 90:
        raise AlpacaError(INVALID_VALUE, f"TargetDeclination {value} is outside -90..90")
    telescope.target_declination = value
    return AlpacaResponse(**alpaca.ids())


@router.get("/targetrightascension", response_model=DoubleResponse)
async def get_targetrightascension(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    if telescope.target_right_ascension is None:
        raise AlpacaError(VALUE_NOT_SET, "TargetRightAscension has not been set")
    return DoubleResponse(Value=telescope.target_right_ascension, **alpaca.ids())


@router.put("/targetrightascension", response_model=AlpacaResponse)
async def set_targetrightascension(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    value = alpaca.get_float("TargetRightAscension")
    if not 0 <= value < 24:
        raise AlpacaError(INVALID_VALUE, f"TargetRightAscension {value} is outside 0..24")
    telescope.target_right_ascension = value
    return AlpacaResponse(**alpaca.ids())


# --- Slews ---


@router.put("/slewtocoordinates", response_model=AlpacaResponse)
async def slewtocoordinates(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.slew_to_coordinates(
        alpaca.get_float("RightAscension"), alpaca.get_float("Declination")
    )
    return AlpacaResponse(**alpaca.ids())


@router.put("/slewtocoordinatesasync", response_model=AlpacaResponse)
async def slewtocoordinatesasync(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.slew_to_coordinates_async(
        alpaca.get_float("RightAscension"), alpaca.get_float("Declination")
    )
    return AlpacaResponse(**alpaca.ids())


@router.put("/slewtotarget", response_model=AlpacaResponse)
async def slewtotarget(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.slew_to_target()
    return AlpacaResponse(**alpaca.ids())


@router.put("/slewtotargetasync", response_model=AlpacaResponse)
async def slewtotargetasync(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.slew_to_target_async()
    return AlpacaResponse(**alpaca.ids())


@router.put("/slewtoaltaz", response_model=AlpacaResponse)
async def slewtoaltaz(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.slew_to_alt_az(alpaca.get_float("Azimuth"), alpaca.get_float("Altitude"))
    return AlpacaResponse(**alpaca.ids())


@router.put("/slewtoaltazasync", response_model=AlpacaResponse)
async def slewtoaltazasync(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.slew_to_alt_az_async(alpaca.get_float("Azimuth"), alpaca.get_float("Altitude"))
    return AlpacaResponse(**alpaca.ids())


@router.put("/abortslew", response_model=AlpacaResponse)
async def abortslew(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.abort_slew()
    return AlpacaResponse(**alpaca.ids())


# --- Sync ---


@router.put("/synctocoordinates", response_model=AlpacaResponse)
async def synctocoordinates(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.sync_to_coordinates(
        alpaca.get_float("RightAscension"), alpaca.get_float("Declination")
    )
    return AlpacaResponse(**alpaca.ids())


@router.put("/synctotarget", response_model=AlpacaResponse)
async def synctotarget(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.sync_to_target()
    return AlpacaResponse(**alpaca.ids())


@router.put("/synctoaltaz", response_model=AlpacaResponse)
async def synctoaltaz(request: Request, device_number: str):
    await connected_request(request, device_number)
    raise not_implemented("SyncToAltAz")


# --- Park and home ---


@router.put("/park", response_model=AlpacaResponse)
async def park(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.park()
    return AlpacaResponse(**alpaca.ids())


@router.put("/unpark", response_model=AlpacaResponse)
async def unpark(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.unpark()
    return AlpacaResponse(**alpaca.ids())


@router.put("/setpark", response_model=AlpacaResponse)
async def setpark(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.set_park()
    return AlpacaResponse(**alpaca.ids())


@router.put("/findhome", response_model=AlpacaResponse)
async def findhome(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.find_home()
    return AlpacaResponse(**alpaca.ids())


# --- MoveAxis ---


@router.get("/axisrates", response_model=RateArrayResponse)
async def get_axisrates(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    axis = _enum(alpaca, "Axis", TelescopeAxes)
    limit = telescope.axis_rate_limit(axis)
    rates = [Rate(Minimum=0.0, Maximum=limit)] if limit > 0 else []
    return RateArrayResponse(Value=rates, **alpaca.ids())


@router.get("/canmoveaxis", response_model=BoolResponse)
async def get_canmoveaxis(request: Request, device_number: str):
    alpaca, _ = await connected_request(request, device_number)
    axis = _enum(alpaca, "Axis", TelescopeAxes)
    return BoolResponse(Value=axis is not TelescopeAxes.TERTIARY, **alpaca.ids())


@router.put("/moveaxis", response_model=AlpacaResponse)
async def moveaxis(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    axis = _enum(alpaca, "Axis", TelescopeAxes)
    await telescope.move_axis(axis, alpaca.get_float("Rate"))
    return AlpacaResponse(**alpaca.ids())


# --- Guiding ---


@router.get("/guideratedeclination", response_model=DoubleResponse)
async def get_guideratedeclination(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    # The mount uses one guide rate for both axes, in arcseconds per second.
    return DoubleResponse(Value=telescope.site().guide_rate / 3600, **alpaca.ids())


@router.put("/guideratedeclination", response_model=AlpacaResponse)
async def set_guideratedeclination(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.set_guide_rate(alpaca.get_float("GuideRateDeclination"))
    return AlpacaResponse(**alpaca.ids())


@router.get("/guideraterightascension", response_model=DoubleResponse)
async def get_guideraterightascension(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return DoubleResponse(Value=telescope.site().guide_rate / 3600, **alpaca.ids())


@router.put("/guideraterightascension", response_model=AlpacaResponse)
async def set_guideraterightascension(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.set_guide_rate(alpaca.get_float("GuideRateRightAscension"))
    return AlpacaResponse(**alpaca.ids())


@router.get("/ispulseguiding", response_model=BoolResponse)
async def get_ispulseguiding(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return BoolResponse(Value=await telescope.is_pulse_guiding(), **alpaca.ids())


@router.put("/pulseguide", response_model=AlpacaResponse)
async def pulseguide(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    direction = _enum(alpaca, "Direction", GuideDirections)
    await telescope.pulse_guide(direction, alpaca.get_int("Duration"))
    return AlpacaResponse(**alpaca.ids())


# --- Tracking ---


@router.get("/tracking", response_model=BoolResponse)
async def get_tracking(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return BoolResponse(Value=telescope.state().tracking, **alpaca.ids())


@router.put("/tracking", response_model=AlpacaResponse)
async def set_tracking(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.set_tracking(alpaca.get_bool("Tracking"))
    return AlpacaResponse(**alpaca.ids())


@router.get("/trackingrate", response_model=IntResponse)
async def get_trackingrate(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return IntResponse(Value=await telescope.tracking_rate(), **alpaca.ids())


@router.put("/trackingrate", response_model=AlpacaResponse)
async def set_trackingrate(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.set_tracking_rate(_enum(alpaca, "TrackingRate", DriveRates))
    return AlpacaResponse(**alpaca.ids())


@router.get("/trackingrates", response_model=IntArrayResponse)
async def get_trackingrates(request: Request, device_number: str):
    alpaca, _ = await connected_request(request, device_number)
    return IntArrayResponse(Value=[int(rate) for rate in DriveRates], **alpaca.ids())


@router.get("/rightascensionrate", response_model=DoubleResponse)
async def get_rightascensionrate(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return DoubleResponse(Value=telescope.ra_rate(), **alpaca.ids())


@router.put("/rightascensionrate", response_model=AlpacaResponse)
async def set_rightascensionrate(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.set_ra_rate(alpaca.get_float("RightAscensionRate"))
    return AlpacaResponse(**alpaca.ids())


@router.get("/declinationrate", response_model=DoubleResponse)
async def get_declinationrate(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    return DoubleResponse(Value=telescope.dec_rate(), **alpaca.ids())


@router.put("/declinationrate", response_model=AlpacaResponse)
async def set_declinationrate(request: Request, device_number: str):
    alpaca, telescope = await connected_request(request, device_number)
    await telescope.set_dec_rate(alpaca.get_float("DeclinationRate"))
    return AlpacaResponse(**alpaca.ids())
