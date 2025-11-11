-- Migration: add current_price column and index
ALTER TABLE trapbot_data
  ADD COLUMN IF NOT EXISTS current_price numeric;

CREATE INDEX IF NOT EXISTS idx_trapbot_timestamp_desc ON trapbot_data ("timestamp" DESC);
