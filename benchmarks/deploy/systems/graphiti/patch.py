"""Two fixes to Graphiti's REST server (server/graph_service at the 0.30.2 image), both needed for
`POST /messages` to add any message at all. Apache 2.0; the changes are marked in the files.

1. `get_graphiti` closed the request's Graphiti client when the request ended, but `/messages` answers
   202 and adds the messages later, in a background queue, with that same client: the first episode
   failed on a closed driver. The client is now created once and kept for the process.
2. The queue's worker caught only its own cancellation, so the first failed episode ended the worker and
   every message after it stayed in the queue forever. A failed episode is now logged and the worker
   goes on to the next.
"""

from pathlib import Path

APP = Path("/app/graph_service")

zep = APP / "zep_graphiti.py"
source = zep.read_text()
old = """    try:
        yield client
    finally:
        await client.close()
"""
new = """    # niadra-bench patch 1: one client for the process; /messages uses it after the request ended.
    yield client
"""
assert old in source, "get_graphiti changed upstream"
source = source.replace(old, new)
old_create = "async def get_graphiti(settings: ZepEnvDep):\n    client = _create_graphiti_client(settings)\n"
new_create = (
    "_CLIENT = None\n\n\n"
    "async def get_graphiti(settings: ZepEnvDep):\n"
    "    global _CLIENT\n"
    "    if _CLIENT is None:\n"
    "        _CLIENT = _create_graphiti_client(settings)\n"
    "    client = _CLIENT\n"
)
assert old_create in source, "get_graphiti changed upstream"
zep.write_text(source.replace(old_create, new_create))

ingest = APP / "routers" / "ingest.py"
source = ingest.read_text()
old = """            except asyncio.CancelledError:
                break
"""
new = """            except asyncio.CancelledError:
                break
            except Exception as exc:  # niadra-bench patch 2: one failed episode does not stop the queue
                print(f'Job failed: {type(exc).__name__}: {exc}')
"""
assert old in source, "the worker changed upstream"
ingest.write_text(source.replace(old, new))
print("patched graph_service")
