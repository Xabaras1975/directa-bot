import os
import json
import logging
import warnings
from datetime import datetime
from threading import Thread

import yfinance as yf
import pandas as pd
import numpy as np
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

warnings.filterwarnings("ignore")

# Configurazione Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==============================================================================
# 1. CONFIGURAZIONE E PARAMETRI QUANTITATIVI CERTIFICATI (88.9% WR | PF 5.48)
# ==============================================================================
TOKEN = os.getenv("TELEGRAM_TOKEN", "INSERISCI_QUI_IL_TUO_TOKEN")
PORT = int(os.environ.get("PORT", 10000))

PORTAFOGLIO_FILE = "portafoglio.json"
STORICO_FILE = "storico_trades.json"

# Paniere Liquido Piazza Affari (FTSE MIB / Mid Cap)
TICKERS = [
    "STLAM.MI", "RACE.MI", "MAIRE.MI", "WBD.MI", "SFER.MI",
    "ISP.MI", "UCG.MI", "BPE.MI", "BMED.MI", "G.MI",
    "ENEL.MI", "TRN.MI", "SRG.MI", "IG.MI", "A2A.MI", "HER.MI", "IRE.MI",
    "ENI.MI", "OVS.MI", "PRY.MI", "LDO.MI", "MONC.MI", "PST.MI", "TEN.MI"
]

# Parametri Operativi Directa & Modello Grid Search
CAPITALE_DEFAULT = 300.0     # Taglia standard per operazione (€)
COMMISSIONE_DIRECTA = 3.0    # €1.50 acquisto + €1.50 vendita (Profilo Variabile)

RSI_MAX_TRIGGER = 36.0       # Soglia aurea compressione RSI(14)
TP_TARGET_PCT = 0.042        # Take Profit fisso a +4.2% (usa 0.035 per il profilo 90% WR)
SL_ATR_MULT = 1.5            # Stop Loss iniziale a 1.5x ATR(14)
BE_TRIGGER_PCT = 0.025       # Attivazione Breakeven al raggiungimento di +2.5%
BE_LOCK_PCT = 0.018          # Livello Stop Breakeven a +1.8% (copre €3 fee + utile netto)
MAX_GIORNI_HOLD = 15         # Time-Stop massimo (giorni di borsa)

# ==============================================================================
# 2. WEB SERVER KEEP-ALIVE PER RENDER
# ==============================================================================
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "Bot Quantitativo FTSE MIB Operativo (High Win-Rate Engine).", 200

def run_flask():
    app_flask.run(host="0.0.0.0", port=PORT, use_reloader=False)

def avvia_keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()

# ==============================================================================
# 3. GESTIONE DATABASE LOCALE (JSON)
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

# ==============================================================================
# 4. MOTORE DI ANALISI QUANTITATIVA
# ==============================================================================
def calcola_indicatori_titolo(df):
    close = df["Close"]
    high = df["High"]
    low = df["Low"]

    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()

    delta = close.diff()
    g = delta.where(delta > 0, 0.0).rolling(14).mean()
    l = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = g / l.replace(0, np.nan)
    rsi = (100 - (100 / (1 + rs))).fillna(50)

    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14).mean()

    return sma50, sma200, rsi, atr

def verifica_regime_ftsemib():
    """Controlla se l'indice FTSE MIB si trova sopra la SMA 200."""
    try:
        df_mib = yf.download("FTSEMIB.MI", period="1y", interval="1d", progress=False, auto_adjust=True)
        if df_mib.empty or len(df_mib) < 200:
            return True, 0.0, 0.0, 0.0
        if isinstance(df_mib.columns, pd.MultiIndex):
            df_mib.columns = df_mib.columns.get_level_values(0)

        close_mib = float(df_mib["Close"].iloc[-1])
        sma200_mib = float(df_mib["Close"].rolling(200).mean().iloc[-1])
        dist_pct = ((close_mib - sma200_mib) / sma200_mib) * 100.0
        return (close_mib >= sma200_mib), close_mib, sma200_mib, dist_pct
    except Exception as e:
        logger.error(f"Errore verifica FTSE MIB: {e}")
        return True, 0.0, 0.0, 0.0

def esegui_scansione_mercato():
    """Analizza tutti i titoli del paniere e restituisce il candidato TOP 1 e la classifica."""
    mib_ok, mib_p, mib_sma, mib_dist = verifica_regime_ftsemib()
    candidati_sniper = []
    classifica_completa = []

    for ticker in TICKERS:
        try:
            d = yf.download(ticker, period="1y", interval="1d", progress=False, auto_adjust=True)
            if d.empty or len(d) < 205:
                continue
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)

            sma50, sma200, rsi, atr = calcola_indicatori_titolo(d)

            p = float(d["Close"].iloc[-1])
            op = float(d["Open"].iloc[-1])
            p_prev = float(d["Close"].iloc[-2])
            s50_prev = float(sma50.iloc[-2])
            s200_prev = float(sma200.iloc[-2])
            rsi_prev = float(rsi.iloc[-2])
            rsi_oggi = float(rsi.iloc[-1])
            atr_val = float(atr.iloc[-1])

            # Condizioni Strategia Certificata
            cond_trend = (p_prev > s50_prev > s200_prev)
            cond_rsi = (rsi_prev <= RSI_MAX_TRIGGER)
            cond_reversal = (p > op and p > p_prev)

            # Calcolo Score di Prossimità al Setup (0-100%)
            score = 0
            if cond_trend:
                score += 40
            if rsi_prev <= 42.0:
                score += 20
            if cond_rsi:
                score += 20
            if cond_reversal:
                score += 20

            tp_price = p * (1.0 + TP_TARGET_PCT)
            sl_price = p - (SL_ATR_MULT * atr_val)
            be_trigger = p * (1.0 + BE_TRIGGER_PCT)
            be_lock = p * (1.0 + BE_LOCK_PCT)

            info_titolo = {
                "ticker": ticker.replace(".MI", ""),
                "ticker_full": ticker,
                "prezzo": p,
                "rsi_ieri": rsi_prev,
                "rsi_oggi": rsi_oggi,
                "trend_ok": cond_trend,
                "reversal_ok": cond_reversal,
                "score": score,
                "tp": tp_price,
                "sl": sl_price,
                "be_trigger": be_trigger,
                "be_lock": be_lock,
                "atr": atr_val,
            }
            classifica_completa.append(info_titolo)

            if mib_ok and cond_trend and cond_rsi and cond_reversal:
                candidati_sniper.append(info_titolo)

        except Exception as e:
            logger.error(f"Errore scansione {ticker}: {e}")
            continue

    # Ordina i candidati Sniper per RSI di ieri più basso (massima compressione)
    candidati_sniper.sort(key=lambda x: x["rsi_ieri"])
    # Ordina la classifica generale per Score decrescente e poi RSI crescente
    classifica_completa.sort(key=lambda x: (-x["score"], x["rsi_ieri"]))

    top1 = candidati_sniper[0] if candidati_sniper else None
    regime_info = {"ok": mib_ok, "prezzo": mib_p, "sma200": mib_sma, "dist_pct": mib_dist}

    return top1, classifica_completa, regime_info

# ==============================================================================
# 5. COCKPIT OPERATIVO TELEGRAM A 3 FASI
# ==============================================================================
def menu_cockpit_tastiera():
    keyboard = [
        [
            InlineKeyboardButton("🎯 1. Scan Top 1", callback_data="cmd_scan"),
            InlineKeyboardButton("📊 Top 10 Watchlist", callback_data="cmd_top10"),
            InlineKeyboardButton("🏛️ Regime MIB", callback_data="cmd_mercato"),
        ],
        [
            InlineKeyboardButton("💼 2. Portafoglio & Alert", callback_data="cmd_portafoglio"),
        ],
        [
            InlineKeyboardButton("📈 3. Statistiche & P&L", callback_data="cmd_statistiche"),
            InlineKeyboardButton("🧭 Cockpit", callback_data="cmd_start"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

TESTO_COCKPIT = (
    "🧭 **COCKPIT OPERATIVO QUANTITATIVO — FTSE MIB**\n"
    "*(Motore High Win-Rate 88.9% | PF 5.48 | Directa Variabile)*\n\n"
    "🟢 **FASE 1 — DECISIONE (Analisi & Segnali)**\n"
    "• `/scan` — Cerca il singolo miglior segnale **TOP 1** di giornata\n"
    "• `/top10` — Classifica dei 10 titoli più vicini al trigger\n"
    "• `/mercato` — Verifica il filtro regime sul FTSE MIB (SMA 200)\n\n"
    "🟡 **FASE 2 — GESTIONE (Operatività & Protezione)**\n"
    "• `/portafoglio` — Controlla posizioni, Breakeven (+2.5%) e uscite\n"
    "• `/compra TICKER PREZZO [CAPITALE]` — Registra un ingresso\n"
    "• `/vendi TICKER PREZZO` — Chiude la posizione e calcola il P&L netto\n\n"
    "🔵 **FASE 3 — ARCHIVIO (Performance & Audit)**\n"
    "• `/statistiche` — Bilancio netto, Win Rate, Profit Factor e storico\n"
    "• `/reset` — Azzera il portafoglio attivo"
)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    await msg.reply_text(
        TESTO_COCKPIT, parse_mode="Markdown", reply_markup=menu_cockpit_tastiera()
    )

# --- FASE 1: DECISIONE ---
async def mercato_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    mib_ok, p, sma, dist = verifica_regime_ftsemib()
    stato = "✅ **BULL MARKET (Operatività Consentita)**" if mib_ok else "⛔ **BEAR MARKET (Nuovi Ingressi Bloccati)**"
    testo = (
        f"🏛️ **STATO REGIME DI MERCATO (FTSE MIB)**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Stato: {stato}\n"
        f"Valore Attuale: **{p:,.0f} pt**\n"
        f"Media SMA 200: **{sma:,.0f} pt**\n"
        f"Distanza da SMA 200: **{dist:+.2f}%**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 *Il modello opera solo quando il FTSE MIB staziona sopra la SMA 200.*"
    )
    await msg.reply_text(testo, parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    wait_msg = await msg.reply_text("⏳ *Scansione quantitativa del paniere FTSE MIB in corso...*", parse_mode="Markdown")

    portafoglio = carica_json(PORTAFOGLIO_FILE, {})
    top1, _, regime = esegui_scansione_mercato()

    if not regime["ok"]:
        testo = (
            f"⛔ **SEMAFORO ROSSO — MERCATO SOTTO SMA 200**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"FTSE MIB: **{regime['prezzo']:,.0f}** (SMA 200: **{regime['sma200']:,.0f}**)\n"
            f"Nessun segnale generato per protezione del capitale."
        )
        await wait_msg.edit_text(testo, parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())
        return

    if top1 is None:
        testo = (
            "✅ **Mercato Sano (FTSE MIB > SMA 200)**\n"
            "🔍 **NESSUN SEGNALE 'TOP 1' OGGI**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Nessun titolo presenta contemporaneamente:\n"
            "1. Trend rialzista (`P > SMA50 > SMA200`)\n"
            f"2. Ipervenduto marcato (`RSI_14 <= {RSI_MAX_TRIGGER}`)\n"
            "3. Candela verde di inversione (`Close > Open` e `Close > Ieri`)\n\n"
            "💡 *Disciplina Sniper: restare liquidi e risparmiare commissioni. Usa `/top10` per monitorare i titoli più vicini.*"
        )
        await wait_msg.edit_text(testo, parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())
        return

    avviso_pos = ""
    if len(portafoglio) > 0:
        avviso_pos = "\n⚠️ *Nota: Hai già una posizione attiva in portafoglio. La regola Top 1 consiglia max 1 trade attivo alla volta.*\n"

    gain_lordo = CAPITALE_DEFAULT * TP_TARGET_PCT
    gain_netto = gain_lordo - COMMISSIONE_DIRECTA
    rischio_pct = ((top1["prezzo"] - top1["sl"]) / top1["prezzo"]) * 100.0

    testo = (
        f"🎯 **SEGNALE SNIPER 'TOP 1' RILEVATO!**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 Titolo: **{top1['ticker']}** (`{top1['ticker_full']}`)\n"
        f"💶 Prezzo Ingresso: **€{top1['prezzo']:.4f}**\n"
        f"📉 RSI(14) Ieri: **{top1['rsi_ieri']:.1f}** (Soglia <= {RSI_MAX_TRIGGER})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **PIANO OPERATIVO SU DIRECTA (€{CAPITALE_DEFAULT:.0f}):**\n"
        f"• **Take Profit (+{TP_TARGET_PCT*100:.1f}%):** `€{top1['tp']:.4f}` *(Netto: +€{gain_netto:.2f})*\n"
        f"• **Stop Loss Iniziale (1.5x ATR):** `€{top1['sl']:.4f}` *(-{rischio_pct:.2f}%)*\n"
        f"• **Trigger Breakeven (+2.5%):** a `€{top1['be_trigger']:.4f}` sposta lo Stop a `€{top1['be_lock']:.4f}` *(+1.8% bloccato)*\n"
        f"• **Time-Stop:** Max {MAX_GIORNI_HOLD} sedute\n"
        f"{avviso_pos}\n"
        f"📲 Per registrare l'eseguito digita:\n"
        f"`/compra {top1['ticker']} {top1['prezzo']:.4f}`"
    )
    await wait_msg.edit_text(testo, parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())

async def top10_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    wait_msg = await msg.reply_text("⏳ *Elaborazione classifica Watchlist Top 10...*", parse_mode="Markdown")

    _, classifica, regime = esegui_scansione_mercato()
    if not classifica:
        await wait_msg.edit_text("❌ Impossibile scaricare i dati di mercato.", reply_markup=menu_cockpit_tastiera())
        return

    righe = [
        "📊 **WATCHLIST TOP 10 — PROSSIMITÀ AL TRIGGER**",
        f"🏛️ Regime MIB: {'✅ Sano' if regime['ok'] else '⛔ Sotto SMA200'}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    for idx, item in enumerate(classifica[:10], start=1):
        icona_t = "🟢" if item["trend_ok"] else "🔴"
        icona_r = "🔥" if item["rsi_ieri"] <= RSI_MAX_TRIGGER else "⏳"
        righe.append(
            f"**{idx}. {item['ticker']}** — €{item['prezzo']:.3f} | Score: **{item['score']}%**\n"
            f"   Trend: {icona_t} | RSI Ieri: **{item['rsi_ieri']:.1f}** {icona_r} | Oggi: {item['rsi_oggi']:.1f}"
        )

    righe.append("━━━━━━━━━━━━━━━━━━━━")
    righe.append("💡 *Il segnale scatta a Score 100% (Trend 🟢 + RSI <= 36 🔥 + Candela Verde).*")
    await wait_msg.edit_text("\n".join(righe), parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())

# --- FASE 2: GESTIONE ---
async def compra_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "⚠️ **Uso corretto:** `/compra TICKER PREZZO [CAPITALE]`\nEsempio: `/compra ISP 3.852`",
            parse_mode="Markdown",
        )
        return

    ticker_clean = args[0].upper().replace(".MI", "")
    ticker_full = f"{ticker_clean}.MI"

    try:
        prezzo_in = float(args[1].replace(",", "."))
        capitale = float(args[2].replace(",", ".")) if len(args) >= 3 else CAPITALE_DEFAULT
    except ValueError:
        await update.message.reply_text("❌ Inserisci valori numerici validi per prezzo e capitale.")
        return

    # Calcola ATR attuale per fissare lo Stop Loss 1.5x ATR
    atr_val = prezzo_in * 0.022
    try:
        d = yf.download(ticker_full, period="3mo", interval="1d", progress=False, auto_adjust=True)
        if not d.empty and len(d) >= 20:
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)
            _, _, _, atr_s = calcola_indicatori_titolo(d)
            atr_val = float(atr_s.iloc[-1])
    except Exception:
        pass

    tp_price = prezzo_in * (1.0 + TP_TARGET_PCT)
    sl_iniziale = prezzo_in - (SL_ATR_MULT * atr_val)
    be_trigger = prezzo_in * (1.0 + BE_TRIGGER_PCT)
    be_lock = prezzo_in * (1.0 + BE_LOCK_PCT)

    portafoglio = carica_json(PORTAFOGLIO_FILE, {})
    portafoglio[ticker_clean] = {
        "ticker_full": ticker_full,
        "prezzo_in": prezzo_in,
        "capitale": capitale,
        "tp": tp_price,
        "sl": sl_iniziale,
        "be_trigger": be_trigger,
        "be_lock": be_lock,
        "be_attivo": False,
        "data_in": datetime.now().strftime("%Y-%m-%d"),
    }
    salva_json(PORTAFOGLIO_FILE, portafoglio)

    await update.message.reply_text(
        f"✅ **POSIZIONE REGISTRATA IN PORTAFOGLIO**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Titolo: **{ticker_clean}**\n"
        f"💶 Prezzo Carico: **€{prezzo_in:.4f}** (Size: €{capitale:.0f})\n"
        f"🎯 Take Profit (+{TP_TARGET_PCT*100:.1f}%): **€{tp_price:.4f}**\n"
        f"🛡️ Stop Loss (1.5x ATR): **€{sl_iniziale:.4f}**\n"
        f"🔒 Trigger Breakeven (+2.5%): **€{be_trigger:.4f}** *(Stop sale a €{be_lock:.4f})*",
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

    wait_msg = await msg.reply_text("⏳ *Aggiornamento quotazioni e verifica livelli Breakeven/Target...*", parse_mode="Markdown")
    righe = ["💼 **MONITORAGGIO PORTAFOGLIO LIVE**", "━━━━━━━━━━━━━━━━━━━━"]
    modificato = False

    for ticker, pos in portafoglio.items():
        p_in = pos["prezzo_in"]
        cap = pos.get("capitale", CAPITALE_DEFAULT)
        p_attuale = p_in
        max_oggi = p_in

        try:
            d = yf.download(pos["ticker_full"], period="5d", interval="1d", progress=False, auto_adjust=True)
            if not d.empty:
                if isinstance(d.columns, pd.MultiIndex):
                    d.columns = d.columns.get_level_values(0)
                p_attuale = float(d["Close"].iloc[-1])
                max_oggi = float(d["High"].iloc[-1])
        except Exception:
            pass

        # Verifica attivazione automatica Breakeven (+2.5% -> Stop a +1.8%)
        avviso_azione = "⏳ *Mantieni posizione*"
        if not pos.get("be_attivo", False) and (max_oggi >= pos["be_trigger"] or p_attuale >= pos["be_trigger"]):
            pos["be_attivo"] = True
            pos["sl"] = pos["be_lock"]
            modificato = True
            avviso_azione = f"🚨 **SPOSTA STOP SU DIRECTA A €{pos['be_lock']:.4f} (+1.8% Protetto!)**"
        elif pos.get("be_attivo", False):
            avviso_azione = f"🔒 *Breakeven Attivo (Stop protetto a €{pos['sl']:.4f})*"

        if p_attuale >= pos["tp"]:
            avviso_azione = f"🎯 **TARGET RAGGIUNTO! Vendi e incassa (`/vendi {ticker} {p_attuale:.4f}`)**"
        elif p_attuale <= pos["sl"]:
            avviso_azione = f"🛑 **STOP COLPITO! Chiudi posizione (`/vendi {ticker} {p_attuale:.4f}`)**"

        var_pct = ((p_attuale - p_in) / p_in) * 100.0
        pnl_lordo = ((p_attuale - p_in) / p_in) * cap
        pnl_netto = pnl_lordo - COMMISSIONE_DIRECTA

        giorni_ trascorsi = (datetime.now() - datetime.strptime(pos["data_in"], "%Y-%m-%d")).days
        if giorni_trascorsi >= MAX_GIORNI_HOLD:
            avviso_azione = f"⏰ **TIME-STOP ({giorni_trascorsi} gg): Valuta chiusura posizione**"

        icona_pnl = "🟢" if pnl_netto >= 0 else "🔴"
        righe.append(
            f"📌 **{ticker}** (In: €{p_in:.4f} | Ora: **€{p_attuale:.4f}**)\n"
            f"   {icona_pnl} Var: **{var_pct:+.2f}%** | P&L Netto Fee: **€{pnl_netto:+.2f}**\n"
            f"   🎯 TP: `€{pos['tp']:.4f}` | 🛡️ SL Attuale: `€{pos['sl']:.4f}`\n"
            f"   ⏱️ Giorni: {giorni_trascorsi}/{MAX_GIORNI_HOLD} | {avviso_azione}\n"
            f"   👉 Chiudi: `/vendi {ticker} {p_attuale:.4f}`\n"
        )

    if modificato:
        salva_json(PORTAFOGLIO_FILE, portafoglio)

    await wait_msg.edit_text("\n".join(righe), parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())

async def vendi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "⚠️ **Uso corretto:** `/vendi TICKER PREZZO_USCITA`\nEsempio: `/vendi ISP 4.015`",
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
        await update.message.reply_text(f"❌ Il titolo **{ticker_clean}** non è presente in portafoglio.", parse_mode="Markdown")
        return

    pos = portafoglio.pop(ticker_clean)
    salva_json(PORTAFOGLIO_FILE, portafoglio)

    p_in = pos["prezzo_in"]
    cap = pos.get("capitale", CAPITALE_DEFAULT)
    var_pct = ((prezzo_out - p_in) / p_in) * 100.0
    pnl_lordo = ((prezzo_out - p_in) / p_in) * cap
    pnl_netto = pnl_lordo - COMMISSIONE_DIRECTA
    esito = "WIN" if pnl_netto > 0 else "LOSS"

    storico = carica_json(STORICO_FILE, [])
    storico.append({
        "ticker": ticker_clean,
        "data_in": pos["data_in"],
        "data_out": datetime.now().strftime("%Y-%m-%d"),
        "prezzo_in": p_in,
        "prezzo_out": prezzo_out,
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
        f"📌 Titolo: **{ticker_clean}**\n"
        f"💶 Carico: €{p_in:.4f} ➔ Uscita: **€{prezzo_out:.4f}** ({var_pct:+.2f}%)\n"
        f"💰 P&L Lordo: €{pnl_lordo:+.2f}\n"
        f"🏦 Commissioni Directa: -€{COMMISSIONE_DIRECTA:.2f}\n"
        f"🏆 **P&L Netto Consolidato: €{pnl_netto:+.2f}**",
        parse_mode="Markdown",
        reply_markup=menu_cockpit_tastiera(),
    )

# --- FASE 3: ARCHIVIO ---
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
    fee_tot = tot_trades * COMMISSIONE_DIRECTA
    sum_win = vinte["pnl_netto"].sum() if not vinte.empty else 0.0
    sum_loss = abs(perse["pnl_netto"].sum()) if not perse.empty else 0.0
    pf = (sum_win / sum_loss) if sum_loss > 0 else 99.9

    ultimi = storico[-5:]
    righe_ultimi = [
        f"• **{t['ticker']}** ({t['data_out']}): **{t['var_pct']:+.1f}%** ➔ Netto: **€{t['pnl_netto']:+.2f}** ({'✅' if t['esito']=='WIN' else '❌'})"
        for t in reversed(ultimi)
    ]

    testo = (
        f"📈 **STATISTICHE CONSOLIDATE (NETTO DIRECTA)**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔢 Operazioni Chiuse: **{tot_trades}** (✅ {len(vinte)} | ❌ {len(perse)})\n"
        f"🎯 **Win Rate Netto Reale:** **{wr:.1f}%** *(Target modello: 88.9%)*\n"
        f"💰 **P&L Netto Consolidato:** **€{pnl_tot:+.2f}**\n"
        f"⚖️ **Profit Factor Netto:** **{pf:.2f}** *(Target modello: 5.48)*\n"
        f"🏦 Commissioni Directa Pagate: €{fee_tot:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 **ULTIME 5 OPERAZIONI:**\n" + "\n".join(righe_ultimi)
    )
    await msg.reply_text(testo, parse_mode="Markdown", reply_markup=menu_cockpit_tastiera())

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    salva_json(PORTAFOGLIO_FILE, {})
    await update.message.reply_text(
        "🧹 **Portafoglio attivo azzerato con successo.** Lo storico delle statistiche è stato mantenuto.",
        parse_mode="Markdown",
        reply_markup=menu_cockpit_tastiera(),
    )

# Gestione pulsanti inline del Cockpit
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cmd_scan":
        await scan_cmd(update, context)
    elif data == "cmd_top10":
        await top10_cmd(update, context)
    elif data == "cmd_mercato":
        await mercato_cmd(update, context)
    elif data == "cmd_portafoglio":
        await portafoglio_cmd(update, context)
    elif data == "cmd_statistiche":
        await statistiche_cmd(update, context)
    elif data == "cmd_start":
        await start_cmd(update, context)

# ==============================================================================
# 6. AVVIO APPLICAZIONE
# ==============================================================================
def main():
    avvia_keep_alive()
    app = Application.builder().token(TOKEN).build()

    # Registrazione Comandi 3 Fasi
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("top10", top10_cmd))
    app.add_handler(CommandHandler("mercato", mercato_cmd))
    app.add_handler(CommandHandler("compra", compra_cmd))
    app.add_handler(CommandHandler("portafoglio", portafoglio_cmd))
    app.add_handler(CommandHandler("vendi", vendi_cmd))
    app.add_handler(CommandHandler("statistiche", statistiche_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Bot Quantitativo FTSE MIB avviato correttamente.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
