# Local Model Setup

Installed analysis model: `qwen3:14b` with its verified native 40,960-token context.

Hermes runtime model: `qwen3:4b-hermes` derived from official `qwen3:4b` with a 64,000-token context. Hermes 0.20.0 has a hard 64,000-token minimum, while official Ollama `qwen3:14b` is capped at 40,960. The 14B model therefore remains available for direct local analysis but cannot safely be presented to Hermes as a 64K model. The Hermes profile preserves tool calling while applying Qwen's `/no_think` control to avoid forced hidden reasoning on every maintenance turn.

Required Ollama settings:
- `OLLAMA_CONTEXT_LENGTH=64000`
- `OLLAMA_FLASH_ATTENTION=1`
- `OLLAMA_KV_CACHE_TYPE=q8_0`
- `OLLAMA_NUM_PARALLEL=1`
- `OLLAMA_MAX_LOADED_MODELS=1`
- `OLLAMA_KEEP_ALIVE=24h`

Hermes provider:
- provider: `custom`
- endpoint: `http://127.0.0.1:11434/v1`
- context length: `64000`
- no cloud fallback

This machine is CPU-only as detected on 2026-08-09. Large-context prefill can take several minutes. `ollama ps` is the authoritative residency/context check. Do not recreate a `qwen3:14b-64k` alias: Ollama clamps it to 40,960 and Hermes rejects it.

All Hermes auxiliary model slots used by this installation are pinned to `custom`, `qwen3:4b-hermes`, and the localhost Ollama endpoint. `auxiliary.free_only` is enabled, title generation is disabled, and the maintenance runner loads only terminal, file, code-execution, skill, and task-tracking toolsets. This prevents auxiliary cloud fallback probes during maintenance runs. The 24-hour keep-alive preserves Ollama's prompt cache across Hermes tool turns; without it, each turn reprocesses the full system prompt.
