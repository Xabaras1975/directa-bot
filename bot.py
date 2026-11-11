import os
import json
import logging
import warnings
import asyncio
from datetime import datetime
from threading import Thread, Lock
from http.server import BaseHTTPRequestHandler, HTTPServer

import yfinance as yf
import pandas as pd
import numpy as np
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

warnings.filterwarnings("ignore")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Lock per garantire che yfinance non mescoli mai i prezzi tra due chiamate
YF_LOCK = Lock()

# ==============================================================================
# 1. CONFIGURAZIONE E PARAMETRI QUANTITATIVI (ITA + WALL STREET + GEM)
# ==============================================================================
TOKEN = "8645159870:AAHEj0C5ffCoU_6a5ujTNXaWkYvAdqDmOe4"
PORT = int(os.environ.get("PORT", 10000))

PORTAFOGLIO_FILE = "portafoglio.json"
STORICO_FILE = "storico_trades.json"

# 24 Titoli Liquidi Piazza Affari (FTSE MIB / Mid Cap)
TICKERS_ITA = [
    "STLAM.MI", "RACE.MI", "MAIRE.MI", "WBD.MI", "SFER.MI",
    "ISP.MI", "UCG.MI", "BPE.MI", "BMED.MI", "G.MI",
    "ENEL.MI", "TRN.MI", "SRG.MI", "IG.MI", "A2A.MI", "HER.MI", "IRE.MI",
    "ENI.MI", "OVS.MI", "PRY.MI", "LDO.MI", "MONC.MI", "PST.MI", "TEN.MI"
]

# 30 Titoli Liquidi Wall Street (NYSE / NASDAQ)
TICKERS_USA = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD",
    "AVGO", "NFLX", "CRM", "ADBE", "ORCL", "INTC", "CSCO", "QCOM",
    "JPM", "V", "MA", "BAC", "GS", "UNH", "LLY", "JNJ",
    "WMT", "COST", "HD", "PG", "KO", "DIS"
]

# Sottoinsieme di Big Tech USA negoziabili anche su Borsa Italiana GEM (a €3 di fee)
BIG_TECH_GEM = {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX", "INTC"}

TICKERS = TICKERS_ITA + TICKERS_USA

# Parametri Core
RSI_MAX_TRIGGER = 36.0
SL_ATR_MULT = 1.5
MAX_GIORNI_HOLD = 15

# Parametri Specifici per Mercato (Ripristinato Breakeven rapido +2.5% anche su USA con Size €2.000)
PARAMS_MERCATO = {
    "ITA": {
        "tp_pct": 0.042,       # +4.2% Take Profit Italia
        "be_trig": 0.025,      # +2.5% Attivazione Breakeven
        "be_lock": 0.018,      # +1.8% Stop Protetto (+€15.00 netti su €1.000)
        "fee": 3.0,            # €1.50 + €1.50 su Borsa Italiana
        "cap_def": 1000.0,     # Taglia standard Italia €1.000 (personalizzabile)
    },
    "USA": {
        "tp_pct": 0.045,       # +4.5% Take Profit USA (+€73.50 netti su €2.000)
        "be_trig": 0.025,      # +2.5% Attivazione Breakeven rapida (evita che +3% diventi Stop Loss!)
        "be_lock": 0.018,      # +1.8% Stop Protetto (+€19.50 netti su €2.000 pagate le fee USA)
        "fee": 16.5,           # $9.00 + $9.00 su NYSE/NASDAQ (~€16.50)
        "cap_def": 2000.0,     # Taglia consigliata Wall Street €2.000 per abbattere le fee fisse
    }
}

# ==============================================================================
# 2. WEB SERVER NATIVO KEEP-ALIVE PER RENDER
# ==============================================================================
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot Quantitativo Dual-Market (ITA + USA) Operativo.")

    def log_message(self, format, *args):
        return

def avvia_keep_alive():
    def run_server():
        try:
            server = HTTPServer(("0.0.0.0", PORT), KeepAliveHandler)
            server.serve_forever()
        except Exception as e:
            logger.error(f"Errore avvio server HTTP: {e}")
    t = Thread(target=run_server, daemon=True)
    t.start()

# ==============================================================================
# 3. GESTIONE DATABASE LOCALE (JSON) & DOWNLOAD PREZZI REALI
# ==============================================================================
def carica_json(percorso, default):
    if os.path.exists(percorso):
        try:
            with open(percorso, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def salva_json(percorso, dati):
    with open(percorso, "w", encoding="utf-8") as f:
        json.dump(dati, f, indent=4, ensure_ascii=False)

def is_mercato_ita(ticker_str):
    t = ticker_str.upper().replace(".MI", "")
    return f"{t}.MI" in TICKERS_ITA or ticker_str.upper().endswith(".MI")

def scarica_ticker_sicuro(simbolo, periodo="1y"):
    """Scarica un singolo ticker con auto_adjust=False (Prezzi Reali di Borsa identici a Directa)."""
    with YF_LOCK:
        try:
            t_obj = yf.Ticker(simbolo)
            df = t_obj.history(period=periodo, interval="1d", auto_adjust=False)
            if df is None or df.empty:
                return pd.DataFrame()
            cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
            df_clean = df[cols].dropna(subset=["Close"])
            if df_clean.index.tz is not None:
                df_clean.index = df_clean.index.tz_localize(None)
            return df_clean
        except Exception as e:
            logger.error(f"Errore download sicuro {simbolo}: {e}")
            return pd.DataFrame()

# ==============================================================================
# 4. MOTORE DI ANALISI QUANTITATIVA & 3 CONFLUENZE PURE (FIBO, BB, MACD)
# ==============================================================================
def prepara_indicatori_completi(df):
    d = df.copy()
    if d.index.tz is not None:
        d.index = d.index.tz_localize(None)

    c = d["Close"]
    h = d["High"]
    l = d["Low"]

    # 1. SMA 50 & 200
    d["sma50"] = c.rolling(50).mean()
    d["sma200"] = c.rolling(200).mean()

    # 2. RSI(14)
    delta = c.diff()
    g = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = g / loss.replace(0, np.nan)
    d["rsi"] = (100 - (100 / (1 + rs))).fillna(50)

    # 3. ATR(14)
    tr = pd.concat(
        [h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1
    ).max(axis=1)
    d["atr"] = tr.rolling(14).mean()

    # 4. Fibonacci Retracement (Swing 60 sedute)
    roll_max = h.rolling(60).max().shift(1)
    roll_min = l.rolling(60).min().shift(1)
    rng = (roll_max - roll_min).replace(0, np.nan)
    d["fib_retr"] = ((roll_max - c.shift(1)) / rng) * 100.0

    # 5. Bollinger Bands (20, 2)
    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    d["bb_lower"] = sma20 - 2.0 * std20

    # 6. MACD Histogram (12, 26, 9)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    sig_line = macd_line.ewm(span=9, adjust=False).mean()
    d["macd_hist"] = macd_line - sig_line

    return d

def verifica_regime_indice(simbolo):
    """Controlla se l'indice (FTSEMIB.MI o ^GSPC) staziona sopra la SMA 200."""
    df_idx = scarica_ticker_sicuro(simbolo, periodo="1y")
    if df_idx.empty or len(df_idx) < 200:
        return True, 0.0, 0.0, 0.0

    close_val = float(df_idx["Close"].iloc[-1])
    sma200_val = float(df_idx["Close"].rolling(200).mean().iloc[-1])
    dist_pct = ((close_val - sma200_val) / sma200_val) * 100.0
    return (close_val >= sma200_val), close_val, sma200_val, dist_pct

def esegui_scansione_mercato():
    """Analizza tutti i 54 titoli (ITA + USA) separando i segnali freschi da quelli già partiti."""
    mib_ok, mib_p, mib_sma, mib_dist = verifica_regime_indice("FTSEMIB.MI")
    sp_ok, sp_p, sp_sma, sp_dist = verifica_regime_indice("^GSPC")

    candidati_sniper = []
    classifica_completa = []

    with YF_LOCK:
        raw_batch = yf.download(
            TICKERS, period="1y", interval="1d", group_by="ticker",
            progress=False, auto_adjust=False, threads=False
        )

    for ticker in TICKERS:
        try:
            d_raw = raw_batch[ticker].dropna(how="all") if isinstance(raw_batch.columns, pd.MultiIndex) else raw_batch
            if d_raw.empty or len(d_raw) < 205:
                continue

            d = prepara_indicatori_completi(d_raw).dropna()
            if len(d) < 5:
                continue

            row_oggi = d.iloc[-1]
            row_ieri = d.iloc[-2]

            p = float(row_oggi["Close"])
            op = float(row_oggi["Open"])
            p_prev = float(row_ieri["Close"])
            s50_prev = float(row_ieri["sma50"])
            s200_prev = float(row_ieri["sma200"])
            rsi_prev = float(row_ieri["rsi"])
            rsi_oggi = float(row_oggi["rsi"])
            atr_val = float(row_oggi["atr"])

            is_ita = ticker.endswith(".MI")
            mercato = "ITA" if is_ita else "USA"
            valuta = "€" if is_ita else "$"
            flag = "🇮🇹" if is_ita else "🇺🇸"
            regime_ok = mib_ok if is_ita else sp_ok
            prm = PARAMS_MERCATO[mercato]

            # 3 Pilastri Core
            cond_trend = (p_prev > s50_prev > s200_prev)
            cond_rsi = (rsi_prev <= RSI_MAX_TRIGGER)
            cond_reversal = (p > op and p > p_prev)

            # Controllo di freschezza del segnale (quanto è già salito oggi rispetto a ieri)
            rimbalzo_oggi_pct = ((p - p_prev) / p_prev) * 100.0
            segnale_fresco = (rimbalzo_oggi_pct <= 2.2) and (rsi_oggi <= 44.0)

            score = 0
            if cond_trend: score += 40
            if rsi_prev <= 42.0: score += 20
            if cond_rsi: score += 20
            if cond_reversal: score += 20

            # 3 Confluenze Pure (Fibonacci, Bollinger, MACD Tick-Up)
            f_fibo = (30.0 <= float(row_ieri["fib_retr"]) <= 68.0)
            f_bb = (float(row_ieri["Low"]) <= float(row_ieri["bb_lower"]) * 1.008) or (float(row_oggi["Low"]) <= float(row_oggi["bb_lower"]) * 1.008)
            f_macd = (float(row_oggi["macd_hist"]) > float(row_ieri["macd_hist"]))
            n_conf = int(f_fibo) + int(f_bb) + int(f_macd)

            badges = []
            if f_fibo: badges.append(f"📐Fibo({row_ieri['fib_retr']:.0f}%)")
            if f_bb: badges.append("🎸BB")
            if f_macd: badges.append("📈MACD")
            conf_str = " ".join(badges) if badges else "Nessuna"

            tp_price = p * (1.0 + prm["tp_pct"])
            sl_price = p - (SL_ATR_MULT * atr_val)
            be_trigger = p * (1.0 + prm["be_trig"])
            be_lock = p * (1.0 + prm["be_lock"])

            info_titolo = {
                "ticker": ticker.replace(".MI", ""),
                "ticker_full": ticker,
                "mercato": mercato,
                "flag": flag,
                "valuta": valuta,
                "prezzo": p,
                "rimbalzo_oggi_pct": rimbalzo_oggi_pct,
                "fresco": segnale_fresco,
                "rsi_ieri": rsi_prev,
                "rsi_oggi": rsi_oggi,
                "trend_ok": cond_trend,
                "reversal_ok": cond_reversal,
                "score": score,
                "n_conf": n_conf,
                "conf_str": conf_str,
                "tp": tp_price,
                "tp_pct": prm["tp_pct"],
                "sl": sl_price,
                "be_trigger": be_trigger,
                "be_trig_pct": prm["be_trig"],
                "be_lock": be_lock,
                "be_lock_pct": prm["be_lock"],
                "atr": atr_val,
            }
            classifica_completa.append(info_titolo)

            if regime_ok and cond_trend and cond_rsi and cond_reversal:
                candidati_sniper.append(info_titolo)

        except Exception as e:
            logger.error(f"Errore scansione {ticker}: {e}")
            continue

    candidati_sniper.sort(key=lambda x: (-int(x["fresco"]), -x["n_conf"], x["rsi_ieri"]))
    classifica_completa.sort(key=lambda x: (-x["score"], -x["n_conf"], x["rsi_ieri"]))

    regime_info = {
        "ITA": {"ok": mib_ok, "prezzo": mib_p, "sma200": mib_sma, "dist_pct": mib_dist},
        "USA": {"ok": sp_ok, "prezzo": sp_p, "sma200": sp_sma, "dist_pct": sp_dist},
    }

    return candidati_sniper, classifica_completa, regime_info

def calcola_backtest_storico():
    """Backtest completo sugli ultimi 2 anni: mostra Italia, Wall Street (Size €2.000) e GEM (€3 fee)."""
    df_mib = scarica_ticker_sicuro("FTSEMIB.MI", periodo="2y")
    df_sp = scarica_ticker_sicuro("^GSPC", periodo="2y")

    mib_bull_series = (df_mib["Close"] >= df_mib["Close"].rolling(200).mean()) if not df_mib.empty else pd.Series(dtype=bool)
    sp_bull_series = (df_sp["Close"] >= df_sp["Close"].rolling(200).mean()) if not df_sp.empty else pd.Series(dtype=bool)

    with YF_LOCK:
        raw_batch = yf.download(
            TICKERS, period="2y", interval="1d", group_by="ticker",
            progress=False, auto_adjust=False, threads=False
        )

    tutti_i_segnali = []

    for ticker in TICKERS:
        try:
            df_raw = raw_batch[ticker].dropna(how="all") if isinstance(raw_batch.columns, pd.MultiIndex) else raw_batch
            if df_raw.empty or len(df_raw) < 220:
                continue

            d = prepara_indicatori_completi(df_raw).dropna()
            is_ita = ticker.endswith(".MI")
            mercato = "ITA" if is_ita else "USA"
            prm = PARAMS_MERCATO[mercato]
            cap_test = prm["cap_def"]     # €1.000 per ITA, €2.000 per USA
            fee = prm["fee"]              # €3.00 per ITA, €16.50 per USA
            bull_map = mib_bull_series if is_ita else sp_bull_series

            n_rows = len(d)
            i = 2
            while i < n_rows - MAX_GIORNI_HOLD - 1:
                row_oggi = d.iloc[i]
                row_ieri = d.iloc[i - 1]
                data_curr = d.index[i]

                regime_storico_ok = True
                if not bull_map.empty and data_curr in bull_map.index:
                    regime_storico_ok = bool(bull_map.loc[data_curr])

                cond_trend = (row_ieri["Close"] > row_ieri["sma50"] > row_ieri["sma200"])
                cond_rsi = (row_ieri["rsi"] <= RSI_MAX_TRIGGER)
                cond_rev = (row_oggi["Close"] > row_oggi["Open"]) and (row_oggi["Close"] > row_ieri["Close"])

                if regime_storico_ok and cond_trend and cond_rsi and cond_rev:
                    f_fibo = (30.0 <= row_ieri["fib_retr"] <= 68.0)
                    f_bb = (row_ieri["Low"] <= row_ieri["bb_lower"] * 1.008) or (row_oggi["Low"] <= row_oggi["bb_lower"] * 1.008)
                    f_macd = (row_oggi["macd_hist"] > row_ieri["macd_hist"])
                    n_conf = int(f_fibo) + int(f_bb) + int(f_macd)

                    p_in = float(row_oggi["Close"])
                    atr_in = float(row_oggi["atr"])
                    tp = p_in * (1.0 + prm["tp_pct"])
                    sl = p_in - (SL_ATR_MULT * atr_in)
                    be_trig = p_in * (1.0 + prm["be_trig"])
                    be_lock = p_in * (1.0 + prm["be_lock"])

                    be_attivo = False
                    p_out = p_in
                    giorni_hold = MAX_GIORNI_HOLD

                    for g_idx in range(1, MAX_GIORNI_HOLD + 1):
                        bar = d.iloc[i + g_idx]
                        if bar["Low"] <= sl:
                            p_out = sl
                            giorni_hold = g_idx
                            break
                        if bar["High"] >= tp:
                            p_out = tp
                            giorni_hold = g_idx
                            break
                        if not be_attivo and bar["High"] >= be_trig:
                            be_attivo = True
                            sl = be_lock
                        if g_idx == MAX_GIORNI_HOLD:
                            p_out = float(bar["Close"])

                    ret_pct = (p_out - p_in) / p_in
                    pnl_netto = (ret_pct * cap_test) - fee
                    # Simulazione parallela: stesso trade USA se eseguito su GEM Milano (Size €1.000, Fee €3.00)
                    pnl_gem_1k = (ret_pct * 1000.0) - 3.0

                    tutti_i_segnali.append({
                        "mercato": mercato,
                        "pnl_netto": pnl_netto,
                        "win": 1 if pnl_netto > 0 else 0,
                        "pnl_gem": pnl_gem_1k,
                        "win_gem": 1 if pnl_gem_1k > 0 else 0,
                        "fibo": f_fibo,
                        "bb": f_bb,
                        "macd": f_macd,
                        "confluenze": n_conf,
                    })
                    i += giorni_hold
                else:
                    i += 1
        except Exception:
            continue

    df_res = pd.DataFrame(tutti_i_segnali)
    if df_res.empty:
        return "❌ Nessun dato sufficiente per il backtest."

    def fmt_riga(titolo, sub, col_pnl="pnl_netto", col_win="win"):
        if len(sub) == 0:
            return f"• **{titolo}**: 0 trade"
        n_t = len(sub)
        wr = sub[col_win].mean() * 100.0
        pnl = sub[col_pnl].sum()
        vinte = sub[sub[col_pnl] > 0][col_pnl].sum()
        perse = abs(sub[sub[col_pnl] <= 0][col_pnl].sum())
        pf = (vinte / perse) if perse > 0 else 99.9
        return (
            f"🔹 **{titolo}**\n"
            f"   Trade: `{n_t}` | WR: **{wr:.1f}%** | PF: `{pf:.2f}` | Netto: **€{pnl:+.0f}**"
        )

    df_ita = df_res[df_res["mercato"] == "ITA"]
    df_usa_all = df_res[df_res["mercato"] == "USA"]
    df_usa_conf2 = df_usa_all[df_usa_all["confluenze"] >= 2]
    df_tot_ottimale = pd.concat([df_ita[df_ita["confluenze"] >= 1], df_usa_conf2])

    righe = [
        "🧪 **AUDIT BACKTEST DEFINITIVO (2Y — SMA200 + BE +2.5%)**",
        "*(ITA: Size €1.000 Fee €3 | USA: Size €2.000 Fee €16.50, TP +4.5%)*",
        "━━━━━━━━━━━━━━━━━━━━",
        fmt_riga("1. 🇮🇹 PIAZZA AFFARI (Tutti i segnali)", df_ita),
        fmt_riga("2. 🇮🇹 PIAZZA AFFARI (Confluence >= 1)", df_ita[df_ita["confluenze"] >= 1]),
        "━━━━━━━━━━━━━━━━━━━━",
        fmt_riga("3. 🇺🇸 WALL STREET (Tutti - Size €2.000)", df_usa_all),
        fmt_riga("4. 🇺🇸 WALL STREET SNIPER (>= 2 Confluenze)", df_usa_conf2),
        fmt_riga("5. 💡 USA comprati su GEM Milano (Size €1k, Fee €3)", df_usa_conf2, "pnl_gem", "win_gem"),
        "━━━━━━━━━━━━━━━━━━━━",
        fmt_riga("🏆 PORTAFOGLIO COMBINATO (ITA>=1 + USA>=2)", df_tot_ottimale),
    ]
    return "\n".join(righe)

# ==============================================================================
# 5. COCKPIT OPERATIVO TELEGRAM A 4 FASI
# ==============================================================================
def menu_cockpit_tastiera():
    keyboard = [
        [
            InlineKeyboardButton("🎯 1. Scan Segnali", callback_data="cmd_scan"),
            InlineKeyboardButton("📊 Top 12 Watchlist", callback_data="cmd_top10"),
        ],
        [
            InlineKeyboardButton("🏛️ Regimi ITA/USA", callback_data="cmd_mercato"),
            InlineKeyboardButton("💼 2. Portafoglio", callback_data="cmd_portafoglio"),
        ],
        [
            InlineKeyboardButton("📈 3. Statistiche", callback_data="cmd_statistiche"),
            InlineKeyboardButton("🧪 4. Backtest 2Y", callback_data="cmd_backtest"),
        ],
        [
            InlineKeyboardButton("🧭 Menu Cockpit", callback_data="cmd_start"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

TESTO_COCKPIT = (
    "🧭 **COCKPIT QUANTITATIVO — FTSE MIB 🇮🇹 & WALL STREET 🇺🇸**\n"
    "*(3 Confluenze Pure: Fibo, BB, MACD | Breakeven Rapido +2.5%)*\n\n"
    "🟢 **FASE 1 — DECISIONE & ORARI IDEALI**\n"
    "• `/scan` — Cerca i segnali **Score 100%** e ne verifica la freschezza\n"
    "• `/top10` — Watchlist dei 12 titoli più vicini al trigger\n"
    "• `/mercato` — Filtro regime su FTSE MIB e S&P 500 (SMA 200)\n"
    "• ⏰ *Orari:* Italia/GEM `09:00–17:30` | Wall Street `15:30–22:00`\n\n"
    "🟡 **FASE 2 — REGOLE DI PROFITTO (ITA vs USA)**\n"
    "• 🇮🇹 **Italia (Fee €3):** Size `€1.000` | TP `+4.2%` | BE a `+2.5%` (Stop `+1.8%`)\n"
    "• 🇺🇸 **USA (Fee €16.50):** Size `€2.000` (o su GEM `.MI` a €3 fee) | TP `+4.5%` | BE a `+2.5%`\n"
    "• `/portafoglio` — Monitora posizioni, Breakeven e uscite\n"
    "• `/compra TICKER PREZZO [CAPITALE]` — Registra ingresso\n"
    "• `/vendi TICKER PREZZO` — Chiude posizione e calcola P&L netto\n\n"
    "🔵 **FASE 3 — ARCHIVIO & BACKTEST STORICO**\n"
    "• `/statistiche` — Bilancio netto, Win Rate e Profit Factor\n"
    "• `/backtest` — Backtest 2Y separato per Italia, USA (Size €2k) e GEM"
)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    await msg.reply_text(TESTO_COCKPIT, parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())

async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    wait_msg = await msg.reply_text(
        "⏳ *Avvio Backtest Definitivo (2 Anni su 54 titoli ITA + USA con Breakeven +2.5% e spaccato GEM)...*\n"
        "*(Attendere circa 20 secondi)*",
        parse_mode="Markdown"
    )
    report = await asyncio.to_thread(calcola_backtest_storico)
    await wait_msg.edit_text(report, parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())

async def mercato_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    mib_ok, mib_p, mib_sma, mib_dist = await asyncio.to_thread(verifica_regime_indice, "FTSEMIB.MI")
    sp_ok, sp_p, sp_sma, sp_dist = await asyncio.to_thread(verifica_regime_indice, "^GSPC")

    stato_ita = "✅ **BULL MARKET**" if mib_ok else "⛔ **BEAR MARKET (Bloccato)**"
    stato_usa = "✅ **BULL MARKET**" if sp_ok else "⛔ **BEAR MARKET (Bloccato)**"

    testo = (
        f"🏛️ **STATO REGIME DEI MERCATI (SMA 200)**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🇮🇹 **FTSE MIB (Piazza Affari — 09:00/17:30)**\n"
        f"Stato: {stato_ita}\n"
        f"Valore: **{mib_p:,.0f} pt** | SMA 200: **{mib_sma:,.0f} pt** (**{mib_dist:+.2f}%**)\n\n"
        f"🇺🇸 **S&P 500 (Wall Street — 15:30/22:00)**\n"
        f"Stato: {stato_usa}\n"
        f"Valore: **{sp_p:,.0f} pt** | SMA 200: **{sp_sma:,.0f} pt** (**{sp_dist:+.2f}%**)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 *Il bot abilita gli ingressi su ciascun mercato solo quando il rispettivo indice è sopra la SMA 200.*"
    )
    await msg.reply_text(testo, parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    wait_msg = await msg.reply_text("⏳ *Scansione quantitativa in corso su 54 titoli (FTSE MIB 🇮🇹 + Wall Street 🇺🇸)...*", parse_mode="Markdown")

    portafoglio = carica_json(PORTAFOGLIO_FILE, {})
    candidati, _, regime = await asyncio.to_thread(esegui_scansione_mercato)

    ita_ok = regime["ITA"]["ok"]
    usa_ok = regime["USA"]["ok"]

    if not ita_ok and not usa_ok:
        testo = (
            f"⛔ **SEMAFORO ROSSO GLOBALE — INDICI SOTTO SMA 200**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🇮🇹 FTSE MIB: **{regime['ITA']['prezzo']:,.0f}** (SMA 200: **{regime['ITA']['sma200']:,.0f}**)\n"
            f"🇺🇸 S&P 500: **{regime['USA']['prezzo']:,.0f}** (SMA 200: **{regime['USA']['sma200']:,.0f}**)\n"
            f"Nessun segnale generato per protezione del capitale."
        )
        await wait_msg.edit_text(testo, parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())
        return

    if not candidati:
        testo = (
            f"🏛️ **Regimi:** 🇮🇹 MIB {'✅' if ita_ok else '⛔'} | 🇺🇸 S&P 500 {'✅' if usa_ok else '⛔'}\n"
            f"🔍 **NESSUN SEGNALE 'SCORE 100%' OGGI**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Nessun titolo su 54 (ITA + USA) presenta contemporaneamente:\n"
            f"1. Trend rialzista (`P > SMA50 > SMA200`)\n"
            f"2. Ipervenduto marcato (`RSI Ieri <= {RSI_MAX_TRIGGER}`)\n"
            f"3. Candela verde di inversione (`Close > Open` e `Close > Ieri`)\n\n"
            f"💡 *Disciplina Sniper: attendere l'allineamento perfetto. Controlla `/top10` per i titoli più vicini.*"
        )
        await wait_msg.edit_text(testo, parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())
        return

    blocchi_testo = [
        f"🎯 **SEGNALI RILEVATI ({len(candidati)} Titoli a Score 100%)**",
        f"🏛️ Regimi: 🇮🇹 MIB {'✅' if ita_ok else '⛔'} | 🇺🇸 S&P 500 {'✅' if usa_ok else '⛔'}",
        "━━━━━━━━━━━━━━━━━━━━"
    ]

    for top in candidati[:3]:
        mercato = top["mercato"]
        prm = PARAMS_MERCATO[mercato]
        cap_sim = prm["cap_def"]
        fee_sim = prm["fee"]
        val = top["valuta"]

        gain_netto = (cap_sim * top["tp_pct"]) - fee_sim
        rischio_pct = ((top["prezzo"] - top["sl"]) / top["prezzo"]) * 100.0

        # Indicatore di Attendibilità / Timing Live (risponde esattamente al caso UNH!)
        if top["fresco"]:
            stato_timing = f"🟢 **SEGNALE FRESCO ATTIVO** *(Oggi {top['rimbalzo_oggi_pct']:+.2f}%, RSI {top['rsi_oggi']:.1f})*"
        else:
            stato_timing = f"⚠️ **GIÀ PARTITO / IN CORSA** *(Oggi {top['rimbalzo_oggi_pct']:+.2f}%, RSI salito a {top['rsi_oggi']:.1f} — Non inseguire!)*"

        nota_gem = ""
        if mercato == "USA" and top["ticker"] in BIG_TECH_GEM:
            nota_gem = f"\n💡 *Disponibile anche su Borsa Italiana GEM (`{top['ticker']}.MI`) in Euro con soli €3 di commissione!*"

        blocco = (
            f"🏆 {top['flag']} **{top['ticker']}** ({mercato}) — Prezzo: **{val}{top['prezzo']:.2f}**\n"
            f"⏱️ Timing: {stato_timing}\n"
            f"📉 RSI Ieri: **{top['rsi_ieri']:.1f}** 🔥 | Oggi: **{top['rsi_oggi']:.1f}**\n"
            f"🧩 Confluenze ({top['n_conf']}/3): `{top['conf_str']}`\n"
            f"• **Take Profit (+{top['tp_pct']*100:.1f}%):** `{val}{top['tp']:.2f}` *(Netto su €{cap_sim:.0f}: +€{gain_netto:.2f})*\n"
            f"• **Stop Loss (1.5x ATR):** `{val}{top['sl']:.2f}` *(-{rischio_pct:.2f}%)*\n"
            f"• **Breakeven (+{top['be_trig_pct']*100:.1f}%):** a `{val}{top['be_trigger']:.2f}` ➔ Stop a `{val}{top['be_lock']:.2f}` (+{top['be_lock_pct']*100:.1f}%){nota_gem}\n"
            f"📲 Registra: `/compra {top['ticker']} {top['prezzo']:.2f} {cap_sim:.0f}`\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        blocchi_testo.append(blocco)

    if len(portafoglio) > 0:
        blocchi_testo.append(f"⚠️ *Nota: Hai già {len(portafoglio)} posizione/i attiva/e in portafoglio.*")

    await wait_msg.edit_text("\n".join(blocchi_testo), parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())

async def top10_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    wait_msg = await msg.reply_text("⏳ *Elaborazione Watchlist Top 12 (Piazza Affari + Wall Street)...*", parse_mode="Markdown")

    _, classifica, regime = await asyncio.to_thread(esegui_scansione_mercato)
    if not classifica:
        await wait_msg.edit_text("❌ Impossibile scaricare i dati di mercato.", reply_markup=menu_cockpit_tastiera())
        return

    ita_ok = regime["ITA"]["ok"]
    usa_ok = regime["USA"]["ok"]

    righe = [
        "📊 **WATCHLIST TOP 12 — PROSSIMITÀ AL TRIGGER (ITA & USA)**",
        f"🏛️ Regimi: 🇮🇹 MIB {'✅ Sano' if ita_ok else '⛔ Sotto SMA'} | 🇺🇸 S&P {'✅ Sano' if usa_ok else '⛔ Sotto SMA'}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    for idx, item in enumerate(classifica[:12], start=1):
        icona_t = "🟢" if item["trend_ok"] else "🔴"
        icona_r = "🔥" if item["rsi_ieri"] <= RSI_MAX_TRIGGER else "⏳"
        val = item["valuta"]
        avviso_100 = ""
        if item["score"] == 100:
            avviso_100 = " 🟢*(Fresco)*" if item["fresco"] else " ⚠️*(Già rimbalzato)*"

        righe.append(
            f"**{idx}. {item['flag']} {item['ticker']}** — {val}{item['prezzo']:.2f} | Score: **{item['score']}%**{avviso_100}\n"
            f"   Trend: {icona_t} | RSI Ieri: **{item['rsi_ieri']:.1f}** {icona_r} | Oggi: {item['rsi_oggi']:.1f}\n"
            f"   🧩 Extra ({item['n_conf']}/3): `{item['conf_str']}`"
        )

    righe.append("━━━━━━━━━━━━━━━━━━━━")
    righe.append("💡 *Il segnale è eseguibile a Score 100% 🟢(Fresco) prima che l'RSI Oggi superi 44.*")
    await wait_msg.edit_text("\n".join(righe), parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())

async def compra_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "⚠️ **Uso corretto:** `/compra TICKER PREZZO [CAPITALE_EUR]`\n"
            "Esempi:\n"
            "• `/compra ENI 24.75 1000` (Italia — Fee €3)\n"
            "• `/compra UNH 405.12 2000` (USA — Fee €16.50)\n"
            "• `/compra AAPL.MI 320.50 1000` (USA su GEM Milano — Fee €3)",
            parse_mode="Markdown",
        )
        return

    raw_ticker = args[0].upper()
    is_ita = is_mercato_ita(raw_ticker)
    ticker_clean = raw_ticker.replace(".MI", "")
    ticker_full = f"{ticker_clean}.MI" if is_ita else ticker_clean
    mercato = "ITA" if is_ita else "USA"
    valuta = "€" if is_ita else "$"
    flag = "🇮🇹" if is_ita else "🇺🇸"
    prm = PARAMS_MERCATO[mercato]

    try:
        prezzo_in = float(args[1].replace(",", "."))
        capitale = float(args[2].replace(",", ".")) if len(args) >= 3 else prm["cap_def"]
    except ValueError:
        await update.message.reply_text("❌ Inserisci valori numerici validi per prezzo e capitale.")
        return

    atr_val = prezzo_in * 0.022
    try:
        d = await asyncio.to_thread(scarica_ticker_sicuro, ticker_full, "3mo")
        if not d.empty and len(d) >= 20:
            d_ind = prepara_indicatori_completi(d)
            atr_val = float(d_ind["atr"].iloc[-1])
    except Exception:
        pass

    tp_price = prezzo_in * (1.0 + prm["tp_pct"])
    sl_iniziale = prezzo_in - (SL_ATR_MULT * atr_val)
    be_trigger = prezzo_in * (1.0 + prm["be_trig"])
    be_lock = prezzo_in * (1.0 + prm["be_lock"])

    portafoglio = carica_json(PORTAFOGLIO_FILE, {})
    portafoglio[ticker_clean] = {
        "ticker_full": ticker_full,
        "mercato": mercato,
        "valuta": valuta,
        "flag": flag,
        "prezzo_in": prezzo_in,
        "capitale": capitale,
        "tp": tp_price,
        "tp_pct": prm["tp_pct"],
        "sl": sl_iniziale,
        "be_trigger": be_trigger,
        "be_trig_pct": prm["be_trig"],
        "be_lock": be_lock,
        "be_lock_pct": prm["be_lock"],
        "be_attivo": False,
        "data_in": datetime.now().strftime("%Y-%m-%d"),
    }
    salva_json(PORTAFOGLIO_FILE, portafoglio)

    await update.message.reply_text(
        f"✅ **POSIZIONE REGISTRATA IN PORTAFOGLIO**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Titolo: {flag} **{ticker_clean}** ({mercato} — Fee €{prm['fee']:.2f})\n"
        f"💶 Prezzo Carico: **{valuta}{prezzo_in:.4f}** (Taglia: **€{capitale:.0f}**)\n"
        f"🎯 Take Profit (+{prm['tp_pct']*100:.1f}%): **{valuta}{tp_price:.4f}**\n"
        f"🛡️ Stop Loss (1.5x ATR): **{valuta}{sl_iniziale:.4f}**\n"
        f"🔒 Trigger Breakeven (+{prm['be_trig']*100:.1f}%): **{valuta}{be_trigger:.4f}** *(Stop sale a {valuta}{be_lock:.4f} | +{prm['be_lock']*100:.1f}%)*",
        parse_mode="Markdown",
        reply_markup=menu_cockpit_tastiera(),
    )

async def portafoglio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    portafoglio = carica_json(PORTAFOGLIO_FILE, {})

    if not portafoglio:
        await msg.reply_text(
            "💼 **PORTAFOGLIO ATTIVO VUOTO**\nNessuna operazione aperta al momento. Usa `/scan` per cercare un setup.",
            parse_mode="Markdown",
            reply_markup=menu_cockpit_tastiera(),
        )
        return

    wait_msg = await msg.reply_text("⏳ *Aggiornamento quotazioni live ITA/USA e verifica livelli...*", parse_mode="Markdown")
    righe = ["💼 **MONITORAGGIO PORTAFOGLIO LIVE**", "━━━━━━━━━━━━━━━━━━━━"]
    modificato = False

    for ticker, pos in portafoglio.items():
        p_in = pos["prezzo_in"]
        mercato = pos.get("mercato", "ITA" if is_mercato_ita(pos["ticker_full"]) else "USA")
        prm = PARAMS_MERCATO[mercato]
        val = pos.get("valuta", "€" if mercato == "ITA" else "$")
        flag = pos.get("flag", "🇮🇹" if mercato == "ITA" else "🇺🇸")
        fee = prm["fee"]
        cap = pos.get("capitale", prm["cap_def"])
        be_lock_pct = pos.get("be_lock_pct", prm["be_lock"])

        p_attuale = p_in
        max_oggi = p_in

        try:
            d = await asyncio.to_thread(scarica_ticker_sicuro, pos["ticker_full"], "5d")
            if not d.empty:
                p_attuale = float(d["Close"].iloc[-1])
                max_oggi = float(d["High"].iloc[-1])
        except Exception:
            pass

        avviso_azione = "⏳ *Mantieni posizione*"
        if not pos.get("be_attivo", False) and (max_oggi >= pos["be_trigger"] or p_attuale >= pos["be_trigger"]):
            pos["be_attivo"] = True
            pos["sl"] = pos["be_lock"]
            modificato = True
            avviso_azione = f"🚨 **SPOSTA STOP SU DIRECTA A {val}{pos['be_lock']:.4f} (+{be_lock_pct*100:.1f}% Protetto!)**"
        elif pos.get("be_attivo", False):
            avviso_azione = f"🔒 *Breakeven Attivo (Stop protetto a {val}{pos['sl']:.4f})*"

        if p_attuale >= pos["tp"]:
            avviso_azione = f"🎯 **TARGET RAGGIUNTO! Vendi (`/vendi {ticker} {p_attuale:.4f}`)**"
        elif p_attuale <= pos["sl"]:
            avviso_azione = f"🛑 **STOP COLPITO! Chiudi (`/vendi {ticker} {p_attuale:.4f}`)**"

        var_pct = ((p_attuale - p_in) / p_in) * 100.0
        pnl_netto = (((p_attuale - p_in) / p_in) * cap) - fee
        giorni_trascorsi = (datetime.now() - datetime.strptime(pos["data_in"], "%Y-%m-%d")).days

        icona_pnl = "🟢" if pnl_netto >= 0 else "🔴"
        righe.append(
            f"📌 {flag} **{ticker}** [Size: €{cap:.0f}] (In: {val}{p_in:.2f} | Ora: **{val}{p_attuale:.2f}**)\n"
            f"   {icona_pnl} Var: **{var_pct:+.2f}%** | P&L Netto Fee: **€{pnl_netto:+.2f}**\n"
            f"   🎯 TP: `{val}{pos['tp']:.2f}` | 🛡️ SL: `{val}{pos['sl']:.2f}`\n"
            f"   ⏱️ Giorni: {giorni_trascorsi}/{MAX_GIORNI_HOLD} | {avviso_azione}\n"
            f"   👉 Chiudi: `/vendi {ticker} {p_attuale:.2f}`\n"
        )

    if modificato:
        salva_json(PORTAFOGLIO_FILE, portafoglio)

    await wait_msg.edit_text("\n".join(righe), parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())

async def vendi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "⚠️ **Uso corretto:** `/vendi TICKER PREZZO_USCITA`\nEsempio: `/vendi UNH 423.35`",
            parse_mode="Markdown",
        )
        return

    ticker_clean = args[0].upper().replace(".MI", "")
    try:
        prezzo_out = float(args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Prezzo di uscita non valido.")
        return

    portafoglio = carica_json(PORTAFOGLIO_FILE, {})
    if ticker_clean not in portafoglio:
        await update.message.reply_text(f"❌ Il titolo **{ticker_clean}** non è in portafoglio.", parse_mode="Markdown")
        return

    pos = portafoglio.pop(ticker_clean)
    salva_json(PORTAFOGLIO_FILE, portafoglio)

    mercato = pos.get("mercato", "ITA" if is_mercato_ita(pos["ticker_full"]) else "USA")
    prm = PARAMS_MERCATO[mercato]
    val = pos.get("valuta", "€" if mercato == "ITA" else "$")
    flag = pos.get("flag", "🇮🇹" if mercato == "ITA" else "🇺🇸")
    fee = prm["fee"]

    p_in = pos["prezzo_in"]
    cap = pos.get("capitale", prm["cap_def"])
    var_pct = ((prezzo_out - p_in) / p_in) * 100.0
    pnl_lordo = ((prezzo_out - p_in) / p_in) * cap
    pnl_netto = pnl_lordo - fee
    esito = "WIN" if pnl_netto > 0 else "LOSS"

    storico = carica_json(STORICO_FILE, [])
    storico.append({
        "ticker": ticker_clean,
        "mercato": mercato,
        "data_in": pos["data_in"],
        "data_out": datetime.now().strftime("%Y-%m-%d"),
        "prezzo_in": p_in,
        "prezzo_out": prezzo_out,
        "capitale": cap,
        "fee": fee,
        "var_pct": round(var_pct, 2),
        "pnl_lordo": round(pnl_lordo, 2),
        "pnl_netto": round(pnl_netto, 2),
        "esito": esito,
    })
    salva_json(STORICO_FILE, storico)

    emoji = "✅ **TRADE CHIUSO IN PROFITTO NETTO**" if esito == "WIN" else "❌ **TRADE CHIUSO IN PERDITA**"
    await update.message.reply_text(
        f"{emoji}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Titolo: {flag} **{ticker_clean}** ({mercato} — Size: €{cap:.0f})\n"
        f"💶 Carico: {val}{p_in:.4f} ➔ Uscita: **{val}{prezzo_out:.4f}** ({var_pct:+.2f}%)\n"
        f"💰 P&L Lordo: €{pnl_lordo:+.2f}\n"
        f"🏦 Commissioni Directa ({mercato}): -€{fee:.2f}\n"
        f"🏆 **P&L Netto Consolidato (Esente 26% fino a €302.35 minus): €{pnl_netto:+.2f}**",
        parse_mode="Markdown",
        reply_markup=menu_cockpit_tastiera(),
    )

async def statistiche_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    storico = carica_json(STORICO_FILE, [])

    if not storico:
        await msg.reply_text(
            "📈 **ARCHIVIO STATISTICHE VUOTO**\nNessuna operazione chiusa registrata finora.",
            parse_mode="Markdown",
            reply_markup=menu_cockpit_tastiera(),
        )
        return

    df_s = pd.DataFrame(storico)
    tot_trades = len(df_s)
    vinte = df_s[df_s["pnl_netto"] > 0]
    perse = df_s[df_s["pnl_netto"] <= 0]

    wr = (len(vinte) / tot_trades) * 100.0
    pnl_tot = df_s["pnl_netto"].sum()
    fee_tot = df_s["fee"].sum() if "fee" in df_s.columns else tot_trades * 3.0
    sum_win = vinte["pnl_netto"].sum() if not vinte.empty else 0.0
    sum_loss = abs(perse["pnl_netto"].sum()) if not perse.empty else 0.0
    pf = (sum_win / sum_loss) if sum_loss > 0 else 99.9

    minus_residua = max(0.0, 302.35 - max(0.0, pnl_tot))

    ultimi = storico[-5:]
    righe_ultimi = [
        f"• **{t['ticker']}** ({t['data_out']}): **{t['var_pct']:+.1f}%** ➔ Netto: **€{t['pnl_netto']:+.2f}**"
        for t in reversed(ultimi)
    ]

    testo = (
        f"📈 **STATISTICHE CONSOLIDATE (FTSE MIB & WALL STREET)**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔢 Operazioni Chiuse: **{tot_trades}** (✅ {len(vinte)} | ❌ {len(perse)})\n"
        f"🎯 **Win Rate Netto Reale:** **{wr:.1f}%**\n"
        f"💰 **P&L Netto Consolidato:** **€{pnl_tot:+.2f}**\n"
        f"⚖️ **Profit Factor Netto:** **{pf:.2f}**\n"
        f"🏦 Commissioni Directa Totali: €{fee_tot:.2f}\n"
        f"🛡️ Zainetto Minusvalenze Residuo: **€{minus_residua:.2f}**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 **ULTIME OPERAZIONI:**\n" + "\n".join(righe_ultimi)
    )
    await msg.reply_text(testo, parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    salva_json(PORTAFOGLIO_FILE, {})
    await update.message.reply_text(
        "🧹 **Portafoglio attivo azzerato con successo.**",
        parse_mode="Markdown",
        reply_markup=menu_cockpit_tastiera(),
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cmd_scan": await scan_cmd(update, context)
    elif data == "cmd_top10": await top10_cmd(update, context)
    elif data == "cmd_mercato": await mercato_cmd(update, context)
    elif data == "cmd_portafoglio": await portafoglio_cmd(update, context)
    elif data == "cmd_statistiche": await statistiche_cmd(update, context)
    elif data == "cmd_backtest": await backtest_cmd(update, context)
    elif data == "cmd_start": await start_cmd(update, context)

# ==============================================================================
# 6. AVVIO APPLICAZIONE
# ==============================================================================
def main():
    avvia_keep_alive()
    logger.info(f"Server HTTP keep-alive avviato sulla porta {PORT}.")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("top10", top10_cmd))
    app.add_handler(CommandHandler("mercato", mercato_cmd))
    app.add_handler(CommandHandler("compra", compra_cmd))
    app.add_handler(CommandHandler("portafoglio", portafoglio_cmd))
    app.add_handler(CommandHandler("vendi", vendi_cmd))
    app.add_handler(CommandHandler("statistiche", statistiche_cmd))
    app.add_handler(CommandHandler("backtest", backtest_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Bot Quantitativo Dual-Market Definitivo in ascolto su Telegram...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
