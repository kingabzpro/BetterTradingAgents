"""One shared httpx.AsyncClient per event loop (P2.10).

Every provider call used to open and close its own client, paying a fresh
connection pool per request. One client per loop keeps sockets warm; the
loops are keyed because checks and tools create several over a process
lifetime, and an AsyncClient is bound to the loop it was created in.
Per-call timeouts stay explicit at each call site.
"""

import asyncio
from weakref import WeakKeyDictionary

import httpx

_clients: WeakKeyDictionary = WeakKeyDictionary()


def get_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """The running loop's shared client, created on first use."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)
        _clients[loop] = client
    return client
