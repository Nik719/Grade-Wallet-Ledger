# tests/test_reversals.py
import asyncio
import os
import uuid
import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://wallet_user:wallet_pass@localhost:5432/wallet_db"
)

REVERSE_BODY = {"actor": "ops_admin", "reason": "Customer dispute"}


def setup_accounts(sender_balance: int = 100000, receiver_balance: int = 0):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            s = uuid.uuid4().hex[:8]
            cur.execute(
                """
                INSERT INTO users (name, email, phone, kyc_status)
                VALUES (%s, %s, %s, 'verified'), (%s, %s, %s, 'verified')
                RETURNING id;
                """,
                (f"Rev Sender {s}", f"rev_sender_{s}@test.com", f"+91{s}51",
                 f"Rev Receiver {s}", f"rev_receiver_{s}@test.com", f"+91{s}52"),
            )
            u1, u2 = [r["id"] for r in cur.fetchall()]
            cur.execute(
                """
                INSERT INTO accounts (user_id, account_type, name, currency, balance)
                VALUES (%s, 'user', 'Main', 'INR', %s), (%s, 'user', 'Main', 'INR', %s)
                RETURNING id;
                """,
                (u1, sender_balance, u2, receiver_balance),
            )
            a1, a2 = [r["id"] for r in cur.fetchall()]
        conn.commit()
    return a1, a2


async def make_transfer(client, src, dst, amount) -> int:
    res = await client.post(
        f"{API_BASE_URL}/transfers",
        json={"source_account_id": src, "destination_account_id": dst, "amount": amount},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert res.status_code == 200, res.text
    return res.json()["transfer_id"]


def balances(*account_ids):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, balance FROM accounts WHERE id = ANY(%s);", (list(account_ids),))
            rows = {r["id"]: r["balance"] for r in cur.fetchall()}
    return [rows[a] for a in account_ids]


@pytest.mark.asyncio
async def test_reversal_restores_balances_and_links_records():
    src, dst = setup_accounts(sender_balance=100000)
    async with httpx.AsyncClient(timeout=10.0) as client:
        transfer_id = await make_transfer(client, src, dst, 25000)
        assert balances(src, dst) == [75000, 25000]

        res = await client.post(f"{API_BASE_URL}/admin/transfers/{transfer_id}/reverse", json=REVERSE_BODY)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["original_transfer_id"] == transfer_id
        assert body["status"] == "reversed"
        assert body["amount"] == 25000
        reversal_id = body["reversal_transfer_id"]

    assert balances(src, dst) == [100000, 0], "Reversal must restore both balances exactly"

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM transfers WHERE id = %s;", (transfer_id,))
            assert cur.fetchone()["status"] == "reversed"

            cur.execute("SELECT * FROM transfers WHERE id = %s;", (reversal_id,))
            rev = cur.fetchone()
            assert rev["type"] == "reversal"
            assert rev["status"] == "completed"
            assert rev["parent_transfer_id"] == transfer_id
            assert (rev["source_account_id"], rev["destination_account_id"]) == (dst, src)

            # Original ledger rows untouched; reversal has its own balanced pair.
            cur.execute(
                "SELECT direction, account_id, amount FROM ledger_entries WHERE transfer_id = %s ORDER BY direction;",
                (reversal_id,),
            )
            entries = cur.fetchall()
            assert [(e["direction"], e["account_id"], e["amount"]) for e in entries] == [
                ("debit", dst, 25000), ("credit", src, 25000)
            ]
            cur.execute("SELECT COUNT(*) AS cnt FROM ledger_entries WHERE transfer_id = %s;", (transfer_id,))
            assert cur.fetchone()["cnt"] == 2

            cur.execute("SELECT from_status, to_status, actor FROM audit_logs WHERE transfer_id = %s;", (transfer_id,))
            log = cur.fetchone()
            assert (log["from_status"], log["to_status"], log["actor"]) == ("completed", "reversed", "ops_admin")


@pytest.mark.asyncio
async def test_double_reversal_rejected():
    src, dst = setup_accounts()
    async with httpx.AsyncClient(timeout=10.0) as client:
        transfer_id = await make_transfer(client, src, dst, 10000)
        first = await client.post(f"{API_BASE_URL}/admin/transfers/{transfer_id}/reverse", json=REVERSE_BODY)
        assert first.status_code == 200, first.text

        second = await client.post(f"{API_BASE_URL}/admin/transfers/{transfer_id}/reverse", json=REVERSE_BODY)
        assert second.status_code == 409, second.text
        assert second.json()["code"] == "TRANSFER_ALREADY_REVERSED"

    assert balances(src, dst) == [100000, 0], "Money must move back exactly once"


@pytest.mark.asyncio
async def test_parallel_reversals_execute_once():
    src, dst = setup_accounts()
    async with httpx.AsyncClient(timeout=15.0) as client:
        transfer_id = await make_transfer(client, src, dst, 10000)
        responses = await asyncio.gather(*[
            client.post(f"{API_BASE_URL}/admin/transfers/{transfer_id}/reverse", json=REVERSE_BODY)
            for _ in range(10)
        ])

    codes = sorted(r.status_code for r in responses)
    assert codes.count(200) == 1, f"Exactly one reversal may succeed, got {codes}"
    assert codes.count(409) == 9, f"Others must be rejected with 409, got {codes}"
    assert balances(src, dst) == [100000, 0]

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM transfers WHERE parent_transfer_id = %s AND type = 'reversal';",
                (transfer_id,),
            )
            assert cur.fetchone()["cnt"] == 1


@pytest.mark.asyncio
async def test_reversal_blocked_when_recipient_spent_funds():
    src, dst = setup_accounts()
    async with httpx.AsyncClient(timeout=10.0) as client:
        transfer_id = await make_transfer(client, src, dst, 10000)
        # Recipient withdraws the money before the reversal is attempted.
        wd = await client.post(
            f"{API_BASE_URL}/wallets/withdraw",
            json={"account_id": dst, "amount": 10000},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert wd.status_code == 200, wd.text

        res = await client.post(f"{API_BASE_URL}/admin/transfers/{transfer_id}/reverse", json=REVERSE_BODY)
        assert res.status_code == 400, res.text
        assert res.json()["code"] == "INSUFFICIENT_FUNDS"

    assert balances(src, dst) == [90000, 0], "A failed reversal must not move money"
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM transfers WHERE id = %s;", (transfer_id,))
            assert cur.fetchone()["status"] == "completed"


@pytest.mark.asyncio
async def test_non_reversible_transfers():
    src, dst = setup_accounts(sender_balance=20_000_000)
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Unknown transfer
        res = await client.post(f"{API_BASE_URL}/admin/transfers/999999999/reverse", json=REVERSE_BODY)
        assert res.status_code == 404
        assert res.json()["code"] == "TRANSFER_NOT_FOUND"

        # Held transfer (above the ₹100,000 high-amount threshold) cannot be reversed
        held = await client.post(
            f"{API_BASE_URL}/transfers",
            json={"source_account_id": src, "destination_account_id": dst, "amount": 15_000_000},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert held.json()["status"] == "held_for_review"
        res = await client.post(
            f"{API_BASE_URL}/admin/transfers/{held.json()['transfer_id']}/reverse", json=REVERSE_BODY
        )
        assert res.status_code == 400
        assert res.json()["code"] == "NOT_REVERSIBLE"

        # A reversal cannot itself be reversed
        transfer_id = await make_transfer(client, src, dst, 5000)
        rev = await client.post(f"{API_BASE_URL}/admin/transfers/{transfer_id}/reverse", json=REVERSE_BODY)
        reversal_id = rev.json()["reversal_transfer_id"]
        res = await client.post(f"{API_BASE_URL}/admin/transfers/{reversal_id}/reverse", json=REVERSE_BODY)
        assert res.status_code == 400
        assert res.json()["code"] == "NOT_REVERSIBLE"

        # actor / reason are required
        res = await client.post(f"{API_BASE_URL}/admin/transfers/{transfer_id}/reverse", json={"actor": ""})
        assert res.status_code == 422
