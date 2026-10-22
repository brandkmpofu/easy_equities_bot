"""
Cron entrypoint: log in, split free cash per allocations.csv, place buy
orders and email a summary.

    python src/main.py                    # place orders
    python src/main.py --dry-run          # log in and show what would be bought
    python src/main.py --inspect EQU.ZA.EASYBF   # dump an instrument's buy form, no trade

Exit code is 0 on success, 1 if anything failed (so cron/monitoring notices).
"""
import argparse
import fcntl
import logging
import os
import sys
import time
import traceback
from datetime import date
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

from functions import buy_etf, check_order, get_buy_form, get_free_cash, load_allocations, login, send_email

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SRC_DIR)
LOG_DIR = os.path.join(ROOT_DIR, "logs")
ORDER_LOG_DIR = os.path.join(LOG_DIR, "orders")
ORDER_LOG_RETENTION_DAYS = 30

log = logging.getLogger("ee_bot")


def setup_logging():
    os.makedirs(ORDER_LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(os.path.join(LOG_DIR, "bot.log"), maxBytes=1_000_000, backupCount=5)
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    log.addHandler(stream_handler)
    log.setLevel(logging.INFO)


def load_config():
    # cron runs from $HOME, so resolve the env file relative to this script.
    candidates = [os.path.join(d, n) for d in (ROOT_DIR, SRC_DIR) for n in (".env", "_env")]
    env_file = next((p for p in candidates if os.path.exists(p)), None)
    if env_file:
        load_dotenv(env_file, override=True)

    config = {
        "username": os.getenv("EE_USERNAME"),
        "password": os.getenv("EE_PASSWORD"),
        "account_id": os.getenv("ZAR_TRADING_ACCOUNT_ID"),
        "min_trade": float(os.getenv("MIN_TRADE") or 1),
        "email_sender": os.getenv("EMAIL_SENDER"),
        "email_password": os.getenv("EMAIL_PASSWORD"),
        "email_receiver": os.getenv("EMAIL_RECEIVER"),
    }
    missing = [k for k in ("EE_USERNAME", "EE_PASSWORD", "ZAR_TRADING_ACCOUNT_ID") if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing required settings in .env: {', '.join(missing)}")
    return config


def notify(config, lines, today, subject=None):
    if not (config["email_sender"] and config["email_password"] and config["email_receiver"]):
        log.info("Email not configured (EMAIL_* vars blank) — skipping email notification.")
        return
    try:
        send_email(
            lines, today, config["email_sender"], config["email_receiver"], config["email_password"], subject=subject
        )
        log.info("Email sent.")
    except Exception:
        log.exception("Failed to send email")


def save_order_response(today, contract_code, response):
    """Keep the raw order response so a rejected order can be diagnosed later."""
    path = os.path.join(ORDER_LOG_DIR, f"{today}_{contract_code}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<!-- HTTP {response.status_code} {response.url} -->\n{response.text}")
    return path


def prune_order_logs():
    cutoff = time.time() - ORDER_LOG_RETENTION_DAYS * 86400
    for name in os.listdir(ORDER_LOG_DIR):
        path = os.path.join(ORDER_LOG_DIR, name)
        if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
            os.remove(path)


def inspect(client, config, contract_code):
    client.accounts._switch_account(config["account_id"])
    page, fields, action = get_buy_form(client, contract_code)
    print(f"GET {page.url} -> HTTP {page.status_code}")
    print(f"form action: {action}")
    for name, value in fields.items():
        if name in ("__RequestVerificationToken", "Token", "AntiTamperingToken"):
            value = f"<{len(value)} chars>"
        print(f"  {name} = {value!r}")
    path = os.path.join(ORDER_LOG_DIR, f"inspect_{contract_code}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page.text)
    print(f"full buy page saved to {path}")


def run(config, today, dry_run):
    client = login(config["username"], config["password"])
    log.info("Logged in.")

    free_cash = get_free_cash(client, config["account_id"])
    log.info("Free cash: R%.2f", free_cash)
    if free_cash < config["min_trade"]:
        log.info("Free cash below MIN_TRADE (R%.2f) — nothing to buy.", config["min_trade"])
        return [], False

    allocations = load_allocations(free_cash)

    results = [f"Free cash: R{free_cash:.2f}", ""]
    failed = False
    for ticker, data in allocations.items():
        amount = data["amount"]
        contract_code = data["contract_code"]
        ticker_name = data["ticker_name"]

        if amount < config["min_trade"]:
            results.append(f"SKIP {ticker_name}: Amount too small (R{amount:.2f})")
            continue
        if dry_run:
            results.append(f"DRY-RUN {ticker_name} ({contract_code}): would buy R{amount:.2f}")
            continue

        try:
            response = buy_etf(client, config["account_id"], contract_code, amount)
            saved = save_order_response(today, contract_code, response)
            status, detail = check_order(response)
        except Exception as e:
            log.exception("Buy order for %s raised", ticker_name)
            status, detail, saved = "FAILED", str(e), None

        if status == "OK":
            results.append(f"BUY {ticker_name}: R{amount:.2f} ✅")
        elif status == "UNCONFIRMED":
            results.append(f"CHECK {ticker_name}: R{amount:.2f} ⚠️ {detail} (response: {saved})")
            failed = True
        else:
            results.append(f"FAILED {ticker_name}: R{amount:.2f} ❌ {detail}")
            failed = True

    for line in results:
        log.info(line)
    return results, failed


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="log in and compute orders without placing them")
    parser.add_argument("--inspect", metavar="CONTRACT_CODE", help="dump an instrument's buy form and exit")
    args = parser.parse_args()

    setup_logging()

    # Prevent overlapping runs (e.g. a slow run still going when cron fires again).
    lock_file = open(os.path.join(LOG_DIR, ".lock"), "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("Another run is still in progress — exiting.")
        return 1

    config = load_config()
    today = date.today().strftime("%Y-%m-%d")

    if args.inspect:
        inspect(login(config["username"], config["password"]), config, args.inspect)
        return 0

    try:
        results, failed = run(config, today, args.dry_run)
    except Exception:
        log.exception("Run failed")
        notify(config, ["The Easy Equities bot run failed:", "", traceback.format_exc()], today,
               subject=f"Easy Equities bot FAILED on {today}")
        return 1
    finally:
        prune_order_logs()

    if results and not args.dry_run:
        subject = f"Easy Equities Buy Orders for {today}" + (" — ACTION NEEDED" if failed else "")
        notify(config, results, today, subject=subject)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
