# tests/test_wallet_errors.py
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


def create_wallet(balance_paise: int) -> int:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            s = uuid.uuid4().hex[:8]
            cur.execute(
                "INSERT INTO users (name, email, phone, kyc_status) VALUES (%s, %s, %s, 'verified') RETURNING id;",
                (f"Withdrawer {s}", f"withdrawer_{s}@test.com", f"+91{s}41"),
            )
            user_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO accounts (user_id, account_type, name, currency, balance) "
                "VALUES (%s, 'user', 'Main', 'INR', %s) RETURNING id;",
                (user_id, balance_paise),
            )
            account_id = cur.fetchone()["id"]
        conn.commit()
    return account_id


@pytest.mark.asyncio
async def test_withdraw_overdraft_returns_400_not_500():
    """Withdrawing more than the balance must be a clean, idempotent 400 — never a 500."""
    account_id = create_wallet(balance_paise=1000)
    key = str(uuid.uuid4())
    payload = {"account_id": account_id, "amount": 5000}

    async with httpx.AsyncClient(timeout=10.0) as client:
        first = await client.post(
            f"{API_BASE_URL}/wallets/withdraw", json=payload, headers={"Idempotency-Key": key}
        )
        assert first.status_code == 400, f"Expected 400, got {first.status_code}: {first.text}"
        assert first.json()["code"] == "INSUFFICIENT_FUNDS"

        # Retrying with the same key replays the stored failure rather than re-executing.
        replay = await client.post(
            f"{API_BASE_URL}/wallets/withdraw", json=payload, headers={"Idempotency-Key": key}
        )
        assert replay.status_code == 400, f"Replay expected 400, got {replay.status_code}: {replay.text}"
        assert replay.json()["code"] == "INSUFFICIENT_FUNDS"

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM accounts WHERE id = %s;", (account_id,))
            assert cur.fetchone()["balance"] == 1000, "Failed withdrawal must not move money"
            cur.execute("SELECT COUNT(*) AS cnt FROM transfers WHERE source_account_id = %s;", (account_id,))
            assert cur.fetchone()["cnt"] == 0, "Failed withdrawal must not create a transfer"


@pytest.mark.asyncio
async def test_withdraw_and_topup_happy_path():
    account_id = create_wallet(balance_paise=0)
    async with httpx.AsyncClient(timeout=10.0) as client:
        topup = await client.post(
            f"{API_BASE_URL}/wallets/topup",
            json={"account_id": account_id, "amount": 10000},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert topup.status_code == 200, topup.text
        assert topup.json()["status"] == "completed"

        withdraw = await client.post(
            f"{API_BASE_URL}/wallets/withdraw",
            json={"account_id": account_id, "amount": 4000},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert withdraw.status_code == 200, withdraw.text

        balance = await client.get(f"{API_BASE_URL}/wallets/{account_id}/balance")
        assert balance.json()["balance"] == 6000
