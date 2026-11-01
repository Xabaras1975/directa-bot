import os
import json
import logging
import asyncio
import math
from datetime import datetime, time
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from scipy.stats import norm
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler
)

# Configurazione logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Configurazione credenziali protetta da variabili d'ambiente
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

PORTFOLIO_FILE = "portfolio.json"
ATTESA_DETTAGLI_ACQUISTO = 1

# POSIZIONI FISSE PERMANENTI
POSIZIONI_PREDEFINITE = {
    "ISP.MI": {
        "nome": "Intesa Sanpaolo",
        "quote": 3,
        "pmc": 5.929,
        "data": "2026-10-29 09:48"
    }
}

# PANIERE AMPLIATO: Grandi titoli, Mid/Small Cap a basso prezzo nominale ed ETF
PANIERE = {
    # Titoli a basso prezzo nominale (< 3 € - 5 €)
    "A2A.MI": "A2A",
    "HER.MI": "Hera",
    "IRE.MI": "Iren",
    "SAL.MI": "Webuild",
    "OVS.MI": "OVS",
    "MREC.MI": "Maire Tecnimont",
    "SFER.MI": "Salvatore Ferragamo",
    "IG.MI": "Italgas",
    "SRG.MI": "Snam",
    "TRN.MI": "Terna",
    "BPE.MI": "BPER Banca",

    # Blue Chip e Finanziari FTSE MIB
    "ENEL.MI": "Enel",
    "ISP.MI": "Intesa Sanpaolo",
    "UCG.MI": "UniCredit",
    "ENI.MI": "Eni",
    "STLAM.MI": "Stellantis",
    "RACE.MI": "Ferrari",
    "G.MI": "Generali",
    "BMED.MI": "Banca Mediolanum",

    # ETF di riferimento
    "SWDA.MI": "iShares MSCI World (SWDA)",
    "CSSPX.MI": "iShares S&P 500 (CSSPX)",
    "XEON.MI": "Xtrackers Overnight €STR (XEON)"
}

class SimpleWebServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot Directa & Quant Sports Online")

    def log_message(self, format, *args):
        return

def avvia_server_porta():
    porta = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", porta), SimpleWebServer)
    logging.info(f"Server web attivo sulla porta {porta}")
    server.serve_forever()

def carica_portafoglio():
    portafoglio = dict(POSIZIONI_PREDEFINITE)
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r") as f:
                dati_salvati = json.load(f)
                portafoglio.update(dati_salvati)
        except Exception:
            pass
    return portafoglio

def salva_portafoglio(data):
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(data, f, indent=4)

def calcola_atr(df, period=14):
    high = df['High'].iloc[:, 0] if isinstance(df['High'], pd.DataFrame) else df['High']
    low = df['Low'].iloc[:, 0] if isinstance(df['Low'], pd.DataFrame) else df['Low']
    close = df['Close'].iloc[:, 0] if isinstance(df['Close'], pd.DataFrame) else df['Close']
    
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return float(atr.iloc[-1])

def verifica_regime_ftsemib():
    try:
        df_index = yf.download("FTSEMIB.MI", period="6mo", interval="1d", progress=False)
        if df_index.empty:
            return True, 0.0
        c = df_index['Close'].iloc[:, 0] if isinstance(df_index['Close'], pd.DataFrame) else df_index['Close']
        p_idx = float(c.iloc[-1])
        sma50_idx = float(c.rolling(50).mean().iloc[-1])
        bullish = p_idx >= sma50_idx
        return bullish, p_idx
    except Exception:
        return True, 0.0

# ==========================================
# MOTORI MATEMATICI TENNIS & BASKET
# ==========================================

def prob_game_servizio(p):
    if p >= 1.0: return 1.0
    if p <= 0.0: return 0.0
    num = (p**4) * (15 - 34*p + 28*(p**2) - 8*(p**3))
    den = 1 - 2*p*(1 - p)
    return float(num / den)

def prob_tiebreak(pA, pB):
    p_medio_A = (pA + (1 - pB)) / 2.0
    q = 1.0 - p_medio_A
    prob_vittoria_secca = 0.0
    for punti_b in range(6):
        modi = math.comb(6 + punti_b, punti_b)
        prob_vittoria_secca += modi * (p_medio_A**6) * (q**punti_b) * p_medio_A
    prob_6_pari = math.comb(12, 6) * (p_medio_A**6) * (q**6)
    prob_vantaggi = (p_medio_A**2) / (p_medio_A**2 + q**2)
    return prob_vittoria_secca + (prob_6_pari * prob_vantaggi)

async def cmd_tennis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Calcola probabilità Markoviane, Fair Odds ed EV% per il Tennis"""
    args = context.args
    if len(args) < 4:
        msg_help = (
            "🎾 <b>UTILIZZO COMANDO /tennis</b>\n\n"
            "Formato:\n"
            "<code>/tennis &lt;p_serv_A&gt; &lt;p_serv_B&gt; &lt;quota_A&gt; &lt;quota_B&gt;</code>\n\n"
            "Esempio (A vince il 68% al servizio, B il 63%):\n"
            "<code>/tennis 0.68 0.63 1.65 2.30</code>"
        )
        await update.message.reply_text(msg_help, parse_mode='HTML')
        return

    try:
        pA = float(args[0].replace(",", "."))
        pB = float(args[1].replace(",", "."))
        quota_A = float(args[2].replace(",", "."))
        quota_B = float(args[3].replace(",", "."))
    except ValueError:
        await update.message.reply_text("⚠️ Parametri numerici non validi.", parse_mode='HTML')
        return

    gA = prob_game_servizio(pA)
    gB = prob_game_servizio(pB)
    tb = prob_tiebreak(pA, pB)

    p_game_A = (gA + (1.0 - gB)) / 2.0
    q_game = 1.0 - p_game_A
    set_secco = sum(math.comb(5 + b, b) * (p_game_A**6) * (q_game**b) for b in range(5))
    set_75 = math.comb(10, 5) * (p_game_A**5) * (q_game**5) * (p_game_A**2)
    set_tb = math.comb(10, 5) * (p_game_A**5) * (q_game**5) * 2 * p_game_A * q_game * tb
    prob_set_A = set_secco + set_75 + set_tb

    # Match best of 3 sets
    prob_match_A = (prob_set_A**2) + (2 * prob_set_A * (1 - prob_set_A) * prob_set_A)
    prob_match_B = 1.0 - prob_match_A

    fair_A = 1.0 / prob_match_A
    fair_B = 1.0 / prob_match_B

    ev_A = ((quota_A * prob_match_A) - 1.0) * 100
    ev_B = ((quota_B * prob_match_B) - 1.0) * 100

    icon_A = "🔥 VALORE" if ev_A > 2.0 else "⚪"
    icon_B = "🔥 VALORE" if ev_B > 2.0 else "⚪"

    msg = (
        "🎾 <b>MODELLO MARKOVIANO TENNIS (PUNTO-A-PUNTO)</b>\n\n"
        f"• Game Servizio A: <code>{gA*100:.1f}%</code> | B: <code>{gB*100:.1f}%</code>\n"
        f"• Probabilità Set A: <code>{prob_set_A*100:.1f}%</code>\n\n"
        f"👤 <b>Giocatore A:</b>\n"
        f"   Probabilità Reale: <b>{prob_match_A*100:.1f}%</b> | Quota Equa: <code>{fair_A:.2f}</code>\n"
        f"   Quota Book: <code>{quota_A:.2f}</code> ➔ EV: <b>{ev_A:+.2f}%</b> {icon_A}\n\n"
        f"👤 <b>Giocatore B:</b>\n"
        f"   Probabilità Reale: <b>{prob_match_B*100:.1f}%</b> | Quota Equa: <code>{fair_B:.2f}</code>\n"
        f"   Quota Book: <code>{quota_B:.2f}</code> ➔ EV: <b>{ev_B:+.2f}%</b> {icon_B}"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_basket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Calcola Possessi, Spread e Copertura Handicap per il Basket"""
    args = context.args
    if len(args) < 8:
        msg_help = (
            "🏀 <b>UTILIZZO COMANDO /basket</b>\n\n"
            "Formato:\n"
            "<code>/basket &lt;paceA&gt; &lt;offA&gt; &lt;defA&gt; &lt;paceB&gt; &lt;offB&gt; &lt;defB&gt; &lt;linea_spread&gt; &lt;quota_spread&gt;</code>\n\n"
            "Esempio (Casa pace 102, off 118, def 112 vs Ospite pace 98, off 113, def 115 su linea -4.5 a 1.91):\n"
            "<code>/basket 102 118 112 98 113 115 -4.5 1.91</code>"
        )
        await update.message.reply_text(msg_help, parse_mode='HTML')
        return

    try:
        p_A, o_A, d_A = float(args[0]), float(args[1]), float(args[2])
        p_B, o_B, d_B = float(args[3]), float(args[4]), float(args[5])
        linea_spread = float(args[6].replace(",", "."))
        quota_spread = float(args[7].replace(",", "."))
    except ValueError:
        await update.message.reply_text("⚠️ Parametri basket non validi.", parse_mode='HTML')
        return

    pace_match = (p_A * p_B) / 100.0
    home_adv = 2.5
    sigma = 11.2

    punti_A = (pace_match * (o_A * d_B) / (114.0 * 100.0)) + (home_adv / 2.0)
    punti_B = (pace_match * (o_B * d_A) / (114.0 * 100.0)) - (home_adv / 2.0)

    totale = punti_A + punti_B
    scarto_atteso = punti_A - punti_B

    soglia = -linea_spread
    z = (soglia - scarto_atteso) / sigma
    prob_copertura = 1.0 - norm.cdf(z)
    fair_odds = 1.0 / prob_copertura if prob_copertura > 0 else 999.0
    ev = ((quota_spread * prob_copertura) - 1.0) * 100
    icon_ev = "🔥 VALORE" if ev > 2.0 else "⚪"

    msg = (
        "🏀 <b>MODELLO PACE & RATING BASKET</b>\n\n"
        f"• Possessi Stimati: <code>{pace_match:.1f}</code>\n"
        f"• Punteggio Atteso: <b>{punti_A:.1f} - {punti_B:.1f}</b>\n"
        f"• Totale Punti Previsti: <code>{totale:.1f}</code>\n"
        f"• Spread Modellato: <b>{-scarto_atteso:+.1f}</b> punti\n\n"
        f"🎯 <b>Valutazione Linea Handicap ({linea_spread:+.1f}):</b>\n"
        f"• Probabilità Copertura: <b>{prob_copertura*100:.1f}%</b>\n"
        f"• Quota Equa: <code>{fair_odds:.2f}</code> | Book: <code>{quota_spread:.2f}</code>\n"
        f"• Expected Value (EV): <b>{ev:+.2f}%</b> {icon_ev}"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

# ==========================================
# MONITORAGGIO E SCANSIONE DIRECTA (AZIONI)
# ==========================================

async def esegui_scansione_e_monitoraggio(app_bot):
    portafoglio = carica_portafoglio()

    # 1. Monitoraggio posizioni aperte (Target Dinamici ATR)
    for ticker, info in list(portafoglio.items()):
        try:
            d = yf.download(ticker, period="3mo", interval="1d", progress=False)
            if d.empty:
                continue
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)

            c_series = d['Close'].iloc[:, 0] if isinstance(d['Close'], pd.DataFrame) else d['Close']
            p_live = float(c_series.iloc[-1])
            pmc = info['pmc']
            quote = info['quote']

            atr_val = calcola_atr(d, 14) if len(d) >= 15 else (pmc * 0.03)
            tp_dinamico = pmc + (2.0 * atr_val)
            sl_dinamico = pmc - (1.5 * atr_val)

            rend_pct = ((p_live - pmc) / pmc) * 100

            if p_live >= tp_dinamico or p_live <= sl_dinamico:
                tipo = f"🎯 TAKE PROFIT (ATR +2.0x a €{tp_dinamico:.3f})" if p_live >= tp_dinamico else f"⚠️ STOP LOSS (ATR -1.5x a €{sl_dinamico:.3f})"
                tastiera = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Venduto su Directa", callback_data=f"venduto_{ticker}")],
                    [InlineKeyboardButton("⏳ Tieni Posizione", callback_data=f"tieni_{ticker}")]
                ])
                msg = (
                    f"🚨 <b>SEGNALE DI USCITA: {info['nome']} ({ticker})</b>\n\n"
                    f"• Condizione: <b>{tipo}</b>\n"
                    f"• Prezzo di Carico (PMC): <code>€{pmc:.3f}</code>\n"
                    f"• Prezzo Attuale: <code>€{p_live:.3f}</code>\n"
                    f"• Performance: <b>{rend_pct:+.2f}%</b>\n"
                    f"• Quantità: <code>{quote} quote</code>\n\n"
                    f"Hai chiuso l'operazione su Directa?"
                )
                await app_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML', reply_markup=tastiera)
        except Exception as e:
            logging.error(f"Errore monitoraggio {ticker}: {e}")

    # 2. Controllo Regime FTSE MIB
    indice_rialzista, idx_val = verifica_regime_ftsemib()
    regime_icon = "🟢 Favorevole" if indice_rialzista else "🟠 Prudente (Sotto SMA 50)"

    # 3. Scansione Paniere con Volumi, ATR e Z-Score
    risultati = []
    for ticker, nome in PANIERE.items():
        if ticker in portafoglio:
            continue
        try:
            df = yf.download(ticker, period="1y", interval="1d", progress=False)
            if df.empty or len(df) < 30:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            close = df['Close'].iloc[:, 0] if isinstance(df['Close'], pd.DataFrame) else df['Close']
            volume = df['Volume'].iloc[:, 0] if isinstance(df['Volume'], pd.DataFrame) else df['Volume']

            p_curr = float(close.iloc[-1])
            sma20 = float(close.rolling(20).mean().iloc[-1])
            sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else float(close.rolling(50).mean().iloc[-1])
            std20 = float(close.rolling(20).std().iloc[-1])
            z_score = (p_curr - sma20) / std20 if std20 > 0 else 0.0

            delta = close.diff()
            g = (delta.where(delta > 0, 0)).rolling(14).mean()
            l = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = g / l
            rsi_series = 100 - (100 / (1 + rs))
            rsi = float(rsi_series.iloc[-1])

            vol_last = float(volume.iloc[-1])
            sma_vol = float(volume.rolling(20).mean().iloc[-1])
            vol_ratio = (vol_last / sma_vol) if sma_vol > 0 else 1.0

            atr = calcola_atr(df, 14)
            tp_stimato = p_curr + (2.0 * atr)
            sl_stimato = p_curr - (1.5 * atr)

            score = 50.0
            if indice_rialzista: score += 5
            else: score -= 10

            if z_score <= -2.0: score += 20
            elif z_score <= -1.0: score += 10
            elif z_score >= 2.0: score -= 20

            if rsi < 32: score += 18
            elif rsi < 42: score += 8
            elif rsi > 70: score -= 20

            if p_curr > sma200: score += 8
            else: score -= 12

            if z_score < -1.0 and vol_ratio > 1.6:
                score -= 15
            elif z_score >= -1.0 and vol_ratio > 1.3:
                score += 10

            score = int(np.clip(score, 0, 100))

            risultati.append({
                "ticker": ticker,
                "nome": nome,
                "prezzo": p_curr,
                "score": score,
                "rsi": rsi,
                "z_score": z_score,
                "vol_ratio": vol_ratio,
                "atr": atr,
                "tp": tp_stimato,
                "sl": sl_stimato
            })
        except Exception as e:
            logging.error(f"Errore scan {ticker}: {e}")
            continue

    if not risultati:
        await app_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="ℹ️ Scansione completata: nessun dato disponibile al momento.")
        return

    risultati.sort(key=lambda x: x["score"], reverse=True)
    top10 = risultati[:10]

    classifica_txt = (
        f"📊 <b>TOP 10 OPPORTUNITÀ BORSA ITALIANA</b>\n"
        f"🌐 <i>Regime FTSE MIB: {regime_icon}</i>\n\n"
    )
    for i, item in enumerate(top10, start=1):
        icona = "🔥" if item['score'] >= 55 else "🔹"
        classifica_txt += (
            f"{i}. {icona} <b>{item['nome']}</b> (<code>{item['ticker']}</code>)\n"
            f"   Prezzo: <code>€{item['prezzo']:.3f}</code> | Score: <b>{item['score']}/100</b>\n"
            f"   RSI: <code>{item['rsi']:.1f}</code> | Z-Score: <code>{item['z_score']:+.2f}σ</code> | Vol: <code>{item['vol_ratio']:.1f}x</code>\n"
            f"   🎯 TP: <code>€{item['tp']:.3f}</code> | ⚠️ SL: <code>€{item['sl']:.3f}</code>\n\n"
        )
    await app_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=classifica_txt, parse_mode='HTML')

    opportunita = [r for r in top10 if r["score"] >= 55]
    for opp in opportunita:
        tastiera = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Ho Comprato", callback_data=f"buy_{opp['ticker']}_{opp['prezzo']:.3f}")],
            [InlineKeyboardButton("❌ Ignora", callback_data=f"ignora_{opp['ticker']}")]
        ])
        testo_alert = (
            f"📢 <b>SEGNALE INGRESSO QUANTITATIVO</b>\n\n"
            f"• <b>{opp['nome']}</b> (<code>{opp['ticker']}</code>)\n"
            f"• Prezzo: <code>€{opp['prezzo']:.3f}</code> | Score: <b>{opp['score']}/100</b>\n"
            f"• Volumi: <code>{opp['vol_ratio']:.1f}x media</code> | ATR: <code>€{opp['atr']:.3f}</code>\n"
            f"• 🎯 Target Dinamico (+2 ATR): <code>€{opp['tp']:.3f}</code>\n"
            f"• ⚠️ Stop Dinamico (-1.5 ATR): <code>€{opp['sl']:.3f}</code>\n\n"
            f"Vuoi registrare l'operazione su Directa?"
        )
        await app_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=testo_alert, parse_mode='HTML', reply_markup=tastiera)

async def job_schedulato(context: ContextTypes.DEFAULT_TYPE):
    if datetime.now().weekday() < 5:
        await esegui_scansione_e_monitoraggio(context.bot)

async def cmd_compra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "⚠️ Formato non corretto! Esempio:\n<code>/compra ISP.MI 3 5.929</code>",
            parse_mode='HTML'
        )
        return

    ticker_raw = args[0].upper().strip()
    ticker = ticker_raw if ticker_raw.endswith(".MI") else f"{ticker_raw}.MI"

    try:
        quote = int(args[1])
        pmc = float(args[2].replace(",", "."))
    except ValueError:
        await update.message.reply_text("⚠️ Quantità o prezzo non validi.", parse_mode='HTML')
        return

    nome = PANIERE.get(ticker, ticker)
    portafoglio = carica_portafoglio()
    portafoglio[ticker] = {
        "nome": nome,
        "quote": quote,
        "pmc": pmc,
        "data": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    salva_portafoglio(portafoglio)

    await update.message.reply_text(
        f"✅ <b>ACQUISTO REGISTRATO!</b>\n\n"
        f"• Titolo: <b>{nome}</b> (<code>{ticker}</code>)\n"
        f"• Quantità: <code>{quote} quote</code>\n"
        f"• Prezzo di Carico: <code>€{pmc:.3f}</code>\n"
        f"• Controvalore: <code>€{quote * pmc:.2f}</code>",
        parse_mode='HTML'
    )

async def bottone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("buy_"):
        _, ticker, prezzo = data.split("_")
        context.user_data['pending_ticker'] = ticker
        context.user_data['pending_prezzo'] = float(prezzo)
        context.user_data['nome_titolo'] = PANIERE.get(ticker, ticker)

        await query.edit_message_text(
            f"👍 Segnale confermato per <b>{context.user_data['nome_titolo']}</b>!\n\n"
            f"👉 <b>Quante quote hai comprato e a che prezzo?</b>\n"
            f"Rispondi scrivendo ad esempio: <code>3 a {prezzo}</code>",
            parse_mode='HTML'
        )
        return ATTESA_DETTAGLI_ACQUISTO

    elif data.startswith("ignora_"):
        _, ticker = data.split("_")
        await query.edit_message_text(f"Segnale per {ticker} ignorato.")
        return ConversationHandler.END

    elif data.startswith("venduto_"):
        _, ticker = data.split("_")
        portafoglio = carica_portafoglio()
        if ticker in portafoglio:
            del portafoglio[ticker]
            salva_portafoglio(portafoglio)
        await query.edit_message_text(f"✅ Posizione su <b>{ticker}</b> chiusa e rimossa dal monitoraggio.", parse_mode='HTML')
        return ConversationHandler.END

    elif data.startswith("tieni_"):
        _, ticker = data.split("_")
        await query.edit_message_text(f"⏳ Posizione su {ticker} mantenuta in portafoglio.", parse_mode='HTML')
        return ConversationHandler.END

async def registra_dettagli_acquisto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    testo = update.message.text.lower().replace(",", ".")
    ticker = context.user_data.get('pending_ticker')
    nome = context.user_data.get('nome_titolo', ticker)

    try:
        parti = [p for p in testo.replace("a", " ").split() if p]
        quote = int(parti[0])
        pmc = float(parti[1])

        portafoglio = carica_portafoglio()
        portafoglio[ticker] = {
            "nome": nome,
            "quote": quote,
            "pmc": pmc,
            "data": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
        salva_portafoglio(portafoglio)

        await update.message.reply_text(
            f"✅ <b>REGISTRATO CON SUCCESSO!</b>\n\n"
            f"• Titolo: <b>{nome}</b>\n"
            f"• Quantità: <code>{quote} quote</code>\n"
            f"• Prezzo Medio di Carico: <code>€{pmc:.3f}</code>\n"
            f"• Controvalore totale: <code>€{quote * pmc:.2f}</code>",
            parse_mode='HTML'
        )
    except Exception:
        await update.message.reply_text("⚠️ Formato non riconosciuto. Esempio: <code>3 a 5.929</code>", parse_mode='HTML')
        return ATTESA_DETTAGLI_ACQUISTO

    return ConversationHandler.END

async def cmd_portafoglio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    portafoglio = carica_portafoglio()
    if not portafoglio:
        await update.message.reply_text("💼 Nessun titolo attualmente monitorato nel portafoglio.")
        return

    msg = "💼 <b>STATO PORTAFOGLIO & PROGRESSIONE OBIETTIVI DINAMICI</b>\n\n"

    for t, info in portafoglio.items():
        pmc = info['pmc']
        quote = info['quote']

        p_live = pmc
        atr_val = pmc * 0.03
        try:
            d = yf.download(t, period="3mo", interval="1d", progress=False)
            if not d.empty:
                if isinstance(d.columns, pd.MultiIndex):
                    d.columns = d.columns.get_level_values(0)
                c_series = d['Close'].iloc[:, 0] if isinstance(d['Close'], pd.DataFrame) else d['Close']
                p_live = float(c_series.iloc[-1])
                if len(d) >= 15:
                    atr_val = calcola_atr(d, 14)
        except Exception:
            pass

        tp_prezzo = pmc + (2.0 * atr_val)
        sl_prezzo = pmc - (1.5 * atr_val)

        rend_attuale = ((p_live - pmc) / pmc) * 100
        dist_tp = ((tp_prezzo - p_live) / p_live) * 100
        dist_sl = ((p_live - sl_prezzo) / p_live) * 100

        segno = "🟢" if rend_attuale >= 0 else "🔴"

        msg += (
            f"🔹 <b>{info['nome']}</b> (<code>{t}</code>)\n"
            f"• Quote: <code>{quote}</code> | Investito: <code>€{quote * pmc:.2f}</code>\n"
            f"• PMC: <code>€{pmc:.3f}</code> ➔ Attuale: <code>€{p_live:.3f}</code>\n"
            f"• Rendimento attuale: {segno} <b>{rend_attuale:+.2f}%</b>\n"
            f"🎯 <b>Target Dinamico (+2 ATR a €{tp_prezzo:.3f}):</b>\n"
            f"   Mancano: <b>{dist_tp:+.2f}%</b> al traguardo\n"
            f"⚠️ <b>Soglia Stop Dinamico (-1.5 ATR a €{sl_prezzo:.3f}):</b>\n"
            f"   Margine di sicurezza: <b>{dist_sl:.2f}%</b> prima dello stop\n"
            f"────────────────────\n"
        )

    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Avvio scansione quantitativa con Volumi, Regime FTSE MIB e ATR...")
    await esegui_scansione_e_monitoraggio(context.bot)

async def cmd_doge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = "https://api.crypto.com/v2/public/get-candlestick?instrument_name=DOGE_EUR&timeframe=1h"
    try:
        res = requests.get(url, timeout=10).json()
        candlesticks = res["result"]["data"]
        closes = [float(c["c"]) for c in candlesticks]
    except Exception:
        await update.message.reply_text("⚠️ Errore nel recupero dati da Crypto.com.")
        return

    prezzo_live = closes[-1]
    prezzo_base = 0.07280
    holdings = 186.49
    valore_saldo = holdings * prezzo_live

    recenti = closes[-20:]
    sma = float(np.mean(recenti))
    std = float(np.std(recenti))
    upper_band = sma + (2.0 * std)
    lower_band = sma - (2.0 * std)

    diff_base = ((prezzo_live - prezzo_base) / prezzo_base) * 100
    manca_tp = ((upper_band - prezzo_live) / prezzo_live) * 100
    margine_sl = ((prezzo_live - lower_band) / prezzo_live) * 100
    icona_diff = "🟢" if diff_base >= 0 else "🔴"

    testo = (
        f"🐕 <b>RADAR DOGECOIN (Crypto.com)</b>\n\n"
        f"• Saldo: <code>{holdings} DOGE</code> | Valore: <b>€{valore_saldo:.2f}</b>\n"
        f"• Base Pre-Crollo: <code>€{prezzo_base:.5f}</code>\n"
        f"• Prezzo Attuale: <code>€{prezzo_live:.5f}</code>\n"
        f"• Variazione dal Crollo: {icona_diff} <b>{diff_base:+.2f}%</b>\n\n"
        f"🎯 <b>Target Profitto (+2σ a €{upper_band:.5f}):</b>\n"
        f"   Distanza: <b>{manca_tp:+.2f}%</b> dal picco statistico\n\n"
        f"⚠️ <b>Supporto Accumulo (-2σ a €{lower_band:.5f}):</b>\n"
        f"   Margine: <b>{margine_sl:.2f}%</b> dalla soglia\n"
    )
    await update.message.reply_text(testo, parse_mode='HTML')

def main():
    threading.Thread(target=avvia_server_porta, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(bottone_callback)],
        states={
            ATTESA_DETTAGLI_ACQUISTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, registra_dettagli_acquisto)]
        },
        fallbacks=[]
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("compra", cmd_compra))
    app.add_handler(CommandHandler("portafoglio", cmd_portafoglio))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("doge", cmd_doge))
    app.add_handler(CommandHandler("tennis", cmd_tennis))
    app.add_handler(CommandHandler("basket", cmd_basket))

    if app.job_queue:
        app.job_queue.run_daily(job_schedulato, time=time(hour=18, minute=0))

    print("🤖 Bot Telegram avviato con moduli Finanza, Crypto, Tennis e Basket...")
    app.run_polling()

if __name__ == "__main__":
    main()
