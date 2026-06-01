"""
Run this script ONCE to generate your Polymarket CLOB API credentials.

Usage:
    py generate_api_creds.py

It will:
  1. Read your POLYMARKET_PRIVATE_KEY from .env
  2. Derive API credentials (key, secret, passphrase) via wallet signature
  3. Print the values to paste into .env
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()

if not PRIVATE_KEY:
    print()
    print("ERROR: POLYMARKET_PRIVATE_KEY is not set in .env")
    print()
    print("1. Open MetaMask > three dots > Account Details > Show Private Key")
    print("2. Copy the 64-character hex string")
    print("3. Open .env and set:  POLYMARKET_PRIVATE_KEY=<your key>")
    print("4. Re-run this script")
    sys.exit(1)

if PRIVATE_KEY.startswith("0x"):
    PRIVATE_KEY = PRIVATE_KEY[2:]

if len(PRIVATE_KEY) != 64:
    print(f"ERROR: Private key looks wrong (got {len(PRIVATE_KEY)} chars, expected 64)")
    sys.exit(1)

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
except ImportError:
    print("ERROR: py-clob-client not installed.")
    print("Run:  pip install py-clob-client")
    sys.exit(1)

print("\nConnecting to Polymarket CLOB API...")

try:
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=POLYGON,
        private_key=PRIVATE_KEY,
        signature_type=0,   # EOA wallet
    )
    creds = client.create_or_derive_api_creds()

    print("\n" + "=" * 55)
    print("  SUCCESS - paste these into your .env file:")
    print("=" * 55)
    print(f"POLYMARKET_API_KEY={creds.api_key}")
    print(f"POLYMARKET_API_SECRET={creds.api_secret}")
    print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
    print("=" * 55)
    print()

    # Verify connection
    try:
        balance = client.get_balance()
        print(f"Wallet USDC balance on Polymarket: ${float(balance):.2f}")
    except Exception:
        print("(Could not fetch balance — credentials are still valid)")

except Exception as exc:
    print(f"\nERROR generating credentials: {exc}")
    print()
    print("Common causes:")
    print("  - Wrong private key format")
    print("  - No internet / RPC issue")
    print("  - Wallet not yet registered on Polymarket (visit polymarket.com first)")
    sys.exit(1)
