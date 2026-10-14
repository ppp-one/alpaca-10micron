"""Pydantic models for Alpaca responses, and the ASCOM enumerations the driver uses."""

import enum
from typing import Any

from pydantic import BaseModel


class AlpacaResponse(BaseModel):
    ClientTransactionID: int = 0
    ServerTransactionID: int = 0
    ErrorNumber: int = 0
    ErrorMessage: str = ""


class BoolResponse(AlpacaResponse):
    Value: bool


class IntResponse(AlpacaResponse):
    Value: int


class DoubleResponse(AlpacaResponse):
    Value: float


class StringResponse(AlpacaResponse):
    Value: str


class StringArrayResponse(AlpacaResponse):
    Value: list[str]


class IntArrayResponse(AlpacaResponse):
    Value: list[int]


class DeviceStateItem(BaseModel):
    Name: str
    Value: Any


class DeviceStateResponse(AlpacaResponse):
    Value: list[DeviceStateItem]


class Rate(BaseModel):
    """A range of MoveAxis rates, in degrees per second."""

    Maximum: float
    Minimum: float


class RateArrayResponse(AlpacaResponse):
    Value: list[Rate]


class ServerDescription(BaseModel):
    ServerName: str
    Manufacturer: str
    ManufacturerVersion: str
    Location: str


class ServerDescriptionResponse(AlpacaResponse):
    Value: ServerDescription


class ConfiguredDevice(BaseModel):
    DeviceName: str
    DeviceType: str
    DeviceNumber: int
    UniqueID: str


class ConfiguredDevicesResponse(AlpacaResponse):
    Value: list[ConfiguredDevice]


class AlignmentModes(enum.IntEnum):
    ALT_AZ = 0
    POLAR = 1
    GERMAN_POLAR = 2


class EquatorialCoordinateType(enum.IntEnum):
    OTHER = 0
    TOPOCENTRIC = 1
    J2000 = 2
    J2050 = 3
    B1950 = 4


class PierSide(enum.IntEnum):
    UNKNOWN = -1
    EAST = 0
    WEST = 1


class DriveRates(enum.IntEnum):
    SIDEREAL = 0
    LUNAR = 1
    SOLAR = 2
    KING = 3


class GuideDirections(enum.IntEnum):
    NORTH = 0
    SOUTH = 1
    EAST = 2
    WEST = 3


class TelescopeAxes(enum.IntEnum):
    PRIMARY = 0
    SECONDARY = 1
    TERTIARY = 2
