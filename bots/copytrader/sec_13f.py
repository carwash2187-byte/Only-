"""Follow famous investors via their SEC 13F filings (official, free API).

Every institutional manager with >$100M must disclose its holdings quarterly
in a 13F filing. This module pulls the latest 13F for a set of well-known
managers straight from SEC EDGAR and returns their top holdings.

EDGAR asks for a descriptive User-Agent with contact info; set SEC_EDGAR_UA
in your environment, e.g. "MyBot myemail@example.com".
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

EDGAR_UA = os.environ.get("SEC_EDGAR_UA", "Only-TradingDesk research bot (set SEC_EDGAR_UA env var)")
HEADERS = {"User-Agent": EDGAR_UA, "Accept-Encoding": "gzip, deflate"}
TIMEOUT = 30

# Well-known managers and their SEC CIK numbers. Add your own via
# top_holdings(extra_managers={"Name": cik}).
FAMOUS_MANAGERS: Dict[str, int] = {
    "Berkshire Hathaway (Warren Buffett)": 1067983,
    "Scion Asset Management (Michael Burry)": 1649339,
    "Pershing Square (Bill Ackman)": 1336528,
    "Duquesne Family Office (Stanley Druckenmiller)": 1536411,
    "Bridgewater Associates (Ray Dalio's firm)": 1350694,
}


@dataclass
class Holding:
    issuer: str
    value_usd: float
    shares: float
    ticker: Optional[str] = None


def _get_json(url: str) -> dict:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _latest_13f_accession(cik: int) -> Optional[str]:
    data = _get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    recent = data.get("filings", {}).get("recent", {})
    for form, accession in zip(recent.get("form", []), recent.get("accessionNumber", [])):
        if form.startswith("13F-HR"):
            return accession
    return None


def _info_table_xml(cik: int, accession: str) -> Optional[str]:
    accession_nodash = accession.replace("-", "")
    index = _get_json(f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/index.json")
    xml_files = [
        item["name"]
        for item in index.get("directory", {}).get("item", [])
        if item["name"].lower().endswith(".xml") and "primary_doc" not in item["name"].lower()
    ]
    if not xml_files:
        return None
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{xml_files[0]}"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_info_table(xml_text: str) -> List[Holding]:
    # Match elements by local name so any filer's namespace prefix works.
    root = ET.fromstring(xml_text)
    holdings: List[Holding] = []
    for table in root.iter():
        if _local_name(table.tag) != "infoTable":
            continue
        fields = {
            _local_name(child.tag): (child.text or "").strip() for child in table.iter()
        }
        issuer = fields.get("nameOfIssuer", "")
        value = float(fields.get("value") or 0)
        shares = float(fields.get("sshPrnamt") or 0)
        if issuer:
            holdings.append(Holding(issuer=issuer, value_usd=value, shares=shares))
    return holdings


_ticker_map_cache: Optional[Dict[str, str]] = None


def _normalize_name(name: str) -> str:
    name = name.upper()
    name = re.sub(r"[^A-Z0-9 ]", " ", name)
    for suffix in (" INC", " CORP", " CO", " PLC", " LTD", " SA", " NV", " CL A", " CL B",
                   " CLASS A", " CLASS B", " COM", " NEW", " DEL", " HLDGS", " HOLDINGS",
                   " GROUP", " COMPANIES", " COMPANY", " THE"):
        name = name.replace(suffix + " ", " ")
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return " ".join(name.split())


def _ticker_map() -> Dict[str, str]:
    """Issuer-name -> ticker map built from the SEC's official company list."""
    global _ticker_map_cache
    if _ticker_map_cache is None:
        data = _get_json("https://www.sec.gov/files/company_tickers.json")
        mapping: Dict[str, str] = {}
        # The file is ordered by market cap, so on duplicate names keep the
        # first (primary) listing, e.g. GOOGL over exotic Alphabet listings.
        for entry in data.values():
            mapping.setdefault(_normalize_name(entry["title"]), entry["ticker"])
        _ticker_map_cache = mapping
    return _ticker_map_cache


def resolve_ticker(issuer: str) -> Optional[str]:
    mapping = _ticker_map()
    normalized = _normalize_name(issuer)
    if normalized in mapping:
        return mapping[normalized]
    # Prefix fallback (e.g. "TAIWAN SEMICONDUCTOR MANUFAC" vs the full name),
    # but only for reasonably specific names -- short generic ones like
    # "ISHARES" would fuzzy-match the wrong fund in a family of hundreds.
    if len(normalized) < 8:
        return None
    for name, ticker in mapping.items():
        if name.startswith(normalized) or normalized.startswith(name):
            return ticker
    return None


def top_holdings(
    top_n: int = 10,
    managers: Optional[Dict[str, int]] = None,
    resolve_tickers: bool = True,
) -> Dict[str, List[Holding]]:
    """Latest top-N holdings (by reported value) for each manager.

    Managers whose data cannot be fetched are skipped with a warning entry
    rather than raising, so one broken filing doesn't kill the whole run.
    """
    result: Dict[str, List[Holding]] = {}
    for name, cik in (managers or FAMOUS_MANAGERS).items():
        try:
            accession = _latest_13f_accession(cik)
            if not accession:
                continue
            xml_text = _info_table_xml(cik, accession)
            if not xml_text:
                continue
            holdings = _parse_info_table(xml_text)
            # 13F values are reported in whole dollars (pre-2023 filings used
            # thousands); either way, sorting by value gives the top positions.
            holdings.sort(key=lambda h: h.value_usd, reverse=True)
            top = holdings[:top_n]
            if resolve_tickers:
                for holding in top:
                    try:
                        holding.ticker = resolve_ticker(holding.issuer)
                    except requests.RequestException:
                        break
            result[name] = top
        except requests.RequestException as exc:
            print(f"[13F] skipping {name}: {exc}")
    return result
