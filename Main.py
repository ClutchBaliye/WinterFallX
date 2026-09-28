"""WINTERFALLX — autonomous code review and repetitive-error intelligence.

The app intentionally keeps the product surface in one file so it can be
copied into a fresh Replit project and run with the requested Streamlit
command. Memory is backed by the real Hindsight Cloud API (retain/recall),
with a local SQLite ledger kept alongside it to power the dashboard's
per-team analytics (feedback counts, affected files, engagement hours).

Credentials are read from environment secrets:
    HINDSIGHT_API_URL, HINDSIGHT_API_KEY
"""

from __future__ import annotations

import hashlib
import html
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st
from hindsight_client import Hindsight


APP_NAME = "WINTERFALLX"
DB_PATH = Path(__file__).with_name("pipeline_logs.db")

SCENARIOS: dict[str, dict[str, str]] = {
    "auth_middleware.py — Token Validation Failure": {
        "file": "auth_middleware.py",
        "error": "JWT token validation is bypassed when the Authorization header is malformed.",
        "violation": "Authentication boundary must fail closed",
        "code": '''def validate_request(request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    return payload.get("sub")''',
        "resolution": '''def validate_request(request):
    auth = request.headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("Bearer token required")
    return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])''',
        "guide": "Reject malformed credentials before decoding and keep the authentication boundary fail-closed.",
    },
    "db_pool.cpp — Memory Leak Pointer Crash": {
        "file": "db_pool.cpp",
        "error": "Pooled connection is returned on the happy path but leaked when query execution throws.",
        "violation": "Resources must use RAII ownership",
        "code": '''Result run_query(Pool* pool, Query query) {
    Connection* connection = pool->acquire();
    Result result = connection->execute(query);
    pool->release(connection);
    return result;
}''',
        "resolution": '''Result run_query(Pool* pool, Query query) {
    auto connection = pool->acquire();
    if (!connection) throw PoolExhausted();
    auto guard = finally([&] { pool->release(connection.get()); });
    return connection->execute(query);
}''',
        "guide": "Treat checked-out connections as owned resources and guarantee release during exceptions with RAII.",
    },
    "stripe_payment.py — Signature Certificate Timeout": {
        "file": "stripe_payment.py",
        "error": "Webhook verification performs a network fetch inside the request thread with no bounded timeout.",
        "violation": "External calls require explicit timeout budgets",
        "code": '''def handle_webhook(request):
    event = stripe.Webhook.construct_event(
        request.body, request.headers["Stripe-Signature"], get_webhook_secret()
    )
    process_payment(event)
    return {"ok": True}''',
        "resolution": '''def handle_webhook(request):
    signature = request.headers.get("Stripe-Signature")
    secret = get_webhook_secret(timeout=2.0)
    event = stripe.Webhook.construct_event(request.body, signature, secret)
    process_payment(event)
    return {"ok": True}''',
        "guide": "Keep certificate and secret lookups bounded, observable, and outside an unprotected request path.",
    },
}

SEED_ROWS = [
    {
        "timestamp": "2026-09-26T09:12:00+00:00",
        "team_name": "AlphaBuilders",
        "developer_id": "dev-042",
        "file_name": "session_store.py",
        "code_snippet": "cache.set(user.id, session)",
        "error_msg": "Session cache write has no expiry policy.",
        "resolution": "Set an explicit TTL and encrypt session payloads before persistence.",
        "standard_violation": "Security-sensitive state requires expiry and encryption",
        "feedback_count": 3,
        "preference_flag": "prefer-explicit-security-boundaries",
    },
    {
        "timestamp": "2026-09-25T14:38:00+00:00",
        "team_name": "DeltaCoders",
        "developer_id": "maya-17",
        "file_name": "retry_queue.go",
        "code_snippet": "for err != nil { send(job) }",
        "error_msg": "Retry loop has no backoff or attempt ceiling.",
        "resolution": "Use bounded exponential backoff with a dead-letter queue.",
        "standard_violation": "Retries require backoff and a bounded attempt budget",
        "feedback_count": 5,
        "preference_flag": "prefer-bounded-retries",
    },
    {
        "timestamp": "2026-09-24T11:06:00+00:00",
        "team_name": "AlphaBuilders",
        "developer_id": "dev-018",
        "file_name": "feature_flags.ts",
        "code_snippet": "if (flags[name]) enable(feature)",
        "error_msg": "Unknown flags silently enable a feature.",
        "resolution": "Require typed flag definitions and log unknown-key reads.",
        "standard_violation": "Feature configuration must be typed and observable",
        "feedback_count": 2,
        "preference_flag": "prefer-typed-config",
    },
]

TEAM_HOURS = {"AlphaBuilders": 14.5, "DeltaCoders": 9.0, "Northstar Labs": 5.5}


# ---------------------------------------------------------------------------
# Hindsight client (real Cloud API, credentials come from environment secrets)
# ---------------------------------------------------------------------------

_hindsight_client: Hindsight | None = None


def get_hindsight_client() -> Hindsight:
    """Lazily build a singleton Hindsight client from environment secrets."""
    global _hindsight_client
    if _hindsight_client is None:
        try:
            base_url = os.environ["HINDSIGHT_API_URL"]
            api_key = os.environ["HINDSIGHT_API_KEY"]
        except KeyError as exc:
            raise RuntimeError(
                "Missing Hindsight credentials. Set HINDSIGHT_API_URL and "
                "HINDSIGHT_API_KEY in Replit Secrets before running."
            ) from exc
        _hindsight_client = Hindsight(base_url=base_url, api_key=api_key)
    return _hindsight_client


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def init_database() -> None:
    with db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                team_name TEXT NOT NULL,
                developer_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                code_snippet TEXT NOT NULL,
                error_msg TEXT NOT NULL,
                resolution TEXT NOT NULL,
                standard_violation TEXT NOT NULL,
                feedback_count INTEGER NOT NULL DEFAULT 1,
                preference_flag TEXT NOT NULL DEFAULT ''
            )
            """
        )
        if connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0:
            connection.executemany(
                """
                INSERT INTO incidents
                (timestamp, team_name, developer_id, file_name, code_snippet,
                 error_msg, resolution, standard_violation, feedback_count, preference_flag)
                VALUES (:timestamp, :team_name, :developer_id, :file_name, :code_snippet,
                        :error_msg, :resolution, :standard_violation, :feedback_count, :preference_flag)
                """,
                SEED_ROWS,
            )
        connection.commit()


def signature_for(team_name: str, file_name: str, code_snippet: str, error_msg: str) -> str:
    raw = "|".join([team_name.strip().lower(), file_name.strip().lower(), code_snippet.strip(), error_msg.strip()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def row_signature(row: sqlite3.Row) -> str:
    return signature_for(row["team_name"], row["file_name"], row["code_snippet"], row["error_msg"])


class HindsightLedger:
    """Adapter around the real Hindsight Cloud API.

    Hindsight (retain/recall) is the memory of record: it searches by
    meaning (semantic, keyword, graph and temporal) rather than by exact hash.
    The local SQLite table underneath is the data source for the executive
    dashboard (feedback counts, affected files, per-team engagement) and for
    repeat-incident bookkeeping. It is analytics, not memory.
    """

    def __init__(self, bank_id: str):
        self.bank_id = bank_id
        self.client = get_hindsight_client()

    def recall(
        self, team_name: str, file_name: str, code_snippet: str, error_msg: str
    ) -> tuple[sqlite3.Row | None, list[str]]:
        query = f"{file_name}: {error_msg}"
        memory_texts: list[str] = []
        try:
            result = self.client.recall(bank_id=self.bank_id, query=query)
            memory_texts = [memory.text for memory in result.results]
        except Exception as exc:  # network / quota / API failure path
            st.warning(f"Hindsight recall failed, continuing with local ledger only: {exc}")

        target = signature_for(team_name, file_name, code_snippet, error_msg)
        with db_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM incidents WHERE team_name = ? ORDER BY id DESC",
                (team_name,),
            ).fetchall()
        matched = next((row for row in rows if row_signature(row) == target), None)
        return matched, memory_texts

    def retain(self, payload: dict[str, Any]) -> int:
        content = (
            f"Team {payload['team_name']} (developer {payload['developer_id']}) hit an issue in "
            f"{payload['file_name']}: {payload['error_msg']}. "
            f"Standard violated: {payload['standard_violation']}. "
            f"Resolution applied: {payload['resolution']}."
        )
        try:
            self.client.retain(bank_id=self.bank_id, content=content)
        except Exception as exc:  # network / quota / API failure path
            st.warning(f"Hindsight retain failed, incident still logged locally: {exc}")

        with db_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO incidents
                (timestamp, team_name, developer_id, file_name, code_snippet,
                 error_msg, resolution, standard_violation, feedback_count, preference_flag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["timestamp"],
                    payload["team_name"],
                    payload["developer_id"],
                    payload["file_name"],
                    payload["code_snippet"],
                    payload["error_msg"],
                    payload["resolution"],
                    payload["standard_violation"],
                    1,
                    payload["preference_flag"],
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def increment_repeat(self, incident_id: int) -> int:
        with db_connection() as connection:
            connection.execute(
                "UPDATE incidents SET feedback_count = feedback_count + 1 WHERE id = ?",
                (incident_id,),
            )
            connection.commit()
            return int(
                connection.execute(
                    "SELECT feedback_count FROM incidents WHERE id = ?", (incident_id,)
                ).fetchone()[0]
            )


def all_rows() -> list[sqlite3.Row]:
    with db_connection() as connection:
        return connection.execute("SELECT * FROM incidents ORDER BY id DESC").fetchall()


def preference_for(team_name: str, rows: list[sqlite3.Row]) -> str:
    team_rows = [row for row in rows if row["team_name"] == team_name]
    if not team_rows:
        return "prefer-explicit-remediation"
    return team_rows[0]["preference_flag"] or "prefer-explicit-remediation"


# ---------------------------------------------------------------------------
# Styling: black / white / gray-by-opacity only. Real color exists solely in
# the four status dots. Light and dark mode swap the same CSS variables.
# ---------------------------------------------------------------------------

def inject_styles() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Lobster&family=Manrope:wght@400;500;600;700;800&display=swap');

        :root {
            --ink: #000000;
            --paper: #ffffff;
            --line: rgba(0,0,0,.08);
            --line-strong: rgba(0,0,0,.16);
            --muted: rgba(0,0,0,.58);
            --muted-soft: rgba(0,0,0,.4);
            --glass: rgba(255,255,255,.72);
            --glass-strong: rgba(255,255,255,.88);
            --sidebar-bg: rgba(236,236,236,.88);
            --tint: rgba(0,0,0,.045);
            --code-bg: #000000;
            --code-fg: #ffffff;
            --code-dim: rgba(255,255,255,.5);
            --shadow: 0 20px 60px rgba(0,0,0,.07);
            --shadow-soft: 0 8px 28px rgba(0,0,0,.05);
            --radius-lg: 24px;
            --radius-md: 16px;
            --radius-sm: 12px;
        }

        html, body, [class*="css"] {
            font-family: 'Manrope', -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', sans-serif;
            color: var(--ink);
        }

        /* Base surfaces */
        .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] { background: var(--paper) !important; }
        [data-testid="stHeader"] { background: transparent !important; z-index: 1000; }

        /* Force Streamlit's own text to follow the variables (fixes white-on-white) */
        [data-testid="stMarkdownContainer"],
        [data-testid="stMarkdownContainer"] :where(p, span, li, h1, h2, h3, h4, b, strong),
        [data-testid="stWidgetLabel"], [data-testid="stWidgetLabel"] p,
        [data-testid="stRadio"] label, [data-testid="stRadio"] label p,
        [data-testid="stToggle"] label, [data-testid="stToggle"] p,
        [data-testid="stExpander"] summary, [data-testid="stExpander"] summary p,
        [data-testid="stSpinner"], [data-testid="stSpinner"] *,
        [data-testid="stProgress"] p, [data-testid="stCaptionContainer"] {
            color: var(--ink) !important;
        }

        /* Sidebar: a shade darker than the main page */
        [data-testid="stSidebar"] {
            background: var(--sidebar-bg) !important;
            backdrop-filter: blur(28px); -webkit-backdrop-filter: blur(28px);
            border-right: 1px solid var(--line);
        }
        [data-testid="stSidebar"] > div:first-child, [data-testid="stSidebarContent"] { background: transparent !important; }
        [data-testid="stSidebar"] > div:first-child { padding: 2rem 1.25rem; }

        .block-container { max-width: 1440px; padding: 5.5rem 4.5rem 5rem; }

        h1, h2, h3, h4, p { letter-spacing: -.02em; }
        h1 { font-weight: 800; font-size: clamp(2.1rem, 3vw, 3.45rem); line-height: .98; letter-spacing: -.075em; margin: 0; }
        h2 { font-size: 1.4rem; font-weight: 800; margin: 0; }
        h3 { font-size: 1rem; font-weight: 800; margin: 0; }

        /* Brand: clean capital cursive W, no outline */
        .brand { display:flex; align-items:center; gap:.65rem; }
        .brand-mark {
            display:inline-flex; align-items:center; justify-content:center;
            width:auto; height:auto; background:none; border:0; border-radius:0;
            font: 400 1.9rem/1 'Lobster', 'Brush Script MT', 'Segoe Script', cursive;
            letter-spacing: -.02em; color: var(--ink) !important;
        }
        .brand-name { font-weight: 800; font-size: 1.02rem; letter-spacing: -.02em; color: var(--ink) !important; }
        .overlay-name { font: 800 1rem/1 'Manrope', sans-serif; letter-spacing:-.02em; color: var(--ink) !important; }

        /* Header bar: full width, above everything including the sidebar */
        .app-overlay {
            position:fixed; top:0; left:0; right:0; height:3.7rem; z-index:2147483000;
            display:flex; align-items:center; padding:0 1.4rem;
            background: var(--glass-strong); border-bottom:1px solid var(--line);
            backdrop-filter:blur(22px); -webkit-backdrop-filter:blur(22px);
            pointer-events:none;
        }
        .overlay-wordmark { display:flex; align-items:center; gap:.6rem; }
        .overlay-wordmark b {
            display:inline-flex; align-items:center; justify-content:center;
            width:auto; height:auto; background:none; border:0; border-radius:0;
            font: 400 1.9rem/1 'Lobster', 'Brush Script MT', 'Segoe Script', cursive;
            letter-spacing: -.02em; color: var(--ink) !important;
        }
        .overlay-context { color: var(--muted-soft) !important; font:500 .58rem 'DM Mono', monospace; letter-spacing:.13em; text-transform:uppercase; }

        /* Typography helpers */
        .eyebrow { color: var(--muted) !important; font-size:.74rem; font-family:'DM Mono', monospace; text-transform:uppercase; letter-spacing:.16em; font-weight:600; margin-bottom:1.3rem; }
        .subhead { color: var(--muted) !important; line-height:1.6; font-size:.94rem; max-width:650px; margin-top:1rem; }
        .hindsight-word { color: var(--ink) !important; font-weight:800; text-decoration:underline; text-decoration-thickness:2px; text-underline-offset:4px; }
        .hero-sage { color: var(--ink) !important; font:800 clamp(2.1rem, 3vw, 3.45rem)/.98 'Manrope', sans-serif; letter-spacing:-.075em; margin:0 0 1.3rem; }
        .surface-label { color: var(--muted) !important; text-transform:uppercase; font-family:'DM Mono',monospace; letter-spacing:.12em; font-size:.62rem; margin-bottom:.55rem; }
        .status-line { display:flex; align-items:center; gap:.55rem; font-size:.78rem; color: var(--muted) !important; }
        .status-line span { color: var(--muted) !important; }
        .engine-note { color: var(--muted-soft) !important; font: .6rem 'DM Mono', monospace; margin-top:.65rem; }
        .body-note { color: var(--muted) !important; font-size:.84rem; line-height:1.6; margin:.5rem 0 1.15rem; }
        .alert-note { color: var(--muted) !important; font-size:.72rem; }
        .code-pre { font: .74rem/1.7 'DM Mono', monospace; white-space: pre-wrap; color: var(--ink) !important; margin:.8rem 0 0; background: transparent !important; }

        /* Translucent Apple-style cards */
        .surface, .surface-tight, .metric-card, [data-testid="stVerticalBlockBorderWrapper"] {
            background: var(--glass) !important;
            backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
            border-radius: var(--radius-lg) !important;
            box-shadow: var(--shadow-soft);
            border: 1px solid var(--line) !important;
        }
        .surface { padding:1.5rem; }
        .surface-tight { padding:1.1rem 1.25rem; border-radius: var(--radius-md) !important; }
        .metric-card { padding:1.1rem 1.15rem; }
        [data-testid="stVerticalBlockBorderWrapper"] { padding:1.2rem; }
        [data-testid="stVerticalBlockBorderWrapper"] > div { border:0; }

        [data-testid="stExpander"] {
            background: var(--glass) !important; border: 1px solid var(--line) !important;
            border-radius: var(--radius-lg) !important; box-shadow: var(--shadow-soft);
            overflow: hidden;
        }
        [data-testid="stExpander"] details, [data-testid="stExpander"] summary { background: transparent !important; }
        [data-testid="stExpander"] svg { color: var(--ink) !important; fill: var(--ink) !important; }

        /* Inputs */
        [data-testid="stTextInput"] input, [data-baseweb="select"] > div {
            border-radius: var(--radius-sm) !important;
            border: 1px solid var(--line-strong) !important;
            background: var(--paper) !important;
            color: var(--ink) !important;
        }
        [data-baseweb="select"] * { color: var(--ink) !important; }
        [data-baseweb="select"] svg { fill: var(--ink) !important; }
        [data-baseweb="popover"], [data-baseweb="popover"] > div, [data-baseweb="menu"], [role="listbox"], [role="dialog"] {
            background: var(--paper) !important; color: var(--ink) !important;
            border-radius: var(--radius-md) !important; border: 1px solid var(--line) !important;
        }
        [data-baseweb="popover"] li, [data-baseweb="popover"] li *,
        [data-baseweb="popover"] [role="option"], [data-baseweb="popover"] [role="option"] *,
        [data-baseweb="popover"] [role="listbox"] *, [data-baseweb="popover"] [data-baseweb="menu"] *,
        [data-baseweb="menu"] li, [data-baseweb="menu"] li *,
        [role="listbox"] li, [role="listbox"] li *, [role="listbox"] [role="option"], [role="listbox"] [role="option"] * {
            color: var(--ink) !important; -webkit-text-fill-color: var(--ink) !important;
            background: transparent !important; opacity: 1 !important;
        }
        [data-baseweb="popover"] li:hover, [data-baseweb="popover"] [role="option"]:hover,
        [data-baseweb="popover"] [role="option"][aria-selected="true"], [role="listbox"] [role="option"]:hover {
            background: var(--tint) !important;
        }

        [data-testid="stTextArea"] textarea {
            border-radius: var(--radius-sm) !important;
            border: 1px solid var(--line-strong) !important;
            background: var(--code-bg) !important;
            color: var(--code-fg) !important;
            caret-color: var(--code-fg);
            font: 500 .76rem/1.7 "DM Mono", monospace;
        }
        [data-testid="stTextInput"] input:focus, [data-testid="stTextArea"] textarea:focus, [data-baseweb="select"] > div:focus-within {
            border-color: var(--ink) !important; box-shadow: 0 0 0 1px var(--ink) !important;
        }

        /* The only colors in the app: four Apple system dots */
        .dot { display:inline-block; width:8px; height:8px; border-radius:50%; flex:0 0 8px; }
        .dot-red { background:#FF3B30; box-shadow:0 0 0 4px rgba(255,59,48,.14); }
        .dot-yellow { background:#FFCC00; box-shadow:0 0 0 4px rgba(255,204,0,.16); }
        .dot-blue { background:#007AFF; box-shadow:0 0 0 4px rgba(0,122,255,.14); }
        .dot-green { background:#34C759; box-shadow:0 0 0 4px rgba(52,199,89,.14); }

        .hero-row { display:flex; justify-content:space-between; align-items:flex-start; gap:1rem; margin-bottom:2rem; padding-top:1rem; }
        .hero-meta { display:flex; gap:.5rem; align-items:center; padding-top:.2rem; color: var(--muted) !important; font-size:.72rem; font-family:'DM Mono',monospace; }

        .stat-value { font-size:2rem; font-weight:800; letter-spacing:-.06em; margin:.1rem 0 .25rem; color: var(--ink) !important; }
        .stat-caption { font-size:.74rem; color: var(--muted) !important; }
        .stat-grid { display:grid; grid-template-columns:repeat(3, 1fr); gap:.8rem; margin:1.4rem 0 1.8rem; }
        .metric-card .metric-kicker { color: var(--muted) !important; font-size:.65rem; text-transform:uppercase; letter-spacing:.1em; font-family:'DM Mono',monospace; }

        .log-box { background: var(--code-bg); border: 1px solid var(--line-strong); border-radius: var(--radius-md); padding:1rem 1.1rem; font-family:'DM Mono',monospace; font-size:.69rem; line-height:1.8; box-shadow: var(--shadow-soft); }
        .log-box div { color: var(--code-fg) !important; }
        .log-box .log-dim { color: var(--code-dim) !important; }

        .alert { display:flex; align-items:flex-start; gap:.8rem; border-radius: var(--radius-md); padding:1.05rem 1.15rem; margin:1rem 0; font-size:.82rem; line-height:1.5; background: var(--glass) !important; border: 1px solid var(--line) !important; color: var(--ink) !important; }
        .alert div { color: var(--ink) !important; }
        .alert strong { display:block; font-weight:800; margin-bottom:.15rem; color: var(--ink) !important; }
        .alert .dot { margin-top:.35rem; }

        .code-label { display:flex; justify-content:space-between; align-items:center; margin:1.25rem 0 .45rem; color: var(--muted) !important; font-family:'DM Mono',monospace; font-size:.68rem; text-transform:uppercase; letter-spacing:.08em; }
        .code-label span { color: var(--muted) !important; }

        .tag { display:inline-flex; align-items:center; background: var(--tint); border-radius:100px; padding:.35rem .7rem; color: var(--muted) !important; font-size:.68rem; font-family:'DM Mono',monospace; margin:.15rem .15rem .15rem 0; }
        .tag-black { background: var(--ink); color: var(--paper) !important; }

        .team-header { display:flex; justify-content:space-between; align-items:center; gap:1rem; }
        .team-name { font-size:1.1rem; font-weight:800; letter-spacing:-.04em; color: var(--ink) !important; }
        .team-meta { color: var(--muted) !important; font-size:.76rem; margin-top:.25rem; }
        .engagement { background: var(--ink); border-radius:12px; padding:.6rem .8rem; text-align:right; white-space:nowrap; }
        .engagement small { display:block; color: var(--paper) !important; opacity:.6; font-size:.57rem; font-family:'DM Mono',monospace; text-transform:uppercase; letter-spacing:.08em; }
        .engagement b { color: var(--paper) !important; font-size:1.05rem; letter-spacing:-.05em; }

        .section-rule { height:1px; background: var(--line); margin:1.7rem 0; }
        .sidebar-copy { color: var(--muted) !important; font-size:.72rem; line-height:1.55; margin-top:.75rem; }
        .legend-row { display:flex; align-items:center; gap:.6rem; color: var(--muted) !important; font-size:.68rem; margin:.55rem 0; }
        .footer-note { color: var(--muted-soft) !important; text-align:center; font: .65rem 'DM Mono', monospace; margin-top:3rem; }

        /* Table (custom HTML so light/dark both render correctly) */
        .table-wrap { overflow-x:auto; }
        .data-table { width:100%; border-collapse:collapse; font-size:.78rem; }
        .data-table th { text-align:left; color: var(--muted) !important; font: 500 .62rem 'DM Mono', monospace; text-transform:uppercase; letter-spacing:.12em; padding:.55rem .6rem; border-bottom:1px solid var(--line-strong); }
        .data-table td { color: var(--ink) !important; padding:.75rem .6rem; border-bottom:1px solid var(--line); }
        .data-table tr:last-child td { border-bottom:0; }

        /* Primary button: black text on white, pill, Apple-style */
        button[data-testid="stBaseButton-primary"], .stButton > button[kind="primary"] {
            background: var(--paper) !important;
            color: var(--ink) !important;
            border: 1px solid var(--line-strong) !important;
            border-radius: 100px !important;
            padding: .85rem 1.5rem !important;
            font-weight: 700 !important;
            letter-spacing: -.01em;
            box-shadow: var(--shadow-soft) !important;
            transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
        }
        button[data-testid="stBaseButton-primary"] p, .stButton > button[kind="primary"] p { color: var(--ink) !important; }
        button[data-testid="stBaseButton-primary"]:hover, .stButton > button[kind="primary"]:hover {
            transform: translateY(-1px);
            border-color: var(--ink) !important;
            box-shadow: var(--shadow) !important;
        }

        /* Sidebar toggle: invisible click target sitting exactly on the header's cursive W */
        [data-testid="stSidebar"], [data-testid="stSidebar"] > div:first-child { transition:width .38s cubic-bezier(.2,.75,.2,1), transform .38s cubic-bezier(.2,.75,.2,1); }
        [data-testid="stSidebarCollapseButton"], [data-testid="stSidebarCollapsedControl"], [data-testid="collapsedControl"] {
            position:fixed !important; top:.48rem !important; left:1.1rem !important;
            z-index:2147483001 !important; visibility:visible !important; opacity:1 !important;
        }
        [data-testid="stSidebarCollapseButton"] button, [data-testid="stSidebarCollapsedControl"] button, [data-testid="collapsedControl"] button,
        button[aria-label="Close sidebar"], button[aria-label="Open sidebar"] {
            width:44px !important; height:44px !important; border-radius:12px !important;
            background: transparent !important; border:0 !important; outline:0 !important;
            box-shadow:none !important; padding:0 !important; cursor:pointer;
        }
        [data-testid="stSidebarCollapseButton"] button:focus-visible, [data-testid="stSidebarCollapsedControl"] button:focus-visible, [data-testid="collapsedControl"] button:focus-visible {
            outline: 2px solid var(--ink) !important; outline-offset: 2px;
        }
        [data-testid="stSidebarCollapseButton"] svg, [data-testid="stSidebarCollapsedControl"] svg, [data-testid="collapsedControl"] svg,
        [data-testid="stSidebarCollapseButton"] [data-testid="stIconMaterial"], [data-testid="stSidebarCollapsedControl"] [data-testid="stIconMaterial"],
        button[aria-label="Close sidebar"] svg, button[aria-label="Open sidebar"] svg { display:none !important; }
        [data-testid="stSidebarCollapseButton"] button::before, [data-testid="stSidebarCollapsedControl"] button::before, [data-testid="collapsedControl"] button::before,
        button[aria-label="Close sidebar"]::before, button[aria-label="Open sidebar"]::before { content:none !important; }

        [data-testid="stPopover"] { position:fixed !important; top:.42rem !important; right:2rem !important; left:auto !important; width:32px !important; height:32px !important; z-index:2147483002 !important; }
        [data-testid="stPopover"] > div, [data-testid="stPopover"] button[data-testid="stPopoverButton"] { width:32px !important; height:32px !important; }
        [data-testid="stPopover"] button[data-testid="stPopoverButton"] {
            padding:0 !important; border-radius:0 !important; border:0 !important; outline:0 !important;
            background:transparent !important; color: var(--ink) !important; box-shadow:none !important; font-size:1rem !important;
        }
        [data-testid="stPopover"] button[data-testid="stPopoverButton"] p { display:none !important; }
        [data-testid="stPopover"] button[data-testid="stPopoverButton"] [data-testid="stIconMaterial"] { color: var(--ink) !important; font-size:1.3rem !important; }
        [data-testid="stPopover"] button[data-testid="stPopoverButton"]:hover { transform:translateY(-1px); }

        .accessibility-title { font-size:.95rem; font-weight:800; letter-spacing:-.03em; margin-bottom:.2rem; color: var(--ink) !important; }
        .accessibility-copy { color: var(--muted) !important; font-size:.72rem; line-height:1.5; margin-bottom:.8rem; }

        @media (max-width: 900px) {
            .block-container { padding:5rem 1.1rem 4rem; }
            .hero-row { flex-direction:column; }
            .stat-grid { grid-template-columns:1fr; }
            .overlay-context { display:none; }
            [data-testid="stPopover"] { right:2rem !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    accessibility_css = []
    if st.session_state.get("accessibility_dark_mode", False):
        # Same black/white palette, inverted. Only the variables change.
        accessibility_css.append(
            """
            :root {
                --ink: #ffffff;
                --paper: #000000;
                --line: rgba(255,255,255,.12);
                --line-strong: rgba(255,255,255,.24);
                --muted: rgba(255,255,255,.62);
                --muted-soft: rgba(255,255,255,.42);
                --glass: rgba(28,28,30,.72);
                --glass-strong: rgba(18,18,20,.88);
                --sidebar-bg: rgba(30,30,32,.92);
                --tint: rgba(255,255,255,.08);
                --code-bg: #1c1c1e;
                --code-fg: #ffffff;
                --code-dim: rgba(255,255,255,.5);
                --shadow: 0 20px 60px rgba(0,0,0,.6);
                --shadow-soft: 0 8px 28px rgba(0,0,0,.5);
            }
            [data-testid="stToggle"] [role="switch"], [data-baseweb="checkbox"] > div { border-color: var(--ink) !important; }
            """
        )
    if st.session_state.get("accessibility_high_contrast", False):
        accessibility_css.append(
            """
            :root { --muted: var(--ink); --muted-soft: var(--ink); --line: var(--ink); --line-strong: var(--ink); }
            .surface, .surface-tight, .metric-card, .alert, [data-testid="stVerticalBlockBorderWrapper"], [data-testid="stExpander"] { border: 2px solid var(--ink) !important; }
            """
        )
    if st.session_state.get("accessibility_large_text", False):
        accessibility_css.append(
            """ .subhead { font-size:1.08rem; } .status-line, .team-meta, .stat-caption, .legend-row, .sidebar-copy { font-size:.9rem; } .data-table { font-size:.92rem; } """
        )
    if st.session_state.get("accessibility_reduce_motion", False):
        accessibility_css.append(
            """ *, *::before, *::after { animation:none !important; transition:none !important; scroll-behavior:auto !important; } """
        )
    if accessibility_css:
        st.markdown("<style>" + "".join(accessibility_css) + "</style>", unsafe_allow_html=True)


def sidebar() -> str:
    with st.sidebar:
        st.markdown('<div class="brand"><span class="brand-mark">W</span><span class="brand-name">WINTERFALLX</span></div>', unsafe_allow_html=True)
        st.markdown(
            '<p class="sidebar-copy">Autonomous review memory for teams that want every incident to make the next commit safer.</p>',
            unsafe_allow_html=True,
        )
        st.markdown("<div style='height:1.5rem'></div>", unsafe_allow_html=True)
        st.markdown('<div class="surface-label">Workspace</div>', unsafe_allow_html=True)
        mode = st.radio(
            "Workspace view",
            ["Developer Sandbox", "Company Executive Dashboard"],
            label_visibility="collapsed",
        )
        st.markdown("<div style='height:1.2rem'></div>", unsafe_allow_html=True)
        st.markdown('<div class="surface-label">Signal legend</div>', unsafe_allow_html=True)
        for dot_class, label in [
            ("dot-red", "Runtime anomaly / cache miss"),
            ("dot-yellow", "Team convention mismatch"),
            ("dot-blue", "Recall / retain in progress"),
            ("dot-green", "Memory hit / remediation"),
        ]:
            st.markdown(
                f'<div class="legend-row"><span class="dot {dot_class}"></span>{label}</div>',
                unsafe_allow_html=True,
            )
        st.markdown("<div style='height:1.5rem'></div>", unsafe_allow_html=True)
        st.markdown(
            '<div class="status-line"><span class="dot dot-green"></span><span>Ledger online · Hindsight Cloud</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="engine-note"><span class="hindsight-word">Hindsight</span> ENGINE / cloud</div>',
            unsafe_allow_html=True,
        )
    return mode


def highlight_hindsight(value: str) -> str:
    return html.escape(value).replace(
        "Hindsight", '<span class="hindsight-word">Hindsight</span>'
    )


def render_accessibility_controls() -> None:
    with st.popover("Accessibility", icon=":material/accessibility:", help="Accessibility settings"):
        st.markdown('<div class="accessibility-title">Accessibility</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="accessibility-copy">Adjust the workspace without leaving the review.</div>',
            unsafe_allow_html=True,
        )
        st.toggle("Dark mode", key="accessibility_dark_mode")
        st.toggle("High contrast", key="accessibility_high_contrast")
        st.toggle("Larger text", key="accessibility_large_text")
        st.toggle("Reduce motion", key="accessibility_reduce_motion")


def render_header(title: str, description: str, label: str) -> None:
    st.markdown(
        """
        <div class="app-overlay">
            <div class="overlay-wordmark"><b>W</b><span class="overlay-name">WinterFallX</span><span class="overlay-context">Hindsight workspace</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    render_accessibility_controls()
    st.markdown(
        f"""
        <div class="hero-row">
            <div><div class="hero-sage">A smart code review agent that actually remembers and evolves.</div><div class="eyebrow">{highlight_hindsight(label)}</div><h1>{highlight_hindsight(title)}</h1>
            <p class="subhead">{highlight_hindsight(description)}</p></div>
            <div class="hero-meta"><span class="dot dot-green"></span>LIVE LEDGER&nbsp;&nbsp;·&nbsp;&nbsp;{datetime.now().strftime('%d %b %Y').upper()}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_metrics(rows: list[sqlite3.Row], team_name: str) -> None:
    team_rows = [row for row in rows if row["team_name"] == team_name]
    repeats = sum(max(0, int(row["feedback_count"]) - 1) for row in team_rows)
    unique_files = len({row["file_name"] for row in team_rows})
    st.markdown(
        f"""
        <div class="stat-grid">
            <div class="metric-card"><div class="metric-kicker">Memory bank</div><div class="stat-value">{len(team_rows):02d}</div><div class="stat-caption">retained incidents for this team</div></div>
            <div class="metric-card"><div class="metric-kicker">Repeat feedback</div><div class="stat-value">{repeats:02d}</div><div class="stat-caption">avoidable review cycles identified</div></div>
            <div class="metric-card"><div class="metric-kicker">Files observed</div><div class="stat-value">{unique_files:02d}</div><div class="stat-caption">surfaces connected to team memory</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def run_review(team_name: str, developer_id: str, selected: str, code_snippet: str) -> dict[str, Any]:
    scenario = SCENARIOS[selected]
    bank_id = f"winterfallx-{team_name.strip()}-memory"
    ledger = HindsightLedger(bank_id)
    existing, memory_texts = ledger.recall(team_name, scenario["file"], code_snippet, scenario["error"])
    log_lines = [
        f"Hindsight.recall(bank_id='{bank_id}', query='{scenario['file']}: {scenario['error']}')",
        f"→ {len(memory_texts)} memory match(es) returned from Hindsight Cloud",
    ]
    if existing:
        repeat_count = ledger.increment_repeat(existing["id"])
        log_lines.extend(
            [
                f"→ context match found in incident #{existing['id']}",
                f"→ Hindsight.retain() skipped, prior resolution promoted (repeat count: {repeat_count})",
            ]
        )
        return {
            "kind": "hit",
            "logs": log_lines,
            "repeat_count": repeat_count,
            "resolution": existing["resolution"],
            "preference": existing["preference_flag"],
            "incident_id": existing["id"],
            "scenario": scenario,
        }

    log_lines.extend(
        [
            "→ no matching incident on record for this team",
            f"→ Hindsight.retain(bank_id='{bank_id}') storing this incident as a new memory",
        ]
    )
    incident_id = ledger.retain(
        {
            "timestamp": utc_now(),
            "team_name": team_name,
            "developer_id": developer_id,
            "file_name": scenario["file"],
            "code_snippet": code_snippet,
            "error_msg": scenario["error"],
            "resolution": scenario["resolution"],
            "standard_violation": scenario["violation"],
            "preference_flag": "prefer-explicit-remediation",
        }
    )
    return {
        "kind": "miss",
        "logs": log_lines,
        "repeat_count": 0,
        "resolution": scenario["resolution"],
        "preference": "prefer-explicit-remediation",
        "incident_id": incident_id,
        "scenario": scenario,
    }


def render_logs(lines: list[str], kind: str) -> None:
    output = []
    for line in lines:
        cls = "log-dim" if line.startswith("→") else ""
        output.append(f'<div class="{cls}">{html.escape(line)}</div>')
    st.markdown(f'<div class="log-box">{"".join(output)}</div>', unsafe_allow_html=True)


def render_review_result(result: dict[str, Any]) -> None:
    scenario = result["scenario"]
    if result["kind"] == "hit":
        st.markdown(
            f"""
            <div class="alert"><span class="dot dot-green"></span><div>
                <strong>Memory match found</strong>
                Retrieved context from this team's Hindsight memory bank and applied the prior resolution.
                <br><span class="alert-note">This convention mistake has repeated <b>{result['repeat_count']} times</b> across the team workspace.</span>
            </div></div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
            <div class="alert"><span class="dot dot-red"></span><div>
                <strong>No prior match on record</strong>
                Hindsight search returned no matching context. Retaining this incident as a new team memory.
            </div></div>
            """,
            unsafe_allow_html=True,
        )
        progress = st.progress(0, text="Synthesizing fault context…")
        for value, text in [(32, "Mapping failure surface…"), (68, "Comparing team conventions…"), (100, "Retaining learned preference…")]:
            progress.progress(value, text=text)
            time.sleep(0.25)

    st.markdown('<div class="eyebrow" style="margin-top:1.55rem">Review synthesis</div>', unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="surface">
            <div class="surface-label">What went wrong & where</div>
            <h3>{html.escape(scenario['file'])}</h3>
            <p class="body-note">{html.escape(scenario['error'])}</p>
            <div class="tag"><span class="dot dot-yellow" style="margin-right:.45rem"></span>{html.escape(scenario['violation'])}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""
        <div class="surface" style="margin-top:1rem">
            <div class="surface-label">Refactored remediation code</div>
            <pre class="code-pre">{html.escape(result['resolution'])}</pre>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""
        <div class="surface" style="margin-top:1rem">
            <div class="surface-label">Team coding standard learning guide</div>
            <div class="status-line"><span class="dot dot-yellow"></span><span>{html.escape(scenario['guide'])}</span></div>
            <div style="margin-top:.9rem"><span class="tag tag-black">TEAM PREFERENCE · {html.escape(result['preference'])}</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def developer_view() -> None:
    rows = all_rows()
    render_header(
        "Make every review compound.",
        "WINTERFALLX turns every code review into durable Hindsight context, so repetitive failures stop costing your best engineers time.",
        "Developer workspace / autonomous reviewer",
    )
    with st.container(border=True):
        st.markdown('<div class="surface-label">Team identity</div>', unsafe_allow_html=True)
        team_name = st.text_input(
            "Enter Team Name",
            value="AlphaBuilders",
            label_visibility="collapsed",
            placeholder="Enter Team Name",
        )
        developer_id = st.text_input(
            "Enter Developer ID",
            value="dev-042",
            label_visibility="collapsed",
            placeholder="Enter Developer ID",
        )
        bank_id = f"winterfallx-{team_name.strip() or 'team'}-memory"
        st.markdown(
            f'<div class="status-line" style="margin-top:.85rem"><span class="dot dot-blue"></span><span><span class="hindsight-word">Hindsight</span> memory bank <b>{html.escape(bank_id)}</b></span></div>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown('<div class="surface-label">Fault pattern</div>', unsafe_allow_html=True)
        selected = st.selectbox("Production failure scenario", list(SCENARIOS), label_visibility="collapsed")
        scenario = SCENARIOS[selected]
        st.markdown(
            f'<div class="status-line" style="margin-top:.75rem"><span class="dot dot-yellow"></span><span>{html.escape(scenario["error"])}</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="code-label"><span>Raw broken code · editable</span><span>source / paste</span></div>',
            unsafe_allow_html=True,
        )
        code_snippet = st.text_area(
            "Raw broken code",
            value=scenario["code"],
            height=210,
            label_visibility="collapsed",
        )
        run = st.button("⚡ Run AI Code Review Pipeline", type="primary", width="stretch")
    if run:
        if not team_name.strip() or not developer_id.strip() or not code_snippet.strip():
            st.error("Team name, developer ID, and code are required to create a memory bank.")
        else:
            with st.spinner("Running recall → synthesis → retain…"):
                st.session_state["last_review"] = run_review(
                    team_name.strip(), developer_id.strip(), selected, code_snippet
                )
            st.rerun()

    st.markdown("<div style='height:1.15rem'></div>", unsafe_allow_html=True)
    render_metrics(rows, team_name)

    result = st.session_state.get("last_review")
    if result:
        st.markdown("<div class='section-rule'></div>", unsafe_allow_html=True)
        st.markdown('<div class="eyebrow">Pipeline trace / latest run</div>', unsafe_allow_html=True)
        render_logs(result["logs"], result["kind"])
        render_review_result(result)
    else:
        st.markdown("<div class='section-rule'></div>", unsafe_allow_html=True)
        st.markdown(
            '<div class="surface-tight"><div class="status-line"><span class="dot dot-blue"></span><span>Ready to recall. Run the pipeline to compare this code against your team’s accumulated standards.</span></div></div>',
            unsafe_allow_html=True,
        )
    st.markdown('<div class="footer-note">LOCAL LEDGER · EVERY REVIEW IS A FUTURE SIGNAL</div>', unsafe_allow_html=True)


def team_summary(team_name: str, rows: list[sqlite3.Row]) -> tuple[float, list[sqlite3.Row]]:
    team_rows = [row for row in rows if row["team_name"] == team_name]
    base = TEAM_HOURS.get(team_name, 2.0)
    hours = base + max(0, len(team_rows) - (2 if team_name == "AlphaBuilders" else 1)) * 0.75
    return hours, team_rows


def render_feedback_table(team_rows: list[sqlite3.Row]) -> None:
    body = "".join(
        f"<tr><td>{html.escape(row['file_name'])}</td><td>{html.escape(row['standard_violation'])}</td><td>{int(row['feedback_count'])}</td></tr>"
        for row in sorted(team_rows, key=lambda item: int(item["feedback_count"]), reverse=True)
    )
    st.markdown(
        f"""
        <div class="table-wrap"><table class="data-table">
            <thead><tr><th>File surface</th><th>Standard at risk</th><th>Feedback loops</th></tr></thead>
            <tbody>{body}</tbody>
        </table></div>
        """,
        unsafe_allow_html=True,
    )


def executive_view() -> None:
    rows = all_rows()
    teams = sorted({row["team_name"] for row in rows}, key=lambda name: team_summary(name, rows)[0], reverse=True)
    render_header(
        "Know what your teams are doing",
        "A cross-team view of the errors, standards, and review cycles WINTERFALLX is converting into institutional Hindsight memory.",
        "Company executive dashboard / portfolio intelligence",
    )
    st.markdown('<div class="eyebrow">Team engagement ranking / highest to lowest</div>', unsafe_allow_html=True)
    for index, team in enumerate(teams, 1):
        hours, team_rows = team_summary(team, rows)
        developers = sorted({row["developer_id"] for row in team_rows})
        with st.expander(f"{index:02d}  {team}  ·  {len(team_rows)} tracked incidents", expanded=index == 1):
            st.markdown(
                f"""
                <div class="team-header">
                    <div><div class="team-name">{html.escape(team)}</div><div class="team-meta">{len(developers)} active developers · {len({row['file_name'] for row in team_rows})} affected files</div></div>
                    <div class="engagement"><small>Platform engagement</small><b>⏳ {hours:.1f} hrs</b></div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.markdown("<div class='section-rule'></div>", unsafe_allow_html=True)
            col_a, col_b = st.columns([1, 1.8], gap="large")
            with col_a:
                st.markdown('<div class="surface-label">Active developer IDs</div>', unsafe_allow_html=True)
                for developer in developers:
                    st.markdown(f'<div class="status-line" style="margin:.55rem 0"><span class="dot dot-blue"></span><span>{html.escape(developer)}</span></div>', unsafe_allow_html=True)
                st.markdown('<div class="surface-label" style="margin-top:1.25rem">Preference footprint</div>', unsafe_allow_html=True)
                preferences = sorted({row["preference_flag"] for row in team_rows if row["preference_flag"]})
                for pref in preferences:
                    st.markdown(f'<div class="tag">{html.escape(pref)}</div>', unsafe_allow_html=True)
            with col_b:
                st.markdown('<div class="surface-label">Systemic feedback loop repetition</div>', unsafe_allow_html=True)
                violations: dict[str, int] = {}
                for row in team_rows:
                    violations[row["standard_violation"]] = violations.get(row["standard_violation"], 0) + int(row["feedback_count"])
                render_feedback_table(team_rows)
                highest = max(violations.items(), key=lambda item: item[1])
                st.markdown(
                    f'<div class="alert"><span class="dot dot-yellow"></span><div><strong>Management signal</strong>{html.escape(highest[0])} is the dominant repeated convention with {highest[1]} feedback events.</div></div>',
                    unsafe_allow_html=True,
                )
    total_repeats = sum(max(0, int(row["feedback_count"]) - 1) for row in rows)
    st.markdown("<div class='section-rule'></div>", unsafe_allow_html=True)
    st.markdown('<div class="eyebrow">Portfolio health / Hindsight signal</div>', unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="stat-grid">
            <div class="metric-card"><div class="metric-kicker">Active teams</div><div class="stat-value">{len(teams):02d}</div><div class="stat-caption">profiles contributing to the ledger</div></div>
            <div class="metric-card"><div class="metric-kicker">Tracked incidents</div><div class="stat-value">{len(rows):02d}</div><div class="stat-caption">review events across the portfolio</div></div>
            <div class="metric-card"><div class="metric-kicker">Cycles prevented</div><div class="stat-value">{total_repeats:02d}</div><div class="stat-caption">repetitions surfaced by memory</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<div class="footer-note">EXECUTIVE VIEW · MEMORY IS A TEAM ASSET</div>', unsafe_allow_html=True)


def main() -> None:
    st.set_page_config(page_title="WINTERFALLX", page_icon="◉", layout="wide", initial_sidebar_state="expanded")
    init_database()
    inject_styles()
    mode = sidebar()
    if mode == "Developer Sandbox":
        developer_view()
    else:
        executive_view()


if __name__ == "__main__":
    main()


