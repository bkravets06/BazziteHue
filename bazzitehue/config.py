"""Saved lamps, aliases and scenes.

Stored as JSON under ``$XDG_CONFIG_HOME/bazzitehue/config.json`` so it survives
Bazzite's image updates -- nothing here lives in the immutable part of the OS.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import ConfigError

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
# macOS-style UUID addresses show up if bleak is ever run on a non-BlueZ backend.
UUID_RE = re.compile(r"^[0-9A-Fa-f-]{36}$")


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "bazzitehue"


def config_path() -> Path:
    override = os.environ.get("BAZZITEHUE_CONFIG")
    return Path(override) if override else config_dir() / "config.json"


def is_address(value: str) -> bool:
    return bool(MAC_RE.match(value) or UUID_RE.match(value))


@dataclass
class SavedLight:
    address: str
    name: str | None = None
    gamut: str = "C"
    model: str | None = None


@dataclass
class Scene:
    """A stored look: any subset of power, colour and brightness."""

    color: str | None = None
    brightness: float | None = None
    power: bool | None = True


@dataclass
class Config:
    lights: dict[str, SavedLight] = field(default_factory=dict)
    scenes: dict[str, Scene] = field(default_factory=dict)
    default: str | None = None

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        if not path.exists():
            return cls()

        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{path} is not valid JSON: {exc}",
                hint="Fix or delete the file and re-add your lamps with 'bazzitehue add'.",
            ) from exc

        lights = {
            alias: SavedLight(**entry) for alias, entry in (raw.get("lights") or {}).items()
        }
        scenes = {name: Scene(**entry) for name, entry in (raw.get("scenes") or {}).items()}
        return cls(lights=lights, scenes=scenes, default=raw.get("default"))

    def save(self, path: Path | None = None) -> Path:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "lights": {alias: asdict(light) for alias, light in self.lights.items()},
            "scenes": {name: asdict(scene) for name, scene in self.scenes.items()},
            "default": self.default,
        }
        # Write via a temp file so an interrupted save cannot truncate the config.
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=2) + "\n")
        temp.replace(path)
        return path

    # ------------------------------------------------------------------ #
    # Lamps
    # ------------------------------------------------------------------ #

    def add_light(
        self,
        alias: str,
        address: str,
        *,
        name: str | None = None,
        gamut: str = "C",
        model: str | None = None,
    ) -> SavedLight:
        if not alias or is_address(alias):
            raise ConfigError(f"{alias!r} is not a usable alias; pick a short name")
        if not is_address(address):
            raise ConfigError(f"{address!r} does not look like a Bluetooth address")

        light = SavedLight(address=address.upper(), name=name, gamut=gamut.upper(), model=model)
        self.lights[alias] = light
        if self.default is None:
            self.default = alias
        return light

    def remove_light(self, alias: str) -> None:
        if alias not in self.lights:
            raise ConfigError(f"no saved lamp called {alias!r}")
        del self.lights[alias]
        if self.default == alias:
            self.default = next(iter(self.lights), None)

    def resolve(self, target: str | None) -> list[tuple[str | None, SavedLight]]:
        """Turn a ``--light`` argument into concrete lamps.

        Accepts an alias, a raw address, a comma-separated list of either, the
        word ``all``, or nothing at all (meaning the configured default).
        """
        if target is None:
            if self.default and self.default in self.lights:
                return [(self.default, self.lights[self.default])]
            if len(self.lights) == 1:
                alias, light = next(iter(self.lights.items()))
                return [(alias, light)]
            if not self.lights:
                raise ConfigError(
                    "no lamp specified and none saved",
                    hint=(
                        "Run 'bazzitehue scan' to find your bulbs, then "
                        "'bazzitehue add <alias> <address>' to save one."
                    ),
                )
            raise ConfigError(
                "several lamps are saved and no default is set",
                hint="Pass --light <alias|all>, or run 'bazzitehue use <alias>'.",
            )

        if target.lower() == "all":
            if not self.lights:
                raise ConfigError("no saved lamps to address as 'all'")
            return list(self.lights.items())

        resolved: list[tuple[str | None, SavedLight]] = []
        for piece in (part.strip() for part in target.split(",")):
            if not piece:
                continue
            if piece in self.lights:
                resolved.append((piece, self.lights[piece]))
            elif is_address(piece):
                resolved.append((None, SavedLight(address=piece.upper())))
            else:
                raise ConfigError(
                    f"unknown lamp {piece!r}",
                    hint="Use an address, or an alias from 'bazzitehue lights'.",
                )

        if not resolved:
            raise ConfigError(f"could not resolve any lamp from {target!r}")
        return resolved

    # ------------------------------------------------------------------ #
    # Scenes
    # ------------------------------------------------------------------ #

    def add_scene(self, name: str, scene: Scene) -> None:
        self.scenes[name] = scene

    def remove_scene(self, name: str) -> None:
        if name not in self.scenes:
            raise ConfigError(f"no scene called {name!r}")
        del self.scenes[name]
