"""Cart and checkout steps.

Everything here was recovered from the site's own bundle and, where marked
VERIFIED, exercised against the live API (and then cleaned up).
"""

from __future__ import annotations

import logging
from typing import Any

from .api import ApiError, TeeItUpClient
from .search import Candidate

log = logging.getLogger(__name__)


class SlotGone(RuntimeError):
    """Someone else took the slot, or it stopped being bookable."""


def build_cart_item(c: Candidate, players: int) -> dict[str, Any]:
    """The exact ``item`` the site posts for a tee time. VERIFIED live.

    ``facilityId`` is the numeric GolfNow facility (the API rejects the string
    entity id), and ``groupSize`` must be 1 for every non-simulator rate.
    The server fills in ``extra.slots`` itself, one per player.
    """
    return {
        "facilityId": c.gn_facility_id,
        "type": "TeeTime",
        "extra": {
            "teetime": c.raw_teetime["teetime"],
            "players": players,
            "groupSize": c.group_size,
            "isPnasSelected": False,
            "price": c.price,
            "rate": {
                "holes": c.holes,
                "price": c.price,
                "rateId": c.rate_id,
                "rateSetId": c.rate_set_id,
                "name": c.rate_name,
                "transactionFees": c.raw_rate.get("transactionFees", 0),
                "transportation": c.transportation,
                "isSimulator": c.raw_rate.get("isSimulator", False),
            },
            "productLineups": [],
            "slots": [],
        },
    }


def create_cart(client: TeeItUpClient) -> str:
    """POST /shopping-cart -> cart id. VERIFIED live."""
    return client.post("/shopping-cart")["id"]


def add_to_cart(client: TeeItUpClient, cart_id: str, c: Candidate, players: int) -> dict[str, Any]:
    """Stage the tee time in the cart. VERIFIED live.

    Note this does NOT hold the slot -- it validates availability and stages the
    item, but another booker can still take it until checkout completes.
    """
    try:
        cart = client.post(
            f"/shopping-cart/{cart_id}/cart-item",
            json={"item": build_cart_item(c, players)},
        )
    except ApiError as e:
        if e.is_gone or _unavailable(e):
            raise SlotGone(f"{c.label()}: {e.body}") from e
        raise
    items = cart.get("items") or []
    if not items:
        raise SlotGone("cart came back empty after add")
    return cart


def _unavailable(e: ApiError) -> bool:
    blob = str(e.body).upper()
    return "UNAVAILABLE" in blob or "NOT_AVAILABLE" in blob or "ALREADY" in blob


def delete_cart(client: TeeItUpClient, cart_id: str) -> None:
    """Best-effort cleanup so a failed attempt leaves nothing behind."""
    try:
        client.delete(f"/shopping-cart/{cart_id}")
    except ApiError as e:
        log.debug("cart cleanup failed (harmless): %s", e)


def is_bookable(client: TeeItUpClient, cart_id: str, item_id: str, players: int) -> Any:
    """POST .../cart-item/{id}/is-bookable -- the server's own pre-check."""
    return client.post(
        f"/shopping-cart/{cart_id}/cart-item/{item_id}/is-bookable",
        json={"players": players},
    )
