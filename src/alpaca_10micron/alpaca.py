"""Request handling shared by all Alpaca endpoints.

Alpaca parameter names are not case-sensitive ("ClientID", "clientid" and "CLIENTID" are
the same), so parameters are read here from the query string (GET) or the form body
(PUT) with lower-case keys, instead of through FastAPI's Query()/Form() binding.
"""

import itertools
import math
import threading
from dataclasses import dataclass

from fastapi import Request

from alpaca_10micron.errors import BadRequest

_UINT32_MAX = 2**32 - 1
_transaction_ids = itertools.count(1)
_transaction_lock = threading.Lock()


def next_server_transaction_id() -> int:
    with _transaction_lock:
        return (next(_transaction_ids) - 1) % _UINT32_MAX + 1


def _uint32_or_zero(text: str | None) -> int:
    """ClientID and ClientTransactionID: a missing or invalid value counts as 0."""
    try:
        value = int(text) if text is not None else 0
    except ValueError:
        return 0
    return value if 0 <= value <= _UINT32_MAX else 0


@dataclass(frozen=True)
class AlpacaRequest:
    params: dict[str, str]  # keys in lower case
    client_id: int
    client_transaction_id: int

    def ids(self) -> dict[str, int]:
        """The transaction fields for a response."""
        return {
            "ClientTransactionID": self.client_transaction_id,
            "ServerTransactionID": next_server_transaction_id(),
        }

    def get(self, name: str) -> str:
        value = self.params.get(name.lower())
        if value is None:
            raise BadRequest(f"Missing parameter '{name}'")
        return value

    def get_bool(self, name: str) -> bool:
        value = self.get(name).strip().lower()
        if value not in ("true", "false"):
            raise BadRequest(f"Parameter '{name}' must be True or False, not {value!r}")
        return value == "true"

    def get_float(self, name: str) -> float:
        text = self.get(name)
        try:
            value = float(text)
        except ValueError:
            raise BadRequest(f"Parameter '{name}' must be a number, not {text!r}") from None
        if not math.isfinite(value):
            raise BadRequest(f"Parameter '{name}' must be a finite number, not {text!r}")
        return value

    def get_int(self, name: str) -> int:
        text = self.get(name)
        try:
            return int(text)
        except ValueError:
            raise BadRequest(f"Parameter '{name}' must be an integer, not {text!r}") from None


async def read_request(request: Request) -> AlpacaRequest:
    """Parse the Alpaca parameters and remember them for the error handlers."""
    items = []
    if request.method == "PUT":
        # PUT parameters belong in the body; the body wins over the query string.
        form = await request.form()
        items += [(key, value) for key, value in form.multi_items() if isinstance(value, str)]
    items += request.query_params.multi_items()
    params: dict[str, str] = {}
    for key, value in items:
        params.setdefault(key.lower(), value)
    alpaca = AlpacaRequest(
        params=params,
        client_id=_uint32_or_zero(params.get("clientid")),
        client_transaction_id=_uint32_or_zero(params.get("clienttransactionid")),
    )
    request.state.alpaca = alpaca
    return alpaca
