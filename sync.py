"""
Main sync script — run daily via GitHub Actions.

Usage:
    python sync.py                      # syncs yesterday
    python sync.py --date 2025-06-10    # syncs a specific date
    python sync.py --date 2025-06-01 --date 2025-06-10  # syncs a range

Environment variables required:
    CLINICA_EXPERTS_API_KEY   – Bearer token from the Integrações page
"""

import argparse
import json
import logging
import sys
import traceback
from datetime import date, datetime, timedelta

import database as db
from api_client import ClinicaExpertsClient, ClinicaExpertsAPIError
from reconcile import reconcile_bookings, print_daily_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Normalisation helpers
#  Adapt field names from the actual API response to our DB columns.
#  Adjust these mappings once you observe real API payloads.
# ------------------------------------------------------------------ #

def _norm_booking(raw: dict) -> dict:
    patient    = raw.get("patient") or {}
    prof       = raw.get("professional") or {}
    procedure  = raw.get("procedure") or {}

    return {
        "uuid":                     raw.get("uuid") or raw.get("id"),
        "patient_uuid":             patient.get("uuid") or patient.get("id") or raw.get("patient_uuid"),
        "patient_name":             patient.get("name") or raw.get("patient_name"),
        "professional_uuid":        prof.get("uuid") or prof.get("id") or raw.get("professional_uuid"),
        "professional_name":        prof.get("name") or raw.get("professional_name"),
        "procedure_uuid":           procedure.get("uuid") or procedure.get("id") or raw.get("procedure_uuid"),
        "procedure_name":           procedure.get("name") or raw.get("procedure_name"),
        "healthcare_company_uuid":  raw.get("healthcare_company_uuid") or
                                    (raw.get("healthcare_company") or {}).get("uuid"),
        "room_uuid":                raw.get("room_uuid") or (raw.get("room") or {}).get("uuid"),
        "starts_at":                raw.get("starts_at"),
        "ends_at":                  raw.get("ends_at"),
        "status":                   raw.get("status"),
        "bill_uuid":                raw.get("bill_uuid") or raw.get("bill_id") or
                                    (raw.get("bill") or {}).get("uuid"),
        "raw_json":                 json.dumps(raw, ensure_ascii=False),
    }


def _norm_bill(raw: dict) -> dict:
    patient = raw.get("patient") or {}
    return {
        "uuid":          raw.get("uuid") or raw.get("id"),
        "type":          raw.get("type"),
        "emission_date": raw.get("emission_date") or raw.get("created_at"),
        "amount":        raw.get("amount") or raw.get("total"),
        "description":   raw.get("description") or raw.get("name"),
        "patient_uuid":  patient.get("uuid") or patient.get("id") or raw.get("patient_uuid"),
        "booking_uuid":  raw.get("booking_uuid") or (raw.get("booking") or {}).get("uuid"),
        "raw_json":      json.dumps(raw, ensure_ascii=False),
    }


def _norm_parcel(raw: dict) -> dict:
    return {
        "uuid":           raw.get("uuid") or raw.get("id"),
        "bill_uuid":      raw.get("bill_uuid") or raw.get("bill_id") or
                          (raw.get("bill") or {}).get("uuid"),
        "due_date":       raw.get("due_date"),
        "amount":         raw.get("amount") or raw.get("value"),
        "status":         raw.get("status"),
        "paid_at":        raw.get("paid_at") or raw.get("received_at"),
        "payment_method": raw.get("payment_method") or
                          (raw.get("financial_account") or {}).get("name"),
        "raw_json":       json.dumps(raw, ensure_ascii=False),
    }


# ------------------------------------------------------------------ #
#  Core sync for a single date
# ------------------------------------------------------------------ #

def sync_date(client: ClinicaExpertsClient, conn, target_date: date) -> dict:
    logger.info(f"Starting sync for {target_date}")

    starts_at = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    ends_at   = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)

    # ---- Bookings ------------------------------------------------- #
    bookings_count = 0
    with conn:
        for raw in client.list_bookings(starts_at, ends_at):
            normed = _norm_booking(raw)
            if not normed["uuid"]:
                logger.warning(f"Booking without UUID skipped: {raw}")
                continue
            db.upsert_booking(conn, normed)
            bookings_count += 1

    logger.info(f"  Bookings upserted : {bookings_count}")

    # ---- Bills ---------------------------------------------------- #
    bills_count = 0
    with conn:
        for raw in client.list_bills(target_date, target_date):
            normed = _norm_bill(raw)
            if not normed["uuid"]:
                continue
            db.upsert_bill(conn, normed)
            bills_count += 1

    logger.info(f"  Bills upserted    : {bills_count}")

    # ---- Parcels -------------------------------------------------- #
    parcels_count = 0
    with conn:
        for raw in client.list_parcels(target_date, target_date):
            normed = _norm_parcel(raw)
            if not normed["uuid"]:
                continue
            db.upsert_parcel(conn, normed)
            parcels_count += 1

    logger.info(f"  Parcels upserted  : {parcels_count}")

    # ---- Reconciliation ------------------------------------------- #
    with conn:
        rec_counts = reconcile_bookings(conn, target_date)

    return {
        "bookings_fetched": bookings_count,
        "bills_fetched":    bills_count,
        "parcels_fetched":  parcels_count,
        **rec_counts,
    }


# ------------------------------------------------------------------ #
#  Entry point
# ------------------------------------------------------------------ #

def parse_args():
    parser = argparse.ArgumentParser(description="Clínica Experts daily sync")
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        action="append",
        dest="dates",
        help="Date to sync (can be specified multiple times for a range). Defaults to yesterday.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database (defaults to data/clinica_experts.db)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    db_path = db.DB_PATH if args.db is None else __import__("pathlib").Path(args.db)
    db.init_db(db_path)
    conn = db.get_connection(db_path)

    if args.dates:
        try:
            dates = sorted({date.fromisoformat(d) for d in args.dates})
            if len(dates) == 2:
                # treat as inclusive range
                start, end = dates
                delta = (end - start).days
                dates = [start + timedelta(days=i) for i in range(delta + 1)]
        except ValueError as e:
            logger.error(f"Invalid date format: {e}")
            sys.exit(1)
    else:
        dates = [date.today() - timedelta(days=1)]

    client = ClinicaExpertsClient()
    all_errors = []

    for target_date in dates:
        try:
            stats = sync_date(client, conn, target_date)
            print_daily_summary(conn, target_date)
            with conn:
                db.insert_sync_log(conn, {"run_date": target_date.isoformat(), **stats, "errors": None})
        except ClinicaExpertsAPIError as e:
            msg = f"API error on {target_date}: {e}"
            logger.error(msg)
            all_errors.append(msg)
            with conn:
                db.insert_sync_log(conn, {
                    "run_date": target_date.isoformat(),
                    "bookings_fetched": 0, "bills_fetched": 0,
                    "parcels_fetched": 0, "reconciled": 0,
                    "paid": 0, "pending": 0,
                    "errors": msg,
                })
        except Exception as e:
            msg = f"Unexpected error on {target_date}: {traceback.format_exc()}"
            logger.error(msg)
            all_errors.append(msg)

    conn.close()

    if all_errors:
        logger.error(f"{len(all_errors)} error(s) occurred during sync.")
        sys.exit(1)

    logger.info("Sync completed successfully.")


if __name__ == "__main__":
    main()
