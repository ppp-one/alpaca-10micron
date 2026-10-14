"""Driver configuration, read from a YAML file (see config.example.yaml)."""

import dataclasses
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from alpaca_10micron.mount import MountSettings


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 11111
    discovery: bool = True
    discovery_port: int = 32227
    location: str = ""


@dataclass(frozen=True)
class DeviceConfig:
    name: str = "10Micron mount"
    unique_id: str = ""  # empty: derived from the mount address
    connect_wait: float = 15.0  # seconds a Connect waits for the first link to the mount
    aperture_diameter: float | None = None  # metres
    aperture_area: float | None = None  # square metres
    focal_length: float | None = None  # metres
    # Sync changes the mount's alignment; a wrong sync damages pointing. Off by default,
    # as in the vendor driver.
    allow_sync: bool = False
    # Park at the keypad-defined park position (:hP#) instead of the one saved with SetPark.
    use_keypad_park_position: bool = False
    slew_timeout: float = 600.0  # seconds a synchronous slew may take
    home_timeout: float = 600.0  # seconds FindHome may take


@dataclass(frozen=True)
class AppConfig:
    mount: MountSettings
    server: ServerConfig = field(default_factory=ServerConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)

    @property
    def unique_id(self) -> str:
        if self.device.unique_id:
            return self.device.unique_id
        key = f"alpaca-10micron:{self.mount.host}:{self.mount.port}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _build(cls, section: str, values: dict | None):
    values = values or {}
    if not isinstance(values, dict):
        raise ValueError(f"'{section}' must be a mapping")
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"unknown setting(s) in '{section}': {', '.join(sorted(unknown))}")
    return cls(**values)


def config_from_dict(data: dict) -> AppConfig:
    unknown = set(data) - {"mount", "server", "device"}
    if unknown:
        raise ValueError(f"unknown section(s): {', '.join(sorted(unknown))}")
    mount = data.get("mount") or {}
    if not mount.get("host"):
        raise ValueError("'mount.host' is required (the IP address of the mount)")
    return AppConfig(
        mount=_build(MountSettings, "mount", mount),
        server=_build(ServerConfig, "server", data.get("server")),
        device=_build(DeviceConfig, "device", data.get("device")),
    )


def load_config(path: str | Path) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return config_from_dict(data)
