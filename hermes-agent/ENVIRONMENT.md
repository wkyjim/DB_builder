# Environment

Public configuration belongs in `%LOCALAPPDATA%\hermes\config.yaml` and project documentation. Private values belong only in `%LOCALAPPDATA%\hermes\.env`.

Expected private variable names:
- `LOCAL_PG_READONLY_DSN`
- `NEON_READONLY_DSN`
- `HERMES_API_TIMEOUT=86400` (the maintenance runner sets this per process)

Required Ollama user setting:
- `OLLAMA_KEEP_ALIVE=24h`

Do not place owner/admin DSNs in Hermes. Ollama is local and requires no API key.
