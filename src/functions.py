def login(username, password, base_url="https://platform.easyequities.io"):
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
    from urllib.parse import urljoin
    from bs4 import BeautifulSoup
    from easy_equities_client.clients import EasyEquitiesClient

    # A real browser User-Agent is required — the default python-requests UA
    # is now rejected by CloudFront with a 403 before the request even reaches
    # the application.
    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    def _form_fields(form):
        """Collect a form's hidden/named input values into a POST payload."""
        return {
            inp.get("name"): inp.get("value", "")
            for inp in form.find_all("input")
            if inp.get("name")
        }

    client = EasyEquitiesClient(base_url=base_url)
    session = client.session
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
    free_cash = float(next(item['Value'] for item in client.accounts.valuations(account_id)['FundSummaryItems']
    if item['Label'] == 'Your Funds to Invest').replace('R', '').replace(',', '').replace(' ', '').strip())

    return free_cash


def list_accounts(client):
    """
    List all trading accounts on the profile with their IDs.

    Handy for discovering the account IDs to put in your `_env` file.

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


def load_allocations(free_cash, file_path="allocations.xlsx", sheet_name=0):
    import pandas as pd
    import openpyxl

    # Read Excel
    try:
        df = pd.read_excel(
            file_path,
            sheet_name=sheet_name,
            engine="openpyxl"   # standard for .xlsx
        )
    except Exception as e:
        raise ValueError(f"Failed to read Excel file: {e}")

    # Normalize column names
    df.columns = df.columns.str.strip().str.lower()

    required_cols = {"ticker", "proportion", "contract_code", "name"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    # Clean and validate data
    try:
        df["ticker"] = df["ticker"].astype(str).str.strip()
        df["name"] = df["name"].astype(str).str.strip()
        df["contract_code"] = (df["contract_code"].astype(str).str.strip().str.strip("'\""))

        # Handle comma/dot decimals safely
        df["proportion"] = (
            df["proportion"]
            .astype(str)
            .str.replace(",", ".", regex=False)
            .astype(float)
        )

    except Exception as e:
        raise ValueError(f"Data cleaning error: {e}")

    # Validate data
    if (df["proportion"] < 0).any():
        raise ValueError("Proportions cannot be negative")

    if df["ticker"].eq("").any():
        raise ValueError("Empty ticker found")

    total = df["proportion"].sum()
    if not abs(total - 1.0) < 1e-6:
        raise ValueError(f"Total allocation proportions must sum to 1. Found: {total}")

    # Compute allocation amounts
    df["amount"] = (df["proportion"] * free_cash).round(2)

    # Convert to required output format (same as before)
    allocation_amounts = {
        row["ticker"]: {
            "amount": row["amount"],
            "contract_code": row["contract_code"],
            "ticker_name": row["name"]
        }
        for _, row in df.iterrows()
    }

    return allocation_amounts


def buy_etf(client, account_id, contract_code, amount, trading_currency_id=2):
    """
    Place a market buy order for a specified ETF.

    Args:
        client (EasyEquitiesClient): Authenticated EasyEquities client instance.
        account_id (str): The account identifier (used to select the right account).
        contract_code (str): The instrument contract code of the ETF.
        amount (float): Amount in currency (ZAR by default) to invest.
        trading_currency_id (int): Trading currency id (2 = ZAR, 3 = TFSA, 10 = USD, ...).

    Returns:
        requests.Response: Response from the order placement API.
    """
    from bs4 import BeautifulSoup

    # Make sure the intended account is the currently-selected one before we
    # read the buy page (the buy page is scoped to the active account).
    client.accounts._switch_account(account_id)

    page = client.session.get(
        "https://platform.easyequities.io/ValueAllocation/Buy"
        f"?contractCode={contract_code}&tradingCurrencyId={trading_currency_id}"
    )

    soup = BeautifulSoup(page.text, "html.parser")

    def _field(name):
        el = soup.find("input", {"name": name})
        if el is None or el.get("value") is None:
            raise Exception(
                f"Could not find '{name}' on the buy page for {contract_code} "
                f"(status {page.status_code}). The buy page may have changed, "
                f"or the instrument/currency is not tradable on this account."
            )
        return el["value"]

    csrf = _field("__RequestVerificationToken")
    token = _field("Token")
    anti = _field("AntiTamperingToken")
    isin = _field("TradeInstrument.ISINCode")

    payload = {
    "__RequestVerificationToken": csrf,
    "Token": token,
    "TradeType": "Buy",

    "TradeInstrument.ISINCode": isin,
    "TradeInstrument.ContractCode": contract_code,
    "TradeInstrument.IsInstrumentUnitTrust": "False",

    "AntiTamperingToken": anti,

    "TradeValue": str(amount),

    "IsLimitOrderAvailable": "True",
    "IsPlacingLimitOrder": "False",

    "TradeBreakdown.NetAmountDue": str(amount),
    "HasSufficientFundsToTrade": "True"
    }

    response = client.session.post(
    "https://platform.easyequities.io/ValueAllocation/BuyInstruction",
    data=payload
    )
    
    return response


def send_email(body, today, EMAIL_SENDER, EMAIL_RECEIVER, EMAIL_PASSWORD):
    """
    Send an email with trade results.

    Args:
        body (str): Email body content.
        today (str): Date string to include in the subject line.
        EMAIL_SENDER (str): Sender email address.
        EMAIL_RECEIVER (str): Recipient email address.
        EMAIL_PASSWORD (str): Sender email app password.

    Returns:
        None
    """
    import smtplib
    from email.mime.text import MIMEText

    subject = f'Easy Equities Buy Orders for {today}'
    body = "\n".join(body)
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)