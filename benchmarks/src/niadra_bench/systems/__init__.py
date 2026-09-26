"""Every memory system added through an adapter module in this package (`base.HttpSystem`), by its key.

Importing the package imports each module in it; a module adds its system by defining one
`HttpSystem` subclass with a `system` key. Nothing else lists them.
"""

from __future__ import annotations

import importlib
import pkgutil

from niadra_bench.systems.base import HttpSystem


def _discover() -> dict[str, type[HttpSystem]]:
    found: dict[str, type[HttpSystem]] = {}
    for module in pkgutil.iter_modules(__path__):
        if module.name == "base" or module.name.startswith("_"):
            continue
        loaded = importlib.import_module(f"{__name__}.{module.name}")
        for value in vars(loaded).values():
            if (
                isinstance(value, type)
                and issubclass(value, HttpSystem)
                and value is not HttpSystem
                and value.__module__ == loaded.__name__
            ):
                if value.system in found:
                    raise RuntimeError(f"two adapters use the system key {value.system!r}")
                found[value.system] = value
    return dict(sorted(found.items()))


REGISTRY: dict[str, type[HttpSystem]] = _discover()

__all__ = ["REGISTRY", "HttpSystem"]
