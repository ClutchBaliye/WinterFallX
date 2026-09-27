"""WINTERFALLX — autonomous code review and repetitive-error intelligence.

The app intentionally keeps the product surface in one file so it can be
copied into a fresh Replit project and run with the requested Streamlit
command. HindsightLedger mirrors the recall/retain contract locally with
SQLite, which keeps the demo deterministic and useful without an API key.
"""

from __future__ import annotations

import hashlib
import html
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st


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
    """A small local adapter exposing the same operational shape as Hindsight."""

    def __init__(self, bank_id: str):
        self.bank_id = bank_id

    def recall(self, team_name: str, file_name: str, code_snippet: str, error_msg: str) -> sqlite3.Row | None:
        target = signature_for(team_name, file_name, code_snippet, error_msg)
        with db_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM incidents WHERE team_name = ? ORDER BY id DESC",
                (team_name,),
            ).fetchall()
        return next((row for row in rows if row_signature(row) == target), None)

    def retain(self, payload: dict[str, Any]) -> int:
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


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Bodoni+Moda:ital,wght@0,400;0,700;1,400&family=DM+Mono:wght@400;500&family=Lobster...&family=Manrope:wght@400;500;600;700;800&display=swap');
        :root { --ink: #101010; --muted: #767676; --line: #ececec; --soft: #f7f7f7; --shadow: 0 14px 40px rgba(0,0,0,.055); }
        html, body, [class*="css"] { font-family: 'Manrope', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: var(--ink); }
        .stApp { background: #fff; }
        [data-testid="stAppViewContainer"] { background: #fff; }
        [data-testid="stHeader"] { background: transparent !important; z-index:1000; }
        [data-testid="stSidebar"] { background: #fafafa; border-right: 1px solid #f0f0f0; z-index:900 !important; }
        [data-testid="stSidebar"] > div:first-child { padding: 2rem 1.25rem; }
        .block-container { max-width: 1440px; padding: 5.5rem 4.5rem 5rem; }
        h1, h2, h3, h4, p { letter-spacing: -.02em; }
        h1 { font-weight: 800; font-size: clamp(2.1rem, 3vw, 3.45rem); line-height: .98; letter-spacing: -.075em; margin: 0; }
        h2 { font-size: 1.4rem; font-weight: 800; margin: 0; }
        h3 { font-size: 1rem; font-weight: 800; margin: 0; }
        .brand { display:flex; align-items:center; gap:.6rem; color:#111; }
        .brand-mark { width:30px; height:30px; display:grid; place-items:center; color:#111; font:400 1.55rem/1 "Lobster", "Brush Script MT", "Segoe Script", cursive; letter-spacing:-.12em; }
        .brand-name { color:#111; font:400 1.12rem/1 "Lobster", "Brush Script MT", "Segoe Script", cursive; letter-spacing:-.025em; }
        .overlay-name { color: navyblue; font:900 1.12rem/1 "Monospace", Arial, sans-serif; letter-spacing:-.02em; }
        .app-overlay { position:fixed; top:0; left:0; right:0; height:3.65rem; z-index:10000; display:flex; align-items:center; padding:0 1.35rem; background:rgba(255,255,255,.86); border-bottom:1px solid rgba(0,0,0,.07); backdrop-filter:blur(18px); -webkit-backdrop-filter:blur(18px); pointer-events:none; }
        .overlay-wordmark { display:flex; align-items:center; gap:.6rem; margin-left:0; color:#111; }
        .overlay-wordmark b { display:grid; place-items:center; width:28px; height:28px; color:#111; font:400 1.7rem/1 "Lobster", "Brush Script MT", "Segoe Script", cursive; letter-spacing:-.12em; }
        .overlay-context { color:#8b8b8b; font:500 .58rem 'DM Mono', monospace; letter-spacing:.13em; text-transform:uppercase; }
        .eyebrow { color:#565656; font-size:.74rem; font-family:'DM Mono', monospace; text-transform:uppercase; letter-spacing:.16em; font-weight:600; margin-bottom:1.3rem; }
        .subhead { color:#707070; line-height:1.6; font-size:.94rem; max-width:650px; margin-top:1rem; }
        .hindsight-word { color:#111; font-weight:800; text-decoration:underline; text-decoration-thickness:2px; text-underline-offset:4px; }
        .surface { background:#fff; border-radius:12px; box-shadow:var(--shadow); padding:1.45rem; border:1px solid #f5f5f5; }
        .surface-tight { background:#fff; border-radius:12px; box-shadow:var(--shadow); padding:1.05rem 1.2rem; border:1px solid #f5f5f5; }
        [data-testid="stVerticalBlockBorderWrapper"] { border:1px solid #f4f4f4; border-radius:12px; box-shadow:var(--shadow); padding:1.15rem; background:#fff; }
        [data-testid="stVerticalBlockBorderWrapper"] > div { border:0; }
        [data-testid="stTextInput"] input, [data-testid="stSelectbox"] > div { border-radius:9px; border-color:#ededed; background:#fafafa; }
        [data-testid="stTextArea"] textarea { border-radius:9px; border:1px solid #243b53; background:#0d1b2a !important; color:#dbeafe !important; caret-color:#fff; font:500 .76rem/1.7 "DM Mono", monospace; }
        [data-testid="stTextInput"] input:focus, [data-testid="stTextArea"] textarea:focus { border-color:#a9a9a9; box-shadow:0 0 0 1px #a9a9a9; }
        .surface-label { color:#8b8b8b; text-transform:uppercase; font-family:'DM Mono',monospace; letter-spacing:.12em; font-size:.62rem; margin-bottom:.55rem; }
        .status-line { display:flex; align-items:center; gap:.55rem; font-size:.78rem; color:#555; }
        .dot { display:inline-block; width:8px; height:8px; border-radius:50%; flex:0 0 8px; }
        .dot-red { background:#ef4f4f; box-shadow:0 0 0 4px #fff0f0; }
        .dot-yellow { background:#e6b82c; box-shadow:0 0 0 4px #fff9e5; }
        .dot-blue { background:#367eea; box-shadow:0 0 0 4px #edf4ff; }
        .dot-green { background:#35ad67; box-shadow:0 0 0 4px #ecfbf2; }
        .hero-row { display:flex; justify-content:space-between; align-items:flex-start; gap:1rem; margin-bottom:2rem; }
        .hero-meta { display:flex; gap:.5rem; align-items:center; padding-top:.2rem; color:#777; font-size:.72rem; font-family:'DM Mono',monospace; }
        .stat-value { font-size:2rem; font-weight:800; letter-spacing:-.06em; margin:.1rem 0 .25rem; }
        .stat-caption { font-size:.74rem; color:#777; }
        .stat-grid { display:grid; grid-template-columns:repeat(3, 1fr); gap:.8rem; margin:1.4rem 0 1.8rem; }
        .metric-card { padding:1.05rem 1.1rem; border-radius:12px; background:#fafafa; border:1px solid #f1f1f1; }
        .metric-card .metric-kicker { color:#888; font-size:.65rem; text-transform:uppercase; letter-spacing:.1em; font-family:'DM Mono',monospace; }
        .log-box { background:#111; color:#e8e8e8; border-radius:12px; padding:1rem 1.1rem; font-family:'DM Mono',monospace; font-size:.69rem; line-height:1.8; box-shadow:var(--shadow); }
        .log-blue { color:#8cb4ff; } .log-green { color:#8ce1ab; } .log-red { color:#ff8e8e; } .log-muted { color:#989898; }
        .alert { display:flex; align-items:flex-start; gap:.75rem; border-radius:12px; padding:1rem 1.1rem; margin:1rem 0; font-size:.82rem; line-height:1.5; }
        .alert-green { background:#effaf2; color:#195d33; } .alert-red { background:#fff2f1; color:#7f2525; } .alert-yellow { background:#fff9e5; color:#6f5600; } .alert-blue { background:#eef5ff; color:#1d4e9b; }
        .alert strong { display:block; font-weight:800; margin-bottom:.12rem; }
        .code-label { display:flex; justify-content:space-between; align-items:center; margin:1.25rem 0 .45rem; color:#777; font-family:'DM Mono',monospace; font-size:.68rem; text-transform:uppercase; letter-spacing:.08em; }
        .tag { display:inline-flex; align-items:center; background:#f1f1f1; border-radius:100px; padding:.35rem .65rem; color:#5f5f5f; font-size:.68rem; font-family:'DM Mono',monospace; }
        .tag-black { background:#111; color:#fff; }
        .team-header { display:flex; justify-content:space-between; align-items:center; gap:1rem; }
        .team-name { font-size:1.1rem; font-weight:800; letter-spacing:-.04em; }
        .team-meta { color:#888; font-size:.76rem; margin-top:.25rem; }
        .engagement { background:#111; color:#fff; border-radius:8px; padding:.55rem .7rem; text-align:right; white-space:nowrap; }
        .engagement small { display:block; color:#a9a9a9; font-size:.57rem; font-family:'DM Mono',monospace; text-transform:uppercase; letter-spacing:.08em; }
        .engagement b { font-size:1.05rem; letter-spacing:-.05em; }
        .section-rule { height:1px; background:#f0f0f0; margin:1.7rem 0; }
        .sidebar-copy { color:#7f7f7f; font-size:.72rem; line-height:1.55; margin-top:.75rem; }
        .legend-row { display:flex; align-items:center; gap:.6rem; color:#777; font-size:.68rem; margin:.55rem 0; }
        .footer-note { color:#a0a0a0; text-align:center; font: .65rem 'DM Mono', monospace; margin-top:3rem; }
        .hero-row { padding-top:1rem; }
        .hero-sage { color:#111; font:400 clamp(2.1rem, 3vw, 3.45rem)/.98 "Sage", cursive; letter-spacing:-.075em; margin:0 0 1.3rem; }
        [data-testid="stSidebar"], [data-testid="stSidebar"] > div:first-child { transition:width .38s cubic-bezier(.2,.75,.2,1), transform .38s cubic-bezier(.2,.75,.2,1); }
        [data-testid="stSidebarCollapseButton"], [data-testid="stSidebarCollapsedControl"] { position:fixed !important; top:.42rem !important; left:1.15rem !important; z-index:10006 !important; }
        [data-testid="stSidebarCollapseButton"] button, [data-testid="stSidebarCollapsedControl"] button, button[aria-label="Close sidebar"], button[aria-label="Open sidebar"] { width:38px !important; height:38px !important; border-radius:50% !important; background:#fff !important; border:1px solid #111 !important; box-shadow:0 6px 18px rgba(0,0,0,.09) !important; padding:0 !important; }
        [data-testid="stSidebarCollapseButton"] svg, [data-testid="stSidebarCollapsedControl"] svg, [data-testid="stSidebarCollapseButton"] [data-testid="stIconMaterial"], [data-testid="stSidebarCollapsedControl"] [data-testid="stIconMaterial"], button[aria-label="Close sidebar"] svg, button[aria-label="Open sidebar"] svg { display:none !important; }
        [data-testid="stSidebarCollapseButton"] button::before, [data-testid="stSidebarCollapsedControl"] button::before, button[aria-label="Close sidebar"]::before, button[aria-label="Open sidebar"]::before { content:"W"; display:block; color:#fff; -webkit-text-stroke:1px #111; font:900 1rem Georgia, serif; line-height:1; }
        [data-testid="stPopover"] { position:fixed !important; top:.42rem !important; right:2rem !important; left:auto !important; width:32px !important; height:32px !important; z-index:10006 !important; }
        [data-testid="stPopover"] > div, [data-testid="stPopover"] button[data-testid="stPopoverButton"] { width:32px !important; height:32px !important; }
        [data-testid="stPopover"] button[data-testid="stPopoverButton"] { padding:0 !important; border-radius:0 !important; border:0 !important; outline:0 !important; background:transparent !important; color:#111 !important; box-shadow:none !important; font-size:1rem !important; }
        [data-testid="stPopover"] button[data-testid="stPopoverButton"] p { display:none !important; }
        [data-testid="stPopover"] button[data-testid="stPopoverButton"] [data-testid="stIconMaterial"] { color:#111 !important; font-size:1.3rem !important; }
        [data-testid="stPopover"] button[data-testid="stPopoverButton"]:hover { transform:translateY(-1px); }
        .accessibility-title { font-size:.95rem; font-weight:800; letter-spacing:-.03em; margin-bottom:.2rem; }
        .accessibility-copy { color:#777; font-size:.72rem; line-height:1.5; margin-bottom:.8rem; }
        @media (max-width: 900px) { .block-container { padding:5rem 1.1rem 4rem; } .hero-row { flex-direction:column; } .stat-grid { grid-template-columns:1fr; } .overlay-context { display:none; } [data-testid="stPopover"] { right:2rem !important; } }
        </style>
        """,
        unsafe_allow_html=True,
    )
    accessibility_css = []
    if st.session_state.get("accessibility_dark_mode", False):
        accessibility_css.append(
            """
            html, body, [class*="css"] { color:#f5f5f5 !important; }
            .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"], [data-testid="stHeader"] { background:#101010 !important; }
            [data-testid="stSidebar"] { background:#171717 !important; border-color:#2b2b2b !important; }
            .app-overlay { background:rgba(16,16,16,.86); border-color:#2c2c2c; }
            .overlay-wordmark, .overlay-wordmark b, .overlay-name, .brand-name, .brand-mark, .hero-sage, h1, h2, h3, .hindsight-word { color:#fff !important; }
            .overlay-context, .subhead, .hero-meta, .team-meta, .stat-caption, .sidebar-copy, .legend-row, .status-line, .surface-label, .eyebrow { color:#aaa !important; }
            .surface, .surface-tight, [data-testid="stVerticalBlockBorderWrapper"], .metric-card { background:#191919 !important; border-color:#2c2c2c !important; }
            .surface p, .surface pre, .surface-tight, .metric-card, .team-name, .metric-kicker, .code-label, [data-testid="stWidgetLabel"] p, [data-testid="stRadio"] label, [data-testid="stToggle"] label, [data-testid="stToggle"] p, [role="dialog"] p { color:#f5f5f5 !important; }
            [data-testid="stTextInput"] input, [data-testid="stSelectbox"] > div { background:#222 !important; color:#fff !important; border-color:#3a3a3a !important; }
            [data-testid="stTextArea"] textarea { background:#0c2238 !important; color:#dbeafe !important; border-color:#315a7d !important; }
            [data-testid="stSelectbox"] svg { color:#fff !important; }
            [role="dialog"] { background:#191919 !important; color:#f5f5f5 !important; border-color:#3a3a3a !important; }
            [data-testid="stPopover"] button[data-testid="stPopoverButton"] [data-testid="stIconMaterial"] { color:#fff !important; }
            .section-rule { background:#2c2c2c; }
            [data-testid="stSidebarCollapseButton"] button, [data-testid="stSidebarCollapsedControl"] button, button[aria-label="Close sidebar"], button[aria-label="Open sidebar"] { background:#191919 !important; border-color:#fff !important; }
            """
        )
    if st.session_state.get("accessibility_high_contrast", False):
        accessibility_css.append(
            """ .subhead, .status-line, .team-meta, .stat-caption, .sidebar-copy, .legend-row { color:#333 !important; } .surface, .surface-tight, [data-testid="stVerticalBlockBorderWrapper"], .metric-card { border:2px solid #111 !important; } """
        )
    if st.session_state.get("accessibility_large_text", False):
        accessibility_css.append(""" .subhead { font-size:1.08rem; } .status-line, .team-meta, .stat-caption { font-size:.9rem; } """)
    if st.session_state.get("accessibility_reduce_motion", False):
        accessibility_css.append(""" *, *::before, *::after { animation:none !important; transition:none !important; scroll-behavior:auto !important; } """)
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
            '<div class="status-line"><span class="dot dot-green"></span><span>Ledger online · SQLite local</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div style="color:#a0a0a0;font: .6rem DM Mono, monospace;margin-top:.65rem"><span class="hindsight-word">Hindsight</span> ENGINE / v0.9.4</div>',
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
    existing = ledger.recall(team_name, scenario["file"], code_snippet, scenario["error"])
    log_lines = [
        f"Hindsight.recall() called for bank_id: {bank_id}...",
        f"→ searching semantic ledger / signature {signature_for(team_name, scenario['file'], code_snippet, scenario['error'])[:12]}",
    ]
    if existing:
        repeat_count = ledger.increment_repeat(existing["id"])
        log_lines.extend(
            [
                f"→ context match found in incident #{existing['id']} / confidence 0.98",
                f"Hindsight.retain() skipped — prior resolution promoted (repeat count: {repeat_count})",
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
            "→ context match found: 0",
            "→ unknown fault pattern routed to synthesis layer",
            f"Hindsight.retain() queued for {bank_id}",
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
        if "recall" in line or "searching" in line:
            cls = "log-blue"
        elif "retain" in line or "match found" in line:
            cls = "log-green" if kind == "hit" else "log-red"
        else:
            cls = "log-muted"
        output.append(f'<div class="{cls}">{html.escape(line)}</div>')
    st.markdown(f'<div class="log-box">{"".join(output)}</div>', unsafe_allow_html=True)


def render_review_result(result: dict[str, Any]) -> None:
    scenario = result["scenario"]
    if result["kind"] == "hit":
        st.markdown(
            f"""
            <div class="alert alert-green"><span class="dot dot-green"></span><div>
                <strong>[HINDSIGHT MEMORY CACHE HIT]</strong>
                Successfully retrieved context from historical team memory bank. Applying prior reviewer feedback to prevent redundant human cycles.
                <br><span style="font-size:.72rem">This convention mistake has repeated <b>{result['repeat_count']} times</b> across the team workspace.</span>
            </div></div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
            <div class="alert alert-red"><span class="dot dot-red"></span><div>
                <strong>Unknown fault pattern · Hindsight search returned 0 context matches.</strong>
                Executing LLM synthesis, then retaining the new team standard in the local ledger.
            </div></div>
            """,
            unsafe_allow_html=True,
        )
        progress = st.progress(0, text="Synthesizing fault context…")
        for value, text in [(32, "Mapping failure surface…"), (68, "Comparing team conventions…"), (100, "Retaining learned preference…")]:
            progress.progress(value, text=text)

    st.markdown('<div class="eyebrow" style="margin-top:1.55rem">Review synthesis</div>', unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="surface">
            <div class="surface-label">What went wrong & where</div>
            <h3>{html.escape(scenario['file'])}</h3>
            <p style="color:#666;font-size:.84rem;line-height:1.6;margin:.5rem 0 1.15rem">{html.escape(scenario['error'])}</p>
            <div class="tag"><span class="dot dot-yellow" style="margin-right:.45rem"></span>{html.escape(scenario['violation'])}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""
        <div class="surface" style="margin-top:1rem">
            <div class="surface-label">Refactored remediation code</div>
            <pre style="font: .74rem/1.7 'DM Mono',monospace;white-space:pre-wrap;color:#252525;margin:.8rem 0 0">{html.escape(result['resolution'])}</pre>
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
                    st.markdown(f'<div class="tag" style="margin:.15rem .15rem .15rem 0">{html.escape(pref)}</div>', unsafe_allow_html=True)
            with col_b:
                st.markdown('<div class="surface-label">Systemic feedback loop repetition</div>', unsafe_allow_html=True)
                violations: dict[str, int] = {}
                for row in team_rows:
                    violations[row["standard_violation"]] = violations.get(row["standard_violation"], 0) + int(row["feedback_count"])
                table_rows = [
                    {"File surface": row["file_name"], "Standard at risk": row["standard_violation"], "Feedback loops": int(row["feedback_count"])}
                    for row in sorted(team_rows, key=lambda item: int(item["feedback_count"]), reverse=True)
                ]
                st.dataframe(table_rows, width="stretch", hide_index=True)
                highest = max(violations.items(), key=lambda item: item[1])
                st.markdown(
                    f'<div class="alert alert-yellow"><span class="dot dot-yellow"></span><div><strong>Management signal</strong>{html.escape(highest[0])} is the dominant repeated convention with {highest[1]} feedback events.</div></div>',
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
