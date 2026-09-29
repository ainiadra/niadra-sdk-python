"""The contact token's offline check, at the company's gateway (`spec/contact-token.md`).

A coordination check that allows an outbound contact of a purpose that needs a token (by default
`marketing`, `retention` and `collection`) carries `contact_token`: `nct1.<payload>.<signature>`, signed with
the space's Ed25519 key. The gateway that dispatches the message checks it without calling anyone, and lets
the message out once:

```python
from niadra.coordination import ContactGateway

gateway = niadra.contact_gateway("wa_gateway", space=SPACE_ID, key=GATEWAY_KEY)
claims = gateway.verify(token, handle=phone("+5511987654321"), channel="whatsapp")  # raises ContactTokenError
```

`verify_contact_token()` is the check itself, the thirteen steps of the spec in their order, each refusal
with its code. `ContactGateway` adds what a gateway keeps: the space's public keys (read from
`GET /.well-known/niadra-contact-keys.json?space=`, again every hour, and at most once a minute for a key it
does not know; the last set read stays in use while Niadra is out of reach) and the `jti` of every token it
let through, until the token's expiry plus the leeway. A gateway of several processes passes a shared
`seen` store. The check needs `cryptography` (`pip install "niadra[gateway]"`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import threading
import time
from collections.abc import Awaitable, Callable, Container, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from niadra.coordination.destination import canonical_destination
from niadra.errors import ConfigurationError, NiadraError
from niadra.models.coordination import ContactKey, ContactKeys

LEEWAY = 5
"""Seconds of clock skew a gateway allows, before `iat` and after `exp`."""
MAX_LIFETIME = 120
MAX_LENGTH = 1024
KEYS_REFRESH = 3600.0
"""Seconds after which a gateway reads the key set again."""
UNKNOWN_KID_REFRESH = 60.0
"""Seconds a gateway waits between two reads for a key it does not know."""

Refusal = Literal[
    "malformed",
    "unsupported_version",
    "unknown_key",
    "bad_signature",
    "wrong_space",
    "wrong_gateway",
    "lifetime_too_long",
    "not_yet_valid",
    "expired",
    "wrong_channel",
    "wrong_recipient",
    "replayed",
]

_MEMBERS: tuple[tuple[str, type], ...] = (
    ("kid", str),
    ("space", str),
    ("jti", str),
    ("purpose", str),
    ("channel", str),
    ("rcpt", str),
    ("gateway", str),
    ("iat", int),
    ("exp", int),
)
_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_KID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class ContactTokenError(NiadraError, ValueError):
    """A token the gateway refuses, with the spec's code. A refused token is never retried: the caller asks
    for a new decision."""

    def __init__(self, code: Refusal) -> None:
        super().__init__(code)
        self.code: Refusal = code


@dataclass(frozen=True, slots=True)
class ContactClaims:
    """The payload of a token that passed."""

    kid: str
    space: str
    jti: str
    purpose: str
    channel: str
    rcpt: str
    gateway: str
    iat: int
    exp: int


class SeenTokens(Protocol):
    """Where a gateway remembers the tokens it let through. `add` records `jti` until `until` (seconds since
    the epoch) and returns False when it was already there: shared between processes, it must do both in
    one atomic step (a Redis `SET NX EXAT`, a unique row)."""

    def add(self, jti: str, until: int) -> bool: ...


class MemorySeen:
    """The tokens one process let through, forgotten after their expiry and the leeway."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._until: dict[str, int] = {}
        self._lock = threading.Lock()

    def add(self, jti: str, until: int) -> bool:
        now = self._clock()
        with self._lock:
            if len(self._until) > 1024:
                self._until = {k: v for k, v in self._until.items() if v > now}
            if self._until.get(jti, 0) > now:
                return False
            self._until[jti] = until
            return True


def recipient_hash(gateway_key: str | bytes, destination: str) -> str:
    """`rcpt` for a canonical destination (`phone:+5511987654321`): the base64url of HMAC-SHA256 keyed with
    the 32-byte key the gateway shares with Niadra (base64url, or the bytes)."""
    key = _b64(gateway_key) if isinstance(gateway_key, str) else gateway_key
    if key is None:
        raise ValueError("the gateway key is not base64url")
    mac = hmac.new(key, destination.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def verify_contact_token(
    token: str,
    *,
    keys: Iterable[ContactKey | Mapping[str, Any]],
    gateway_id: str,
    space: str | UUID,
    gateway_key: str | bytes,
    destination: str,
    channel: str,
    now: float | None = None,
    seen: Container[str] = (),
) -> ContactClaims:
    """Checks `token` for a message of `channel` to `destination` (its canonical form) about to leave
    through `gateway_id`, in the spec's order, and returns its payload. Raises `ContactTokenError` with the
    first refusal that applies. `seen` holds the `jti` the gateway already let through; remembering this one
    is the caller's (`ContactGateway` does it)."""
    moment = int(time.time() if now is None else now)
    parts = token.split(".") if isinstance(token, str) and len(token) <= MAX_LENGTH else []
    if len(parts) != 3 or not all(parts):
        raise ContactTokenError("malformed")
    if parts[0] != "nct1":
        raise ContactTokenError("unsupported_version")
    raw_payload, signature = _b64(parts[1]), _b64(parts[2])
    if raw_payload is None or signature is None or len(signature) != 64:
        raise ContactTokenError("malformed")
    claims = _claims(raw_payload)
    key = _key(keys, claims, moment)
    if key is None:
        raise ContactTokenError("unknown_key")
    if not _signed(key, f"{parts[0]}.{parts[1]}".encode("ascii"), signature):
        raise ContactTokenError("bad_signature")
    if claims.space != str(space).lower():
        raise ContactTokenError("wrong_space")
    if claims.gateway != gateway_id:
        raise ContactTokenError("wrong_gateway")
    if not 0 < claims.exp - claims.iat <= MAX_LIFETIME:
        raise ContactTokenError("lifetime_too_long")
    if claims.iat - LEEWAY > moment:
        raise ContactTokenError("not_yet_valid")
    if moment >= claims.exp + LEEWAY:
        raise ContactTokenError("expired")
    if claims.channel != channel:
        raise ContactTokenError("wrong_channel")
    if not hmac.compare_digest(claims.rcpt, recipient_hash(gateway_key, destination)):
        raise ContactTokenError("wrong_recipient")
    if claims.jti in seen:
        raise ContactTokenError("replayed")
    return claims


KeySet = ContactKeys | Mapping[str, Any]


class _Gateway:
    """What both gateways keep: the id, the space, the shared key, the key set and the tokens let through."""

    def __init__(
        self,
        gateway_id: str,
        *,
        space: str | UUID,
        key: str | bytes,
        keys: Iterable[ContactKey | Mapping[str, Any]] = (),
        seen: SeenTokens | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not _NAME.match(gateway_id):
            raise ConfigurationError("a gateway id matches ^[a-z][a-z0-9_]{0,39}$")
        if (_b64(key) if isinstance(key, str) else key) is None:
            raise ConfigurationError("the gateway key is not base64url")
        self.gateway_id = gateway_id
        self.space = str(space).lower()
        self._key = key
        self._clock = clock
        self._seen: SeenTokens = seen if seen is not None else MemorySeen(clock)
        self._keys = [_as_key(k) for k in keys]
        self._read_at: float | None = None
        self._missed_at = -UNKNOWN_KID_REFRESH
        self._lock = threading.Lock()

    @property
    def keys(self) -> list[ContactKey]:
        with self._lock:
            return list(self._keys)

    def _due(self, now: float) -> bool:
        return self._read_at is None or now - self._read_at >= KEYS_REFRESH

    def _again(self, error: ContactTokenError, now: float) -> bool:
        """Whether an unknown key is worth reading the set again for: at most once a minute."""
        if error.code != "unknown_key" or now - self._missed_at < UNKNOWN_KID_REFRESH:
            return False
        self._missed_at = now
        return True

    def _took(self, data: KeySet | None) -> bool:
        """A read's answer; None when it failed, and the last set stays."""
        with self._lock:
            self._read_at = self._clock()
            if data is None:
                return False
            try:
                self._keys = list(
                    (data if isinstance(data, ContactKeys) else ContactKeys.model_validate(data)).keys
                )
            except ValueError:
                return False
        return True

    def _check(self, token: str, destination: str, channel: str, now: float) -> ContactClaims:
        claims = verify_contact_token(
            token,
            keys=self.keys,
            gateway_id=self.gateway_id,
            space=self.space,
            gateway_key=self._key,
            destination=destination,
            channel=channel,
            now=now,
        )
        if not self._seen.add(claims.jti, claims.exp + LEEWAY):
            raise ContactTokenError("replayed")
        return claims


class ContactGateway(_Gateway):
    """A gateway's side of the contact token: its id, its space, the key it shares with Niadra, the space's
    public keys and the tokens it already let through.

    `read` reads the space's key set: `Niadra.contact_gateway()` passes one that calls
    `GET /.well-known/niadra-contact-keys.json?space=`; `keys` is a fixed set instead. A read that fails keeps
    the last set, however old: a gateway never stops checking because Niadra is out of reach, and a token it
    cannot check is refused.
    """

    def __init__(
        self,
        gateway_id: str,
        *,
        space: str | UUID,
        key: str | bytes,
        read: Callable[[], KeySet] | None = None,
        keys: Iterable[ContactKey | Mapping[str, Any]] = (),
        seen: SeenTokens | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(gateway_id, space=space, key=key, keys=keys, seen=seen, clock=clock)
        self._read = read

    def verify(
        self, token: str, *, channel: str, destination: str | None = None, handle: Any = None
    ) -> ContactClaims:
        """Checks a token for a message of `channel` to `destination` (canonical) or `handle` (a `Handle`, or
        `{"type", "value"}`), and remembers it. Raises `ContactTokenError`."""
        target = _destination(destination, handle)
        now = self._clock()
        if self._read is not None and self._due(now):
            self.refresh()
        try:
            return self._check(token, target, channel, now)
        except ContactTokenError as error:
            if self._read is None or not self._again(error, now):
                raise
        self.refresh()
        return self._check(token, target, channel, now)

    def refresh(self) -> bool:
        """Reads the key set again; False when that failed and the last set stays in use."""
        if self._read is None:
            return False
        try:
            data = self._read()
        except Exception:
            return self._took(None)
        return self._took(data)


class AsyncContactGateway(_Gateway):
    """`ContactGateway` for an event loop: the key set is read without blocking it."""

    def __init__(
        self,
        gateway_id: str,
        *,
        space: str | UUID,
        key: str | bytes,
        read: Callable[[], Awaitable[KeySet]],
        seen: SeenTokens | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(gateway_id, space=space, key=key, seen=seen, clock=clock)
        self._read = read

    async def verify(
        self, token: str, *, channel: str, destination: str | None = None, handle: Any = None
    ) -> ContactClaims:
        """Checks a token and remembers it. See `ContactGateway.verify`."""
        target = _destination(destination, handle)
        now = self._clock()
        if self._due(now):
            await self.refresh()
        try:
            return self._check(token, target, channel, now)
        except ContactTokenError as error:
            if not self._again(error, now):
                raise
        await self.refresh()
        return self._check(token, target, channel, now)

    async def refresh(self) -> bool:
        try:
            data = await self._read()
        except Exception:
            return self._took(None)
        return self._took(data)


def _destination(destination: str | None, handle: Any) -> str:
    if destination is not None:
        return destination
    if handle is None:
        raise ValueError("pass destination= or handle=")
    kind, value = (
        (handle["type"], handle["value"]) if isinstance(handle, Mapping) else (handle.type, handle.value)
    )
    return canonical_destination(str(getattr(kind, "value", kind)), str(value))


def _b64(text: str) -> bytes | None:
    """Strict base64url without padding, or None."""
    if not _SEGMENT.fullmatch(text) or len(text) % 4 == 1:
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None


def _claims(raw: bytes) -> ContactClaims:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ContactTokenError("malformed") from None
    if not isinstance(payload, dict) or set(payload) != {name for name, _ in _MEMBERS}:
        raise ContactTokenError("malformed")
    for name, kind in _MEMBERS:
        value = payload[name]
        if not isinstance(value, kind) or isinstance(value, bool):
            raise ContactTokenError("malformed")
    if not (
        _KID.match(payload["kid"])
        and _uuid(payload["space"])
        and _uuid(payload["jti"])
        and all(_NAME.match(payload[n]) for n in ("purpose", "channel", "gateway"))
        and _b64(payload["rcpt"]) is not None
        and len(payload["rcpt"]) == 43
    ):
        raise ContactTokenError("malformed")
    return ContactClaims(**payload)


def _uuid(text: str) -> bool:
    try:
        return str(UUID(text)) == text
    except ValueError:
        return False


def _as_key(key: ContactKey | Mapping[str, Any]) -> ContactKey:
    return key if isinstance(key, ContactKey) else ContactKey.model_validate(key)


def _key(
    keys: Iterable[ContactKey | Mapping[str, Any]], claims: ContactClaims, now: int
) -> ContactKey | None:
    """The key with the token's `kid` in the token's space; a retiring key past its `not_after` is gone."""
    for item in keys:
        try:
            key = _as_key(item)
        except ValueError:
            continue
        if key.kid != claims.kid or str(key.space) != claims.space:
            continue
        if key.not_after is not None and key.not_after.timestamp() <= now:
            continue
        return key
    return None


def _signed(key: ContactKey, message: bytes, signature: bytes) -> bool:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:  # pragma: no cover - the extra is missing
        raise ConfigurationError(
            'the contact token check needs cryptography: pip install "niadra[gateway]"'
        ) from None
    public = _b64(key.x)
    if public is None or len(public) != 32:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True
