# WinterFallX

**An autonomous team code review and repetitive-error intelligence platform, built on the Vectorize Hindsight long-term cloud memory engine.**

Most AI code checkers are stateless: every review starts from zero. WinterFallX keeps a persistent, team-wide memory of coding standards, recurring mistakes, and architectural conventions, so your developers stop debugging the same problems twice. It is a code review agent that remembers, adapts, and evolves with you. The more your team uses it, the more personalized and efficient it becomes.

---

## Tech Stack

| Layer | Technology |
| --- | --- |
| Frontend | [Streamlit](https://streamlit.io), a monochrome, Apple-inspired glass UI with light and dark modes |
| Long-term memory | Hindsight Cloud via the official `hindsight-client` Python SDK (`retain` / `recall`) |
| Analytics ledger | Local SQLite database (`pipeline_logs.db`) powering the executive dashboard |
| Credentials | Environment secrets (`HINDSIGHT_API_URL`, `HINDSIGHT_API_KEY`) |

---

## How It Works

### 1. Developer Sandbox (default view)

1. **Recall.** When a review runs, the app queries the team's own memory bank (`winterfallx-<team>-memory`) with `Hindsight.recall()`, matching the current fault against past incidents by meaning rather than exact text.
2. **Memory hit.** If the incident was seen before, the app shows a green confirmation banner, increments the team's repeat counter, and applies the previously verified remediation.
3. **Memory miss.** If the fault is new, the app flags it, runs a short progress sequence, and calls `Hindsight.retain()` to store the incident, its context, and the resolution in the cloud memory bank for future reviews.

Built-in demo scenarios:

- `auth_middleware.py`: JWT validation bypass on a malformed header
- `db_pool.cpp`: leaked pooled connection when a query throws
- `stripe_payment.py`: webhook verification with no timeout budget

### 2. Company Executive Dashboard

Switch views from the sidebar to get a cross-team cockpit built from the SQLite ledger:

- **Engagement ranking:** teams ordered from highest to lowest platform engagement.
- **Team drill-downs:** expand a team to see its active developer IDs, preference footprint, and which files and standards keep failing.
- **Management signal:** the most frequently repeated convention violation per team.

### Accessibility

A built-in accessibility menu offers dark mode, high contrast, larger text, and reduced motion.

---

## Getting Started

### 1. Install dependencies

```bash
pip install streamlit hindsight-client
```

You need a recent version of Streamlit, since the app uses newer widget options such as `width="stretch"` on buttons. If you hit errors on an older install, upgrade with `pip install --upgrade streamlit`.

### 2. Set your environment secrets

Set these in your shell or in Replit Secrets before launching:

```bash
HINDSIGHT_API_URL="<your Hindsight API base URL>"
HINDSIGHT_API_KEY="<your Hindsight API key>"
```

Copy the base URL and API key from your Hindsight Cloud dashboard. The app raises a clear error at startup if either variable is missing.

### 3. Add the theme config (recommended)

Create `.streamlit/config.toml` in the project root so the light theme applies on every machine, regardless of browser or OS theme:

```toml
[theme]
base = "light"
primaryColor = "#000000"
backgroundColor = "#FFFFFF"
secondaryBackgroundColor = "#FFFFFF"
textColor = "#000000"
```

The folder name must start with a dot, and the server needs a restart after changes to this file.

### 4. Run the app

```bash
streamlit run Main.py --server.port 8501 --server.address 0.0.0.0
```

Then open the preview in your browser.

---

## Project Structure

```
.
├── Main.py                  # Entire app: UI, Hindsight adapter, SQLite ledger
├── pipeline_logs.db         # Auto-created local analytics ledger (seeded on first run)
├── .streamlit/
│   └── config.toml          # Theme settings
└── README.md
```

## Notes

- Hindsight is the memory of record. The local SQLite ledger only drives dashboard analytics and repeat-incident counts.
- If a Hindsight call fails (network or quota), the app shows a warning and keeps working with the local ledger.
- The dashboard's demo data is seeded automatically the first time the app runs.


