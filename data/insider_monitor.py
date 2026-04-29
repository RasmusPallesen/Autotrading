"""
Insider trading monitor.
Fetches Form 4 filings from SEC EDGAR to track significant insider transactions.
Insider buying by executives is a strong leading indicator — they know their company best.

Integrates with the research agent as an additional data source.
"""

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional
from xml.etree import ElementTree

import requests

logger = logging.getLogger(__name__)

EDGAR_HEADERS = {"User-Agent": "TradingAgent rasmus.pallesen@gmail.com"}
EDGAR_BASE = "https://www.sec.gov"

# Minimum transaction value to report (filter noise)
MIN_TRANSACTION_VALUE = 50_000  # $50,000


@dataclass
class InsiderTransaction:
    symbol: str
    company_name: str
    insider_name: str
    insider_title: str
    transaction_type: str    # "Buy" or "Sell"
    shares: float
    price_per_share: float
    total_value: float
    transaction_date: date
    filing_date: date
    form_url: str

    @property
    def is_significant_buy(self) -> bool:
        return (
            self.transaction_type == "Buy" and
            self.total_value >= MIN_TRANSACTION_VALUE
        )

    @property
    def signal_strength(self) -> str:
        if self.total_value >= 1_000_000:
            return "VERY STRONG"
        elif self.total_value >= 500_000:
            return "STRONG"
        elif self.total_value >= 100_000:
            return "MODERATE"
        return "WEAK"

    def to_research_summary(self) -> str:
        direction = "BOUGHT" if self.transaction_type == "Buy" else "SOLD"
        return (
            f"INSIDER TRANSACTION: {self.insider_name} ({self.insider_title}) "
            f"{direction} {self.shares:,.0f} shares of {self.symbol} "
            f"at ${self.price_per_share:.2f}/share "
            f"(total: ${self.total_value:,.0f}) on {self.transaction_date}. "
            f"Signal strength: {self.signal_strength}. "
            f"{'Insider buying at this scale is a strong bullish signal — executives rarely buy significant amounts unless they expect the stock to rise.' if self.transaction_type == 'Buy' else 'Insider selling can indicate profit-taking or diversification — less conclusive than insider buying.'}"
        )


class InsiderMonitor:
    """
    Monitors SEC Form 4 filings for insider transactions on watchlist symbols.
    Fetches from EDGAR's free public API — no key required.
    """

    def __init__(self):
        self._ticker_map: dict = {}
        self._ticker_map_loaded = False
        self._cache: dict = {}  # symbol -> list of transactions

    def _load_ticker_map(self):
        """Load ticker -> CIK mapping from EDGAR."""
        if self._ticker_map_loaded:
            return
        try:
            resp = requests.get(
                f"{EDGAR_BASE}/files/company_tickers.json",
                headers=EDGAR_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            self._ticker_map = {
                v["ticker"].upper(): {
                    "cik": str(v["cik_str"]).zfill(10),
                    "name": v["title"],
                }
                for v in resp.json().values()
            }
            self._ticker_map_loaded = True
            logger.debug("Loaded %d tickers from EDGAR", len(self._ticker_map))
        except Exception as e:
            logger.warning("Could not load EDGAR ticker map: %s", e)

    def get_transactions(
        self,
        symbols: List[str],
        days_back: int = 30,
        buys_only: bool = False,
    ) -> List[InsiderTransaction]:
        """
        Fetch recent insider transactions for a list of symbols.
        Returns transactions from the last `days_back` days.
        """
        self._load_ticker_map()
        all_transactions = []
        cutoff = date.today() - timedelta(days=days_back)

        for symbol in symbols:
            info = self._ticker_map.get(symbol.upper())
            if not info:
                continue

            try:
                transactions = self._fetch_form4(
                    symbol, info["cik"], info["name"], cutoff
                )
                if buys_only:
                    transactions = [t for t in transactions if t.transaction_type == "Buy"]
                all_transactions.extend(transactions)
                time.sleep(0.15)  # Respect EDGAR rate limits
            except Exception as e:
                logger.debug("Insider fetch error for %s: %s", symbol, e)

        # Sort by total value descending — biggest moves first
        all_transactions.sort(key=lambda t: t.total_value, reverse=True)
        return all_transactions

    def get_significant_buys(
        self,
        symbols: List[str],
        days_back: int = 14,
    ) -> List[InsiderTransaction]:
        """
        Get only significant insider buys (>$50k) from the last 14 days.
        These are the highest-signal transactions.
        """
        transactions = self.get_transactions(symbols, days_back, buys_only=True)
        return [t for t in transactions if t.is_significant_buy]

    def _fetch_form4(
        self,
        symbol: str,
        cik: str,
        company_name: str,
        cutoff: date,
    ) -> List[InsiderTransaction]:
        """Fetch and parse Form 4 filings for a company."""
        transactions = []

        # Get list of recent Form 4 filings
        try:
            resp = requests.get(
                f"https://data.sec.gov/submissions/CIK{cik}.json",
                headers=EDGAR_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            recent = data.get("filings", {}).get("recent", {})

            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            accessions = recent.get("accessionNumber", [])

        except Exception as e:
            logger.debug("EDGAR submissions fetch error for %s: %s", symbol, e)
            return []

        # Process Form 4 filings within cutoff
        filings_checked = 0
        for i, form in enumerate(forms):
            if form != "4":
                continue
            if filings_checked >= 10:  # Max 10 Form 4s per symbol
                break

            filing_date_str = dates[i] if i < len(dates) else ""
            try:
                filing_date = date.fromisoformat(filing_date_str)
            except Exception:
                continue

            if filing_date < cutoff:
                break  # Filings are date-sorted, stop when past cutoff

            accession = accessions[i].replace("-", "") if i < len(accessions) else ""
            if not accession:
                continue

            filings_checked += 1
            txns = self._parse_form4_xml(
                symbol, company_name, cik, accession, filing_date
            )
            transactions.extend(txns)

        return transactions

    def _parse_form4_xml(
        self,
        symbol: str,
        company_name: str,
        cik: str,
        accession: str,
        filing_date: date,
    ) -> List[InsiderTransaction]:
        """Download and parse a Form 4 XML filing."""
        transactions = []

        # Form 4 primary document is usually the .xml file
        base_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{accession}"
        xml_url = f"{base_url}/{accession[:10]}-{accession[10:12]}-{accession[12:]}.xml"

        try:
            resp = requests.get(xml_url, headers=EDGAR_HEADERS, timeout=8)
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.content)

            # Extract insider info
            insider_name = ""
            insider_title = ""

            rpt_owner = root.find(".//reportingOwner")
            if rpt_owner is not None:
                name_el = rpt_owner.find(".//rptOwnerName")
                if name_el is not None:
                    insider_name = name_el.text or ""
                rel_el = rpt_owner.find(".//officerTitle")
                if rel_el is not None:
                    insider_title = rel_el.text or ""
                if not insider_title:
                    # Check if director
                    dir_el = rpt_owner.find(".//isDirector")
                    if dir_el is not None and dir_el.text == "1":
                        insider_title = "Director"

            # Extract transactions
            for txn in root.findall(".//nonDerivativeTransaction"):
                try:
                    # Transaction date
                    date_el = txn.find(".//transactionDate/value")
                    txn_date = date.fromisoformat(date_el.text) if date_el is not None else filing_date

                    # Transaction type (A=acquired/buy, D=disposed/sell)
                    code_el = txn.find(".//transactionCode")
                    code = code_el.text if code_el is not None else ""
                    if code == "P":
                        txn_type = "Buy"
                    elif code == "S":
                        txn_type = "Sell"
                    else:
                        continue  # Skip grants, options exercises etc

                    # Shares
                    shares_el = txn.find(".//transactionShares/value")
                    shares = float(shares_el.text) if shares_el is not None else 0

                    # Price
                    price_el = txn.find(".//transactionPricePerShare/value")
                    price = float(price_el.text) if price_el is not None and price_el.text else 0

                    if shares <= 0 or price <= 0:
                        continue

                    total_value = shares * price

                    transactions.append(InsiderTransaction(
                        symbol=symbol,
                        company_name=company_name,
                        insider_name=insider_name,
                        insider_title=insider_title,
                        transaction_type=txn_type,
                        shares=shares,
                        price_per_share=price,
                        total_value=total_value,
                        transaction_date=txn_date,
                        filing_date=filing_date,
                        form_url=xml_url,
                    ))

                except Exception as e:
                    logger.debug("Error parsing transaction: %s", e)
                    continue

        except Exception as e:
            logger.debug("Could not parse Form 4 XML for %s: %s", symbol, e)

        return transactions
