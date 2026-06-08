"""Read-only Python mirror of the validated Rust config."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


@dataclass(frozen=True)
class HashProfileConfig:
    whitespace_insensitive: bool
    normalize_trailing_commas: bool
    include_comments: bool
    include_docstrings: bool


@dataclass(frozen=True)
class SpineConfig:
    retain_cap: int
    default_hash_profile: str


@dataclass(frozen=True)
class LayerConfig:
    origin: str
    depends_on: tuple[str, ...]
    generator: str | None
    generator_version: str | None
    hash_profile: str | None
    store: str | None
    serving: str
    recompute: str
    entity_kinds: tuple[str, ...]
    history: bool
    review_on_change: bool
    key_locality: str = "local"


@dataclass(frozen=True)
class GeneratorConfig:
    type: str | None
    callable: str | None
    command: tuple[str, ...]
    endpoint: str | None
    model: str | None
    dim: int | None
    batch_size: int | None
    concurrency: int | None
    timeout_ms: int | None


@dataclass(frozen=True)
class StoreConfig:
    backend: str
    path: str | None
    url: str | None
    metric: str | None
    dim: int | None
    gc: str


@dataclass(frozen=True)
class SidecarConfig:
    gitignore_cache: bool


@dataclass(frozen=True)
class TyConfig:
    schema_version: int
    spine: SpineConfig
    hashing_profiles: dict[str, HashProfileConfig]
    layers: dict[str, LayerConfig]
    generators: dict[str, GeneratorConfig]
    stores: dict[str, StoreConfig]
    sidecar: SidecarConfig
    topo_order: tuple[str, ...]

    @classmethod
    def from_json(cls, text: str) -> "TyConfig":
        data: dict[str, Any] = json.loads(text)
        raw: dict[str, Any] = data["raw"]
        topo_order = tuple(data["topo_order"])

        raw_layers = raw.get("layers", {})
        layers = {
            name: _layer(raw_layers[name])
            for name in topo_order
            if name != "code" and name in raw_layers
        }

        return cls(
            schema_version=raw["schema_version"],
            spine=SpineConfig(**raw["spine"]),
            hashing_profiles={
                name: HashProfileConfig(**profile)
                for name, profile in raw.get("hashing", {}).get("profiles", {}).items()
            },
            layers=layers,
            generators={
                name: _generator(generator)
                for name, generator in raw.get("generators", {}).items()
            },
            stores={
                name: StoreConfig(**store)
                for name, store in raw.get("stores", {}).items()
            },
            sidecar=SidecarConfig(**raw["sidecar"]),
            topo_order=topo_order,
        )


def _layer(data: dict[str, Any]) -> LayerConfig:
    return LayerConfig(
        origin=data["origin"],
        depends_on=tuple(data.get("depends_on", ())),
        generator=data.get("generator"),
        generator_version=data.get("generator_version"),
        hash_profile=data.get("hash_profile"),
        store=data.get("store"),
        serving=data["serving"],
        recompute=data["recompute"],
        key_locality=data.get("key_locality") or "local",
        entity_kinds=tuple(data.get("entity_kinds", ())),
        history=data["history"],
        review_on_change=data["review_on_change"],
    )


def _generator(data: dict[str, Any]) -> GeneratorConfig:
    return GeneratorConfig(
        type=data.get("type"),
        callable=data.get("callable"),
        command=tuple(data.get("command", ())),
        endpoint=data.get("endpoint"),
        model=data.get("model"),
        dim=data.get("dim"),
        batch_size=data.get("batch_size"),
        concurrency=data.get("concurrency"),
        timeout_ms=data.get("timeout_ms"),
    )


__all__ = [
    "GeneratorConfig",
    "HashProfileConfig",
    "LayerConfig",
    "SidecarConfig",
    "SpineConfig",
    "StoreConfig",
    "TyConfig",
]
