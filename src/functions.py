def get_free_cash(client, account_id):
    """
    Retrieve the available cash balance for an account.

    Args:
        client (EasyEquitiesClient): Authenticated EasyEquities client instance.
        account_id (str): The account identifier.

    Returns:
        float: The amount of free cash available to invest.
    """
    balance = client.accounts.get_cash_balance(account_id)
    free_cash = balance["free"]
    return free_cash


def load_allocations(free_cash):
    """
    Load allocation proportions from CSV and convert to monetary amounts.

    Args:
        free_cash (float): Total available cash to allocate across tickers.

    Returns:
        dict: Dictionary mapping ticker symbols (str) to allocation amounts (float).

    Raises:
        ValueError: If total allocation proportions do not sum to 1.0.
    """
    allocations = {}
    total = 0.0

    with open(ALLOC_FILE, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            ticker = row["ticker"].strip()
            proportion = float(row["proportion"])

            allocations[ticker] = proportion
            total += proportion

    if not abs(total - 1.0) < 1e-6:
        raise ValueError(f"Total allocation proportions must sum to 1. Found: {total}")

    allocation_amounts = {
        ticker: round(proportion * free_cash, 2)
        for ticker, proportion in allocations.items()
    }

    return allocation_amounts


def get_instrument_id(client, ticker):
    """
    Retrieve the instrument ID for a given ticker symbol.

    Args:
        client (EasyEquitiesClient): Authenticated EasyEquities client instance.
        ticker (str): Ticker symbol to search for.

    Returns:
        str: The instrument ID corresponding to the ticker.

    Raises:
        Exception: If no matching instrument is found.
    """
    results = client.instruments.search(ticker)
    
    for r in results:
        if ticker in r["symbol"]:
            instrument_id = r["instrument_id"]
            return instrument_id
    
    raise Exception(f"{ticker} not found")


def buy_etf(client, account_id, instrument_id, amount):
    """
    Place a market buy order for a specified ETF.

    Args:
        client (EasyEquitiesClient): Authenticated EasyEquities client instance.
        account_id (str): The account identifier.
        instrument_id (str): The instrument ID of the ETF.
        amount (float): Amount in currency (ZAR) to invest.

    Returns:
        dict: Response from the order placement API.
    """
    order = client.orders.place_order(
        account_id=account_id,
        instrument_id=instrument_id,
        order_type="MARKET",
        side="BUY",
        amount=amount
    )
    return order


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
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)