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
from niadra_mock.cell import MOCK_KEY, MockCell, StoredEvent

__all__ = ["MOCK_KEY", "MockApp", "MockCell", "Response", "StoredEvent"]
