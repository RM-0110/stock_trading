# rsi_trading_bot.py
import subprocess
import sys
import json

# Load config
with open("config.json") as f:
    config = json.load(f)

# # Install dependencies
# for package in config.get("dependencies", []):
#     subprocess.run([sys.executable, "-m", "pip", "install", package, "--quiet"])

import yfinance as yf
import pandas_ta as ta
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import pytz
import os
from typing import Optional

# Config values
etfs = config["etfs"]
period = config["period"]
interval = config["interval"]
rsi_length = config["rsi_length"]
auth_json = os.getenv('GOOGLE_AUTH_JSON')
sheet_url = config["sheet_url"]

# Fetch RSI
def fetch_rsi(etf_name: str, ticker_symbol: str) -> Optional[pd.DataFrame]:
    data = yf.Ticker(ticker_symbol).history(period=period, interval=interval)
    if data.empty:
        return None
    data["RSI"] = ta.rsi(data["Close"], length=rsi_length)
    latest = data[["Close", "RSI"]].dropna().tail(1).copy()
    latest["ETF"] = etf_name
    return latest

# Load RSI data
rsi_frames = []
for name, symbol in etfs.items():
    rsi_df = fetch_rsi(name, symbol)
    if rsi_df is not None and not rsi_df.empty:
        rsi_frames.append(rsi_df)

results = pd.concat(rsi_frames).reset_index()


# Prepare sheet auth
def get_gsheet_client(auth_json):
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    credentials = Credentials.from_service_account_info(auth_json, scopes=scopes)
    return gspread.authorize(credentials)

client = get_gsheet_client(auth_json)
sheet = client.open_by_url(sheet_url)
trades_ws = sheet.worksheet("Trades")
holdings_ws = sheet.worksheet("Holdings")

# Ensure headers exist
if not trades_ws.get_all_values():
    trades_ws.append_row(["ETF", "Timestamp", "Price", "RSI", "Type"])
if not holdings_ws.get_all_values():
    holdings_ws.append_row(["ETF", "Units", "Average Price"])

# Mock trade logic
def mock_trade(results_df: pd.DataFrame, trades_ws, holdings_ws):
    try:
        holdings_data = holdings_ws.get_all_records()
        holdings = {
            row["ETF"]: {
                "Units": int(row.get("Units", 0)),
                "Average Price": float(row.get("Average Price", 0.0))
            } for row in holdings_data
        }
    except Exception as e:
        print(f"Error loading holdings: {e}")
        holdings = {etf: {"Units": 0, "Average Price": 0.0} for etf in etfs}

    for _, row in results_df.iterrows():
        etf = row["ETF"]
        rsi = row["RSI"]
        price = round(float(row["Close"]), 2)
        now = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")

        # BUY
        if rsi < 30:
            cur = holdings.get(etf, {"Units": 0, "Average Price": 0.0})
            new_units = cur["Units"] + 1
            new_avg = round(((cur["Average Price"] * cur["Units"]) + price) / new_units, 2)
            holdings[etf] = {"Units": new_units, "Average Price": new_avg}
            trades_ws.append_row([etf, now, price, rsi, "BUY"])

        # SELL
        elif rsi > 70 and holdings.get(etf, {}).get("Units", 0) > 0:
            trades_ws.append_row([etf, now, price, rsi, "SELL"])
            holdings[etf]["Units"] -= 1
            if holdings[etf]["Units"] == 0:
                holdings[etf]["Average Price"] = 0.0

    # Save holdings
    holdings_ws.clear()
    holdings_ws.append_row(["ETF", "Units", "Average Price"])
    for etf, data in holdings.items():
        holdings_ws.append_row([etf, data["Units"], data["Average Price"]])

# Market close check
def is_market_closed():
    now = datetime.now(pytz.timezone("Asia/Kolkata"))
    return now.weekday() <= 4 and now >= now.replace(hour=15, minute=15, second=0, microsecond=0)

# PnL logger
def log_daily_pnl(trades_ws, holdings_ws, sheet):
    try:
        pnl_ws = sheet.worksheet("Daily PnL")
    except:
        pnl_ws = sheet.add_worksheet(title="Daily PnL", rows=1000, cols=2)
        pnl_ws.append_row(["Date", "P&L (₹)"])

    trades_df = pd.DataFrame(trades_ws.get_all_records())
    holdings_df = pd.DataFrame(holdings_ws.get_all_records())

    current_prices = {}
    for etf, symbol in etfs.items():
        data = yf.Ticker(symbol).history(period="1d")
        if not data.empty:
            current_prices[etf] = round(data["Close"].iloc[-1], 2)

    realized_pnl = 0
    unrealized_pnl = 0

    # Realized PnL from SELL trades
    for _, trade in trades_df.iterrows():
        if trade["Type"] == "SELL":
            etf = trade["ETF"]
            sell_price = float(trade["Price"])
            match = holdings_df[holdings_df["ETF"] == etf]
            if not match.empty:
                avg_price = float(match.iloc[0]["Average Price"])
                realized_pnl += (sell_price - avg_price)

    # Unrealized PnL from current holdings
    for _, row in holdings_df.iterrows():
        etf = row["ETF"]
        units = int(row["Units"])
        avg_price = float(row["Average Price"])
        if etf in current_prices:
            market_price = current_prices[etf]
            unrealized_pnl += (market_price - avg_price) * units

    total_pnl = round(realized_pnl + unrealized_pnl, 2)
    today = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%d.%m.%Y")
    pnl_ws.append_row([today, total_pnl])
    print(f"Logged today's P&L: {total_pnl} ₹ on {today}")

# Run main
print(results[["ETF", "Datetime", "Close", "RSI"]].to_string(index=False))
mock_trade(results, trades_ws, holdings_ws)
if is_market_closed():
    log_daily_pnl(trades_ws, holdings_ws, sheet)
else:
    print("Market not closed yet or today is not a trading day.")
