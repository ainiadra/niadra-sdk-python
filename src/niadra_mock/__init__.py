"""niadra-mock: a local, in-memory emulator of the Niadra API for tests and development.

In tests, run the SDK against it in-process, with no server and no network:

```python
import httpx
from niadra import Niadra
from niadra_mock import MOCK_KEY, MockApp

mock = MockApp()
transport = httpx.WSGITransport(app=mock.wsgi)
niadra = Niadra(MOCK_KEY, base_url="http://mock", http_client=httpx.Client(transport=transport))
```

Or start it from a shell with `niadra-mock` and point the SDK at `http://127.0.0.1:8765`.
"""

from niadra_mock.app import MockApp, Response
from niadra_mock.cell import MockCell, StoredEvent

MOCK_KEY = "nia_sk_test_local_mock_k1_mocksecret"
"""A well-formed test key. The emulator accepts any `nia_sk_` key; this one is just convenient."""

__all__ = ["MOCK_KEY", "MockApp", "MockCell", "Response", "StoredEvent"]
