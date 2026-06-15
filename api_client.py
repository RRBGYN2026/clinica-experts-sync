"""
Clinica Experts API Client
Handles authentication, pagination, and data fetching for bookings, bills and parcels.
"""

import os
import time
import logging
from datetime import date, datetime
from typing import Generator

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.clinicaexperts.com.br/api/v1"
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2  # seconds


class ClinicaExpertsAPIError(Exception):
    pass


class ClinicaExpertsClient:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ["CLINICA_EXPERTS_API_KEY"]
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _get(self, endpoint, params):
        url = f"{BASE_URL}{endpoint}"
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as e:
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", RETRY_DELAY * attempt))
                    logger.warning(f"Rate limited. Waiting {wait}s...")
                    time.sleep(wait)
                elif resp.status_code >= 500 and attempt < RETRY_ATTEMPTS:
                    logger.warning(f"Server error {resp.status_code}. Retry {attempt}/{RETRY_ATTEMPTS}...")
                    time.sleep(RETRY_DELAY * attempt)
                else:
                    raise ClinicaExpertsAPIError(f"HTTP {resp.status_code}: {resp.text}") from e
            except requests.exceptions.RequestException as e:
                if attempt < RETRY_ATTEMPTS:
                    logger.warning(f"Request failed: {e}. Retry {attempt}/{RETRY_ATTEMPTS}...")
                    time.sleep(RETRY_DELAY * attempt)
                else:
                    raise ClinicaExpertsAPIError(f"Request failed after {RETRY_ATTEMPTS} attempts: {e}") from e
        raise ClinicaExpertsAPIError("Exhausted retry attempts")

    def _paginate(self, endpoint, params):
        """Yields individual records across all pages."""
        page = 1
        while True:
            data = self._get(endpoint, {**params, "page": page})
            if isinstance(data, list):
                records = data
                has_more = False
            else:
                records = data.get("data", data.get("records", []))
                meta = data.get("meta", data.get("pagination", {}))
                current = meta.get("current_page", meta.get("page", page))
                last = meta.get("last_page", meta.get("total_pages", None))
                has_more = last is not None and current < last
            for record in records:
                yield record
            if not has_more or not records:
                break
            page += 1
            time.sleep(0.2)

    def list_bookings(self, starts_at, ends_at, status=None):
        params = {"starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat()}
        if status:
            params["status"] = status
        yield from self._paginate("/bookings", params)

    def list_bills(self, start_date, end_date, bill_type=None):
        params = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "sort_column": "emission_date",
            "sort_direction": "asc",
        }
        if bill_type:
            params["type"] = bill_type
        yield from self._paginate("/bills", params)

    def get_bill(self, uuid):
        return self._get(f"/bills/{uuid}", {})

    def list_parcels(self, start_date, end_date, status=None):
        params = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "sort_column": "due_date",
            "sort_direction": "asc",
        }
        if status:
            params["status"] = status
        yield from self._paginate("/parcels", params)
