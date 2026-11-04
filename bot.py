import os
import json
import logging
import asyncio
import math
import urllib.parse
import xml.etree.ElementTree as ET
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

PORTFOLIO_FILE = "portfolio.json"
STORICO_FILE = "storico_operazioni.json"
ATTESA_DETTAGLI_ACQUISTO = 1

POSIZIONI_PREDEFINITE = {}

# PANIERE STRUTTURATO PER SETTORE
PANIERE = {
    # Ciclici, Industriali & Export
    "STLAM.MI": {"nome": "Stellantis", "settore": "export"},
    "RACE.MI": {"nome": "Ferrari", "settore": "export"},
    "MREC.MI": {"nome": "Maire Tecnimont", "settore": "export"},
    "SAL.MI": {"nome": "Webuild", "settore": "export"},
    "SFER.MI": {"nome": "Salvatore Ferragamo", "settore": "export"},

    # Finanziari & Bancari
    "ISP.MI": {"nome": "Intesa Sanpaolo", "settore": "finanza"},
    "UCG.MI": {"nome": "UniCredit", "settore": "finanza"},
    "BPE.MI": {"nome": "BPER Banca", "settore": "finanza"},
    "BMED.MI": {"nome": "Banca Mediolanum", "settore": "finanza"},
    "G.MI": {"nome": "Generali", "settore": "finanza"},

    # Difensivi & Utility
    "ENEL.MI": {"nome": "Enel", "settore": "utility"},
    "TRN.MI": {"nome": "Terna", "settore": "utility"},
    "SRG.MI": {"nome": "Snam", "settore": "utility"},
    "IG.MI": {"nome": "Italgas", "settore": "utility"},
    "A2A.MI": {"nome": "A2A", "settore": "utility"},
    "HER.MI": {"nome": "Hera", "settore": "utility"},
    "IRE.MI": {"nome": "Iren", "settore": "utility"},

    # Altri titoli & ETF
    "ENI.MI": {"nome": "Eni", "settore": "energy"},
    "OVS.MI": {"nome": "OVS", "settore": "consumer"},
    "SWDA.MI": {"nome": "iShares MSCI World (SWDA)", "settore": "etf"},
    "CSSPX.MI": {"nome": "iShares S&P 500 (CSSPX)", "settore": "etf"},
    "XEON.MI": {"nome": "Xtrackers Overnight €STR", "settore": "cash"}
}

class SimpleWebServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot Directa & Intelligence Online")

    def log_message(self, format, *args):
        return

def avvia_server_porta():
    porta = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", porta), SimpleWebServer)
    logging.info(f"Server web attivo sulla porta {porta}")
    server.serve_forever()

def carica_portafoglio():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return dict(POSIZIONI_PREDEFINITE)

def salva_portafoglio(data):
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(data, f, indent=4)

def carica_storico():
    if os.path.exists(STORICO_FILE):
        try:
            with open(STORICO_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def salva_storico(data):
    with open(STORICO_FILE, "w") as f:
        json.dump(data, f, indent=4)

def registra_chiusura_operazione(ticker, nome, quote, pmc, p_uscita, motivo="Vendita Manuale"):
    pnl_unitario = p_uscita - pmc
    pnl_totale = pnl_unitario * quote
    rend_pct = ((p_uscita - pmc) / pmc) * 100.0 if pmc > 0 else 0.0

    operazione = {
        "ticker": ticker,
        "nome": nome,
        "quote": quote,
        "pmc": pmc,
        "prezzo_uscita": p_uscita,
        "pnl_euro": round(pnl_totale, 2),
        "rendimento_pct": round(rend_pct, 2),
        "data_chiusura": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "motivo": motivo
    }

    storico = carica_storico()
    storico.append(operazione)
    salva_storico(storico)
    return operazione

def estrai_serie(df, col_nome):
    if col_nome not in df.columns:
        return pd.Series(dtype=float)
    col = df[col_nome]
    if isinstance(col, pd.DataFrame):
        col = col.iloc[:, 0]
    return col.dropna()

def calcola_atr(df, period=14):
    high = estrai_serie(df, 'High')
    low = estrai_serie(df, 'Low')
    close = estrai_serie(df, 'Close')
    
    if len(close) < period + 1:
        return float(close.iloc[-1] * 0.02) if not close.empty else 0.05
    
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).dropna()
    atr = tr.rolling(period).mean().dropna()
    
    if atr.empty or pd.isna(atr.iloc[-1]):
        return float(close.iloc[-1] * 0.02)
    return float(atr.iloc[-1])

def verifica_regime_ftsemib():
    try:
        df_index = yf.download("FTSEMIB.MI", period="6mo", interval="1d", progress=False)
        if df_index.empty:
            return True, 0.0
        close = estrai_serie(df_index, 'Close')
        if close.empty:
            return True, 0.0
        p_idx = float(close.iloc[-1])
        sma50_series = close.rolling(50).mean().dropna()
        sma50_idx = float(sma50_series.iloc[-1]) if not sma50_series.empty else p_idx
        bullish = p_idx >= sma50_idx
        return bullish, p_idx
    except Exception:
        return True, 0.0

def analizza_notizie_macro():
    feed_urls = [
        "https://news.google.com/rss/search?q=Borsa+Italiana+FTSE+MIB+economia&hl=it&gl=IT&ceid=IT:it",
        "https://news.google.com/rss/search?q=elezioni+USA+Wall+Street+tassi+BCE+FED&hl=it&gl=IT&ceid=IT:it"
    ]
    notizie = []
    headers = {"User-Agent": "Mozilla/5.0"}

    for url in feed_urls:
        try:
            r = requests.get(url, headers=headers, timeout=6)
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall('.//item')[:4]:
                    titolo = item.find('title').text if item.find('title') is not None else ""
                    if titolo: notizie.append(titolo)
        except Exception:
            continue

    parole_bull = ["taglio tassi", "rally", "crescita", "record", "stimoli", "accordo", "fiducia", "utili"]
    parole_bear = ["dazi", "inflazione", "guerra", "crisi", "tassi alti", "recessione", "spread in aumento"]
    parole_usa = ["elezioni", "midterm", "congresso", "democrat", "trump", "casa bianca"]

    punteggio = 0
    rilevanze = []

    for n in notizie:
        nl = n.lower()
        if any(p in nl for p in parole_bull): punteggio += 1
        if any(p in nl for p in parole_bear): punteggio -= 1
        if any(p in nl for p in parole_usa): rilevanze.append(n)

    if punteggio > 1:
        sentiment = "BULLISH"
        settore_favorito = "export"
        desc_sentiment = "🟢 PROPENSIONE AL RISCHIO / ACCORDI COMMERCIALI"
    elif punteggio < -1:
        sentiment = "BEARISH"
        settore_favorito = "utility"
        desc_sentiment = "🔴 PRUDENZA / INCERTEZZA TASSI E DAZI"
    else:
        sentiment = "NEUTRO"
        settore_favorito = "tutti"
        desc_sentiment = "🟡 EQUILIBRATO / SELEZIONE TECNICA"

    return sentiment, settore_favorito, desc_sentiment, notizie[:4], rilevanze

async def esegui_scansione_e_monitoraggio(app_bot):
    portafoglio = carica_portafoglio()

    # 1. Alert Take Profit / Stop Loss sulle posizioni aperte
    for ticker, info in list(portafoglio.items()):
        try:
            d = yf.download(ticker, period="3mo", interval="1d", progress=False)
            if d.empty: continue
            close = estrai_serie(d, 'Close')
            if close.empty or pd.isna(close.iloc[-1]): continue

            p_live = float(close.iloc[-1])
            pmc = info['pmc']
            quote = info['quote']

            atr_val = calcola_atr(d, 14)
            tp_dinamico = pmc + (2.0 * atr_val)
            sl_dinamico = pmc - (1.5 * atr_val)
            rend_pct = ((p_live - pmc) / pmc) * 100

            if p_live >= tp_dinamico or p_live <= sl_dinamico:
                tipo = f"🎯 TAKE PROFIT (+2 ATR a €{tp_dinamico:.3f})" if p_live >= tp_dinamico else f"⚠️ STOP LOSS (-1.5 ATR a €{sl_dinamico:.3f})"
                tastiera = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Venduto a Mercato", callback_data=f"venduto_{ticker}_{p_live:.3f}")],
                    [InlineKeyboardButton("⏳ Tieni Posizione", callback_data=f"tieni_{ticker}")]
                ])
                msg = (
                    f"🚨 <b>SEGNALE DI USCITA: {info['nome']} ({ticker})</b>\n\n"
                    f"• Condizione: <b>{tipo}</b>\n"
                    f"• PMC: <code>€{pmc:.3f}</code> ➔ Attuale: <code>€{p_live:.3f}</code>\n"
                    f"• Rendimento: <b>{rend_pct:+.2f}%</b> (Quote: {quote})\n\n"
                    f"Hai chiuso l'operazione su Directa?"
                )
                await app_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML', reply_markup=tastiera)
        except Exception as e:
            logging.error(f"Errore monitoraggio {ticker}: {e}")

    # 2. Clima Macro & Regime FTSE MIB
    sentiment, settore_fav, desc_sentiment, news, _ = analizza_notizie_macro()
    indice_rialzista, _ = verifica_regime_ftsemib()
    regime_icon = "🟢 Favorevole" if indice_rialzista else "🟠 Prudente (Sotto SMA 50)"

    # 3. Classifica Top 10 con Prezzi Reali Puliti
    risultati = []
    for ticker, meta in PANIERE.items():
        if ticker in portafoglio:
            continue
        nome = meta["nome"]
        settore = meta["settore"]

        try:
            df = yf.download(ticker, period="1y", interval="1d", progress=False)
            if df.empty: continue

            close = estrai_serie(df, 'Close')
            volume = estrai_serie(df, 'Volume')

            if len(close) < 25: continue

            p_curr = float(close.iloc[-1])
            if pd.isna(p_curr) or p_curr <= 0: continue

            sma20_s = close.rolling(20).mean().dropna()
            std20_s = close.rolling(20).std().dropna()
            sma200_s = close.rolling(200).mean().dropna() if len(close) >= 200 else close.rolling(50).mean().dropna()

            sma20 = float(sma20_s.iloc[-1]) if not sma20_s.empty else p_curr
            std20 = float(std20_s.iloc[-1]) if not std20_s.empty else 0.01
            sma200 = float(sma200_s.iloc[-1]) if not sma200_s.empty else p_curr

            z_score = (p_curr - sma20) / std20 if std20 > 0 else 0.0

            delta = close.diff()
            g = (delta.where(delta > 0, 0)).rolling(14).mean()
            l = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = g / l.replace(0, np.nan)
            rsi_s = (100 - (100 / (1 + rs))).dropna()
            rsi = float(rsi_s.iloc[-1]) if not rsi_s.empty else 50.0

            vol_ratio = 1.0
            if not volume.empty:
                vol_clean = volume[volume > 0]
                if len(vol_clean) >= 20:
                    vol_last = float(vol_clean.iloc[-1])
                    sma_vol = float(vol_clean.rolling(20).mean().iloc[-1])
                    if sma_vol > 0:
                        vol_ratio = vol_last / sma_vol

            atr = calcola_atr(df, 14)
            tp_stimato = p_curr + (2.0 * atr)
            sl_stimato = max(0.01, p_curr - (1.5 * atr))

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

            macro_tag = ""
            if sentiment == "BULLISH" and settore == "export":
                score += 10
                macro_tag = " [🌐 Spinta Export/USA]"
            elif sentiment == "BEARISH" and settore == "utility":
                score += 10
                macro_tag = " [🛡️ Difensivo/Cedola]"
            elif sentiment == "BEARISH" and settore == "export":
                score -= 8
                macro_tag = " [⚠️ Rischio Dazi]"

            score = int(np.clip(score, 0, 100))

            risultati.append({
                "ticker": ticker, "nome": nome, "prezzo": p_curr,
                "score": score, "rsi": rsi, "z_score": z_score,
                "vol_ratio": vol_ratio, "atr": atr, "tp": tp_stimato,
                "sl": sl_stimato, "macro_tag": macro_tag
            })
        except Exception as e:
            logging.error(f"Errore analisi {ticker}: {e}")
            continue

    if not risultati:
        await app_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="ℹ️ Dati di borsa in aggiornamento. Riprova tra poco.")
        return

    risultati.sort(key=lambda x: x["score"], reverse=True)
    top10 = risultati[:10]

    classifica_txt = (
        f"📊 <b>TOP 10 CON INTELLIGENCE NOTIZIE & REGIME</b>\n"
        f"🌐 <i>FTSE MIB: {regime_icon}</i>\n"
        f"🧭 <i>Clima Macro: {desc_sentiment}</i>\n\n"
    )
    for i, item in enumerate(top10, start=1):
        icona = "🔥" if item['score'] >= 55 else "🔹"
        classifica_txt += (
            f"{i}. {icona} <b>{item['nome']}</b> (<code>{item['ticker']}</code>){item['macro_tag']}\n"
            f"   Prezzo: <code>€{item['prezzo']:.3f}</code> | Score: <b>{item['score']}/100</b>\n"
            f"   RSI: <code>{item['rsi']:.1f}</code> | Z-Score: <code>{item['z_score']:+.2f}σ</code> | Vol: <code>{item['vol_ratio']:.1f}x</code>\n"
            f"   🎯 TP: <code>€{item['tp']:.3f}</code> | ⚠️ SL: <code>€{item['sl']:.3f}</code>\n\n"
        )
    await app_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=classifica_txt, parse_mode='HTML')

async def job_schedulato(context: ContextTypes.DEFAULT_TYPE):
    if datetime.now().weekday() < 5:
        await esegui_scansione_e_monitoraggio(context.bot)

# Comandi Utente
async def cmd_compra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("⚠️ Formato: <code>/compra &lt;TICKER&gt; &lt;QUOTE&gt; &lt;PMC&gt;</code>\nEs: <code>/compra UCG.MI 10 33.50</code>", parse_mode='HTML')
        return

    ticker_raw = args[0].upper().strip()
    ticker = ticker_raw if ticker_raw.endswith(".MI") else f"{ticker_raw}.MI"

    try:
        quote = int(args[1])
        pmc = float(args[2].replace(",", "."))
    except ValueError:
        await update.message.reply_text("⚠️ Valori non validi.", parse_mode='HTML')
        return

    nome = PANIERE.get(ticker, {}).get("nome", ticker)
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
        f"• Quantità: <code>{quote} quote</code> | PMC: <code>€{pmc:.3f}</code>\n"
        f"• Controvalore: <code>€{quote * pmc:.2f}</code>",
        parse_mode='HTML'
    )

async def cmd_vendi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("⚠️ Formato: <code>/vendi &lt;TICKER&gt; &lt;PREZZO&gt;</code>\nEs: <code>/vendi ISP.MI 6.12</code>", parse_mode='HTML')
        return

    ticker_raw = args[0].upper().strip()
    ticker = ticker_raw if ticker_raw.endswith(".MI") else f"{ticker_raw}.MI"

    try:
        prezzo_vendita = float(args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("⚠️ Prezzo non valido.", parse_mode='HTML')
        return

    portafoglio = carica_portafoglio()
    if ticker not in portafoglio:
        await update.message.reply_text(f"⚠️ Il titolo <b>{ticker}</b> non è presente nel portafoglio attivo.", parse_mode='HTML')
        return

    info = portafoglio[ticker]
    op = registra_chiusura_operazione(
        ticker=ticker,
        nome=info['nome'],
        quote=info['quote'],
        pmc=info['pmc'],
        p_uscita=prezzo_vendita,
        motivo="Vendita Manuale"
    )

    del portafoglio[ticker]
    salva_portafoglio(portafoglio)

    icona = "🟢 PROFITTO" if op['pnl_euro'] >= 0 else "🔴 PERDITA"
    msg = (
        f"✅ <b>VENDITA REGISTRATA!</b>\n\n"
        f"• Titolo: <b>{op['nome']}</b> (<code>{ticker}</code>)\n"
        f"• PMC: <code>€{op['pmc']:.3f}</code> ➔ Uscita: <code>€{op['prezzo_uscita']:.3f}</code>\n"
        f"• Esito: <b>{icona}</b> | P&L: <b>€{op['pnl_euro']:+.2f} ({op['rendimento_pct']:+.2f}%)</b>\n\n"
        f"<i>Titolo rimosso dal portafoglio e salvato nello storico.</i>"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_portafoglio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    portafoglio = carica_portafoglio()
    if not portafoglio:
        await update.message.reply_text("💼 Nessun titolo attualmente attivo nel portafoglio.")
        return

    sentiment, settore_fav, desc_sentiment, news, _ = analizza_notizie_macro()

    msg = (
        f"💼 <b>PORTAFOGLIO ATTIVO & PERFORMANCE LIVE</b>\n"
        f"🧭 <i>Clima Macro Globale: {desc_sentiment}</i>\n\n"
    )
    totale_investito = 0.0
    totale_attuale = 0.0

    for t, info in portafoglio.items():
        pmc = info['pmc']
        quote = info['quote']
        p_live = pmc
        atr_val = pmc * 0.03

        try:
            d = yf.download(t, period="3mo", interval="1d", progress=False)
            if not d.empty:
                close = estrai_serie(d, 'Close')
                if not close.empty and not pd.isna(close.iloc[-1]):
                    p_live = float(close.iloc[-1])
                atr_val = calcola_atr(d, 14)
        except Exception:
            pass

        val_carico = quote * pmc
        val_live = quote * p_live
        totale_investito += val_carico
        totale_attuale += val_live

        rend_attuale = ((p_live - pmc) / pmc) * 100
        tp_prezzo = pmc + (2.0 * atr_val)
        sl_prezzo = max(0.01, pmc - (1.5 * atr_val))
        manca_tp = ((tp_prezzo - p_live) / p_live) * 100
        margine_sl = ((p_live - sl_prezzo) / p_live) * 100
        segno = "🟢" if rend_attuale >= 0 else "🔴"

        settore = PANIERE.get(t, {}).get("settore", "generico")
        impatto_macro = "⚪ Neutro / Tecnico"
        if sentiment == "BULLISH" and settore == "export":
            impatto_macro = "🟢 VENTO A FAVORE (Spinta Export / USA)"
        elif sentiment == "BEARISH" and settore == "utility":
            impatto_macro = "🛡️ PROTETTO (Flusso Difensivo / Cedola)"
        elif sentiment == "BEARISH" and settore == "export":
            impatto_macro = "⚠️ ATTENZIONE (Pressione Dazi / Ciclo Debole)"
        elif sentiment == "BULLISH" and settore == "finanza":
            impatto_macro = "🟢 FAVOREVOLE (Propensione al rischio / Spread stabile)"

        msg += (
            f"🔹 <b>{info['nome']}</b> (<code>{t}</code>)\n"
            f"• Quote: <code>{quote}</code> | Investito: <code>€{val_carico:.2f}</code> ➔ Attuale: <code>€{val_live:.2f}</code>\n"
            f"• PMC: <code>€{pmc:.3f}</code> ➔ Prezzo: <code>€{p_live:.3f}</code> ({segno} <b>{rend_attuale:+.2f}%</b>)\n"
            f"• 🎯 Target (+2 ATR): <code>€{tp_prezzo:.3f}</code> (distanza: <b>{manca_tp:+.2f}%</b>)\n"
            f"• ⚠️ Stop (-1.5 ATR): <code>€{sl_prezzo:.3f}</code> (margine: <b>{margine_sl:.2f}%</b>)\n"
            f"• 🌐 <b>Impatto Notizie:</b> <code>{impatto_macro}</code>\n"
            f"────────────────────\n"
        )

    pnl_totale_euro = totale_attuale - totale_investito
    pnl_totale_pct = ((totale_attuale - totale_investito) / totale_investito * 100) if totale_investito > 0 else 0.0
    segno_tot = "🟢" if pnl_totale_euro >= 0 else "🔴"

    msg += (
        f"💰 <b>RIEPILOGO ASSET:</b>\n"
        f"• Totale Investito: <code>€{totale_investito:.2f}</code>\n"
        f"• Valore Corrente: <code>€{totale_attuale:.2f}</code>\n"
        f"• P&L Totale: {segno_tot} <b>€{pnl_totale_euro:+.2f} ({pnl_totale_pct:+.2f}%)</b>"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_storico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storico = carica_storico()
    if not storico:
        await update.message.reply_text("📜 Nessuna operazione chiusa registrata nello storico.")
        return

    tot_pnl = sum(item['pnl_euro'] for item in storico)
    vinte = [item for item in storico if item['pnl_euro'] > 0]
    perse = [item for item in storico if item['pnl_euro'] < 0]
    win_rate = (len(vinte) / len(storico)) * 100 if storico else 0.0

    msg = (
        f"📜 <b>STORICO OPERAZIONI CONSOLIDATE</b>\n\n"
        f"• Operazioni Chiuse: <b>{len(storico)}</b>\n"
        f"• Win Rate: <b>{win_rate:.1f}%</b> (✅ {len(vinte)} | ❌ {len(perse)})\n"
        f"• P&L Netto Realizzato: <b>€{tot_pnl:+.2f}</b>\n\n"
        f"<b>Ultime chiusure:</b>\n"
    )
    for op in storico[-5:]:
        icona = "🟢" if op['pnl_euro'] >= 0 else "🔴"
        msg += (
            f"{icona} <b>{op['nome']}</b> ({op['data_chiusura']})\n"
            f"   PMC: <code>€{op['pmc']:.3f}</code> ➔ Uscita: <code>€{op['prezzo_uscita']:.3f}</code> | <b>€{op['pnl_euro']:+.2f} ({op['rendimento_pct']:+.2f}%)</b>\n"
        )
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌐 Scansione notizie macroeconomiche in corso...")
    sentiment, settore_fav, desc_sentiment, news, rilevanze = analizza_notizie_macro()

    msg = (
        f"🧭 <b>RADAR PREVISIONALE & MACRO INTELLIGENCE</b>\n\n"
        f"📊 <b>Sentiment Globale:</b> <b>{desc_sentiment}</b>\n"
        f"🎯 <b>Settore Favorito:</b> <code>{settore_fav.upper()}</code>\n\n"
        f"🔍 <b>Notizie Finanziarie Rilevate:</b>\n"
    )
    for n in news[:4]: msg += f"• <i>{n}</i>\n"

    if rilevanze:
        msg += "\n🗳️ <b>Eventi Politici / USA:</b>\n"
        for r in rilevanze[:2]: msg += f"📌 <b>{r}</b>\n"

    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Scansione quantitativa con ponderazione macro in corso...")
    await esegui_scansione_e_monitoraggio(context.bot)

async def cmd_doge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = "https://api.crypto.com/v2/public/get-candlestick?instrument_name=DOGE_EUR&timeframe=1h"
    try:
        res = requests.get(url, timeout=10).json()
        closes = [float(c["c"]) for c in res["result"]["data"]]
    except Exception:
        await update.message.reply_text("⚠️ Errore connessione Crypto.com.")
        return

    p_live = closes[-1]
    p_base = 0.07280
    holdings = 186.49
    valore = holdings * p_live
    sma = float(np.mean(closes[-20:]))
    std = float(np.std(closes[-20:]))
    up = sma + (2.0 * std)
    low = sma - (2.0 * std)

    diff = ((p_live - p_base) / p_base) * 100
    icon = "🟢" if diff >= 0 else "🔴"

    testo = (
        f"🐕 <b>RADAR DOGECOIN (Crypto.com)</b>\n\n"
        f"• Saldo: <code>{holdings} DOGE</code> | Valore: <b>€{valore:.2f}</b>\n"
        f"• Attuale: <code>€{p_live:.5f}</code> (Base: <code>€{p_base:.5f}</code> ➔ {icon} <b>{diff:+.2f}%</b>)\n"
        f"🎯 Target (+2σ): <code>€{up:.5f}</code> | ⚠️ Supporto (-2σ): <code>€{low:.5f}</code>"
    )
    await update.message.reply_text(testo, parse_mode='HTML')

# Moduli Quant Tennis & Basket
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
    args = context.args
    if len(args) < 4:
        await update.message.reply_text("🎾 Formato: <code>/tennis &lt;p_serv_A&gt; &lt;p_serv_B&gt; &lt;quota_A&gt; &lt;quota_B&gt;</code>", parse_mode='HTML')
        return
    try:
        pA, pB = float(args[0].replace(",", ".")), float(args[1].replace(",", "."))
        quota_A, quota_B = float(args[2].replace(",", ".")), float(args[3].replace(",", "."))
    except ValueError:
        await update.message.reply_text("⚠️ Parametri non validi.", parse_mode='HTML')
        return

    gA, gB = prob_game_servizio(pA), prob_game_servizio(pB)
    tb = prob_tiebreak(pA, pB)
    p_game_A = (gA + (1.0 - gB)) / 2.0
    q_game = 1.0 - p_game_A
    set_secco = sum(math.comb(5 + b, b) * (p_game_A**6) * (q_game**b) for b in range(5))
    set_75 = math.comb(10, 5) * (p_game_A**5) * (q_game**5) * (p_game_A**2)
    set_tb = math.comb(10, 5) * (p_game_A**5) * (q_game**5) * 2 * p_game_A * q_game * tb
    prob_set_A = set_secco + set_75 + set_tb
    prob_match_A = (prob_set_A**2) + (2 * prob_set_A * (1 - prob_set_A) * prob_set_A)
    prob_match_B = 1.0 - prob_match_A

    fair_A = 1.0 / prob_match_A
    fair_B = 1.0 / prob_match_B
    ev_A = ((quota_A * prob_match_A) - 1.0) * 100
    ev_B = ((quota_B * prob_match_B) - 1.0) * 100

    msg = (
        "🎾 <b>MODELLO TENNIS PUNTO-A-PUNTO</b>\n\n"
        f"👤 <b>Giocatore A:</b> Prob <b>{prob_match_A*100:.1f}%</b> | Quota Equa <code>{fair_A:.2f}</code> | EV: <b>{ev_A:+.2f}%</b>\n"
        f"👤 <b>Giocatore B:</b> Prob <b>{prob_match_B*100:.1f}%</b> | Quota Equa <code>{fair_B:.2f}</code> | EV: <b>{ev_B:+.2f}%</b>"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_basket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 8:
        await update.message.reply_text("🏀 Formato: <code>/basket &lt;paceA&gt; &lt;offA&gt; &lt;defA&gt; &lt;paceB&gt; &lt;offB&gt; &lt;defB&gt; &lt;linea&gt; &lt;quota&gt;</code>", parse_mode='HTML')
        return
    try:
        p_A, o_A, d_A = float(args[0]), float(args[1]), float(args[2])
        p_B, o_B, d_B = float(args[3]), float(args[4]), float(args[5])
        linea_spread = float(args[6].replace(",", "."))
        quota_spread = float(args[7].replace(",", "."))
    except ValueError:
        await update.message.reply_text("⚠️ Parametri non validi.", parse_mode='HTML')
        return

    pace_match = (p_A * p_B) / 100.0
    punti_A = (pace_match * (o_A * d_B) / (114.0 * 100.0)) + 1.25
    punti_B = (pace_match * (o_B * d_A) / (114.0 * 100.0)) - 1.25
    scarto_atteso = punti_A - punti_B
    soglia = -linea_spread
    z = (soglia - scarto_atteso) / 11.2
    prob_copertura = 1.0 - norm.cdf(z)
    fair_odds = 1.0 / prob_copertura if prob_copertura > 0 else 999.0
    ev = ((quota_spread * prob_copertura) - 1.0) * 100

    msg = (
        "🏀 <b>MODELLO BASKET</b>\n\n"
        f"• Punteggio Stimato: <b>{punti_A:.1f} - {punti_B:.1f}</b>\n"
        f"• Spread Previsto: <b>{-scarto_atteso:+.1f}</b>\n"
        f"• Copertura ({linea_spread:+.1f}): <b>{prob_copertura*100:.1f}%</b> | Quota Equa: <code>{fair_odds:.2f}</code> | EV: <b>{ev:+.2f}%</b>"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

# Callback Bottoni
async def bottone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("venduto_"):
        parti = data.split("_")
        ticker = parti[1]
        p_live = float(parti[2]) if len(parti) > 2 else 0.0

        portafoglio = carica_portafoglio()
        if ticker in portafoglio:
            info = portafoglio[ticker]
            prezzo_chiusura = p_live if p_live > 0 else info['pmc']
            op = registra_chiusura_operazione(
                ticker=ticker,
                nome=info['nome'],
                quote=info['quote'],
                pmc=info['pmc'],
                p_uscita=prezzo_chiusura,
                motivo="Segnale Automatico"
            )
            del portafoglio[ticker]
            salva_portafoglio(portafoglio)

            await query.edit_message_text(
                f"✅ Posizione su <b>{ticker}</b> chiusa a <code>€{prezzo_chiusura:.3f}</code>!\n"
                f"P&L: <b>€{op['pnl_euro']:+.2f} ({op['rendimento_pct']:+.2f}%)</b> salvato nello storico.",
                parse_mode='HTML'
            )
        else:
            await query.edit_message_text(f"Posizione su {ticker} già chiusa.")
        return ConversationHandler.END

    elif data.startswith("tieni_"):
        _, ticker = data.split("_")
        await query.edit_message_text(f"⏳ Posizione su {ticker} mantenuta in portafoglio.")
        return ConversationHandler.END

    elif data.startswith("buy_"):
        _, ticker, prezzo = data.split("_")
        context.user_data['pending_ticker'] = ticker
        context.user_data['pending_prezzo'] = float(prezzo)
        context.user_data['nome_titolo'] = PANIERE.get(ticker, {}).get("nome", ticker)

        await query.edit_message_text(
            f"👍 Segnale confermato per <b>{context.user_data['nome_titolo']}</b>!\n"
            f"👉 Rispondi con quote e prezzo (es: <code>3 a {prezzo}</code>):",
            parse_mode='HTML'
        )
        return ATTESA_DETTAGLI_ACQUISTO

    elif data.startswith("ignora_"):
        _, ticker = data.split("_")
        await query.edit_message_text(f"Segnale per {ticker} ignorato.")
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
            f"✅ <b>ACQUISTO REGISTRATO!</b>\n\n"
            f"• <b>{nome}</b>: <code>{quote} quote</code> a <code>€{pmc:.3f}</code>\n"
            f"• Totale: <code>€{quote * pmc:.2f}</code>",
            parse_mode='HTML'
        )
    except Exception:
        await update.message.reply_text("⚠️ Formato non valido. Esempio: <code>3 a 5.929</code>", parse_mode='HTML')
        return ATTESA_DETTAGLI_ACQUISTO

    return ConversationHandler.END

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
    app.add_handler(CommandHandler("vendi", cmd_vendi))
    app.add_handler(CommandHandler("portafoglio", cmd_portafoglio))
    app.add_handler(CommandHandler("storico", cmd_storico))
    app.add_handler(CommandHandler("outlook", cmd_outlook))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("doge", cmd_doge))
    app.add_handler(CommandHandler("tennis", cmd_tennis))
    app.add_handler(CommandHandler("basket", cmd_basket))

    if app.job_queue:
        app.job_queue.run_daily(job_schedulato, time=time(hour=18, minute=0))

    print("🤖 Bot Telegram avviato con estrazione prezzi pulita e resiliente a borsa chiusa...")
    app.run_polling()

if __name__ == "__main__":
    main()
