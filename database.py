"""
SQLite database setup and helpers.
All tables use the API UUID as the natural primary key to ensure idempotent upserts.
"""

import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "clinica_experts.db"


def get_connection(db_path=DB_PATH):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path=DB_PATH):
    conn = get_connection(db_path)
    with conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS bookings (
            uuid                TEXT PRIMARY KEY,
            patient_uuid        TEXT,
            patient_name        TEXT,
            professional_uuid   TEXT,
            professional_name   TEXT,
            procedure_uuid      TEXT,
            procedure_name      TEXT,
            healthcare_company_uuid TEXT,
            room_uuid           TEXT,
            starts_at           TEXT,
            ends_at             TEXT,
            status              TEXT,
            bill_uuid           TEXT,
            raw_json            TEXT,
            synced_at           TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS bills (
            uuid            TEXT PRIMARY KEY,
            type            TEXT,
            emission_date   TEXT,
            amount          REAL,
            description     TEXT,
            patient_uuid    TEXT,
            booking_uuid    TEXT,
            raw_json        TEXT,
            synced_at       TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS parcels (
            uuid            TEXT PRIMARY KEY,
            bill_uuid       TEXT REFERENCES bills(uuid),
            due_date        TEXT,
            amount          REAL,
            status          TEXT,
            paid_at         TEXT,
            payment_method  TEXT,
            raw_json        TEXT,
            synced_at       TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS reconciliation (
            booking_uuid        TEXT PRIMARY KEY REFERENCES bookings(uuid),
            bill_uuid           TEXT,
            parcel_uuid         TEXT,
            payment_status      TEXT NOT NULL,
            amount_expected     REAL,
            amount_paid         REAL,
            reconciled_at       TEXT NOT NULL DEFAULT (datetime('now')),
            notes               TEXT
        );

        CREATE TABLE IF NOT EXISTS sync_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date        TEXT NOT NULL,
            bookings_fetched    INTEGER DEFAULT 0,
            bills_fetched       INTEGER DEFAULT 0,
            parcels_fetched     INTEGER DEFAULT 0,
            reconciled          INTEGER DEFAULT 0,
            paid                INTEGER DEFAULT 0,
            pending             INTEGER DEFAULT 0,
            errors              TEXT,
            finished_at     TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_bookings_starts_at  ON bookings(starts_at);
        CREATE INDEX IF NOT EXISTS idx_bookings_status     ON bookings(status);
        CREATE INDEX IF NOT EXISTS idx_bookings_patient    ON bookings(patient_uuid);
        CREATE INDEX IF NOT EXISTS idx_bills_emission_date ON bills(emission_date);
        CREATE INDEX IF NOT EXISTS idx_bills_patient       ON bills(patient_uuid);
        CREATE INDEX IF NOT EXISTS idx_parcels_bill        ON parcels(bill_uuid);
        CREATE INDEX IF NOT EXISTS idx_parcels_status      ON parcels(status);
        CREATE INDEX IF NOT EXISTS idx_reconciliation_status ON reconciliation(payment_status);
        """)
    conn.close()
    logger.info(f"Database ready at {db_path}")


def upsert_booking(conn, b):
    conn.execute("""
        INSERT INTO bookings
            (uuid, patient_uuid, patient_name, professional_uuid, professional_name,
             procedure_uuid, procedure_name, healthcare_company_uuid, room_uuid,
             starts_at, ends_at, status, bill_uuid, raw_json, synced_at)
        VALUES
            (:uuid, :patient_uuid, :patient_name, :professional_uuid, :professional_name,
             :procedure_uuid, :procedure_name, :healthcare_company_uuid, :room_uuid,
             :starts_at, :ends_at, :status, :bill_uuid, :raw_json, datetime('now'))
        ON CONFLICT(uuid) DO UPDATE SET
            status    = excluded.status,
            bill_uuid = excluded.bill_uuid,
            raw_json  = excluded.raw_json,
            synced_at = excluded.synced_at
    """, b)


def upsert_bill(conn, b):
    conn.execute("""
        INSERT INTO bills
            (uuid, type, emission_date, amount, description, patient_uuid, booking_uuid, raw_json, synced_at)
        VALUES
            (:uuid, :type, :emission_date, :amount, :description, :patient_uuid, :booking_uuid, :raw_json, datetime('now'))
        ON CONFLICT(uuid) DO UPDATE SET
            amount      = excluded.amount,
            description = excluded.description,
            raw_json    = excluded.raw_json,
            synced_at   = excluded.synced_at
    """, b)


def upsert_parcel(conn, p):
    conn.execute("""
        INSERT INTO parcels
            (uuid, bill_uuid, due_date, amount, status, paid_at, payment_method, raw_json, synced_at)
        VALUES
            (:uuid, :bill_uuid, :due_date, :amount, :status, :paid_at, :payment_method, :raw_json, datetime('now'))
        ON CONFLICT(uuid) DO UPDATE SET
            status         = excluded.status,
            paid_at        = excluded.paid_at,
            payment_method = excluded.payment_method,
            raw_json       = excluded.raw_json,
            synced_at      = excluded.synced_at
    """, p)


def upsert_reconciliation(conn, r):
    conn.execute("""
        INSERT INTO reconciliation
            (booking_uuid, bill_uuid, parcel_uuid, payment_status,
             amount_expected, amount_paid, reconciled_at, notes)
        VALUES
            (:booking_uuid, :bill_uuid, :parcel_uuid, :payment_status,
             :amount_expected, :amount_paid, datetime('now'), :notes)
        ON CONFLICT(booking_uuid) DO UPDATE SET
            bill_uuid       = excluded.bill_uuid,
            parcel_uuid     = excluded.parcel_uuid,
            payment_status  = excluded.payment_status,
            amount_expected = excluded.amount_expected,
            amount_paid     = excluded.amount_paid,
            reconciled_at   = excluded.reconciled_at,
            notes           = excluded.notes
    """, r)


def insert_sync_log(conn, log):
    conn.execute("""
        INSERT INTO sync_log
            (run_date, bookings_fetched, bills_fetched, parcels_fetched,
             reconciled, paid, pending, errors)
        VALUES
            (:run_date, :bookings_fetched, :bills_fetched, :parcels_fetched,
             :reconciled, :paid, :pending, :errors)
    """, log)
