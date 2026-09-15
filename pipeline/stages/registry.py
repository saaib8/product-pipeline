"""Which stages exist, and how to look one up.

Stages register here as they are built. The registry is deliberately explicit rather
than auto-discovered: `run_stage --all` should do exactly what this file lists, so
turning a stage off is a one-line change and is visible in review.
"""

from __future__ import annotations

from pipeline.stages.base import Stage

_REGISTRY: dict[str, Stage] = {}


def register(stage: Stage) -> Stage:
    if stage.name in _REGISTRY:
        raise ValueError(f"stage {stage.name!r} is already registered")
    _REGISTRY[stage.name] = stage
    return stage


def get(name: str) -> Stage:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown stage {name!r}; known: {', '.join(sorted(_REGISTRY))}") from None


def all_stages() -> list[Stage]:
    return list(_REGISTRY.values())


def names() -> list[str]:
    return sorted(_REGISTRY)
