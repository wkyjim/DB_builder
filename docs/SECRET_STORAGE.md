# External Secret Storage

Local confidential configuration is stored outside this repository under:

```text
C:\Users\User\OneDrive\Coding\DB_builder_env\
|-- .env
|-- market-intelligence-telegram-bot\
|   `-- .env
`-- neon-api\
    `-- .env
```

DB_builder code loads this location automatically. Set `DB_BUILDER_ENV_DIR` only
when using a different private directory. Existing process environment variables
take precedence, so Render, GitHub Actions, and manually supplied variables keep
their current behavior.

The sibling directory is not part of the project and must not be included in
repository audits, prompts, archives, commits, or support bundles. `.env.example`
files contain names and non-secret defaults only.

Launch maintenance agents in audit mode so application imports do not load the
external files:

```powershell
$env:DB_BUILDER_AUDIT_MODE = "1"
Set-Location C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder
hermes
```

Keep that variable enabled for the complete agent process. Database health checks
should use a separately supplied read-only DSN, never these application-owner
credentials.

Moving files outside the repository prevents accidental inclusion, but it is not
a security boundary when an agent runs as the same unrestricted Windows user.
For strong isolation, run the audit agent in a filesystem sandbox limited to
`DB_builder`, or use a separate Windows account that has no read permission on
`DB_builder_env`.
