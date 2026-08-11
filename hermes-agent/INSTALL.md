# Installation

1. Verify Ollama: `ollama --version` and `ollama list`.
2. Pull `qwen3:14b` for direct local analysis and `qwen3:4b`; create `qwen3:4b-hermes` from the Modelfile in this folder.
3. Install official Hermes Agent for native Windows from Nous Research.
4. Configure `%LOCALAPPDATA%\hermes\config.yaml` with provider `custom`, local Ollama base URL, model `qwen3:4b-hermes`, and 64000 context.
5. Link this project's maintenance skill into `%LOCALAPPDATA%\hermes\skills`.
6. Run a one-shot local inference from the DB_builder root.
7. Create cron jobs only after the manual quick scan succeeds.
8. Install/start the Hermes gateway so cron ticks continue after login.

No cloud inference provider is configured as a fallback.
