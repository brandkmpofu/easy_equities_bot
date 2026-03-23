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
    if item['Label'] == 'Your Funds to Invest').replace('R', '').replace(',', '').strip())

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

    import csv
    allocations = {}
    total = 0.0

    with open("allocations.csv", newline='', encoding='utf-8-sig') as csvfile:
        reader = csv.DictReader(csvfile, delimiter=';')
        for row in reader:
            ticker = row["ticker"].strip()
            proportion = float(row["proportion"].replace(',', '.'))
            contract_code = row["contract_code"].strip()
            ticker_name  = row["name"].strip()

            allocations[ticker] = proportion
            allocations[ticker] = {
            "contract_code": contract_code,
            "ticker_name": ticker_name,
            "proportion": proportion
            }
            total += proportion

    if not abs(total - 1.0) < 1e-6:
        raise ValueError(f"Total allocation proportions must sum to 1. Found: {total}")

    allocation_amounts = {
    ticker: {
        "amount": round(details["proportion"] * free_cash, 2),
        "contract_code": details["contract_code"],
        "ticker_name": details["ticker_name"]
    }
       for ticker, details in allocations.items()
    }
    return allocation_amounts


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
    
    from bs4 import BeautifulSoup
    
    page = client.session.get(
    "https://platform.easyequities.io/ValueAllocation/Buy",
    params={
        "contractCode": "EQU.ZA.STX40",
        "tradingCurrencyId": 2}
    )

    soup = BeautifulSoup(page.text, "html.parser")
    csrf = soup.find("input", {"name": "__RequestVerificationToken"})["value"]
    token = soup.find("input", {"name": "Token"})["value"]
    anti = soup.find("input", {"name": "AntiTamperingToken"})["value"]
    isin = soup.find("input", {"name": "TradeInstrument.ISINCode"})["value"]
    contract = soup.find("input", {"name": "TradeInstrument.ContractCode"})["value"]

    payload = {
    "__RequestVerificationToken": csrf,
    "Token": token,
    "TradeType": "Buy",

    "TradeInstrument.ISINCode": isin,
    "TradeInstrument.ContractCode": contract,
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