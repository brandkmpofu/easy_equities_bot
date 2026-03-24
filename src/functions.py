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


def buy_etf(client, account_id, contract_code, amount):
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
        "contractCode": f"{contract_code}",
        "tradingCurrencyId": 2}
    )

    soup = BeautifulSoup(page.text, "html.parser")
    csrf = soup.find("input", {"name": "__RequestVerificationToken"})["value"]
    token = soup.find("input", {"name": "Token"})["value"]
    anti = soup.find("input", {"name": "AntiTamperingToken"})["value"]
    isin = soup.find("input", {"name": "TradeInstrument.ISINCode"})["value"]

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