# 🚆 BD Railway Ticket Buyer

Getting a Bangladesh Railway ticket is like hunting a golden deer. Tickets are released at a fixed time and they are gone in seconds.

I built this tool to make it easier for normal people. It is **free**, **open source**, and anyone can use it.

> **Your data stays with you.** There is no database and no server of mine. Nothing is sent to me or to any third party. Read the code and check it yourself.

Enjoy your next journey! 🌴

---

## ✨ Features

- 🎯 **Auto-buy at release time.** Set a start time (for example `07:59:50`) and the tool polls the site until your train goes live, then reserves your seats immediately.
- 💺 **Pick your seats visually** from a seat map before you start.
- 🌐 **Uses your real Chrome / Edge.** You log in yourself in a normal browser window. Nothing is spoofed.
- 🔄 **Live token handling.** The login token is read from the browser on every request, so it never goes stale.
- 🔐 **OTP support.** The tool asks you for the OTP when it arrives, then confirms.
- 💳 **Payment link.** At the end you get the bKash payment URL, saved to a file and opened for you.
- 👨‍👩‍👧‍👦 **Multiple accounts.** Run one copy per account, each with its own browser, so a big family can book together.

---

## 📦 Requirements

- Python 3.9 or newer (with `tkinter`, included in most Python installs)
- Google Chrome or Microsoft Edge
- Python packages:

```bash
pip install playwright requests
```

---

## 🚀 Quick start

```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
pip install playwright requests
python index.py
```

1. A Chrome/Edge window opens on the railway login page. **Log in there** as you normally would.
2. In the app, choose **from, to, date and seat class**.
3. Select the seats you want and fill in the passenger details.
4. Set a **start time** (optional) and press **Start auto-buy**.
5. When the OTP arrives on your phone, type it into the app.
6. Pay using the payment link the tool gives you.

---

## 👨‍👩‍👧‍👦 Using two (or more) accounts

Each account has its own browser window, so you can run the tool once per account.

Open two terminals:

```bash
# Terminal 1
python index.py 1

# Terminal 2
python index.py 2
```

Each copy gets its **own** browser profile, debugging port and saved files, so the logins never clash:

| | Account 1 | Account 2 |
|---|---|---|
| Debug port | 9222 | 9223 |
| Browser profile | `browser_profile/` | `browser_profile_2/` |
| Passenger info | `passenger_info.json` | `passenger_info_2.json` |
| Saved trip | `trip_data.json` | `trip_data_2.json` |
| Payment link | `payment_url.txt` | `payment_url_2.txt` |

Need a third? Run `python index.py 3` (port 9224).

**Tips**

- Choose different seats in each window so the two accounts do not fight over the same seat.
- Use the same start time in both windows.
- Each account receives its own OTP, so keep both phones nearby.

---

## 🧠 How it works

1. Optionally waits until your start time.
2. Polls `search-trips-v2` until your train appears for the target date.
3. Takes the new `trip_id`, `trip_route_id` and boarding point from the result.
4. Loads the seat layout and maps your chosen seat numbers to that trip's ticket IDs.
5. Reserves the seats (the `x-action-token` is rotated from each response).
6. Submits passenger details.
7. Requests an OTP, verifies it, then confirms the booking and returns the payment URL.

All API calls run **inside your own logged-in browser page**, the same way the website does it.

---

## 📁 Project structure

| File | Purpose |
|---|---|
| `index.py` | Desktop app (Tkinter UI) |
| `booker.py` | The booking engine, with no UI code |
| `browser_session.py` | Controls your real Chrome/Edge for login, tokens and API calls |

---

## 🔒 Privacy and security

- ✅ No database, no backend, no analytics, no tracking.
- ✅ Your login happens in your own browser. The tool never asks for or stores your password.
- ✅ Passenger details are saved **only on your own computer**, in `passenger_info*.json`. Delete the file any time.
- ✅ The only network traffic is to the official railway site and its API, made from your browser.
- ℹ️ The app makes one small request to `api.ipify.org` to detect your public IP address, because the booking request includes it. You can remove it in `detect_ip()` in `index.py` and type your IP by hand instead.
- 🧹 Browser profiles (`browser_profile*/`) contain your saved logins. **Never commit or share them.** Add them to `.gitignore`:

```gitignore
browser_profile*/
passenger_info*.json
trip_data*.json
payment_url*.txt
```

---

## ⚠️ Disclaimer

- This project is **not affiliated with** Bangladesh Railway or Shohoz.
- Use it only to buy tickets for **yourself and your own family or friends**. Please do not use it to hoard or resell tickets.
- It depends on the website's current behaviour. If the site changes, the tool may stop working until it is updated.
- You are responsible for following the railway's terms of use. The software is provided **as is**, with no guarantee that you will get a ticket.

---

## 🤝 Contributing

Pull requests and issues are welcome. If something breaks after a site update, open an issue with the log output (remove your personal info first).

## 📄 License

No license haha

---

Made with ❤️ so everyone can go home for Eid—or just enjoy a cheap trip without having to pay double the price on the black market.
Fuck those who scalp the tickets. 🖕
