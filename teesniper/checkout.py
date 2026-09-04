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

import requests

from . import debuglog
from .api import ApiError, TeeItUpClient
from .config import Config
from .courses import TR_BASE
from .search import Candidate

log = logging.getLogger(__name__)

# The site sends this fixed channel on every booking.
CHANNEL_ID = "20972"
ENGINE = "5.0"


class CheckoutError(RuntimeError):
    """The booking failed and the card was NOT charged."""


class ChargeUncertain(RuntimeError):
    """The charge was sent but we never saw the answer.

    The money may or may not have moved. Nothing may retry after this: the only
    safe response is to stop everything and tell the operator to go look.
    """


class FinalizeFailed(RuntimeError):
    """The card WAS charged but the booking was not confirmed on the Kenna side."""

    def __init__(self, message: str, tr_result: Any):
        self.tr_result = tr_result
        super().__init__(message)


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
    url = TR_BASE + "/AddReservation"
    # Record the attempt BEFORE it goes out: if we never see a reply, the log
    # is the only evidence that the charge was submitted at all.
    debuglog.log_call("POST", url, request=body)
    try:
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
    except requests.RequestException as e:
        # The request went out; we never saw the reply. Assume money moved.
        debuglog.log_call("POST", url, error=f"NO REPLY {type(e).__name__}: {e}")
        raise ChargeUncertain(
            f"no reply from the payment service ({type(e).__name__}: {e})"
        ) from e

    try:
        data = resp.json()
        debuglog.log_call("POST", url, status=resp.status_code, response=data)
    except ValueError:
        debuglog.log_call("POST", url, status=resp.status_code, response=resp.text[:1000])
        if resp.status_code >= 500 or not resp.ok:
            # A 5xx or an HTML error page after the charge was submitted: we
            # cannot tell whether it was taken.
            raise ChargeUncertain(
                f"payment service returned HTTP {resp.status_code} with a non-JSON body: "
                f"{resp.text[:200]}"
            )
        raise CheckoutError(
            f"AddReservation returned non-JSON ({resp.status_code}): {resp.text[:300]}"
        )

    if data.get("PaymentStatus") == "Pending" and data.get("RedirectUrl"):
        raise PaymentPending(data["RedirectUrl"], data)

    # Only an explicit success counts. Treating "not False" as success would
    # report a decline as a booking.
    if data.get("Success") is not True:
        message = (
            data.get("Message")
            or data.get("ValidationErrors")
            or data.get("ResultStatusName")
            or f"unexpected response: {str(data)[:300]}"
        )
        if not resp.ok:
            raise ChargeUncertain(f"HTTP {resp.status_code} from the payment service: {message}")
        raise CheckoutError(str(message))
    return data


def finalize(client: TeeItUpClient, tr_result: dict[str, Any], cart_id: str, cart_item_id: str) -> Any:
    """Tell Kenna the TR order went through so the booking appears in the account.

    The card is already charged by the time this runs, so a failure here is
    never "try something else" -- it is "stop and tell the operator". Raises
    ``FinalizeFailed`` rather than returning quietly.
    """
    status_id = tr_result.get("ReservationStatusID")
    if not status_id:
        raise FinalizeFailed(
            "payment succeeded but the response carried no ReservationStatusID, "
            "so the booking could not be confirmed with the course",
            tr_result,
        )
    try:
        return client.patch(
            f"/order-teetime/status/{status_id}",
            params={"cartId": cart_id, "cartItemId": cart_item_id},
            json={},
        )
    except (ApiError, requests.RequestException) as e:
        raise FinalizeFailed(
            f"payment succeeded but confirming it with the course failed: {e}", tr_result
        ) from e


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
