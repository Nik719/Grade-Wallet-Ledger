-- migrations/003_transfer_reversals.sql
-- Defence in depth for reversals: a transfer can be reversed at most once,
-- even if application-level locking were ever bypassed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_single_reversal_per_transfer
ON transfers (parent_transfer_id)
WHERE type = 'reversal';
