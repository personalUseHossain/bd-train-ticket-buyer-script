"""
Bangladesh Railway e-ticket - future-release ticket buyer (tkinter UI)

Flow:
  0. "Open browser & login": a real Chrome/Edge opens, you log in, the app uses that live
     session (login token + Turnstile cft token are handled automatically).
  1. Search a date where the train ALREADY exists (e.g. today / tomorrow) to find it.
  2. Pick the train and class, load the seat layout, click up to 4 seats
     (taken seats are selectable too - you are choosing seat NUMBERS for a future date).
  3. Enter the BOOKING date (the date that is not released yet), poll interval and an
     optional start time (e.g. 07:59:50), then press "Start auto-buy".
  4. Fill in passenger details. The app polls until the train appears for the booking date,
     then resolves the new trip/ticket IDs and reserves your seats, requests the OTP
     (you type it), confirms, and opens the bKash payment URL. Payment stays manual.

Requires: pip install playwright requests
"""

import json
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import ttk, messagebox, simpledialog

try:
    import requests
except ImportError:  # only used to auto-detect your public IP
    requests = None

from booker import API_BASE, ApiError, Booker
from browser_session import BrowserSession

# ---------------------------------------------------------------- config ---
# Only used as a LAST resort - normally copied live from the site's own requests.
DEVICE_ID = ""
DEVICE_KEY = ""

DEFAULT_FROM = "Dhaka"
DEFAULT_TO = "Cox's Bazar"
DEFAULT_DATE = "17-Oct-2026"        # date used to FIND the train (must already be released)
DEFAULT_BOOK_DATE = "18-Oct-2026"   # date you actually want to buy
DEFAULT_CLASS = "AC_B"
SEARCH_CLASSES = ["AC_B", "AC_S", "S_CHAIR", "SNIGDHA", "SHOVAN", "F_BERTH", "F_SEAT", "F_CHAIR", "SHULOV"]

MAX_SEATS = 4
CFT_REFRESH_SECONDS = 60
# Run one copy per account:   python index.py 1     python index.py 2
ACCOUNT = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else "1"
_SFX = "" if ACCOUNT == "1" else f"_{ACCOUNT}"     # account 1 keeps your old file names
SAVED_INFO = Path(f"passenger_info{_SFX}.json")    # stays on your machine
PAYMENT_FILE = Path(f"payment_url{_SFX}.txt")
TRIP_FILE = Path(f"trip_data{_SFX}.json")

C_FREE, C_TAKEN, C_SELECTED = "#2e9e5b", "#d1d5db", "#2563eb"
C_TAKEN_FG = "#6b7280"
# ---------------------------------------------------------------------------


def detect_ip():
    if requests is None:
        return ""
    try:
        return requests.get("https://api.ipify.org", timeout=3).text.strip()
    except Exception:  # noqa: BLE001
        return ""


class PassengerDialog(tk.Toplevel):
    """Asks for contact + one passenger per selected seat."""
    GENDERS = ["male", "female"]
    TYPES = ["Adult", "Child"]

    def __init__(self, parent, seats, saved):
        super().__init__(parent)
        self.title("Passenger details")
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self.seats = seats  # [(seat_number, floor_name), ...]

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        bold = ("Segoe UI", 10, "bold")

        ttk.Label(frm, text="Contact", font=bold).grid(row=0, column=0, columnspan=4, sticky="w")
        self.v_mobile = tk.StringVar(value=saved.get("mobile", ""))
        self.v_email = tk.StringVar(value=saved.get("email", ""))
        self.v_ip = tk.StringVar(value=saved.get("ip") or detect_ip())
        for r, (lab, var) in enumerate((("Mobile", self.v_mobile), ("Email", self.v_email),
                                        ("Your IP", self.v_ip)), start=1):
            ttk.Label(frm, text=lab).grid(row=r, column=0, sticky="w", pady=2)
            ttk.Entry(frm, textvariable=var, width=34).grid(row=r, column=1, columnspan=3, sticky="w", pady=2)

        ttk.Label(frm, text="Passengers (one per seat, in the order you picked them)",
                  font=bold).grid(row=5, column=0, columnspan=4, sticky="w", pady=(12, 2))
        for c, h in enumerate(("Seat", "Name", "Gender", "Type")):
            ttk.Label(frm, text=h).grid(row=6, column=c, sticky="w")

        self.rows = []
        sp = saved.get("passengers", [])
        for i, (num, _floor) in enumerate(seats):
            p = sp[i] if i < len(sp) else {}
            v_name = tk.StringVar(value=p.get("name", ""))
            v_gender = tk.StringVar(value=p.get("gender", "male"))
            v_type = tk.StringVar(value=p.get("type", "Adult"))
            ttk.Label(frm, text=num).grid(row=7 + i, column=0, sticky="w", pady=2)
            ttk.Entry(frm, textvariable=v_name, width=26).grid(row=7 + i, column=1, padx=4, pady=2)
            ttk.Combobox(frm, textvariable=v_gender, values=self.GENDERS, width=8).grid(row=7 + i, column=2, padx=4)
            ttk.Combobox(frm, textvariable=v_type, values=self.TYPES, width=8).grid(row=7 + i, column=3, padx=4)
            self.rows.append((v_name, v_gender, v_type))

        btns = ttk.Frame(frm)
        btns.grid(row=20, column=0, columnspan=4, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text="Start", command=self._ok).pack(side="right")

        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _ok(self):
        mobile, email, ip = self.v_mobile.get().strip(), self.v_email.get().strip(), self.v_ip.get().strip()
        if not mobile or not email:
            messagebox.showwarning("Missing info", "Mobile and email are required.", parent=self)
            return
        pax = []
        for i, (vn, vg, vt) in enumerate(self.rows):
            if not vn.get().strip():
                messagebox.showwarning("Missing info", f"Enter a name for seat {self.seats[i][0]}.", parent=self)
                return
            pax.append({"name": vn.get().strip(), "gender": vg.get().strip(), "type": vt.get().strip()})
        self.result = {"mobile": mobile, "email": email, "ip": ip, "passengers": pax}
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Railway future-ticket buyer - ACCOUNT {ACCOUNT}")
        self.geometry("1280x880")
        self.minsize(1050, 700)

        self.q = queue.Queue()
        self.trains = []
        self.cur_train = None
        self.cur_type = None
        self.search_info = {}
        self.layout = []
        self.selected = {}       # (floor_name, seat_number) -> reference ticket_id
        self.seat_labels = {}

        self.browser = BrowserSession(ACCOUNT)
        self.cft = None
        self.cft_time = 0.0
        self._cft_lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.running = False

        self._build_ui()
        self.after(100, self._poll)
        self.after(1000, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        self.stop_ev.set()
        self.browser.close()
        self.destroy()

    # ------------------------------------------------------------ threading
    def run_bg(self, fn, done, on_error=None):
        def worker():
            try:
                self.q.put((done, fn(), None, on_error))
            except Exception as e:  # noqa: BLE001
                self.q.put((done, None, e, on_error))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                done, res, err, on_error = self.q.get_nowait()
                if err:
                    if on_error:
                        on_error()
                    self.status.set("Error")
                    self._append_log(f"ERROR: {err}\n")
                    messagebox.showerror("Request failed", str(err))
                else:
                    done(res)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # thread-safe UI helpers (go through the queue)
    def log(self, msg):
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.q.put((self._append_log, f"[{stamp}] {msg}\n", None, None))

    def _append_log(self, text):
        self.log_box.insert("end", text)
        self.log_box.see("end")

    def _set_trip_box(self, text):
        self.trip_box.delete("1.0", "end")
        self.trip_box.insert("1.0", text)

    # ------------------------------------------------- credentials (browser)
    def connect_browser(self):
        self.btn_login.state(["disabled"])
        self.status.set("Browser opened - log in there; waiting for login...")
        self.v_auth.set("auth: waiting for login...")

        def done(_tok):
            self.btn_login.state(["!disabled"])
            self.v_auth.set("auth: OK (logged in)")
            self.status.set("Logged in. Getting verification token...")
            self.refresh_cft_async()

        self.run_bg(self.browser.login_and_get_token, done,
                    on_error=lambda: (self.btn_login.state(["!disabled"]), self.v_auth.set("auth: not logged in")))

    def fetch_cft(self, force=False):
        """Blocking - call from a background thread. Returns a usable cft token."""
        with self._cft_lock:
            fresh = self.cft and (time.time() - self.cft_time) < CFT_REFRESH_SECONDS
            if fresh and not force:
                return self.cft
            if not self.browser.started:
                raise RuntimeError("Browser not connected - click 'Open browser & login' first "
                                   "(or paste a cft token manually).")
            self.cft = self.browser.get_cft()
            self.cft_time = time.time()
            return self.cft

    def refresh_cft_async(self):
        self.run_bg(lambda: self.fetch_cft(force=True), lambda _t: self.status.set("Verification token ready"))

    def _tick(self):
        if self.cft:
            age = int(time.time() - self.cft_time)
            self.v_cft_state.set(f"cft: {age}s old")
            # refresh a bit BEFORE the 60s freshness window ends so a fresh token is always ready
            if self.v_auto.get() and age >= CFT_REFRESH_SECONDS - 15 and self.browser.started \
                    and not self._cft_lock.locked():
                self.refresh_cft_async()
        else:
            self.v_cft_state.set("cft: none")
        self.after(1000, self._tick)

    def _show_auth(self, info):
        rem = info.get("token_remaining_s")
        txt = "auth: OK" if rem is None else f"auth: OK ({max(rem, 0) // 60}m left)"
        self.q.put((lambda t: self.v_auth.set(t), txt, None, None))

    def api(self, method, path, params=None, body=None, headers=None, needs_cft=False, check=True):
        """Blocking - call from a background thread. Returns (status, data, response_headers).

        Runs inside the logged-in browser page; the login token is re-read on every call and the
        cft token is regenerated when needed, so both stay in sync with the browser.
        """
        if not self.browser.started:
            raise RuntimeError("Not logged in - click 'Open browser & login' first.")
        manual_cft = self.v_cft.get().strip()
        url = f"{API_BASE}/{path}"
        err = None
        for attempt in range(3):
            p = dict(params or {})
            if needs_cft:
                p["cft_response"] = manual_cft or self.fetch_cft(force=(attempt > 0))
            status, text, rh, info = self.browser.api_request(method, url, p, body, headers, DEVICE_ID, DEVICE_KEY)
            try:
                data = json.loads(text)
            except ValueError:
                data = {"_raw": text}
            if status == 200:
                self._show_auth(info)
                return status, data, rh
            err = ApiError(status, text, info)
            if status == 401 and attempt == 0:
                time.sleep(1.5)  # token is re-read from the browser on the next attempt
                continue
            if needs_cft and err.is_turnstile and not manual_cft and attempt < 2:
                continue  # loop again with a force-refreshed cft
            if check:
                raise err
            return status, data, rh
        raise err

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}
        bold = ("Segoe UI", 10, "bold")

        # ---- credentials bar
        cred = ttk.Frame(self)
        cred.pack(fill="x", padx=8, pady=(8, 0))
        self.btn_login = ttk.Button(cred, text="Open browser & login", command=self.connect_browser)
        self.btn_login.pack(side="left", padx=(0, 8))
        self.v_auth = tk.StringVar(value="auth: not logged in")
        self.v_cft_state = tk.StringVar(value="cft: none")
        ttk.Label(cred, textvariable=self.v_auth, width=26).pack(side="left")
        ttk.Label(cred, textvariable=self.v_cft_state, width=14).pack(side="left")
        ttk.Button(cred, text="Refresh cft now", command=self.refresh_cft_async).pack(side="left", padx=6)
        self.v_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(cred, text="Keep cft fresh automatically", variable=self.v_auto).pack(side="left", padx=6)

        # ---- search bar
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(6, 2))
        self.v_from = tk.StringVar(value=DEFAULT_FROM)
        self.v_to = tk.StringVar(value=DEFAULT_TO)
        self.v_date = tk.StringVar(value=DEFAULT_DATE)
        self.v_class = tk.StringVar(value=DEFAULT_CLASS)
        for label, var, w in (("From", self.v_from, 14), ("To", self.v_to, 14), ("Search date", self.v_date, 12)):
            ttk.Label(bar, text=label).pack(side="left", **pad)
            ttk.Entry(bar, textvariable=var, width=w).pack(side="left", **pad)
        ttk.Label(bar, text="Class").pack(side="left", **pad)
        ttk.Combobox(bar, textvariable=self.v_class, values=SEARCH_CLASSES, width=9).pack(side="left", **pad)
        self.btn_search = ttk.Button(bar, text="Search trains", command=self.search)
        self.btn_search.pack(side="left", padx=10)

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=6)
        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=2)
        body.add(right, weight=3)

        # ---- left: trains
        ttk.Label(left, text="1. Trains (from the search date)", font=bold).pack(anchor="w")
        cols = ("n", "train", "dep", "arr")
        self.t_trains = ttk.Treeview(left, columns=cols, show="headings", height=6, selectmode="browse")
        for c, t, w in (("n", "#", 30), ("train", "Train", 190), ("dep", "Departure", 110), ("arr", "Arrival", 110)):
            self.t_trains.heading(c, text=t)
            self.t_trains.column(c, width=w, anchor="w")
        self.t_trains.pack(fill="x")
        self.t_trains.bind("<<TreeviewSelect>>", self.on_train)

        ttk.Label(left, text="2. Seat class", font=bold).pack(anchor="w", pady=(8, 0))
        cols = ("type", "fare", "online", "offline")
        self.t_types = ttk.Treeview(left, columns=cols, show="headings", height=4, selectmode="browse")
        for c, t, w in (("type", "Type", 110), ("fare", "Fare", 70), ("online", "Online", 70), ("offline", "Offline", 70)):
            self.t_types.heading(c, text=t)
            self.t_types.column(c, width=w, anchor="w")
        self.t_types.pack(fill="x")
        self.t_types.bind("<<TreeviewSelect>>", self.on_type)

        ttk.Label(left, text="3. Seat layout (reference date)", font=bold).pack(anchor="w", pady=(8, 0))
        row = ttk.Frame(left)
        row.pack(fill="x")
        ttk.Label(row, text="cft override").pack(side="left")
        self.v_cft = tk.StringVar()  # leave empty to use the automatic token
        ttk.Entry(row, textvariable=self.v_cft).pack(side="left", fill="x", expand=True, padx=6)
        self.btn_layout = ttk.Button(left, text="Load seat layout", command=self.load_layout, state="disabled")
        self.btn_layout.pack(anchor="w", pady=4)

        nb = ttk.Notebook(left)
        nb.pack(fill="both", expand=True, pady=(6, 0))
        f1, f2 = ttk.Frame(nb), ttk.Frame(nb)
        nb.add(f1, text="Trip info")
        nb.add(f2, text="Live log")
        self.trip_box = tk.Text(f1, height=10, font=("Consolas", 9), wrap="none")
        self.trip_box.pack(fill="both", expand=True)
        self.log_box = tk.Text(f2, height=10, font=("Consolas", 9), wrap="word")
        self.log_box.pack(fill="both", expand=True)
        nb.select(f2)

        # ---- right: floor + grid
        top = ttk.Frame(right)
        top.pack(fill="x")
        ttk.Label(top, text="4. Floor", font=bold).pack(side="left")
        self.v_floor = tk.StringVar()
        self.cb_floor = ttk.Combobox(top, textvariable=self.v_floor, state="readonly", width=24)
        self.cb_floor.pack(side="left", padx=8)
        self.cb_floor.bind("<<ComboboxSelected>>", self.on_floor)
        for txt, col in (("Free", C_FREE), ("Taken (selectable)", C_TAKEN), ("Yours", C_SELECTED)):
            tk.Label(top, text=f" {txt} ", bg=col, fg="white" if col != C_TAKEN else C_TAKEN_FG).pack(side="left", padx=3)

        wrap = ttk.Frame(right)
        wrap.pack(fill="both", expand=True, pady=6)
        self.canvas = tk.Canvas(wrap, highlightthickness=0, bg="white", height=300)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.grid_frame = tk.Frame(self.canvas, bg="white")
        self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        # ---- auto-buy panel
        ab = ttk.LabelFrame(right, text="5. Auto-buy for a future date", padding=8)
        ab.pack(fill="x", pady=(0, 6))
        r1 = ttk.Frame(ab)
        r1.pack(fill="x")
        self.v_book_date = tk.StringVar(value=DEFAULT_BOOK_DATE)
        self.v_interval = tk.StringVar(value="1.0")
        self.v_start_at = tk.StringVar(value="")
        ttk.Label(r1, text="Booking date").pack(side="left", padx=(0, 4))
        ttk.Entry(r1, textvariable=self.v_book_date, width=12).pack(side="left")
        ttk.Label(r1, text="Poll every (s)").pack(side="left", padx=(12, 4))
        ttk.Entry(r1, textvariable=self.v_interval, width=5).pack(side="left")
        ttk.Label(r1, text="Start polling at (HH:MM:SS, optional)").pack(side="left", padx=(12, 4))
        ttk.Entry(r1, textvariable=self.v_start_at, width=9).pack(side="left")
        r2 = ttk.Frame(ab)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="body action_token (blank = auto-detect)").pack(side="left", padx=(0, 4))
        self.v_body_tok = tk.StringVar(value=self._load_saved().get("body_action_token", ""))
        ttk.Entry(r2, textvariable=self.v_body_tok).pack(side="left", fill="x", expand=True)
        r3 = ttk.Frame(ab)
        r3.pack(fill="x", pady=(8, 0))
        self.btn_start = ttk.Button(r3, text="Start auto-buy", command=self.start_auto)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(r3, text="Stop", command=self.stop_auto, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(r3, text="Clear seats", command=self.clear_seats).pack(side="left", padx=6)

        ttk.Label(right, text="Plan JSON", font=bold).pack(anchor="w")
        self.out_box = tk.Text(right, height=8, font=("Consolas", 9), wrap="none")
        self.out_box.pack(fill="both", expand=False)
        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="Copy JSON", command=self.copy_json).pack(side="left")
        ttk.Button(btns, text="Save to trip_data.json", command=self.save_json).pack(side="left", padx=6)

        self.status = tk.StringVar(value="Ready - click 'Open browser & login' first")
        ttk.Label(self, textvariable=self.status, anchor="w", relief="sunken").pack(fill="x", side="bottom")

    # ------------------------------------------------------------- step 1
    def search(self):
        info = {
            "from_city": self.v_from.get().strip(),
            "to_city": self.v_to.get().strip(),
            "date_of_journey": self.v_date.get().strip(),
        }
        params = dict(info, seat_class=self.v_class.get().strip())
        self.search_info = info
        self.status.set("Searching trains...")
        self.btn_search.state(["disabled"])

        def done(res):
            self.btn_search.state(["!disabled"])
            self.trains = (res.get("data") or {}).get("trains") or []
            self.t_trains.delete(*self.t_trains.get_children())
            self.t_types.delete(*self.t_types.get_children())
            for i, t in enumerate(self.trains):
                self.t_trains.insert("", "end", iid=str(i), values=(
                    i, t["trip_number"], t["departure_date_time"], t["arrival_date_time"]))
            self.status.set(f"{len(self.trains)} train(s) found" if self.trains else "No trains found")

        self.run_bg(lambda: self.api("GET", "bookings/search-trips-v2", params)[1], done,
                    on_error=lambda: self.btn_search.state(["!disabled"]))

    # ------------------------------------------------------------- step 2
    def on_train(self, _evt=None):
        sel = self.t_trains.selection()
        if not sel:
            return
        self.cur_train = self.trains[int(sel[0])]
        self.cur_type = None
        self.btn_layout.state(["disabled"])
        self.t_types.delete(*self.t_types.get_children())
        for i, s in enumerate(self.cur_train["seat_types"]):
            sc = s.get("seat_counts", {})
            self.t_types.insert("", "end", iid=str(i), values=(
                s["type"], s["fare"], sc.get("online", "-"), sc.get("offline", "-")))

    def on_type(self, _evt=None):
        sel = self.t_types.selection()
        if not sel or not self.cur_train:
            return
        self.cur_type = self.cur_train["seat_types"][int(sel[0])]
        self.btn_layout.state(["!disabled"])
        self.selected.clear()
        self._show_trip_info()
        self.update_output()

    def trip_dict(self):
        """Reference trip (the date you searched). Real IDs for the booking date are resolved live."""
        t, s = self.cur_train, self.cur_type
        bp = (t.get("boarding_points") or [{}])[0].get("trip_point_id")
        return {
            "trip_id": s["trip_id"],
            "trip_route_id": s["trip_route_id"],
            "route_id": s["trip_route_id"],
            "trip_number": t["trip_number"],
            "from_city": self.search_info.get("from_city"),
            "to_city": self.search_info.get("to_city"),
            "date_of_journey": self.search_info.get("date_of_journey"),
            "seat_class": s["type"],
            "boarding_point_id": bp,
        }

    def _show_trip_info(self):
        self._set_trip_box(json.dumps({"reference_trip": self.trip_dict()}, indent=2))

    # ------------------------------------------------------------- step 3
    def load_layout(self):
        if not self.cur_type:
            return
        params = {"trip_id": self.cur_type["trip_id"], "trip_route_id": self.cur_type["trip_route_id"]}
        self.status.set("Loading seat layout...")
        self.btn_layout.state(["disabled"])

        def done(res):
            self.btn_layout.state(["!disabled"])
            self.layout = (res.get("data") or {}).get("seatLayout") or []
            self.selected.clear()
            names = []
            for f in self.layout:
                free = sum(1 for row in f["layout"] for c in row if c["seat_number"] and c["seat_availability"] == 1)
                names.append(f'{f["floor_name"]}  ({free} free)')
            self.cb_floor["values"] = names
            self.v_floor.set("")
            self._clear_grid()
            self.update_output()
            self.status.set(f"{len(self.layout)} floor(s) loaded - pick one")

        self.run_bg(lambda: self.api("GET", "bookings/seat-layout", params, needs_cft=True)[1], done,
                    on_error=lambda: self.btn_layout.state(["!disabled"]))

    # ------------------------------------------------------------- step 4
    def on_floor(self, _evt=None):
        idx = self.cb_floor.current()
        if idx >= 0:
            self.draw_floor(self.layout[idx])

    def _clear_grid(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.seat_labels.clear()

    def draw_floor(self, floor):
        self._clear_grid()
        fname = floor["floor_name"]
        for r, row in enumerate(floor["layout"]):
            for c, cell in enumerate(row):
                num = cell["seat_number"]
                if not num:
                    tk.Label(self.grid_frame, text="", width=7, height=1, bg="white").grid(row=r, column=c, padx=3, pady=3)
                    continue
                lbl = tk.Label(self.grid_frame, text=num, width=7, height=2, font=("Segoe UI", 9, "bold"),
                               relief="flat", cursor="hand2")
                lbl.grid(row=r, column=c, padx=3, pady=3)
                lbl._info = (num, cell["ticket_id"], cell["seat_availability"] == 1, fname)
                self.seat_labels[(fname, num)] = lbl
                self._paint(lbl)
                lbl.bind("<Button-1>", lambda e, l=lbl: self.toggle(l))  # ANY seat is selectable
        self.canvas.yview_moveto(0)

    def _paint(self, lbl):
        num, _tid, free, fname = lbl._info
        if (fname, num) in self.selected:
            lbl.configure(bg=C_SELECTED, fg="white")
        elif free:
            lbl.configure(bg=C_FREE, fg="white")
        else:
            lbl.configure(bg=C_TAKEN, fg=C_TAKEN_FG)

    def toggle(self, lbl):
        num, tid, _free, fname = lbl._info
        key = (fname, num)
        if key in self.selected:
            del self.selected[key]
        else:
            if len(self.selected) >= MAX_SEATS:
                messagebox.showwarning("Limit", f"Only {MAX_SEATS} seats per booking.")
                return
            self.selected[key] = tid
        self._paint(lbl)
        self.update_output()

    # ------------------------------------------------------------- output
    def output_dict(self):
        d = {}
        if self.cur_type:
            d["reference_trip"] = self.trip_dict()
            d["target"] = {
                "date_of_journey": self.v_book_date.get().strip(),
                "trip_number": self.cur_train["trip_number"],
                "seat_class": self.cur_type["type"],
            }
        d["seats"] = [{"floor": f, "seat_number": n, "reference_ticket_id": tid}
                      for (f, n), tid in self.selected.items()]
        return d

    def update_output(self):
        self.out_box.delete("1.0", "end")
        self.out_box.insert("1.0", json.dumps(self.output_dict(), indent=2))
        self.status.set(f"{len(self.selected)} seat(s) selected")

    def clear_seats(self):
        self.selected.clear()
        for lbl in self.seat_labels.values():
            self._paint(lbl)
        self.update_output()

    def copy_json(self):
        self.clipboard_clear()
        self.clipboard_append(self.out_box.get("1.0", "end").strip())
        self.status.set("JSON copied to clipboard")

    def save_json(self):
        with open(TRIP_FILE, "w", encoding="utf-8") as f:
            f.write(self.out_box.get("1.0", "end").strip())
        self.status.set(f"Saved {TRIP_FILE}")

    # ------------------------------------------------------------- auto-buy
    @staticmethod
    def _load_saved():
        try:
            return json.loads(SAVED_INFO.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def ask_otp(self):
        """Called from the booking thread; shows the dialog on the UI thread and waits."""
        ev, box = threading.Event(), {}

        def show():
            self.bell()
            self.lift()
            while True:
                v = simpledialog.askstring("OTP", "Enter the SMS OTP (4-8 digits):", parent=self)
                if v is None:
                    box["v"] = None
                    break
                v = v.strip()
                if v.isdigit() and 4 <= len(v) <= 8:
                    box["v"] = v
                    break
            ev.set()

        self.q.put((lambda _r: show(), None, None, None))
        ev.wait()
        return box.get("v")

    def _on_resolved_trip(self, trip):
        self.q.put((self._set_trip_box,
                    json.dumps({"RESOLVED_TRIP_FOR_BOOKING_DATE": trip}, indent=2), None, None))

    def _set_running(self, running):
        self.running = running
        self.btn_start.state(["disabled"] if running else ["!disabled"])
        self.btn_stop.state(["!disabled"] if running else ["disabled"])

    def start_auto(self):
        if self.running:
            return
        if not self.browser.started:
            messagebox.showwarning("Not logged in", "Click 'Open browser & login' first.")
            return
        if not (self.cur_train and self.cur_type):
            messagebox.showwarning("Pick a train", "Search, then select a train and a seat class first.")
            return
        if not self.selected:
            messagebox.showwarning("Pick seats", "Select at least one seat.")
            return
        book_date = self.v_book_date.get().strip()
        if not book_date:
            messagebox.showwarning("Booking date", "Enter the booking date, e.g. 18-Oct-2026.")
            return
        try:
            interval = float(self.v_interval.get())
        except ValueError:
            messagebox.showwarning("Poll interval", "Poll interval must be a number of seconds.")
            return

        seats = [{"floor": f, "seat_number": n, "ref_ticket_id": tid} for (f, n), tid in self.selected.items()]
        dlg = PassengerDialog(self, [(s["seat_number"], s["floor"]) for s in seats], self._load_saved())
        self.wait_window(dlg)
        if not dlg.result:
            return
        pax = dlg.result
        SAVED_INFO.write_text(json.dumps({**pax, "body_action_token": self.v_body_tok.get().strip()},
                                         indent=2), encoding="utf-8")

        cfg = {
            "from_city": self.search_info.get("from_city") or self.v_from.get().strip(),
            "to_city": self.search_info.get("to_city") or self.v_to.get().strip(),
            "date": book_date,
            "trip_number": self.cur_train["trip_number"],
            "seat_class": self.cur_type["type"],
            "seats": seats,
            "passengers": pax["passengers"],
            "contact": {"email": pax["email"], "mobile": pax["mobile"]},
            "ip": pax["ip"],
            "body_token": self.v_body_tok.get().strip(),
            "poll_interval": interval,
            "start_at": self.v_start_at.get().strip(),
        }

        self.v_auto.set(True)       # keep cft warm while we wait
        self.refresh_cft_async()
        self.stop_ev.clear()
        self._set_running(True)
        self.status.set("Auto-buy running...")
        self.log(f"Auto-buy started: {cfg['trip_number']} {cfg['seat_class']} on {book_date}, "
                 f"seats {[s['seat_number'] for s in seats]}")

        booker = Booker(self.api, self.log, self.ask_otp, self.stop_ev, on_trip=self._on_resolved_trip)

        def finish(res):
            self._set_running(False)
            if not res:
                self.status.set("Stopped")
                return
            url = res.get("url")
            self.log(f"Seats booked: {res['seats']}")
            if url:
                PAYMENT_FILE.write_text(url + "\n", encoding="utf-8")
                self.clipboard_clear()
                self.clipboard_append(url)
                self.log(f"PAYMENT URL (also copied, saved to {PAYMENT_FILE}): {url}")
                self.status.set("Booked - complete payment in the opened page")
                webbrowser.open(url)
            else:
                self.log("No payment URL detected - server response:\n" + json.dumps(res["result"], indent=2)[:2000])
                self.status.set("Confirmed, but no payment URL detected - see log")

        self.run_bg(lambda: booker.run(cfg), finish, on_error=lambda: self._set_running(False))

    def stop_auto(self):
        self.stop_ev.set()
        self.log("Stop requested...")
        self.status.set("Stopping...")


if __name__ == "__main__":
    App().mainloop()