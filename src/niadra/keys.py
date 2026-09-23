"""Source keys and the API address derived from them.

A key reads `nia_sk_<live|test>_<region>_<space>_<key_id>_<secret>`. It names the region
and the space but never the cell, so the address stays stable when a space moves between
cells: `https://<space>.<region>.api.niadra.com`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from niadra.errors import ConfigurationError

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_KEY = re.compile(rf"^nia_sk_(live|test)_({_LABEL})_({_LABEL})_([A-Za-z0-9]+)_(\S+)$")

API_DOMAIN = "api.niadra.com"


@dataclass(frozen=True)
class ApiKey:
    environment: Literal["live", "test"]
    region: str
    space: str
    key_id: str
    secret: str = field(repr=False)
    raw: str = field(repr=False)

    @classmethod
    def parse(cls, raw: str) -> ApiKey:
        """Parses a source key. The error never echoes the key, which is a secret."""
        match = _KEY.match(raw.strip())
        if match is None:
            raise ConfigurationError(
                "malformed Niadra key: expected nia_sk_<live|test>_<region>_<space>_<key_id>_<secret>"
            )
        environment, region, space, key_id, secret = match.groups()
        env: Literal["live", "test"] = "live" if environment == "live" else "test"
        return cls(env, region, space, key_id, secret, raw.strip())

    @property
    def base_url(self) -> str:
        return f"https://{self.space}.{self.region}.{API_DOMAIN}"
