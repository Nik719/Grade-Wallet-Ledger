# app/services/reversal.py
import psycopg
from psycopg.rows import dict_row

from app.core.errors import WalletAPIException


def execute_reversal(conn: psycopg.Connection, transfer_id: int, actor: str, reason: str) -> dict:
    """
    Reverses a completed transfer by posting an equal and opposite 'reversal' transfer.

    The original ledger entries are never touched (the ledger is append-only); instead a new
    transfer moves the money back from the original destination to the original source,
    linked via parent_transfer_id, and the original is marked 'reversed'.

    Concurrency: the original transfer row is locked first (serialising competing reversals),
    then both accounts are locked in ascending id order, matching execute_transfer's lock order.
    The caller owns the transaction (commit / rollback).
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM transfers WHERE id = %s FOR UPDATE;", (transfer_id,))
        original = cur.fetchone()
        if not original:
            raise WalletAPIException("TRANSFER_NOT_FOUND", "Transfer not found", 404)
        if original["status"] == "reversed":
            raise WalletAPIException("TRANSFER_ALREADY_REVERSED", "Transfer has already been reversed", 409)
        if original["type"] == "reversal":
            raise WalletAPIException("NOT_REVERSIBLE", "A reversal cannot itself be reversed", 400)
        if original["status"] != "completed":
            raise WalletAPIException(
                "NOT_REVERSIBLE",
                f"Only completed transfers can be reversed (transfer is {original['status']})",
                400,
            )

        amount = original["amount"]
        # The reversal moves money in the opposite direction of the original.
        source_id = original["destination_account_id"]
        destination_id = original["source_account_id"]

        cur.execute(
            "SELECT id, balance, account_type FROM accounts WHERE id IN (%s, %s) ORDER BY id ASC FOR UPDATE;",
            (source_id, destination_id),
        )
        locked = {row["id"]: row for row in cur.fetchall()}
        source_acc = locked[source_id]

        if source_acc["account_type"] == "user" and source_acc["balance"] < amount:
            raise WalletAPIException(
                "INSUFFICIENT_FUNDS",
                "Recipient no longer holds enough funds to reverse this transfer",
                400,
            )

        cur.execute(
            """
            INSERT INTO transfers
                (source_account_id, destination_account_id, amount, type, status, parent_transfer_id, note)
            VALUES (%s, %s, %s, 'reversal', 'completed', %s, %s)
            RETURNING id;
            """,
            (source_id, destination_id, amount, transfer_id, f"Reversal of transfer {transfer_id}: {reason}"),
        )
        reversal_id = cur.fetchone()["id"]

        cur.execute("UPDATE accounts SET balance = balance - %s WHERE id = %s;", (amount, source_id))
        cur.execute("UPDATE accounts SET balance = balance + %s WHERE id = %s;", (amount, destination_id))
        cur.execute(
            """
            INSERT INTO ledger_entries (transfer_id, account_id, direction, amount)
            VALUES (%s, %s, 'debit', %s), (%s, %s, 'credit', %s);
            """,
            (reversal_id, source_id, amount, reversal_id, destination_id, amount),
        )

        cur.execute("UPDATE transfers SET status = 'reversed' WHERE id = %s;", (transfer_id,))
        cur.execute(
            """
            INSERT INTO audit_logs (transfer_id, from_status, to_status, actor, reason)
            VALUES (%s, 'completed', 'reversed', %s, %s);
            """,
            (transfer_id, actor, reason),
        )

    return {
        "original_transfer_id": transfer_id,
        "reversal_transfer_id": reversal_id,
        "status": "reversed",
        "amount": amount,
    }
