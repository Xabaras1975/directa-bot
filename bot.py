import os
import json
import logging
import asyncio
from datetime import datetime, time
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import pandas as pd
import numpy as np
import yfinance as yf
import requests
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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8645159870:AAFYBPwc1DyFactd2N1s0s9B7Xpw3S5MMf0").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "942878993").strip()

PORTFOLIO_FILE = "portfolio.json"
ATTESA_DETTAGLI_ACQUISTO = 1

# POSIZIONI FISSE PERMANENTI (Non si cancellano mai nemmeno se Render si riavvia)
POSIZIONI_PREDEFINITE = {
    "ISP.MI": {
        "nome": "Intesa Sanpaolo",
        "quote": 3,
        "pmc": 5.929,
        "data": "2026-10-29 09:48"
    }
}

PANIERE = {
    "ENEL.MI": "Enel",
    "ISP.MI": "Intesa Sanpaolo",
    "UCG.MI": "UniCredit",
    "ENI.MI": "Eni",
    "STLAM.MI": "Stellantis",
    "RACE.MI": "Ferrari",
    "G.MI": "Generali",
    "SWDA.MI": "iShares MSCI World (SWDA)",
    "CSSPX.MI": "iShares S&P 500 (CSSPX)",
    "XEON.MI": "Xtrackers Overnight €STR (XEON)"
}

class SimpleWebServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot Directa Online")

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

async def esegui_scansione_e_monitoraggio(app_bot):
    portafoglio = carica_portafoglio()

    # 1. Monitoraggio posizioni aperte (Take Profit / Stop Loss)
    for ticker, info in list(portafoglio.items()):
        try:
            d = yf.download(ticker, period="5d", interval="1d", progress=False)
            if d.empty:
                continue
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)

            p_live = float(d['Close'].iloc[-1])
            pmc = info['pmc']
            quote = info['quote']
            rend_pct = ((p_live - pmc) / pmc) * 100

            if rend_pct >= 6.0 or rend_pct <= -4.0:
                tipo = "🎯 TAKE PROFIT (+6%)" if rend_pct >= 6.0 else "⚠️ STOP LOSS (-4%)"
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

    # 2. Ricerca nuovi ingressi
    trovati = 0
    for ticker, nome in PANIERE.items():
        if ticker in portafoglio:
            continue
        try:
            df = yf.download(ticker, period="1y", interval="1d", progress=False)
            if df.empty or len(df) < 60:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            close = df['Close']
            p_curr = float(close.iloc[-1])
            sma20 = float(close.rolling(20).mean().iloc[-1])
            sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else float(close.rolling(50).mean().iloc[-1])
            std20 = float(close.rolling(20).std().iloc[-1])
            z_score = (p_curr - sma20) / std20 if std20 > 0 else 0.0

            delta = close.diff()
            g = (delta.where(delta > 0, 0)).rolling(14).mean()
            l = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi = float(100 - (100 / (1 + (g / l)))).iloc[-1]

            score = 50.0
            if z_score <= -2.0: score += 25
            elif z_score <= -1.0: score += 12
            elif z_score >= 2.0: score -= 20

            if rsi < 32: score += 20
            elif rsi < 42: score += 10
            elif rsi > 70: score -= 20

            if p_curr > sma200: score += 10
            else: score -= 15

            score = int(np.clip(score, 0, 100))

            if score >= 60:
                trovati += 1
                tastiera = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Ho Comprato", callback_data=f"buy_{ticker}_{p_curr:.2f}")],
                    [InlineKeyboardButton("❌ Ignora", callback_data=f"ignora_{ticker}")]
                ])
                testo = (
                    f"📢 <b>OPPORTUNITÀ INGRESSO DIRECTA</b>\n\n"
                    f"• <b>{nome}</b> (<code>{ticker}</code>)\n"
                    f"• Ultimo Prezzo: <code>€{p_curr:.2f}</code>\n"
                    f"• Score Statistico: <b>{score}/100</b>\n"
                    f"• RSI: <code>{rsi:.1f}</code> | Z-Score: <code>{z_score:+.2f}σ</code>\n\n"
                    f"Hai eseguito l'acquisto su Directa?"
                )
                await app_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=testo, parse_mode='HTML', reply_markup=tastiera)
        except Exception:
            continue

    if trovati == 0:
        await app_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="ℹ️ Scansione completata: nessun titolo con score ≥ 60 al momento.")

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
        f"• Controvalore: <code>€{quote * pmc:.2f}</code>\n"
        f"• 🎯 Take Profit (+6%): <code>€{pmc * 1.06:.3f}</code>\n"
        f"• ⚠️ Stop Loss (-4%): <code>€{pmc * 0.96:.3f}</code>",
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

    msg = "💼 <b>STATO PORTAFOGLIO & PROGRESSIONE OBIETTIVI</b>\n\n"

    for t, info in portafoglio.items():
        pmc = info['pmc']
        quote = info['quote']
        tp_prezzo = pmc * 1.06
        sl_prezzo = pmc * 0.96

        # Recupero prezzo live
        p_live = pmc
        try:
            d = yf.download(t, period="2d", interval="1d", progress=False)
            if not d.empty:
                if isinstance(d.columns, pd.MultiIndex):
                    d.columns = d.columns.get_level_values(0)
                p_live = float(d['Close'].iloc[-1])
        except Exception:
            pass

        rend_attuale = ((p_live - pmc) / pmc) * 100
        dist_tp = ((tp_prezzo - p_live) / p_live) * 100
        dist_sl = ((p_live - sl_prezzo) / p_live) * 100

        segno = "🟢" if rend_attuale >= 0 else "🔴"

        msg += (
            f"🔹 <b>{info['nome']}</b> (<code>{t}</code>)\n"
            f"• Quote: <code>{quote}</code> | Investito: <code>€{quote * pmc:.2f}</code>\n"
            f"• PMC: <code>€{pmc:.3f}</code> ➔ Attuale: <code>€{p_live:.3f}</code>\n"
            f"• Rendimento attuale: {segno} <b>{rend_attuale:+.2f}%</b>\n"
            f"🎯 <b>Target Take-Profit (+6% a €{tp_prezzo:.3f}):</b>\n"
            f"   Mancano: <b>{dist_tp:+.2f}%</b> al traguardo\n"
            f"⚠️ <b>Soglia Stop-Loss (-4% a €{sl_prezzo:.3f}):</b>\n"
            f"   Margine di sicurezza: <b>{dist_sl:.2f}%</b> prima dello stop\n"
            f"────────────────────\n"
        )

    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Avvio scansione di Borsa Italiana...")
    await esegui_scansione_e_monitoraggio(context.bot)

async def cmd_doge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando interattivo per monitorare DOGE con calcolo Bollinger e recupero dal pre-crollo"""
    url = "https://api.crypto.com/v2/public/get-candlestick?instrument_name=DOGE_EUR&timeframe=1h"
    try:
        res = requests.get(url, timeout=10).json()
        candlesticks = res["result"]["data"]
        closes = [float(c["c"]) for c in candlesticks]
    except Exception:
        await update.message.reply_text("⚠️ Errore nel recupero dati da Crypto.com.")
        return

    prezzo_live = closes[-1]
    prezzo_base = 0.07280  # Livello prima del crollo (29 Ottobre)
    holdings = 186.49
    valore_saldo = holdings * prezzo_live

    # Statistiche Bollinger (20 periodi orari, 2 std)
    recenti = closes[-20:]
    sma = float(np.mean(recenti))
    std = float(np.std(recenti))
    upper_band = sma + (2.0 * std)
    lower_band = sma - (2.0 * std)

    # Calcolo percentuali
    diff_base = ((prezzo_live - prezzo_base) / prezzo_base) * 100
    manca_tp = ((upper_band - prezzo_live) / prezzo_live) * 100
    margine_sl = ((prezzo_live - lower_band) / prezzo_live) * 100
    icona_diff = "🟢" if diff_base >= 0 else "🔴"

    testo = (
        f"🐕 <b>RADAR DOGECOIN (Crypto.com)</b>\n\n"
        f"• Saldo: <code>{holdings} DOGE</code> | Valore: <b>€{valore_saldo:.2f}</b>\n"
        f"• Base Pre-Crollo: <code>€{prezzo_base:.5f}</code>\n"
        f"• Prezzo Attuale: <code>€{prezzo_live:.5f}</code>\n"
        f"• Recupero dal Pre-Crollo: {icona_diff} <b>{diff_base:+.2f}%</b>\n\n"
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

    if app.job_queue:
        app.job_queue.run_daily(job_schedulato, time=time(hour=18, minute=0))

    print("🤖 Bot Telegram avviato e in ascolto...")
    app.run_polling()

if __name__ == "__main__":
    main()
