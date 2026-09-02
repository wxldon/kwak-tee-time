"""Checkout: turn a staged cart item into a paid booking.

Call chain, recovered from the site bundle:

  1. POST /order-teetime                 -> order + invoice (no charge)
  2. GET  /tr/token                      -> a UUID used to sign the TR call
  3. POST {TR_BASE}/AddReservation       -> THE CHARGE (form-encoded)
  4. finalize on the Kenna side so the booking shows up in the account

Steps 1 and 2 are verified against the live API. Step 3 is the money call and
is only exercised with the operator's explicit go-ahead.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from .api import ApiError, TeeItUpClient
from .config import Config
from .courses import TR_BASE
from .search import Candidate

log = logging.getLogger(__name__)

# The site sends this fixed channel on every booking.
CHANNEL_ID = "20972"
ENGINE = "5.0"


class CheckoutError(RuntimeError):
    pass


class PaymentPending(RuntimeError):
    """The processor wants a 3-D Secure challenge we cannot answer headlessly."""

    def __init__(self, redirect_url: str, payload: Any):
        self.redirect_url = redirect_url
        self.payload = payload
        super().__init__(f"3-D Secure required: {redirect_url}")


def create_order(
    client: TeeItUpClient,
    cart_id: str,
    cart_item_id: str,
    c: Candidate,
    players: int,
) -> dict[str, Any]:
    """POST /order-teetime -- converts the cart item into an order + invoice.

    This is what the site does on checkout page load. It does not take payment.
    """
    body = {
        "teetime": c.raw_teetime["teetime"],
        "rateId": c.rate_id,
        "cartId": cart_id,
        "cartItemId": cart_item_id,
        "golferQuantity": players,
    }
    if c.raw_rate.get("isMemberGuestRate") is not None:
        body["isMemberGuestRate"] = c.raw_rate["isMemberGuestRate"]
    return client.post("/order-teetime", json=body)


def get_tr_token(client: TeeItUpClient) -> str:
    """GET /tr/token -- a UUID the TR service requires on AddReservation."""
    token = client.get("/tr/token")
    return token if isinstance(token, str) else token.get("token", "")


def build_tr_payload(
    client: TeeItUpClient,
    order: dict[str, Any],
    c: Candidate,
    players: int,
    cfg: Config,
) -> dict[str, Any]:
    """Assemble the form body AddReservation expects.

    Key names come from the bundle's constant table
    (beauty_2c5wc9u65d4n3.js:65504-65540 and :66305-66324).
    """
    invoice = order.get("invoice") or {}
    customer = client.customer or {}
    email = customer.get("username") or cfg.username
    name = customer.get("name") or {}
    card = cfg.card
    phone = _digits(cfg.phone) or _primary_phone(customer)

    payload: dict[str, Any] = {
        "TeeTime.InventoryChannelID": order.get("ChannelId") or CHANNEL_ID,
        "TeeTime.FacilityID": invoice.get("facilityId", c.gn_facility_id),
        "TeeTime.TeeTimeRateID": invoice.get("teeTimeRateId", c.rate_id),
        "TeeTime.PlayerCount": invoice.get("playerCount", players),
        "TeeTime.GroupSize": c.group_size,
        "TeeTime.Amount": -1,
        "TeeTime.ReferenceID": invoice.get("referenceId", ""),
        "Reservation.CustomerEmail": email,
        "Reservation.CustomerNotes": "",
        "Reservation.TrackingCode": f"TL:{order.get('_id', '')}",
        "Reservation.IsCaddyRequested": False,
        "SelectedCourses": c.course_id,
        "ENGINE": ENGINE,
        "ALIAS": client.course.alias,
        "tl.holes": invoice.get("holeCount", c.holes),
        "tl.sessionToken": client.session_token or "",
        "tl.customerMobile": phone,
        "Payment.PhoneNumber": phone,
        "Payment.Name": card.name or name.get("formatted", ""),
        "Payment.CC.CreditCardNumber": _digits(card.number),
        "Payment.CC.ExpirationMonth": card.exp_month,
        "Payment.CC.ExpirationYear": card.exp_year,
        "Payment.CC.CVVCode": card.cvv,
        "Payment.Address.PostalCode": card.zip,
        "Payment.Address.Country": "US",
        "emailOptIn": cfg.email_opt_in,
        "smsOptIn": cfg.sms_opt_in,
        "transactionalSmsOptedIn": cfg.sms_opt_in,
        "bookerFirstName": name.get("given", ""),
        "bookerLastName": name.get("family", ""),
        "BookerEmail": email,
    }
    return {k: v for k, v in payload.items() if v not in ("", None)}


def add_reservation(client: TeeItUpClient, payload: dict[str, Any], token: str) -> dict[str, Any]:
    """POST {TR_BASE}/AddReservation -- this is the call that charges the card.

    Form-encoded, with the TR token added as ``Token``. The service signals
    failure with ``Success: false`` in a 200 body, so status alone is not enough.
    """
    body = dict(payload)
    body["Token"] = token
    resp = client.http.post(
        TR_BASE + "/AddReservation",
        data=urlencode(body),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "origin": client.course.origin,
            "referer": client.course.origin + "/",
        },
        timeout=30.0,
    )
    try:
        data = resp.json()
    except ValueError:
        raise CheckoutError(f"AddReservation returned non-JSON ({resp.status_code}): {resp.text[:300]}")

    if data.get("PaymentStatus") == "Pending" and data.get("RedirectUrl"):
        raise PaymentPending(data["RedirectUrl"], data)
    if data.get("Success") is False:
        raise CheckoutError(
            data.get("Message")
            or data.get("ValidationErrors")
            or f"AddReservation rejected: {data}"
        )
    return data


def finalize(client: TeeItUpClient, tr_result: dict[str, Any], cart_id: str, cart_item_id: str) -> Any:
    """Tell Kenna the TR order went through so the booking appears in the account."""
    status_id = tr_result.get("ReservationStatusID")
    if not status_id:
        return None
    return client.patch(
        f"/order-teetime/status/{status_id}",
        params={"cartId": cart_id, "cartItemId": cart_item_id},
        json={},
    )


def mark_failed(client: TeeItUpClient, cart_id: str, cart_item_id: str, players: int) -> None:
    """Release a reservation whose payment did not complete."""
    try:
        client.patch(
            "/order-teetime/failed",
            params={"cartId": cart_id, "cartItemId": cart_item_id},
            json={"playerCount": players},
        )
    except ApiError as e:
        log.debug("failed-order cleanup returned %s (harmless)", e.status)


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _primary_phone(customer: dict[str, Any]) -> str:
    for p in customer.get("phoneNumbers") or []:
        if p.get("primary"):
            return _digits(p.get("value", ""))
    return ""
