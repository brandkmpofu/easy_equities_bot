import os
import re
import smtplib
from decimal import Decimal, ROUND_DOWN
from email.mime.text import MIMEText
from urllib.parse import urljoin

import openpyxl
from bs4 import BeautifulSoup
from easy_equities_client.clients import EasyEquitiesClient
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://platform.easyequities.io"
DEFAULT_ALLOCATIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "allocations.xlsx")

# A real browser User-Agent is required — the default python-requests UA
# is now rejected by CloudFront with a 403 before the request even reaches
# the application.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Seconds before any single HTTP request is abandoned, so a hung connection
# can never leave the cron job running forever.
REQUEST_TIMEOUT = 30


class _TimeoutAdapter(HTTPAdapter):
    """HTTPAdapter that applies a default timeout to every request."""

    def __init__(self, *args, timeout=REQUEST_TIMEOUT, **kwargs):
        self.timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.timeout
        return super().send(request, **kwargs)


def _configure_session(session):
    """
    Add browser headers, timeouts and retries to a requests session.

    Only GETs are retried on read errors / 5xx responses. POSTs (logins and
    buy orders) are never re-sent once they reach the server, so a flaky
    connection can't place the same order twice.
    """
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        status=3,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = _TimeoutAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    )


def _form_fields(form):
    """
    Collect a form's values into a POST payload the way a browser would:
    unchecked checkboxes/radios and submit buttons are left out, and
    <select>/<textarea> values are included.
    """
    data = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        kind = (inp.get("type") or "text").lower()
        if kind in ("submit", "button", "image", "reset", "file"):
            continue
        if kind in ("checkbox", "radio") and not inp.has_attr("checked"):
            continue
        # ASP.NET renders checkboxes as <input checkbox value=true> followed
        # by <input hidden value=false>; keep the first (checked) value.
        data.setdefault(name, inp.get("value", ""))
    for sel in form.find_all("select"):
        if sel.get("name"):
            opt = sel.find("option", selected=True) or sel.find("option")
            data.setdefault(sel["name"], opt.get("value", opt.get_text(strip=True)) if opt else "")
    for ta in form.find_all("textarea"):
        if ta.get("name"):
            data.setdefault(ta["name"], ta.get_text())
    return data


def login(username, password, base_url=BASE_URL):
    """
    Log in to EasyEquities and return an authenticated EasyEquitiesClient.

    EasyEquities migrated its sign-in from a simple form POST to
    /Account/SignIn to a full OpenID Connect / OAuth2 Authorization-Code-
    with-PKCE flow hosted at identity.openeasy.io. Because of that change the
    library's built-in ``client.login()`` no longer works, so this helper
    drives the new browser flow with requests + BeautifulSoup and populates
    the client's session with the resulting authentication cookies.

    Args:
        username (str): EasyEquities username.
        password (str): EasyEquities password.
        base_url (str): Platform base URL.

    Returns:
        EasyEquitiesClient: A logged-in client ready for accounts/instruments calls.

    Raises:
        Exception: if the login flow fails (bad credentials, blocked, etc.).
    """
    client = EasyEquitiesClient(base_url=base_url)
    session = client.session
    _configure_session(session)

    # 1. Follow the redirect chain to the identity provider's login form.
    #    platform.easyequities.io -> /Account/LogInViaIdp -> identity.openeasy.io
    resp = session.get(urljoin(base_url, "/Account/SignIn"), allow_redirects=True)
    resp.raise_for_status()
    page = BeautifulSoup(resp.text, "html.parser")
    login_form = page.find("form", {"id": "loginForm"}) or page.find("form")
    if login_form is None:
        raise Exception("Could not find the EasyEquities login form (login page changed).")

    # 2. Submit credentials together with the hidden anti-forgery / OAuth fields
    #    (ReturnUrl, ClientIdForProperties, __RequestVerificationToken, ...).
    data = _form_fields(login_form)
    data.update({"Username": username, "Password": password, "button": "login"})
    action = urljoin(resp.url, login_form.get("action") or resp.url)
    resp = session.post(action, data=data, allow_redirects=True)
    resp.raise_for_status()

    # If the identity provider returns the login form again, the credentials
    # were rejected.
    page = BeautifulSoup(resp.text, "html.parser")
    if page.find("input", {"name": "Password"}) is not None:
        errors = page.find(class_="validation-summary-errors")
        message = errors.get_text(strip=True) if errors else "Invalid username or password."
        raise Exception(f"EasyEquities login failed: {message}")

    # 3. On success the IdP responds with a self-submitting `form_post` page that
    #    carries the OAuth authorization code back to the platform. requests will
    #    not auto-submit it (it is driven by JavaScript in a browser), so post it
    #    ourselves. This may chain more than once.
    for _ in range(3):
        callback_form = page.find("form")
        if callback_form is None or callback_form.find("input", {"name": "code"}) is None:
            break
        action = urljoin(resp.url, callback_form.get("action") or resp.url)
        resp = session.post(action, data=_form_fields(callback_form), allow_redirects=True)
        resp.raise_for_status()
        page = BeautifulSoup(resp.text, "html.parser")

    # 4. Confirm we ended up with the platform session cookie.
    if "EasyEquities" not in [cookie.name for cookie in session.cookies]:
        raise Exception(
            "Login flow completed but no EasyEquities session cookie was set — "
            "the login flow may have changed again."
        )

    return client


def get_free_cash(client, account_id):
    """
    Retrieve the available cash balance for an account.

    Args:
        client (EasyEquitiesClient): Authenticated EasyEquities client instance.
        account_id (str): The account identifier.

    Returns:
        float: The amount of free cash available to invest.
    """
    items = client.accounts.valuations(account_id)["FundSummaryItems"]
    value = next(
        (item["Value"] for item in items if item["Label"] == "Your Funds to Invest"), None
    )
    if value is None:
        raise Exception(
            "'Your Funds to Invest' not found in account valuations. "
            f"Labels returned: {[item['Label'] for item in items]}"
        )
    # e.g. "R1 234,56" / "R1,234.56" -> 1234.56
    cleaned = re.sub(r"[^\d.,-]", "", value)
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    return float(cleaned.replace(",", ""))


def list_accounts(client):
    """
    List all trading accounts on the profile with their IDs.

    Handy for discovering the account IDs to put in your `.env` file.

    Args:
        client (EasyEquitiesClient): Authenticated EasyEquities client instance.

    Returns:
        list[dict]: [{"id", "name", "trading_currency_id"}, ...]
    """
    return [
        {"id": a.id, "name": a.name, "trading_currency_id": a.trading_currency_id}
        for a in client.accounts.list()
    ]


def get_holdings_summary(client, account_id):
    """
    Return the current holdings (stocks/ETFs) for an account.

    Args:
        client (EasyEquitiesClient): Authenticated EasyEquities client instance.
        account_id (str): The account identifier.

    Returns:
        list[dict]: [{"name", "contract_code", "current_value", "current_price", "purchase_value"}, ...]
    """
    holdings = client.accounts.holdings(account_id)
    return [
        {
            "name": h.get("name"),
            "contract_code": h.get("contract_code"),
            "current_value": h.get("current_value"),
            "current_price": h.get("current_price"),
            "purchase_value": h.get("purchase_value"),
        }
        for h in holdings
    ]


def load_allocations(free_cash, file_path=DEFAULT_ALLOCATIONS, sheet_name=0):
    """
    Split free cash across the tickers in the allocations spreadsheet.

    Amounts are rounded *down* to the cent so their total can never exceed
    the available cash (rounding to nearest could make the last order fail
    for insufficient funds).

    Returns:
        dict: {ticker: {"amount", "contract_code", "ticker_name"}}
    """
    try:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        ws = wb.worksheets[sheet_name] if isinstance(sheet_name, int) else wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception as e:
        raise ValueError(f"Failed to read Excel file {file_path}: {e}")

    if not rows:
        raise ValueError(f"{file_path} is empty")

    header = [str(c).strip().lower() if c is not None else "" for c in rows[0]]
    required_cols = {"ticker", "proportion", "contract_code", "name"}
    missing = required_cols - set(header)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")
    col = {name: header.index(name) for name in required_cols}

    def _text(value):
        return "" if value is None else str(value).strip().strip("'\"").strip()

    cash = Decimal(str(free_cash))
    allocation_amounts = {}
    total = Decimal(0)
    for line_no, row in enumerate(rows[1:], start=2):
        if row is None or all(c is None or str(c).strip() == "" for c in row):
            continue  # skip blank rows
        ticker = _text(row[col["ticker"]])
        if not ticker:
            raise ValueError(f"Empty ticker found on row {line_no}")
        if ticker in allocation_amounts:
            raise ValueError(f"Duplicate ticker {ticker} on row {line_no}")
        try:
            # Handle comma/dot decimals safely
            proportion = Decimal(str(row[col["proportion"]]).strip().replace(",", "."))
        except Exception:
            raise ValueError(f"Invalid proportion for {ticker} on row {line_no}")
        if proportion < 0:
            raise ValueError("Proportions cannot be negative")
        total += proportion

        allocation_amounts[ticker] = {
            "amount": float((proportion * cash).quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
            "contract_code": _text(row[col["contract_code"]]),
            "ticker_name": _text(row[col["name"]]),
        }

    if abs(total - 1) > Decimal("0.000001"):
        raise ValueError(f"Total allocation proportions must sum to 1. Found: {total}")

    return allocation_amounts


def get_buy_form(client, contract_code, trading_currency_id=2):
    """
    Fetch the buy page for an instrument and return (response, form_fields, form_action).

    Useful on its own for diagnosing an instrument whose orders don't go
    through: it shows every field the platform expects without trading.
    """
    page = client.session.get(
        f"{client.base_url}/ValueAllocation/Buy",
        params={"contractCode": contract_code, "tradingCurrencyId": trading_currency_id},
    )
    soup = BeautifulSoup(page.text, "html.parser")
    token = soup.find("input", {"name": "AntiTamperingToken"})
    form = token.find_parent("form") if token else None
    if form is None:
        return page, {}, None
    return page, _form_fields(form), form.get("action")


def buy_etf(client, account_id, contract_code, amount, trading_currency_id=2):
    """
    Place a market buy order for a specified ETF.

    The payload starts from every field on the instrument's own buy form, so
    instrument-specific hidden fields (e.g. the unit-trust flag on actively
    managed ETFs) are sent with the values the platform rendered, rather than
    hardcoded values that only suit plain ETFs.

    Args:
        client (EasyEquitiesClient): Authenticated EasyEquities client instance.
        account_id (str): The account identifier (used to select the right account).
        contract_code (str): The instrument contract code of the ETF.
        amount (float): Amount in currency (ZAR by default) to invest.
        trading_currency_id (int): Trading currency id (2 = ZAR, 3 = TFSA, 10 = USD, ...).

    Returns:
        requests.Response: Response from the order placement API. Pass it to
        ``check_order`` to find out whether the order was accepted.
    """
    # Make sure the intended account is the currently-selected one before we
    # read the buy page (the buy page is scoped to the active account).
    client.accounts._switch_account(account_id)

    page, fields, _ = get_buy_form(client, contract_code, trading_currency_id)

    for name in ("__RequestVerificationToken", "Token", "AntiTamperingToken", "TradeInstrument.ISINCode"):
        if not fields.get(name):
            raise Exception(
                f"Could not find '{name}' on the buy page for {contract_code} "
                f"(status {page.status_code}). The buy page may have changed, "
                f"or the instrument/currency is not tradable on this account."
            )

    payload = dict(fields)
    payload.setdefault("TradeInstrument.IsInstrumentUnitTrust", "False")
    payload.setdefault("IsLimitOrderAvailable", "True")
    payload.update(
        {
            "TradeType": "Buy",
            "TradeInstrument.ContractCode": contract_code,
            "TradeValue": f"{amount:.2f}",
            "IsPlacingLimitOrder": "False",
            "TradeBreakdown.NetAmountDue": f"{amount:.2f}",
            "HasSufficientFundsToTrade": "True",
        }
    )

    return client.session.post(f"{client.base_url}/ValueAllocation/BuyInstruction", data=payload)


def check_order(response):
    """
    Inspect a BuyInstruction response.

    A 200 status alone does not mean the order was placed: the platform
    re-renders the page with validation messages when it rejects an order.

    Returns:
        tuple[str, str]: (status, detail) where status is "OK", "FAILED" or
        "UNCONFIRMED" (200 but the trade form came back, so check the platform).
    """
    if response.status_code != 200:
        return "FAILED", f"HTTP {response.status_code}: {response.text[:300]}"

    content_type = response.headers.get("Content-Type", "")
    if "json" in content_type:
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            for key in ("Success", "IsSuccess", "success", "isSuccess"):
                if key in body and not body[key]:
                    return "FAILED", str(body.get("Message") or body.get("message") or body)[:300]
        return "OK", ""

    soup = BeautifulSoup(response.text, "html.parser")
    errors = [
        el.get_text(" ", strip=True)
        for el in soup.select(
            ".validation-summary-errors, .field-validation-error, .alert-danger, .error-message"
        )
    ]
    errors = [e for e in errors if e]
    if errors:
        return "FAILED", "; ".join(dict.fromkeys(errors))[:300]
    if soup.find("input", {"name": "AntiTamperingToken"}) is not None:
        return "UNCONFIRMED", "platform returned the trade form again instead of a confirmation"
    return "OK", ""


def send_email(body, today, EMAIL_SENDER, EMAIL_RECEIVER, EMAIL_PASSWORD, subject=None):
    """
    Send an email with trade results.

    Args:
        body (list[str]): Lines of the email body.
        today (str): Date string to include in the subject line.
        EMAIL_SENDER (str): Sender email address.
        EMAIL_RECEIVER (str): Recipient email address.
        EMAIL_PASSWORD (str): Sender email app password.
        subject (str): Optional subject override.

    Returns:
        None
    """
    msg = MIMEText("\n".join(body))
    msg["Subject"] = subject or f"Easy Equities Buy Orders for {today}"
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=REQUEST_TIMEOUT) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
