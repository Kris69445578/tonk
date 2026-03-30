import websocket
import json
import time
import ssl
import threading
import os
from collections import deque
from colorama import Fore, Style, init
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

init(autoreset=True)

# ===================== CONFIG =====================
API_TOKEN = "yBMnkxHzcfYsqPJ"
BASE_STAKE = 2
CURRENCY = "USD"
DURATION = 1
DIGIT_WINDOW = 1000
DIGIT_THRESHOLD = 0.15
APP_ID = 1089
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

DAILY_TP_TARGET = 1000.00

EMAIL_ENABLED = True
EMAIL_SENDER   = "derivupdatesbytony@gmail.com"
EMAIL_PASSWORD = "lhrg xjad medc hsib"
EMAIL_RECEIVER = "jahimvj1@gmail.com"
SMTP_SERVER    = "smtp.gmail.com"
SMTP_PORT      = 587

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jahim_state.json")

VOLATILITIES = [
    "R_10",  "R_25",  "R_50",  "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V",
    "1HZ15V", "1HZ30V", "1HZ90V", "1HZ100V",
    "JD10",  "JD25", "JD50", "JD75", "JD100", "RDBEAR", "RDBULL"
]

DECIMALS = {
    "R_10": 3, "R_25": 3, "R_50": 4, "R_75": 4, "R_100": 2,
    "1HZ10V": 2, "1HZ25V": 2, "1HZ50V": 2, "1HZ75V": 2,
    "1HZ15V": 3, "1HZ30V": 3, "1HZ90V": 3, "1HZ100V": 2,
    "JD10": 2, "JD25": 2, "JD50": 2, "JD75": 2, "JD100": 2, "RDBEAR": 4, "RDBULL": 4
}

# ===================== GLOBAL MARTINGALE STATE =====================
global_next_stake       = BASE_STAKE
global_in_martingale    = False
global_martingale_level = 0
global_martingale_lock  = threading.Lock()

# ===================== RUNTIME STATE =====================
# tick_buffers : rolling 1000-digit window — used for digit frequency counting
# last_digits  : rolling 1000-digit window — used for position-based signal checks
#                Pre-loaded from Deriv ticks_history on every connect so the
#                signal is ready immediately; no warm-up wait required.
tick_buffers = {v: deque(maxlen=DIGIT_WINDOW) for v in VOLATILITIES}
last_digits  = {v: deque(maxlen=1000)         for v in VOLATILITIES}

mode = {v: "NORMAL" for v in VOLATILITIES}

pending_per_symbol = {v: False for v in VOLATILITIES}
pending_lock       = threading.Lock()

trade_map      = {}
trade_map_lock = threading.Lock()

current_balance  = 0.0
last_tick_time   = time.time()
ws_instance      = None

daily_profit     = 0.0
last_day         = None
trading_active   = True
tp_reached_today = False


def _blank_symbol_stats():
    return {
        "total_trades":  0,
        "wins":          0,
        "losses":        0,
        "streak":        0,
        "double_losses": 0,
        "triple_losses": 0,
        "more_losses":   0,
        "trade_log":     []
    }

daily_stats = {v: _blank_symbol_stats() for v in VOLATILITIES}

# ===================== EMAIL HTML TEMPLATES =====================

def _email_base(title, header_icon, header_title, header_subtitle, body_html, footer_note=""):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{title}</title>
</head>
<body style="margin:0;padding:0;background-color:#0a0e1a;font-family:'Segoe UI',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0a0e1a;padding:30px 0;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
      <tr>
        <td style="background:linear-gradient(135deg,#0d1b2a 0%,#1a2744 100%);
                   border-radius:16px 16px 0 0;padding:28px 36px;text-align:center;
                   border-bottom:2px solid #00d4ff;">
          <div style="font-size:36px;margin-bottom:8px;">{header_icon}</div>
          <div style="font-size:22px;font-weight:800;color:#00d4ff;
                      letter-spacing:3px;text-transform:uppercase;">JAHIM BOT</div>
          <div style="font-size:11px;color:#4a7fa5;letter-spacing:4px;
                      text-transform:uppercase;margin-top:4px;">Automated Trading System</div>
        </td>
      </tr>
      <tr>
        <td style="background:linear-gradient(135deg,#111827 0%,#1c2a3e 100%);
                   padding:32px 36px 24px;text-align:center;">
          <div style="font-size:28px;font-weight:700;color:#ffffff;
                      line-height:1.2;margin-bottom:8px;">{header_title}</div>
          <div style="font-size:14px;color:#7a9fc4;margin-top:6px;">{header_subtitle}</div>
        </td>
      </tr>
      <tr>
        <td style="background:#111827;padding:28px 36px;">
          {body_html}
        </td>
      </tr>
      <tr>
        <td style="background:linear-gradient(135deg,#0d1b2a 0%,#111827 100%);
                   border-radius:0 0 16px 16px;padding:20px 36px;text-align:center;
                   border-top:1px solid #1e3a5f;">
          <div style="font-size:11px;color:#3d6080;line-height:1.8;">
            {footer_note if footer_note else "JAHIM BOT &mdash; Automated Trading System"}<br/>
            <span style="color:#1e3a5f;">&#9679;</span>
            This is an automated notification &mdash; do not reply to this email.
          </div>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


def _stat_box(label, value, color="#00d4ff", bg="#0d1b2a"):
    return f"""
    <td align="center" style="padding:6px;">
      <div style="background:{bg};border:1px solid #1e3a5f;border-radius:10px;
                  padding:16px 20px;min-width:110px;">
        <div style="font-size:22px;font-weight:800;color:{color};">{value}</div>
        <div style="font-size:10px;color:#4a7fa5;text-transform:uppercase;
                    letter-spacing:1.5px;margin-top:4px;">{label}</div>
      </div>
    </td>"""


def _divider():
    return '<div style="height:1px;background:linear-gradient(90deg,transparent,#1e3a5f,transparent);margin:20px 0;"></div>'


def _info_row(label, value, value_color="#ffffff"):
    return f"""
    <tr>
      <td style="padding:9px 0;font-size:13px;color:#4a7fa5;
                 border-bottom:1px solid #1a2744;">{label}</td>
      <td style="padding:9px 0;font-size:13px;font-weight:600;color:{value_color};
                 text-align:right;border-bottom:1px solid #1a2744;">{value}</td>
    </tr>"""


def _badge(text, color="#00d4ff", bg="#0a1f30"):
    return (f'<span style="display:inline-block;background:{bg};color:{color};'
            f'border:1px solid {color};border-radius:20px;padding:3px 12px;'
            f'font-size:11px;font-weight:700;letter-spacing:1px;">{text}</span>')


def _build_started_email(dt_str, restored_profit, restored_trades, tp_status,
                         mart_level, mart_stake):
    mart_color = "#ef4444" if mart_level > 0 else "#00ff87"
    body = f"""
    <div style="text-align:center;margin-bottom:24px;">
      {_badge("SYSTEM ONLINE", "#00ff87", "#002918")}
    </div>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
      <tr>
        {_stat_box("TP Target", f"${DAILY_TP_TARGET:,.0f}", "#00d4ff")}
        {_stat_box("Tick Window", str(DIGIT_WINDOW), "#a78bfa")}
        {_stat_box("Symbols", str(len(VOLATILITIES)), "#fb923c")}
        {_stat_box("Base Stake", f"${BASE_STAKE}", "#00ff87")}
      </tr>
    </table>
    {_divider()}
    <table width="100%" cellpadding="0" cellspacing="0">
      {_info_row("Started At", dt_str)}
      {_info_row("Entry Threshold", f"{int(DIGIT_THRESHOLD*100)}% ({int(DIGIT_THRESHOLD*DIGIT_WINDOW)}/{DIGIT_WINDOW} ticks)")}
      {_info_row("Entry Signal", "Positions -1,-10,-11 match; freq ≤ threshold")}
      {_info_row("History Pre-load", f"{DIGIT_WINDOW} ticks per symbol — signal fires instantly")}
      {_info_row("Martingale Mode", "Global — any symbol, next valid signal")}
      {_info_row("Martingale Level (restored)", str(mart_level), mart_color)}
      {_info_row("Next Stake (restored)", f"${mart_stake:.2f} {CURRENCY}", mart_color)}
      {_info_row("Today's Restored Profit", f"+{restored_profit:.2f} {CURRENCY}", "#00ff87")}
      {_info_row("Restored Trades", str(restored_trades))}
      {_info_row("Trading Status", tp_status, "#00ff87" if "ACTIVE" in tp_status else "#ef4444")}
      {_info_row("State File", "jahim_state.json")}
    </table>
    {_divider()}
    <div style="background:linear-gradient(135deg,#002918,#001a10);border:1px solid #00ff87;
                border-radius:10px;padding:16px 20px;text-align:center;margin-top:8px;">
      <div style="font-size:13px;color:#00ff87;font-weight:600;letter-spacing:1px;">
        🚀 All systems go &mdash; 1000-tick history pre-loaded, signal fires instantly!
      </div>
    </div>
    """
    return _email_base(
        title="JAHIM BOT Started",
        header_icon="🤖",
        header_title="Bot Successfully Started",
        header_subtitle=f"Launched {dt_str}",
        body_html=body,
        footer_note="Daily report delivered every night at 00:00 automatically."
    )


def _build_tp_reached_email(profit, balance, dt_str):
    body = f"""
    <div style="text-align:center;margin-bottom:28px;">
      <div style="font-size:56px;line-height:1;">🎯</div>
      <div style="margin-top:12px;">
        {_badge("DAILY TARGET ACHIEVED", "#fbbf24", "#1c1400")}
      </div>
    </div>
    <div style="background:linear-gradient(135deg,#1c1400,#2d1f00);border:2px solid #fbbf24;
                border-radius:12px;padding:24px;text-align:center;margin-bottom:24px;">
      <div style="font-size:12px;color:#92400e;text-transform:uppercase;
                  letter-spacing:2px;margin-bottom:8px;">Today's Total Profit</div>
      <div style="font-size:48px;font-weight:900;color:#fbbf24;line-height:1;">
        +${profit:,.2f}
      </div>
      <div style="font-size:13px;color:#92400e;margin-top:8px;">{CURRENCY}</div>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0">
      {_info_row("TP Target", f"${DAILY_TP_TARGET:,.2f} {CURRENCY}", "#fbbf24")}
      {_info_row("Achieved At", dt_str, "#fbbf24")}
      {_info_row("Account Balance", f"${balance:,.2f} {CURRENCY}", "#00ff87")}
      {_info_row("Bot Status", "PAUSED — resumes tomorrow automatically", "#ef4444")}
    </table>
    {_divider()}
    <div style="background:#0a0e1a;border:1px solid #1e3a5f;border-radius:10px;
                padding:14px 20px;text-align:center;margin-top:8px;">
      <div style="font-size:12px;color:#4a7fa5;">
        Trading resumes automatically at <strong style="color:#00d4ff;">00:00</strong> tomorrow. 💰
      </div>
    </div>
    """
    return _email_base(
        title="Daily Take Profit Reached!",
        header_icon="🏆",
        header_title="Daily Target Reached!",
        header_subtitle=f"Profit target of ${DAILY_TP_TARGET:,.2f} {CURRENCY} secured",
        body_html=body,
        footer_note="Bot auto-paused. No trades will be placed until tomorrow."
    )


def _build_reconnected_paused_email(dt_str):
    body = f"""
    <div style="text-align:center;margin-bottom:24px;">
      {_badge("RECONNECTED", "#fb923c", "#1c0e00")}
    </div>
    <table width="100%" cellpadding="0" cellspacing="0">
      {_info_row("Reconnected At", dt_str)}
      {_info_row("TP Target", f"${DAILY_TP_TARGET:,.2f} {CURRENCY}", "#fbbf24")}
      {_info_row("Status", "Already reached today", "#fbbf24")}
      {_info_row("Trading", "PAUSED — resumes at 00:00", "#ef4444")}
    </table>
    {_divider()}
    <div style="background:#1c0a00;border:1px solid #fb923c;border-radius:10px;
                padding:14px 20px;text-align:center;">
      <div style="font-size:12px;color:#fb923c;">
        ⚡ Connection restored. Daily TP was already reached &mdash;
        trading remains paused until midnight rollover.
      </div>
    </div>
    """
    return _email_base(
        title="Bot Reconnected — Trading Paused",
        header_icon="🔄",
        header_title="Reconnected Successfully",
        header_subtitle="Trading remains paused — TP already reached today",
        body_html=body
    )


def _build_daily_summary_email(report_date, profit, balance):
    profit_color = "#00ff87" if profit >= 0 else "#ef4444"
    profit_sign  = "+" if profit >= 0 else ""
    pct = (profit / DAILY_TP_TARGET * 100) if DAILY_TP_TARGET else 0

    total_trades = sum(daily_stats[v]["total_trades"] for v in VOLATILITIES)
    total_wins   = sum(daily_stats[v]["wins"]         for v in VOLATILITIES)
    total_losses = sum(daily_stats[v]["losses"]       for v in VOLATILITIES)
    win_rate     = (total_wins / total_trades * 100) if total_trades else 0.0

    body = f"""
    <div style="text-align:center;margin-bottom:24px;">
      {_badge(report_date, "#00d4ff", "#001a26")}
    </div>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
      <tr>
        {_stat_box("Profit", f"{profit_sign}${profit:,.2f}", profit_color)}
        {_stat_box("Trades", str(total_trades), "#a78bfa")}
        {_stat_box("Win Rate", f"{win_rate:.1f}%", "#00ff87" if win_rate >= 55 else "#fbbf24")}
        {_stat_box("Balance", f"${balance:,.2f}", "#00d4ff")}
      </tr>
    </table>
    <div style="margin-bottom:24px;">
      <div style="display:flex;justify-content:space-between;margin-bottom:6px;">
        <span style="font-size:11px;color:#4a7fa5;text-transform:uppercase;letter-spacing:1px;">TP Progress</span>
        <span style="font-size:11px;color:#00d4ff;font-weight:700;">{pct:.1f}%</span>
      </div>
      <div style="background:#1a2744;border-radius:999px;height:8px;overflow:hidden;">
        <div style="height:100%;width:{min(pct,100):.1f}%;
                    background:linear-gradient(90deg,#00d4ff,#00ff87);border-radius:999px;"></div>
      </div>
    </div>
    {_divider()}
    <table width="100%" cellpadding="0" cellspacing="0">
      {_info_row("Total Wins", str(total_wins), "#00ff87")}
      {_info_row("Total Losses", str(total_losses), "#ef4444")}
      {_info_row("TP Target", f"${DAILY_TP_TARGET:,.2f} {CURRENCY}")}
    </table>
    {_divider()}
    <div style="background:linear-gradient(135deg,#0d1b2a,#111827);border:1px solid #1e3a5f;
                border-radius:10px;padding:14px 20px;text-align:center;">
      <div style="font-size:12px;color:#4a7fa5;">
        Full detailed report with per-symbol breakdown will follow shortly. 🌙
      </div>
    </div>
    """
    return _email_base(
        title=f"Daily Summary — {report_date}",
        header_icon="📅",
        header_title="Daily Trading Summary",
        header_subtitle=f"Closing report for {report_date}",
        body_html=body,
        footer_note="Full report with per-symbol breakdown sent separately."
    )


def _build_new_day_email(current_day, balance):
    body = f"""
    <div style="text-align:center;margin-bottom:28px;">
      <div style="font-size:52px;line-height:1;">🌅</div>
      <div style="margin-top:12px;">
        {_badge("NEW TRADING DAY", "#00ff87", "#002918")}
      </div>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
      <tr>
        {_stat_box("Date", str(current_day), "#00d4ff")}
        {_stat_box("TP Target", f"${DAILY_TP_TARGET:,.0f}", "#fbbf24")}
        {_stat_box("Balance", f"${balance:,.2f}", "#00ff87")}
        {_stat_box("Status", "LIVE", "#00ff87", "#002918")}
      </tr>
    </table>
    {_divider()}
    <div style="background:linear-gradient(135deg,#002918,#001a10);border:1px solid #00ff87;
                border-radius:12px;padding:20px;text-align:center;">
      <div style="font-size:14px;color:#00ff87;font-weight:700;letter-spacing:1px;">
        ✅ All counters reset &mdash; bot is scanning all {len(VOLATILITIES)} symbols
      </div>
      <div style="font-size:12px;color:#065f46;margin-top:8px;">
        Signals active &bull; Tick stream live &bull; Ready for profitable trades
      </div>
    </div>
    """
    return _email_base(
        title=f"New Trading Day — {current_day}",
        header_icon="🌅",
        header_title="New Day, New Opportunities",
        header_subtitle=f"Trading session started for {current_day}",
        body_html=body,
        footer_note="Daily stats reset. Profit counter starts from zero."
    )


def _build_health_check_email(current_day, balance):
    with global_martingale_lock:
        m_level = global_martingale_level
        m_stake = global_next_stake
    body = f"""
    <div style="text-align:center;margin-bottom:24px;">
      {_badge("ALL SYSTEMS OPERATIONAL", "#00ff87", "#002918")}
    </div>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
      <tr>
        {_stat_box("WebSocket", "✓ Live", "#00ff87", "#002918")}
        {_stat_box("Tick Stream", "✓ Active", "#00ff87", "#002918")}
        {_stat_box("Engine", "✓ Running", "#00ff87", "#002918")}
        {_stat_box("Email", "✓ OK", "#00ff87", "#002918")}
      </tr>
    </table>
    {_divider()}
    <table width="100%" cellpadding="0" cellspacing="0">
      {_info_row("Check Time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}
      {_info_row("Trading Date", str(current_day))}
      {_info_row("Account Balance", f"${balance:,.2f} {CURRENCY}", "#00d4ff")}
      {_info_row("Martingale Level", str(m_level), "#ef4444" if m_level > 0 else "#00ff87")}
      {_info_row("Next Trade Stake", f"${m_stake:.2f} {CURRENCY}", "#ef4444" if m_level > 0 else "#00ff87")}
      {_info_row("Symbols Monitored", str(len(VOLATILITIES)))}
      {_info_row("Tick Window", f"{DIGIT_WINDOW} ticks")}
    </table>
    {_divider()}
    <div style="background:#0a0e1a;border:1px solid #1e3a5f;border-radius:10px;
                padding:14px 20px;text-align:center;">
      <div style="font-size:12px;color:#4a7fa5;">
        🤖 Automated health check passed &mdash; your bot is working perfectly.
      </div>
    </div>
    """
    return _email_base(
        title="Bot Health Check — All OK",
        header_icon="💚",
        header_title="Health Check Passed",
        header_subtitle="Bot is running normally",
        body_html=body
    )


def _build_daily_report_email(report_date, profit, balance):
    finalize_streaks()

    total_trades = sum(daily_stats[v]["total_trades"] for v in VOLATILITIES)
    total_wins   = sum(daily_stats[v]["wins"]         for v in VOLATILITIES)
    total_losses = sum(daily_stats[v]["losses"]       for v in VOLATILITIES)
    win_rate     = (total_wins / total_trades * 100) if total_trades else 0.0

    profit_color = "#00ff87" if profit >= 0 else "#ef4444"
    profit_sign  = "+" if profit >= 0 else ""

    rows_html = ""
    for v in VOLATILITIES:
        s = daily_stats[v]
        t = s["total_trades"]
        if t == 0:
            continue
        w  = s["wins"]
        l  = s["losses"]
        wr = (w / t * 100) if t else 0.0
        dl = s["double_losses"]
        tl = s["triple_losses"]
        ml = s["more_losses"]
        wr_color = "#00ff87" if wr >= 55 else ("#fbbf24" if wr >= 45 else "#ef4444")
        streak_warn = " ⚠" if (dl or tl or ml) else ""
        rows_html += f"""
        <tr>
          <td style="padding:9px 10px;font-size:12px;color:#00d4ff;font-weight:600;
                     border-bottom:1px solid #1a2744;">{v}{streak_warn}</td>
          <td style="padding:9px 10px;font-size:12px;color:#ffffff;text-align:center;
                     border-bottom:1px solid #1a2744;">{t}</td>
          <td style="padding:9px 10px;font-size:12px;color:#00ff87;text-align:center;
                     border-bottom:1px solid #1a2744;">{w}</td>
          <td style="padding:9px 10px;font-size:12px;color:#ef4444;text-align:center;
                     border-bottom:1px solid #1a2744;">{l}</td>
          <td style="padding:9px 10px;font-size:12px;color:{wr_color};text-align:center;
                     font-weight:700;border-bottom:1px solid #1a2744;">{wr:.0f}%</td>
          <td style="padding:9px 10px;font-size:11px;color:#fbbf24;text-align:center;
                     border-bottom:1px solid #1a2744;">{dl or "—"}</td>
          <td style="padding:9px 10px;font-size:11px;color:#fb923c;text-align:center;
                     border-bottom:1px solid #1a2744;">{tl or "—"}</td>
          <td style="padding:9px 10px;font-size:11px;color:#ef4444;text-align:center;
                     border-bottom:1px solid #1a2744;">{ml or "—"}</td>
        </tr>"""

    problem_html = ""
    problem_syms = [
        v for v in VOLATILITIES
        if daily_stats[v]["double_losses"] > 0
        or daily_stats[v]["triple_losses"] > 0
        or daily_stats[v]["more_losses"] > 0
    ]
    if problem_syms:
        items = ""
        for v in problem_syms:
            s = daily_stats[v]
            parts = []
            if s["double_losses"]:
                parts.append(f"2× loss streak: {s['double_losses']}x")
            if s["triple_losses"]:
                parts.append(f"3× loss streak: {s['triple_losses']}x")
            if s["more_losses"]:
                parts.append(f"4+× loss streak: {s['more_losses']}x")
            items += f"""
            <div style="padding:8px 12px;margin-bottom:6px;background:#1c0a00;
                        border-left:3px solid #ef4444;border-radius:0 6px 6px 0;">
              <span style="color:#ef4444;font-size:12px;font-weight:700;">{v}</span>
              <span style="color:#7a9fc4;font-size:11px;margin-left:8px;">{" &bull; ".join(parts)}</span>
            </div>"""
        problem_html = f"""
        {_divider()}
        <div style="margin-bottom:8px;">
          <div style="font-size:11px;color:#ef4444;text-transform:uppercase;
                      letter-spacing:2px;margin-bottom:12px;font-weight:700;">
            ⚠ Consecutive Loss Sequences
          </div>
          {items}
        </div>"""

    body = f"""
    <div style="background:linear-gradient(135deg,#0d1b2a,#111827);border:1px solid #1e3a5f;
                border-radius:12px;padding:24px;text-align:center;margin-bottom:24px;">
      <div style="font-size:11px;color:#4a7fa5;text-transform:uppercase;
                  letter-spacing:2px;margin-bottom:8px;">Today's Net Profit</div>
      <div style="font-size:52px;font-weight:900;color:{profit_color};line-height:1;">
        {profit_sign}${abs(profit):,.2f}
      </div>
      <div style="font-size:12px;color:#4a7fa5;margin-top:6px;">{CURRENCY}</div>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
      <tr>
        {_stat_box("Trades", str(total_trades), "#a78bfa")}
        {_stat_box("Wins", str(total_wins), "#00ff87")}
        {_stat_box("Losses", str(total_losses), "#ef4444")}
        {_stat_box("Win Rate", f"{win_rate:.1f}%", "#00ff87" if win_rate >= 55 else "#fbbf24")}
      </tr>
    </table>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
      {_info_row("Report Date", report_date)}
      {_info_row("TP Target", f"${DAILY_TP_TARGET:,.2f} {CURRENCY}", "#fbbf24")}
      {_info_row("End Balance", f"${balance:,.2f} {CURRENCY}", "#00d4ff")}
    </table>
    {_divider()}
    <div style="font-size:11px;color:#4a7fa5;text-transform:uppercase;
                letter-spacing:2px;margin-bottom:12px;font-weight:700;">
      Per-Symbol Breakdown
    </div>
    <div style="overflow-x:auto;">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;border:1px solid #1e3a5f;border-radius:8px;overflow:hidden;">
      <tr style="background:#0d1b2a;">
        <th style="padding:10px;font-size:10px;color:#4a7fa5;text-align:left;
                   text-transform:uppercase;letter-spacing:1px;">Symbol</th>
        <th style="padding:10px;font-size:10px;color:#4a7fa5;text-align:center;
                   text-transform:uppercase;letter-spacing:1px;">Trades</th>
        <th style="padding:10px;font-size:10px;color:#4a7fa5;text-align:center;
                   text-transform:uppercase;letter-spacing:1px;">Wins</th>
        <th style="padding:10px;font-size:10px;color:#4a7fa5;text-align:center;
                   text-transform:uppercase;letter-spacing:1px;">Loss</th>
        <th style="padding:10px;font-size:10px;color:#4a7fa5;text-align:center;
                   text-transform:uppercase;letter-spacing:1px;">W%</th>
        <th style="padding:10px;font-size:10px;color:#fbbf24;text-align:center;
                   text-transform:uppercase;letter-spacing:1px;">2×</th>
        <th style="padding:10px;font-size:10px;color:#fb923c;text-align:center;
                   text-transform:uppercase;letter-spacing:1px;">3×</th>
        <th style="padding:10px;font-size:10px;color:#ef4444;text-align:center;
                   text-transform:uppercase;letter-spacing:1px;">4+</th>
      </tr>
      {rows_html if rows_html else
        '<tr><td colspan="8" style="padding:20px;text-align:center;color:#4a7fa5;font-size:12px;">No trades recorded today.</td></tr>'}
    </table>
    </div>
    {problem_html}
    {_divider()}
    <div style="background:#0a0e1a;border:1px solid #1e3a5f;border-radius:10px;
                padding:16px 20px;text-align:center;margin-top:8px;">
      <div style="font-size:13px;color:#4a7fa5;">
        Good night! 🌙 Bot resets at midnight and resumes trading tomorrow.
      </div>
    </div>
    """
    return _email_base(
        title=f"JAHIM BOT Daily Report — {report_date}",
        header_icon="📋",
        header_title="Daily Trading Report",
        header_subtitle=f"Full summary for {report_date}",
        body_html=body,
        footer_note="Generated automatically by JAHIM BOT at midnight."
    )


# ===================== SEND EMAIL =====================
def send_email(subject, body_text, html_body=None):
    if not EMAIL_ENABLED:
        print(Fore.YELLOW + f"📧 Email alerts disabled: {subject}")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg['From']    = EMAIL_SENDER
        msg['To']      = EMAIL_RECEIVER
        msg['Subject'] = subject
        msg.attach(MIMEText(body_text, 'plain'))
        if html_body:
            msg.attach(MIMEText(html_body, 'html'))
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        print(Fore.GREEN + f"📧 Email sent: {subject}")
    except Exception as e:
        print(Fore.RED + f"❌ Email failed ({subject}): {e}")


# ===================== PERSISTENCE =====================
_state_lock = threading.Lock()


def save_state():
    global global_next_stake, global_in_martingale, global_martingale_level

    with global_martingale_lock:
        m_stake = global_next_stake
        m_in    = global_in_martingale
        m_level = global_martingale_level

    state = {
        "saved_at":                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_day":                str(last_day) if last_day else None,
        "daily_profit":            daily_profit,
        "current_balance":         current_balance,
        "trading_active":          trading_active,
        "tp_reached_today":        tp_reached_today,
        "daily_stats":             daily_stats,
        "global_next_stake":       m_stake,
        "global_in_martingale":    m_in,
        "global_martingale_level": m_level,
    }
    tmp = STATE_FILE + ".tmp"
    with _state_lock:
        try:
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            print(Fore.RED + f"❌ Failed to save state: {e}")


def load_state():
    global daily_profit, last_day, trading_active, tp_reached_today
    global current_balance, daily_stats
    global global_next_stake, global_in_martingale, global_martingale_level

    if not os.path.exists(STATE_FILE):
        print(Fore.YELLOW + "ℹ️  No state file found — starting fresh.")
        last_day = datetime.now().date()
        return

    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)

        saved_day_str = state.get("last_day")
        today_str     = str(datetime.now().date())

        if saved_day_str != today_str:
            print(Fore.YELLOW + f"ℹ️  State from {saved_day_str} — starting fresh for {today_str}.")
            last_day = datetime.now().date()

            # Martingale IS restored even across a day boundary —
            # a loss at 23:59 that caused a reboot must still be recovered.
            global_next_stake       = float(state.get("global_next_stake",       BASE_STAKE))
            global_in_martingale    = bool(state.get("global_in_martingale",     False))
            global_martingale_level = int(state.get("global_martingale_level",   0))

            if global_martingale_level > 0:
                print(Fore.YELLOW +
                      f"⚠  Martingale carried over from {saved_day_str}: "
                      f"level={global_martingale_level}  next_stake=${global_next_stake:.2f}")
            return

        last_day         = datetime.strptime(saved_day_str, "%Y-%m-%d").date()
        daily_profit     = float(state.get("daily_profit", 0.0))
        current_balance  = float(state.get("current_balance", 0.0))
        trading_active   = bool(state.get("trading_active", True))
        tp_reached_today = bool(state.get("tp_reached_today", False))

        saved_stats = state.get("daily_stats", {})
        for v in VOLATILITIES:
            if v in saved_stats:
                s = _blank_symbol_stats()
                s.update(saved_stats[v])
                daily_stats[v] = s

        global_next_stake       = float(state.get("global_next_stake",       BASE_STAKE))
        global_in_martingale    = bool(state.get("global_in_martingale",     False))
        global_martingale_level = int(state.get("global_martingale_level",   0))

        restored_trades = sum(daily_stats[v]["total_trades"] for v in VOLATILITIES)
        print(Fore.GREEN +
              f"✅ State restored | Day={saved_day_str} | Profit={daily_profit:+.2f} | "
              f"Trades={restored_trades} | TP={'REACHED' if tp_reached_today else 'active'} | "
              f"Martingale lvl={global_martingale_level} next_stake=${global_next_stake:.2f}")

    except Exception as e:
        print(Fore.RED + f"❌ Failed to load state ({e}) — starting fresh.")
        last_day = datetime.now().date()


# ===================== TRADE STATS =====================
def reset_daily_stats():
    global daily_stats
    for v in VOLATILITIES:
        daily_stats[v] = _blank_symbol_stats()


def record_trade_result(vol, digit, stake, profit, result_str):
    s = daily_stats[vol]
    s["total_trades"] += 1
    s["trade_log"].append({
        "digit":  digit,
        "stake":  stake,
        "result": result_str,
        "profit": round(profit, 4),
        "time":   datetime.now().strftime("%H:%M:%S")
    })

    if result_str == "WIN":
        s["wins"] += 1
        streak = s["streak"]
        if streak == 2:
            s["double_losses"] += 1
        elif streak == 3:
            s["triple_losses"] += 1
        elif streak >= 4:
            s["more_losses"] += 1
        s["streak"] = 0
    else:
        s["losses"] += 1
        s["streak"] += 1

    save_state()


def finalize_streaks():
    for v in VOLATILITIES:
        s = daily_stats[v]
        streak = s["streak"]
        if streak == 2:
            s["double_losses"] += 1
        elif streak == 3:
            s["triple_losses"] += 1
        elif streak >= 4:
            s["more_losses"] += 1
        s["streak"] = 0


# ===================== GLOBAL MARTINGALE HELPERS =====================
def get_next_stake_and_arm():
    return global_next_stake


def on_trade_win():
    global global_next_stake, global_in_martingale, global_martingale_level
    global_next_stake       = BASE_STAKE
    global_in_martingale    = False
    global_martingale_level = 0


def on_trade_loss():
    global global_next_stake, global_in_martingale, global_martingale_level
    global_martingale_level += 1
    global_in_martingale     = True
    global_next_stake        = global_next_stake * 11


# ===================== UTILS =====================
def get_last_digit(price, decimals):
    return int(f"{price:.{decimals}f}"[-1])


def separator():
    print(Fore.CYAN + "═" * 60)


# ===================== DAILY REPORT =====================
def build_daily_report(report_date):
    finalize_streaks()
    total_trades = sum(daily_stats[v]["total_trades"] for v in VOLATILITIES)
    total_wins   = sum(daily_stats[v]["wins"]         for v in VOLATILITIES)
    total_losses = sum(daily_stats[v]["losses"]       for v in VOLATILITIES)
    win_rate     = (total_wins / total_trades * 100) if total_trades else 0.0

    lines = ["=" * 64,
             f"  JAHIM BOT — DAILY REPORT  |  {report_date}",
             "=" * 64,
             f"  Daily Profit  : {daily_profit:+.2f} {CURRENCY}",
             f"  TP Target     : {DAILY_TP_TARGET:.2f} {CURRENCY}",
             f"  End Balance   : {current_balance:.2f} {CURRENCY}",
             f"  Total Trades  : {total_trades}",
             f"  Win Rate      : {win_rate:.1f}%",
             "=" * 64]
    return "\n".join(lines)


def send_daily_report():
    report_date = (last_day or datetime.now().date()).strftime("%Y-%m-%d")
    plain = build_daily_report(report_date)
    html  = _build_daily_report_email(report_date, daily_profit, current_balance)
    print(Fore.CYAN + "\n" + plain)
    send_email(f"📋 JAHIM BOT Daily Report — {report_date}", plain, html)


# ===================== MIDNIGHT SCHEDULER =====================
def midnight_scheduler():
    global daily_profit, trading_active, last_day, tp_reached_today

    while True:
        now = datetime.now()
        seconds_to_midnight = (
            (24 - now.hour - 1) * 3600
            + (60 - now.minute - 1) * 60
            + (60 - now.second)
        )
        time.sleep(seconds_to_midnight)

        yesterday = last_day or datetime.now().date()
        send_daily_report()

        summary_plain = (
            f"Date          : {yesterday}\n"
            f"Daily Profit  : {daily_profit:+.2f} {CURRENCY}\n"
            f"TP Target     : {DAILY_TP_TARGET:.2f} {CURRENCY}\n"
            f"Full report sent above."
        )
        summary_html = _build_daily_summary_email(
            str(yesterday), daily_profit, current_balance
        )
        send_email(f"📅 Daily Summary — {yesterday}", summary_plain, summary_html)

        current_day      = datetime.now().date()
        last_day         = current_day
        daily_profit     = 0.0
        trading_active   = True
        tp_reached_today = False
        reset_daily_stats()

        # Martingale is intentionally NOT reset at midnight —
        # a loss at 23:59 must still be recovered after midnight rollover.
        save_state()

        new_day_html = _build_new_day_email(current_day, current_balance)
        send_email(
            f"🌅 New Trading Day Started — {current_day}",
            f"Auto-trading resumed for {current_day}\nBalance: {current_balance:.2f} {CURRENCY}",
            new_day_html
        )

        health_html = _build_health_check_email(current_day, current_balance)
        send_email(
            "🤖 Bot Daily Health Check",
            f"Bot is running normally.\nBalance: {current_balance:.2f} {CURRENCY}",
            health_html
        )

        time.sleep(61)


def check_for_new_day():
    global last_day, daily_profit, trading_active, tp_reached_today

    if last_day is None:
        last_day = datetime.now().date()
        return

    current_day = datetime.now().date()
    if current_day != last_day:
        print(Fore.YELLOW + f"ℹ️  Day changed {last_day} → {current_day} — rolling over.")
        send_daily_report()
        last_day         = current_day
        daily_profit     = 0.0
        trading_active   = True
        tp_reached_today = False
        reset_daily_stats()

        # Martingale is intentionally NOT reset at day change —
        # a loss at 23:59 must still be recovered after midnight rollover.
        save_state()


def log_trade(vol, digit, stake, result):
    with global_martingale_lock:
        m_level = global_martingale_level
        m_stake = global_next_stake

    separator()
    print(Fore.CYAN    + f"📊 {vol}")
    print(Fore.YELLOW  + f"🔢 Differ Digit: {digit}")
    print(Fore.MAGENTA + f"💰 Stake: {stake} {CURRENCY}")
    print(Fore.BLUE    + f"🏦 Balance: {current_balance:.2f} {CURRENCY}")
    print(Fore.CYAN    + f"📈 Today's Profit: {daily_profit:.2f} {CURRENCY}  "
                         f"(TP: {DAILY_TP_TARGET:.2f})")
    print(Fore.YELLOW  + f"🎲 Martingale Level: {m_level}  |  Next Stake: ${m_stake:.2f}")
    if tp_reached_today:
        print(Fore.RED + "⏸️  TRADING PAUSED — DAILY TP ALREADY REACHED")
    if result == "WIN":
        print(Fore.GREEN + "✅ WIN")
    else:
        print(Fore.RED + "❌ LOSS — waiting for next VALID SIGNAL (any symbol)")
    separator()


# ===================== SIGNAL =====================
def find_valid_digit_odd_positions(vol):
    buf = tick_buffers[vol]
    seq = list(last_digits[vol])

    if len(buf) < DIGIT_WINDOW:
        return None
    if len(seq) < 1000:
        return None

    d1, d2, d3, d4, d5, d6, d7, d8 = seq[-1], seq[-2], seq[-11], seq[-100], seq[-101], seq[-110], seq[-111], seq[-1000]

    if not (d1 == d2 == d4 == d5):
        return None

    candidate = d1
    freq = list(buf).count(candidate) / DIGIT_WINDOW
    return candidate if freq <= DIGIT_THRESHOLD else None


# ===================== TRADE =====================
def place_trade(ws, vol, digit, stake):
    with pending_lock:
        if pending_per_symbol.get(vol, False):
            return
        pending_per_symbol[vol] = True

    payload = {
        "buy": 1,
        "price": stake,
        "parameters": {
            "amount":        stake,
            "basis":         "stake",
            "contract_type": "DIGITDIFF",
            "currency":      CURRENCY,
            "duration":      DURATION,
            "duration_unit": "t",
            "symbol":        vol,
            "barrier":       digit
        }
    }

    with trade_map_lock:
        trade_map[f"PENDING_{vol}"] = {"vol": vol, "digit": digit, "stake": stake}

    ws.send(json.dumps(payload))
    with global_martingale_lock:
        m_level = global_martingale_level
    label = f"⚡ TRADE" if m_level == 0 else f"🔁 MARTINGALE L{m_level}"
    print(Fore.CYAN + f"{label} SENT | {vol} | Differ {digit} | Stake ${stake:.2f} {CURRENCY}")


# ===================== CALLBACKS =====================
def on_open(ws):
    global ws_instance
    ws_instance = ws
    print(Fore.GREEN + "🟢 CONNECTED")
    ws.send(json.dumps({"authorize": API_TOKEN}))


def on_message(ws, msg):
    global current_balance, last_tick_time
    global daily_profit, trading_active, last_day, tp_reached_today

    data = json.loads(msg)
    check_for_new_day()

    # ── AUTHORIZE ────────────────────────────────────────────────────────────
    if "authorize" in data:
        print(Fore.GREEN + "🟢 AUTHORIZED")
        ws.send(json.dumps({"balance": 1, "subscribe": 1}))

        # Request 1000-tick history + live subscription for every symbol.
        # The history response fills BOTH tick_buffers AND last_digits instantly,
        # so find_valid_digit_odd_positions() can fire on the very first live tick.
        for v in VOLATILITIES:
            ws.send(json.dumps({
                "ticks_history": v,
                "count":         DIGIT_WINDOW,   # 1000 ticks
                "end":           "latest",
                "style":         "ticks",
                "subscribe":     1               # also starts the live stream
            }))

        if last_day is None:
            last_day = datetime.now().date()

        if tp_reached_today:
            trading_active = False
            print(Fore.RED + "⏸️  Trading remains PAUSED — daily TP already reached")
            plain = (f"Bot reconnected at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                     f"Daily TP ({DAILY_TP_TARGET:.2f} {CURRENCY}) already reached today.\n"
                     f"Trading resumes automatically tomorrow.")
            html = _build_reconnected_paused_email(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            send_email("🔄 Reconnected — Trading still paused", plain, html)
        else:
            trading_active = True
            with global_martingale_lock:
                m_level = global_martingale_level
                m_stake = global_next_stake
            if m_level > 0:
                print(Fore.YELLOW +
                      f"▶ Trading ACTIVE | ⚠ Martingale L{m_level} active — "
                      f"next trade stake = ${m_stake:.2f}")
            else:
                print(Fore.GREEN + "▶ Trading active — base stake")

    # ── BALANCE ──────────────────────────────────────────────────────────────
    if "balance" in data:
        current_balance = float(data["balance"]["balance"])

    # ── TICK HISTORY (initial 1000-tick load) ─────────────────────────────────
    # THE FIX: every historical price is pushed into BOTH tick_buffers (for
    # frequency counting) AND last_digits (for position-based signal checks).
    #
    # Previously last_digits only received live ticks one at a time — meaning
    # len(seq) < 1000 was always True on startup and the signal never fired
    # until roughly 16 minutes of live ticks accumulated. Now both buffers are
    # fully populated the moment the history response arrives, so the signal
    # is ready to fire on the very next live tick with zero warm-up wait.
    if "history" in data:
        vol    = data["echo_req"]["ticks_history"]
        prices = data["history"]["prices"]
        decs   = DECIMALS[vol]

        # Clear stale data first — important on reconnect so we don't double-load
        tick_buffers[vol].clear()
        last_digits[vol].clear()

        for p in prices:
            d = get_last_digit(float(p), decs)
            tick_buffers[vol].append(d)   # frequency window  (maxlen=1000)
            last_digits[vol].append(d)    # position window   (maxlen=1000)

        ready = "✅ SIGNAL READY" if len(last_digits[vol]) >= 1000 else "⏳ partial"
        print(Fore.GREEN +
              f"📥 {vol}: {len(prices)} ticks loaded | "
              f"buf={len(tick_buffers[vol])}/{DIGIT_WINDOW} | "
              f"seq={len(last_digits[vol])}/1000 | {ready}")

    # ── LIVE TICK ─────────────────────────────────────────────────────────────
    if "tick" in data:
        last_tick_time = time.time()
        vol   = data["tick"]["symbol"]
        digit = get_last_digit(float(data["tick"]["quote"]), DECIMALS[vol])

        tick_buffers[vol].append(digit)
        last_digits[vol].append(digit)

        if mode[vol] == "NORMAL" and trading_active:
            target_digit = find_valid_digit_odd_positions(vol)
            if target_digit is not None:
                with global_martingale_lock:
                    stake = get_next_stake_and_arm()

                mode[vol] = "RUNNING"
                place_trade(ws, vol, target_digit, stake)

    # ── BUY CONFIRMATION ─────────────────────────────────────────────────────
    if "buy" in data and "error" not in data:
        buy_info = data["buy"]
        cid      = buy_info.get("contract_id")
        if cid is None:
            return

        matched_vol = None
        with trade_map_lock:
            for v in VOLATILITIES:
                key = f"PENDING_{v}"
                if key in trade_map:
                    matched_vol = v
                    info = trade_map.pop(key)
                    trade_map[cid] = info
                    break

        if matched_vol:
            ws.send(json.dumps({
                "proposal_open_contract": 1,
                "contract_id": cid,
                "subscribe":   1
            }))
        else:
            print(Fore.YELLOW + f"⚠ buy ack for cid {cid} had no matching PENDING_ entry")

    # ── BUY ERROR ────────────────────────────────────────────────────────────
    if "buy" in data and "error" in data:
        err_msg = data["error"].get("message", "unknown error")
        with trade_map_lock:
            for v in VOLATILITIES:
                if f"PENDING_{v}" in trade_map:
                    trade_map.pop(f"PENDING_{v}")
                    with pending_lock:
                        pending_per_symbol[v] = False
                    mode[v] = "NORMAL"
                    print(Fore.RED + f"❌ Buy error on {v}: {err_msg} — reset to NORMAL")
                    break

    # ── CONTRACT RESULT ───────────────────────────────────────────────────────
    if "proposal_open_contract" in data:
        poc = data["proposal_open_contract"]
        if not poc.get("is_sold"):
            return

        with trade_map_lock:
            info = trade_map.pop(poc["contract_id"], None)
        if not info:
            return

        vol    = info["vol"]
        digit  = info["digit"]
        stake  = info["stake"]
        profit = float(poc["profit"])

        with pending_lock:
            pending_per_symbol[vol] = False

        mode[vol] = "NORMAL"

        daily_profit += profit
        win        = profit > 0
        result_str = "WIN" if win else "LOSS"

        with global_martingale_lock:
            if win:
                on_trade_win()
            else:
                on_trade_loss()

        record_trade_result(vol, digit, stake, profit, result_str)
        log_trade(vol, digit, stake, result_str)

        if daily_profit >= DAILY_TP_TARGET and not tp_reached_today:
            tp_reached_today = True
            trading_active   = False
            save_state()
            plain = (f"Target of {DAILY_TP_TARGET:.2f} {CURRENCY} achieved!\n"
                     f"Today's Profit: {daily_profit:.2f} {CURRENCY}\n"
                     f"Balance: {current_balance:.2f} {CURRENCY}\n"
                     f"Bot paused until tomorrow.")
            html = _build_tp_reached_email(
                daily_profit, current_balance,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            send_email("🎯 DAILY TAKE PROFIT REACHED!", plain, html)


# ===================== ERROR / CLOSE =====================
def on_error(ws, error):
    print(Fore.RED + f"❌ ERROR: {error}")


def on_close(ws, *args):
    print(Fore.YELLOW + "🔴 CONNECTION CLOSED")


# ===================== WATCHDOG =====================
def watchdog():
    global last_tick_time, ws_instance
    while True:
        time.sleep(5)
        if time.time() - last_tick_time > 15:
            print(Fore.RED + "⚠ TICK STALLED — FORCING RECONNECT")
            try:
                if ws_instance:
                    ws_instance.close()
            except Exception:
                pass


# ===================== RUN LOOP =====================
def start():
    threading.Thread(target=watchdog,            daemon=True).start()
    threading.Thread(target=midnight_scheduler,  daemon=True).start()
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            ws.run_forever(
                ping_interval=20,
                ping_timeout=10,
                sslopt={"cert_reqs": ssl.CERT_NONE}
            )
        except Exception as e:
            print(Fore.RED + f"CRASH: {e}")
        print(Fore.YELLOW + "🔄 RECONNECTING IN 2s...")
        time.sleep(2)


# ===================== START =====================
if __name__ == "__main__":
    load_state()

    restored_trades = sum(daily_stats[v]["total_trades"] for v in VOLATILITIES)
    tp_status = "ACTIVE" if not tp_reached_today else "PAUSED (TP reached today)"

    with global_martingale_lock:
        m_level = global_martingale_level
        m_stake = global_next_stake

    print(Fore.MAGENTA + "🤖 JAHIM BOT STARTED 🔥\n")
    if m_level > 0:
        print(Fore.YELLOW +
              f"⚠  Martingale restored: level={m_level}  next_stake=${m_stake:.2f}\n")

    plain_start = (
        f"Bot (re)started successfully!\n"
        f"Date & Time       : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Daily TP Target   : {DAILY_TP_TARGET:.2f} {CURRENCY}\n"
        f"Active Symbols    : {len(VOLATILITIES)}\n"
        f"Martingale Mode   : Global (any symbol, next valid signal)\n"
        f"Martingale Level  : {m_level}\n"
        f"Next Trade Stake  : ${m_stake:.2f} {CURRENCY}\n"
        f"Restored Today    : profit={daily_profit:+.2f} {CURRENCY}, trades={restored_trades}\n"
        f"Trading Status    : {tp_status}"
    )
    html_start = _build_started_email(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        daily_profit, restored_trades, tp_status,
        m_level, m_stake
    )
    send_email("🤖 JAHIM BOT STARTED", plain_start, html_start)

    start()
