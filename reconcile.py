"""
Reconciliation logic: cross-reference bookings with bills/parcels.

Payment status values:
  paid            - all parcels paid
  partial         - some parcels paid
  pending         - bill exists but no parcel paid
  not_applicable  - canceled/no-show/rescheduled
  unknown         - no linked bill found
"""

import logging
import sqlite3
from datetime import date

logger = logging.getLogger(__name__)

NON_BILLABLE_STATUSES = {"canceled", "noshow", "rescheduled"}
PAID_STATUSES = {"paid", "pago", "recebido", "received"}


def _get_parcels_for_bill(conn, bill_uuid):
    return conn.execute("SELECT * FROM parcels WHERE bill_uuid = ?", (bill_uuid,)).fetchall()


def _find_bill_by_patient_and_date(conn, patient_uuid, booking_date):
    return conn.execute(
        """
        SELECT * FROM bills
        WHERE patient_uuid = ?
          AND date(emission_date) = date(?)
        ORDER BY synced_at DESC LIMIT 1
        """,
        (patient_uuid, booking_date),
    ).fetchone()


def _classify_parcels(parcels):
    if not parcels:
        return "pending", 0.0, 0.0
    total = sum(p["amount"] or 0 for p in parcels)
    paid = sum(p["amount"] or 0 for p in parcels if (p["status"] or "").lower() in PAID_STATUSES)
    if paid >= total and total > 0:
        status = "paid"
    elif paid > 0:
        status = "partial"
    else:
        status = "pending"
    return status, total, paid


def reconcile_bookings(conn, run_date):
    from database import upsert_reconciliation

    bookings = conn.execute(
        "SELECT * FROM bookings WHERE date(starts_at) = date(?)",
        (run_date.isoformat(),),
    ).fetchall()

    counts = {"reconciled": 0, "paid": 0, "pending": 0, "not_applicable": 0, "unknown": 0}

    for booking in bookings:
        uuid = booking["uuid"]
        status_lower = (booking["status"] or "").lower()

        if status_lower in NON_BILLABLE_STATUSES:
            upsert_reconciliation(conn, {
                "booking_uuid": uuid, "bill_uuid": None, "parcel_uuid": None,
                "payment_status": "not_applicable", "amount_expected": 0.0, "amount_paid": 0.0,
                "notes": f"Booking status '{status_lower}' - no charge expected",
            })
            counts["not_applicable"] += 1
            counts["reconciled"] += 1
            continue

        bill_uuid = booking["bill_uuid"]
        bill_row = None

        if bill_uuid:
            bill_row = conn.execute("SELECT * FROM bills WHERE uuid = ?", (bill_uuid,)).fetchone()

        if bill_row is None and booking["patient_uuid"]:
            booking_date = (booking["starts_at"] or "")[:10]
            bill_row = _find_bill_by_patient_and_date(conn, booking["patient_uuid"], booking_date)
            if bill_row:
                bill_uuid = bill_row["uuid"]

        if bill_row is None:
            upsert_reconciliation(conn, {
                "booking_uuid": uuid, "bill_uuid": None, "parcel_uuid": None,
                "payment_status": "unknown", "amount_expected": None, "amount_paid": None,
                "notes": "No bill found for this booking",
            })
            counts["unknown"] += 1
            counts["reconciled"] += 1
            continue

        parcels = _get_parcels_for_bill(conn, bill_uuid)
        payment_status, amount_expected, amount_paid = _classify_parcels(parcels)
        parcel_uuid = parcels[0]["uuid"] if len(parcels) == 1 else None

        upsert_reconciliation(conn, {
            "booking_uuid": uuid, "bill_uuid": bill_uuid, "parcel_uuid": parcel_uuid,
            "payment_status": payment_status, "amount_expected": amount_expected,
            "amount_paid": amount_paid, "notes": f"{len(parcels)} parcel(s) found",
        })
        counts["reconciled"] += 1
        if payment_status == "paid":
            counts["paid"] += 1
        else:
            counts["pending"] += 1

    logger.info(f"Reconciliation complete for {run_date}: {counts}")
    return counts


def print_daily_summary(conn, run_date):
    rows = conn.execute(
        """
        SELECT b.patient_name, b.professional_name, b.starts_at,
               b.status AS booking_status, r.payment_status,
               r.amount_expected, r.amount_paid
        FROM bookings b
        LEFT JOIN reconciliation r ON r.booking_uuid = b.uuid
        WHERE date(b.starts_at) = date(?)
        ORDER BY b.starts_at
        """,
        (run_date.isoformat(),),
    ).fetchall()

    print(f"\n{'='*70}")
    print(f"  RELATORIO DE ATENDIMENTOS - {run_date.strftime('%d/%m/%Y')}")
    print(f"  {'Paciente':<25} {'Profissional':<20} {'Agend.':<12} {'Pagto':<12} {'R$':>8}")

    for r in rows:
        expected = r["amount_expected"] or 0
        paid = r["amount_paid"] or 0
        pay_str = (r["payment_status"] or "-").upper()
        value = paid if pay_str == "PAID" else expected
        print(f"  {(r['patient_name'] or '-'):<25} {(r['professional_name'] or '-'):<20} {(r['booking_status'] or '-'):<12} {pay_str:<12} R${value:>7.2f}")

    totals = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN r.payment_status='paid' THEN 1 ELSE 0 END) AS paid,
               SUM(CASE WHEN r.payment_status='pending' THEN 1 ELSE 0 END) AS pending,
               SUM(r.amount_paid) AS total_received,
               SUM(r.amount_expected) - SUM(COALESCE(r.amount_paid,0)) AS total_open
        FROM bookings b LEFT JOIN reconciliation r ON r.booking_uuid = b.uuid
        WHERE date(b.starts_at) = date(?)
        """,
        (run_date.isoformat(),),
    ).fetchone()

    print(f"  Total: {totals['total']} | Pagos: {totals['paid']} | Pendentes: {totals['pending']}")
    print(f"  Receita recebida: R${(totals['total_received'] or 0):,.2f}")
    print(f"  Receita em aberto: R${(totals['total_open'] or 0):,.2f}")
    print(f"{'='*70}\n")
