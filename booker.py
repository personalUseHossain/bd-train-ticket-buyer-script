"""
booker.py - the booking engine (no UI code).

Flow (run once you press "Start auto-buy"):
  0. optionally wait until a start time (e.g. 07:59:50)
  1. poll search-trips-v2 for the TARGET date until your train shows up
  2. take the new trip_id / trip_route_id / boarding point from that result
  3. load seat-layout for the new trip (gives the first x-action-token)
     and map your chosen seat numbers -> that trip's ticket_ids
  4. reserve-seat  (PATCH, x-action-token rotated from every response)
  5. passenger-details (POST)
  6. OTP request (POST confirm) -> you type the OTP -> verify-otp (POST)
  7. confirm (PATCH) -> payment URL

`api` is a callable supplied by the app:
    api(method, path, params=None, body=None, headers=None, needs_cft=False)
        -> (status, data, response_headers)      raises ApiError on non-200
"""

import json
import re
import time
from datetime import datetime

API_BASE = "https://railspaapi.shohoz.com/v1.0/web"


class ApiError(RuntimeError):
    def __init__(self, status, body, info=None):
        self.status = status
        self.body = body if isinstance(body, str) else json.dumps(body)
        self.info = info or {}
        hint = ""
        if status == 401:
            hint = ("\n\nThe login token was missing/rejected. Make sure the browser window is logged in "
                    "and no longer on the login page, then try again.")
        elif status == 403:
            hint = "\n\nProbably a stale/missing cft token (or blocked request)."
        dbg = f"\n\n[debug] {json.dumps(self.info)}" if self.info else ""
        super().__init__(f"HTTP {status}\n{self.body[:400]}{hint}{dbg}")

    @property
    def is_turnstile(self):
        b = self.body.lower()
        return "turnstile" in b or "cft_response" in b


# ------------------------------------------------------------------ helpers
def pretty(obj, limit=2500):
    s = json.dumps(obj, indent=2, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "\n... (truncated)"


def norm(s):
    return str(s or "").strip().lower()


def find_payment_url(obj):
    """Recursively scan for any bKash / train-pay payment URL."""
    if isinstance(obj, str):
        if re.search(r"https?://[^\s\"']*bkash[^\s\"']*", obj, re.I):
            return obj
        if "train-pay.shohoz.com" in obj:
            return obj
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and ("url" in k.lower() or "link" in k.lower()):
                if any(x in v.lower() for x in ("bkash", "train-pay", "pay")):
                    return v
            r = find_payment_url(v)
            if r:
                return r
    if isinstance(obj, list):
        for v in obj:
            r = find_payment_url(v)
            if r:
                return r
    return None


def find_key(obj, part):
    """First non-empty string value whose key contains `part` (case-insensitive)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if part in k.lower() and isinstance(v, str) and v:
                return v
        for v in obj.values():
            r = find_key(v, part)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, part)
            if r:
                return r
    return None


def seat_index(layout):
    """(floor, seat) -> cell, and seat -> cell (fallback if floor names differ between dates)."""
    exact, loose = {}, {}
    for f in layout:
        for row in f.get("layout", []):
            for c in row:
                n = c.get("seat_number")
                if n:
                    exact[(norm(f.get("floor_name")), norm(n))] = c
                    loose.setdefault(norm(n), c)
    return exact, loose


def wait_until(hhmmss, stop, log):
    """Sleep until today's HH:MM:SS (local time). No-op if empty, invalid or already past."""
    if not hhmmss or not hhmmss.strip():
        return
    try:
        t = datetime.strptime(hhmmss.strip(), "%H:%M:%S").time()
    except ValueError:
        log(f"Ignoring invalid start time '{hhmmss}' (use HH:MM:SS) - starting now.")
        return
    target = datetime.combine(datetime.now().date(), t)
    if target <= datetime.now():
        return
    log(f"Waiting until {target.strftime('%H:%M:%S')} before polling...")
    while not stop.is_set():
        left = (target - datetime.now()).total_seconds()
        if left <= 0:
            return
        stop.wait(min(left, 1.0))


# ------------------------------------------------------------------- Booker
class Booker:
    def __init__(self, api, log, ask_otp, stop, on_trip=None):
        self.api = api
        self.log = log
        self.ask_otp = ask_otp
        self.stop = stop
        self.on_trip = on_trip
        self.xat = None          # current x-action-token (rotated from responses)
        self.body_token = ""     # action_token sent in the reserve-seat body

    # ---- plumbing
    def _call(self, method, path, **kw):
        _status, data, rh = self.api(method, path, **kw)
        new = (rh or {}).get("x-action-token")
        if new and new != self.xat:
            self.xat = new
            self.log(f"x-action-token rotated -> {new[:24]}...")
        return data

    # ---- step 1: wait for the train
    def poll_for_trip(self, cfg):
        params = {
            "from_city": cfg["from_city"],
            "to_city": cfg["to_city"],
            "date_of_journey": cfg["date"],
            "seat_class": cfg["seat_class"],
        }
        interval = max(0.3, float(cfg.get("poll_interval") or 1.0))
        n = errs = 0
        while not self.stop.is_set():
            n += 1
            try:
                data = self._call("GET", "bookings/search-trips-v2", params=params)
                errs = 0
                trains = (data.get("data") or {}).get("trains") or []
                for t in trains:
                    if norm(t.get("trip_number")) == norm(cfg["trip_number"]):
                        for s in t.get("seat_types") or []:
                            if norm(s.get("type")) == norm(cfg["seat_class"]):
                                return t, s
                if n == 1 or n % 20 == 0:
                    self.log(f"poll #{n}: '{cfg['trip_number']}' not live yet "
                             f"({len(trains)} train(s) returned for {cfg['date']})")
            except ApiError as e:
                errs += 1
                if errs == 1 or errs % 10 == 0:
                    self.log(f"poll #{n}: HTTP {e.status} {e.body[:140]}")
            except Exception as e:  # noqa: BLE001
                errs += 1
                if errs == 1 or errs % 10 == 0:
                    self.log(f"poll #{n}: {e}")
            self.stop.wait(interval)
        return None

    # ---- step 3: layout of the new trip
    def load_layout(self, trip, override_token):
        last = None
        for attempt in range(5):
            try:
                data = self._call(
                    "GET", "bookings/seat-layout",
                    params={"trip_id": trip["trip_id"], "trip_route_id": trip["trip_route_id"]},
                    needs_cft=True,
                )
                break
            except ApiError as e:
                last = e
                self.log(f"seat-layout attempt {attempt + 1} failed: HTTP {e.status} {e.body[:120]}")
                time.sleep(0.3)
        else:
            raise last
        self.body_token = override_token or find_key(data, "action_token") or ""
        if not self.xat:
            self.log("WARNING: seat-layout returned no x-action-token header.")
        return (data.get("data") or {}).get("seatLayout") or []

    # ---- step 4-7
    def reserve(self, trip, cell, seat_number):
        tok = self.body_token or self.xat or ""
        body = {
            "ticket_id": cell["ticket_id"],
            "route_id": trip["route_id"],
            "extras": {
                "seat_number": seat_number,
                "trip_number": trip["trip_number"],
                "origin_name": trip["from_city"],
                "destination_name": trip["to_city"],
            },
            "action_token": tok,
        }
        headers = {"x-action-token": self.xat} if self.xat else None
        return self._call("PATCH", "bookings/reserve-seat", body=body, headers=headers)

    def passenger_details(self, trip, ticket_ids):
        return self._call("POST", "bookings/passenger-details", body={
            "trip_id": trip["trip_id"],
            "trip_route_id": trip["trip_route_id"],
            "ticket_ids": ticket_ids,
        })

    def trigger_otp(self, trip, ticket_ids):
        try:
            return self._call("POST", "bookings/confirm", body={
                "trip_id": trip["trip_id"],
                "trip_route_id": trip["trip_route_id"],
                "ticket_ids": ticket_ids,
            })
        except ApiError as e:
            self.log(f"OTP trigger returned HTTP {e.status}: {e.body[:200]}")
            return None

    def verify_otp(self, trip, ticket_ids, otp):
        return self._call("POST", "bookings/verify-otp", body={
            "trip_id": trip["trip_id"],
            "trip_route_id": trip["trip_route_id"],
            "ticket_ids": ticket_ids,
            "otp": otp,
        })

    def confirm(self, trip, ticket_ids, passengers, contact, ip, otp):
        n = len(passengers)
        body = {
            "is_bkash_online": True,
            "boarding_point_id": trip["boarding_point_id"],
            "contactperson": 0,
            "from_city": trip["from_city"],
            "to_city": trip["to_city"],
            "date_of_journey": trip["date_of_journey"],
            "seat_class": trip["seat_class"],
            "gender": [p["gender"] for p in passengers],
            "page": [""] * n,
            "passengerType": [p["type"] for p in passengers],
            "pemail": contact["email"],
            "pmobile": contact["mobile"],
            "pname": [p["name"] for p in passengers],
            "ppassport": [""] * n,
            "priyojon_order_id": None,
            "referral_mobile_number": None,
            "ticket_ids": ticket_ids,
            "trip_id": trip["trip_id"],
            "trip_route_id": trip["trip_route_id"],
            "isShohoz": 0,
            "enable_sms_alert": 0,
            "first_name": [None] * n,
            "middle_name": [None] * n,
            "last_name": [None] * n,
            "date_of_birth": [None] * n,
            "nationality": [None] * n,
            "passport_type": [None] * n,
            "passport_no": [None] * n,
            "passport_expiry_date": [None] * n,
            "visa_type": [None] * n,
            "visa_no": [None] * n,
            "visa_issue_place": [None] * n,
            "visa_issue_date": [None] * n,
            "visa_expire_date": [None] * n,
            "otp": otp,
            "latitude": None,
            "longitude": None,
            "ip_address": ip,
            "selected_mobile_transaction": 1,
        }
        return self._call("PATCH", "bookings/confirm", body=body)

    # ---- the whole thing
    def run(self, cfg):
        """Returns {"url", "result", "seats"} or None if stopped before the train appeared."""
        wait_until(cfg.get("start_at"), self.stop, self.log)
        if self.stop.is_set():
            return None

        self.log(f"Polling for '{cfg['trip_number']}' ({cfg['seat_class']}) on {cfg['date']} ...")
        found = self.poll_for_trip(cfg)
        if not found:
            self.log("Stopped.")
            return None
        train, stype = found
        t0 = time.time()

        trip = {
            "trip_id": stype["trip_id"],
            "trip_route_id": stype["trip_route_id"],
            "route_id": stype["trip_route_id"],
            "trip_number": train["trip_number"],
            "from_city": cfg["from_city"],
            "to_city": cfg["to_city"],
            "date_of_journey": cfg["date"],
            "seat_class": stype["type"],
            "boarding_point_id": (train.get("boarding_points") or [{}])[0].get("trip_point_id"),
        }
        self.log(f"TRAIN IS LIVE -> trip_id={trip['trip_id']} trip_route_id={trip['trip_route_id']} "
                 f"boarding_point_id={trip['boarding_point_id']}")
        if self.on_trip:
            self.on_trip(trip)

        layout = self.load_layout(trip, cfg.get("body_token") or "")
        exact, loose = seat_index(layout)

        pairs = []
        for seat, pax in zip(cfg["seats"], cfg["passengers"]):
            cell = exact.get((norm(seat["floor"]), norm(seat["seat_number"]))) or loose.get(norm(seat["seat_number"]))
            if not cell:
                self.log(f"Seat {seat['seat_number']} does not exist in the new layout - skipped.")
                continue
            pairs.append((seat, cell, pax))
        if not pairs:
            raise RuntimeError("None of your selected seats exist in the new trip's layout.")

        reserved = []
        for seat, cell, pax in pairs:
            if cell.get("seat_availability") != 1:
                self.log(f"Note: {seat['seat_number']} shows as not available - trying anyway.")
            try:
                self.reserve(trip, cell, seat["seat_number"])
                reserved.append((seat, cell, pax))
                self.log(f"RESERVED {seat['seat_number']} (ticket {cell['ticket_id']}) "
                         f"[{time.time() - t0:.2f}s after the train appeared]")
            except ApiError as e:
                self.log(f"Could not reserve {seat['seat_number']}: HTTP {e.status} {e.body[:160]}")
        if not reserved:
            raise RuntimeError("No seat could be reserved (all taken or rejected).")

        ticket_ids = [c["ticket_id"] for _s, c, _p in reserved]
        passengers = [p for _s, _c, p in reserved]

        self.passenger_details(trip, ticket_ids)
        self.log("Passenger list submitted.")

        self.log("Requesting OTP - check your phone...")
        self.trigger_otp(trip, ticket_ids)
        otp = self.ask_otp()
        if not otp:
            raise RuntimeError("No OTP entered - seats stay reserved only until the hold expires.")
        self.verify_otp(trip, ticket_ids, otp)
        self.log("OTP verified.")

        result = self.confirm(trip, ticket_ids, passengers, cfg["contact"], cfg["ip"], otp)
        url = find_payment_url(result)
        self.log("Booking confirmed." if url else "Confirm returned, but no payment URL was detected.")
        return {"url": url, "result": result, "seats": [s["seat_number"] for s, _c, _p in reserved]}