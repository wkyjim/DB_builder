# DB Builder Agent Rules

This project has two parts:

1. Local data pipeline
   - Fetches data from yfinance
   - Updates local PostgreSQL
   - Calculates indicators
   - Syncs selected data to Neon

2. API deployment
   - Located in neon-api/
   - FastAPI app deployed to Render
   - Reads from Neon only

## Safety rules

Never run destructive SQL without asking:
- DROP TABLE
- TRUNCATE
- DELETE FROM
- ALTER TABLE DROP COLUMN

Never expose secrets:
- Do not print .env values
- Do not commit .env
- Do not hardcode database passwords

Prefer:
- bulk upserts
- symbol/date deduplication
- minimal Neon reads
- explicit logging
- small test runs before full runs

Before git commit:
- run tests if available
- run affected script with a small ticker subset
- show git diff
- explain what changed

For Render API:
- only edit neon-api/ unless asked
- keep OpenAPI schema GPT-action friendly
- keep response models explicit