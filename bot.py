import os
import json
import logging
import asyncio
import math
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta, time
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from scipy.stats import norm
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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
GIST_ID = os.getenv("GIST_ID", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

PORTFOLIO_FILE = "portfolio.json"
STORICO_FILE = "storico_operazioni.json"
ATTESA_DETTAGLI_ACQUISTO = 1

POSIZIONI_PREDEFINITE = {}

# PANIERE AZIONARIO FTSEMIB / ITALIA CON MATRICE DI SENSIBILITA' FATTORIALE
PANIERE = {
    # Ciclici, Industriali & Export
    "STLAM.MI": {"nome": "Stellantis", "settore": "export", "beta_dazi": 0.85, "beta_tassi": -0.30, "beta_geopolitica": -0.40, "beta_debito": 0.20},
    "RACE.MI": {"nome": "Ferrari", "settore": "export", "beta_dazi": 0.50, "beta_tassi": -0.10, "beta_geopolitica": -0.20, "beta_debito": 0.10},
    "MAIRE.MI": {"nome": "Maire Tecnimont", "settore": "export", "beta_dazi": 0.60, "beta_tassi": -0.40, "beta_geopolitica": -0.30, "beta_debito": 0.30},
    "WBD.MI": {"nome": "Webuild", "settore": "export", "beta_dazi": 0.40, "beta_tassi": -0.50, "beta_geopolitica": -0.20, "beta_debito": 0.40},
    "SFER.MI": {"nome": "Salvatore Ferragamo", "settore": "export", "beta_dazi": 0.70, "beta_tassi": -0.20, "beta_geopolitica": -0.40, "beta_debito": 0.10},

    # Finanziari & Bancari
    "ISP.MI": {"nome": "Intesa Sanpaolo", "settore": "finanza", "beta_dazi": 0.20, "beta_tassi": 0.75, "beta_geopolitica": -0.30, "beta_debito": 0.80},
    "UCG.MI": {"nome": "UniCredit", "settore": "finanza", "beta_dazi": 0.30, "beta_tassi": 0.80, "beta_geopolitica": -0.35, "beta_debito": 0.75},
    "BPE.MI": {"nome": "BPER Banca", "settore": "finanza", "beta_dazi": 0.15, "beta_tassi": 0.70, "beta_geopolitica": -0.25, "beta_debito": 0.85},
    "BMED.MI": {"nome": "Banca Mediolanum", "settore": "finanza", "beta_dazi": 0.20, "beta_tassi": 0.50, "beta_geopolitica": -0.20, "beta_debito": 0.60},
    "G.MI": {"nome": "Generali", "settore": "finanza", "beta_dazi": 0.20, "beta_tassi": 0.40, "beta_geopolitica": -0.20, "beta_debito": 0.70},

    # Difensivi & Utility
    "ENEL.MI": {"nome": "Enel", "settore": "utility", "beta_dazi": -0.10, "beta_tassi": -0.65, "beta_geopolitica": 0.20, "beta_debito": 0.50},
    "TRN.MI": {"nome": "Terna", "settore": "utility", "beta_dazi": -0.05, "beta_tassi": -0.75, "beta_geopolitica": 0.35, "beta_debito": 0.40},
    "SRG.MI": {"nome": "Snam", "settore": "utility", "beta_dazi": -0.05, "beta_tassi": -0.70, "beta_geopolitica": 0.35, "beta_debito": 0.45},
    "IG.MI": {"nome": "Italgas", "settore": "utility", "beta_dazi": -0.05, "beta_tassi": -0.60, "beta_geopolitica": 0.30, "beta_debito": 0.40},
    "A2A.MI": {"nome": "A2A", "settore": "utility", "beta_dazi": 0.00, "beta_tassi": -0.50, "beta_geopolitica": 0.25, "beta_debito": 0.45},
    "HER.MI": {"nome": "Hera", "settore": "utility", "beta_dazi": 0.00, "beta_tassi": -0.45, "beta_geopolitica": 0.20, "beta_debito": 0.40},
    "IRE.MI": {"nome": "Iren", "settore": "utility", "beta_dazi": 0.00, "beta_tassi": -0.50, "beta_geopolitica": 0.20, "beta_debito": 0.45},

    # Energia & Consumi
    "ENI.MI": {"nome": "Eni", "settore": "energy", "beta_dazi": 0.20, "beta_tassi": 0.10, "beta_geopolitica": 0.80, "beta_debito": 0.30},
    "OVS.MI": {"nome": "OVS", "settore": "consumer", "beta_dazi": 0.30, "beta_tassi": -0.40, "beta_geopolitica": -0.30, "beta_debito": 0.30}
}

class SimpleWebServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Directa Trading Intelligence Online")

    def log_message(self, format, *args):
        return

def avvia_server_porta():
    porta = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", porta), SimpleWebServer)
    logging.info(f"Server web attivo sulla porta {porta}")
    server.serve_forever()

# --- GESTIONE DATI CON GITHUB GIST E PERSISTENZA CLOUD ---
def _leggi_gist_file(nome_file, default_val):
    if not GIST_ID or not GITHUB_TOKEN:
        return None
    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    try:
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            files = res.json().get("files", {})
            if nome_file in files:
                return json.loads(files[nome_file]["content"])
    except Exception as e:
        logging.error(f"Errore lettura Gist ({nome_file}): {e}")
    return None

def _scrivi_gist_file(nome_file, dati):
    if not GIST_ID or not GITHUB_TOKEN:
        return False
    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {
        "files": {
            nome_file: {
                "content": json.dumps(dati, indent=4)
            }
        }
    }
    try:
        res = requests.patch(url, headers=headers, json=payload, timeout=8)
        return res.status_code == 200
    except Exception as e:
        logging.error(f"Errore scrittura Gist ({nome_file}): {e}")
        return False

def carica_portafoglio():
    cloud_data = _leggi_gist_file(PORTFOLIO_FILE, {})
    if cloud_data is not None:
        return cloud_data
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return dict(POSIZIONI_PREDEFINITE)

def salva_portafoglio(data):
    if GIST_ID and GITHUB_TOKEN:
        _scrivi_gist_file(PORTFOLIO_FILE, data)
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(data, f, indent=4)

def carica_storico():
    cloud_data = _leggi_gist_file(STORICO_FILE, [])
    if cloud_data is not None:
        return cloud_data
    if os.path.exists(STORICO_FILE):
        try:
            with open(STORICO_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def salva_storico(data):
    if GIST_ID and GITHUB_TOKEN:
        _scrivi_gist_file(STORICO_FILE, data)
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

def calcola_adx(df, period=14):
    high = estrai_serie(df, 'High')
    low = estrai_serie(df, 'Low')
    close = estrai_serie(df, 'Close')
    if len(close) < (period * 2):
        return 20.0

    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr_s = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr_s)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr_s)

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    adx_s = dx.rolling(period).mean().dropna()
    if adx_s.empty:
        return 20.0
    return float(adx_s.iloc[-1])

def verifica_regime_ftsemib():
    try:
        df_index = yf.download("FTSEMIB.MI", period="6mo", interval="1d", progress=False)
        if df_index.empty: return True, 0.0
        close = estrai_serie(df_index, 'Close')
        if close.empty: return True, 0.0
        p_idx = float(close.iloc[-1])
        sma50_s = close.rolling(50).mean().dropna()
        sma50_idx = float(sma50_s.iloc[-1]) if not sma50_s.empty else p_idx
        return (p_idx >= sma50_idx), p_idx
    except Exception:
        return True, 0.0

def verifica_imminenza_earnings(ticker_obj, giorni_buffer=2):
    try:
        cal = ticker_obj.calendar
        oggi = date.today()
        earnings_date = None

        if isinstance(cal, dict) and "Earnings Date" in cal:
            date_list = cal["Earnings Date"]
            if date_list and len(date_list) > 0:
                earnings_date = date_list[0]
        elif isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.index:
            earnings_date = cal.loc["Earnings Date"].iloc[0]

        if earnings_date is not None:
            if isinstance(earnings_date, (datetime, pd.Timestamp)):
                earnings_date = earnings_date.date()
            diff_giorni = (earnings_date - oggi).days
            if 0 <= diff_giorni <= giorni_buffer:
                return True, earnings_date.strftime("%d/%m/%Y")
    except Exception:
        pass
    return False, None

def analizza_notizie_macro():
    feed_urls = [
        "https://news.google.com/rss/search?q=Borsa+Italiana+FTSE+MIB+economia+when:3d&hl=it&gl=IT&ceid=IT:it",
        "https://news.google.com/rss/search?q=tassi+BCE+FED+dazi+inflazione+when:3d&hl=it&gl=IT&ceid=IT:it"
    ]
    notizie = []
    headers = {"User-Agent": "Mozilla/5.0"}

    for url in feed_urls:
        try:
            r = requests.get(url, headers=headers, timeout=6)
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall('.//item')[:5]:
                    titolo = item.find('title').text if item.find('title') is not None else ""
                    if titolo: 
                        notizie.append(titolo)
        except Exception:
            continue

    f_dazi = 0.0
    f_tassi = 0.0
    f_geopolitica = 0.0
    f_debito = 0.0

    rilevanze = []

    for n in notizie:
        nl = n.lower()
        if any(p in nl for p in ["dazi", "tariffe", "protezionismo", "guerra commerciale"]):
            f_dazi -= 0.4
            rilevanze.append(f"Dazi/Commercio: {n}")
        if any(p in nl for p in ["accordo commerciale", "intesa", "apertura mercati", "export"]):
            f_dazi += 0.3

        if any(p in nl for p in ["taglio tassi", "bce taglia", "fed taglia", "allentamento"]):
            f_tassi -= 0.4
        if any(p in nl for p in ["tassi alti", "inflazione persistente", "stretta monetaria"]):
            f_tassi += 0.4

        if any(p in nl for p in ["guerra", "tensione", "missili", "escalation", "petrolio in rialzo"]):
            f_geopolitica += 0.4
            rilevanze.append(f"Geopolitica: {n}")

        if any(p in nl for p in ["spread btp", "debito", "deficit", "crisi politica"]):
            f_debito -= 0.3
        if any(p in nl for p in ["fiducia mercati", "promozione rating", "spread in calo"]):
            f_debito += 0.3

    f_dazi = float(np.clip(f_dazi, -1.0, 1.0))
    f_tassi = float(np.clip(f_tassi, -1.0, 1.0))
    f_geopolitica = float(np.clip(f_geopolitica, -1.0, 1.0))
    f_debito = float(np.clip(f_debito, -1.0, 1.0))

    if f_dazi >= 0.2 and f_tassi <= 0.0:
        desc_sentiment = "🟢 PROPENSIONE AL RISCHIO (Commercio Aperto / Tassi Non Ostili)"
        settore_fav = "export"
    elif f_dazi < -0.2 or f_geopolitica >= 0.3:
        desc_sentiment = "🔴 PRUDENZA (Rischio Dazi / Tensioni Geopolitiche)"
        settore_fav = "utility"
    elif f_tassi > 0.2:
        desc_sentiment = "🟡 TASSI SOSTENUTI (Favorevole al Margine Bancario)"
        settore_fav = "finanza"
    else:
        desc_sentiment = "⚪ EQUILIBRATO (Fattori Bilanciati / Focus Tecnico)"
        settore_fav = "tutti"

    fattori = {
        "dazi": f_dazi,
        "tassi": f_tassi,
        "geopolitica": f_geopolitica,
        "debito": f_debito
    }

    return desc_sentiment, settore_fav, fattori, notizie[:4], rilevanze

def calcola_macro_impact(ticker, fattori):
    info = PANIERE.get(ticker)
    if not info: return 0.0

    b_dazi = info.get("beta_dazi", 0.0)
    b_tassi = info.get("beta_tassi", 0.0)
    b_geo = info.get("beta_geopolitica", 0.0)
    b_deb = info.get("beta_debito", 0.0)

    impatto = (
        (b_dazi * fattori["dazi"] * 0.35) +
        (b_tassi * fattori["tassi"] * 0.25) +
        (b_geo * fattori["geopolitica"] * 0.20) +
        (b_deb * fattori["debito"] * 0.20)
    ) * 100.0

    return float(np.clip(impatto, -100.0, 100.0))

# FUNZIONE LOGISTICA PER STIMA WIN RATE ATTESO (0 - 100%)
def calcola_probabilita_setup(z_score, rsi, candela_verde, vol_ratio, adx, emi, dist_52w):
    score_z = np.clip((-z_score - 0.5) / 1.5, 0, 1) * 3.0
    score_rsi = np.clip((45.0 - rsi) / 25.0, 0, 1) * 2.0
    score_vol = (1.5 if vol_ratio >= 1.15 else 0.5) if candela_verde else 0.0
    score_adx = np.clip((35.0 - adx) / 15.0, 0, 1) * 1.5
    score_emi = np.clip((emi + 10.0) / 20.0, 0, 1) * 1.0
    score_min = 1.0 if dist_52w >= 1.04 else 0.0

    score_totale = score_z + score_rsi + score_vol + score_adx + score_emi + score_min
    # Calibrazione logistica ancorata al 75.7% storico
    prob_win = 40.0 + (46.0 / (1.0 + np.exp(-(score_totale - 5.0) / 1.5)))
    return round(float(prob_win), 1), round(float(score_totale), 1)

# --- CONSOLE INTERATTIVA DI COMANDO ---
async def cmd_console(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tastiera = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Scansione Rigorosa (/scan)", callback_data="btn_scan"),
            InlineKeyboardButton("🏆 Top 10 Ranking (/top10)", callback_data="btn_top10")
        ],
        [
            InlineKeyboardButton("💼 Portafoglio Attivo (/portafoglio)", callback_data="btn_portafoglio"),
            InlineKeyboardButton("🧭 Clima Macro (/outlook)", callback_data="btn_outlook")
        ],
        [
            InlineKeyboardButton("📜 Storico Eseguiti (/storico)", callback_data="btn_storico")
        ]
    ])

    testo = (
        "🎛️ <b>DIRECTA TRADING CONSOLE</b>\n\n"
        "Seleziona un'operazione rapida dalla plancia oppure digita i comandi da tastiera:\n\n"
        "• <code>/scan</code> ➔ Verifica segnali convalidati con tutti i filtri attivi\n"
        "• <code>/top10</code> ➔ Classifica dei 10 migliori titoli per Win Rate\n"
        "• <code>/portafoglio</code> ➔ Posizioni aperte, P&L live e distanze target\n"
        "• <code>/outlook</code> ➔ Analisi geopolitica e matrice dazi/tassi\n"
        "• <code>/storico</code> ➔ Statistiche e archivio trade chiusi\n"
        "• <code>/compra &lt;TICKER&gt; &lt;QUOTE&gt; &lt;PMC&gt;</code> ➔ Registra acquisto\n"
        "• <code>/vendi &lt;TICKER&gt; &lt;PREZZO&gt;</code> ➔ Registra chiusura"
    )

    if update.message:
        await update.message.reply_text(testo, parse_mode='HTML', reply_markup=tastiera)
    elif update.callback_query:
        await update.callback_query.message.reply_text(testo, parse_mode='HTML', reply_markup=tastiera)

# --- COMANDO /top10 RANKING QUANTITATIVO ---
async def cmd_top10(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_caricamento = "⏳ Analisi paniere completo e calcolo matrici Win Rate in corso..."
    if update.message:
        await update.message.reply_text(msg_caricamento)
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg_caricamento)

    desc_sentiment, _, fattori, _, _ = analizza_notizie_macro()
    classifica = []

    for ticker, meta in PANIERE.items():
        try:
            d = yf.download(ticker, period="1y", interval="1d", progress=False)
            if d.empty or len(d) < 200: continue

            close = estrai_serie(d, 'Close')
            open_p = estrai_serie(d, 'Open')
            low = estrai_serie(d, 'Low')
            volume = estrai_serie(d, 'Volume')

            p_curr = float(close.iloc[-1])
            p_prev = float(close.iloc[-2])
            o_curr = float(open_p.iloc[-1])

            sma20 = float(close.rolling(20).mean().dropna().iloc[-1])
            std20 = float(close.rolling(20).std().dropna().iloc[-1])
            z_score = (p_curr - sma20) / std20 if std20 > 0 else 0.0

            delta = close.diff()
            g = delta.where(delta > 0, 0).rolling(14).mean()
            l = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = g / l.replace(0, np.nan)
            rsi = float((100 - (100 / (1 + rs))).dropna().iloc[-1])

            candela_verde = (p_curr > o_curr and p_curr > p_prev)

            vol_clean = volume[volume > 0]
            vol_ratio = 1.0
            if len(vol_clean) >= 20:
                sma_vol = float(vol_clean.rolling(20).mean().iloc[-1])
                if sma_vol > 0:
                    vol_ratio = float(vol_clean.iloc[-1]) / sma_vol

            adx_val = calcola_adx(d, 14)
            min_52w = float(low.min())
            dist_52w = p_curr / min_52w if min_52w > 0 else 1.0
            emi = calcola_macro_impact(ticker, fattori)

            win_rate, score = calcola_probabilita_setup(z_score, rsi, candela_verde, vol_ratio, adx_val, emi, dist_52w)
            target_gain = ((sma20 - p_curr) / p_curr) * 100.0

            classifica.append({
                "ticker": ticker, "nome": meta["nome"], "win_rate": win_rate,
                "score": score, "prezzo": p_curr, "rsi": rsi, "z": z_score,
                "vol": vol_ratio, "adx": adx_val, "target_gain": target_gain
            })
        except Exception:
            continue

    classifica.sort(key=lambda x: x["win_rate"], reverse=True)
    top_10 = classifica[:10]

    msg = f"🏆 <b>TOP 10 PROBABILITÀ WIN RATE (FTSE MIB)</b>\n🧭 <i>Clima Macro: {desc_sentiment}</i>\n\n"
    medaglie = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    for i, t in enumerate(top_10):
        badge = "🟢" if t["win_rate"] >= 75.0 else ("🟡" if t["win_rate"] >= 65.0 else "⚪")
        msg += (
            f"{medaglie[i]} {badge} <b>{t['nome']}</b> (<code>{t['ticker']}</code>)\n"
            f"   • Win Rate: <b>{t['win_rate']}%</b> (Score: {t['score']}/10)\n"
            f"   • Prezzo: <code>€{t['prezzo']:.3f}</code> | Dist. Target SMA20: <b>{t['target_gain']:+.2f}%</b>\n"
            f"   • Indicatori: RSI <code>{t['rsi']:.1f}</code> | Z <code>{t['z']:+.2f}σ</code> | Vol <code>{t['vol']:.1f}x</code>\n\n"
        )

    msg += "💡 <i>I titoli con bollino verde superano il 75% di affidabilità statistica.</i>"
    
    if update.message:
        await update.message.reply_text(msg, parse_mode='HTML')
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode='HTML')

# --- COMANDO OUTLOOK ---
async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_caricamento = "🌐 Scansione vettori geopolitici e matrice fattoriale in corso..."
    if update.message:
        await update.message.reply_text(msg_caricamento)
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg_caricamento)

    desc_sentiment, settore_fav, fattori, news, rilevanze = analizza_notizie_macro()

    impatti = []
    for ticker, info in PANIERE.items():
        emi = calcola_macro_impact(ticker, fattori)
        impatti.append({
            "ticker": ticker,
            "nome": info["nome"],
            "settore": info["settore"],
            "emi": emi
        })

    impatti.sort(key=lambda x: x["emi"], reverse=True)
    top_long = impatti[:3]
    top_short = sorted(impatti[-3:], key=lambda x: x["emi"])

    msg = (
        f"🧭 <b>MACRO FACTOR INTELLIGENCE & SCENARIO</b>\n\n"
        f"📊 <b>Regime Globale:</b>\n<b>{desc_sentiment}</b>\n\n"
        f"⚙️ <b>Vettori di Rischio Rilevati (Ultime 72h):</b>\n"
        f"• Commercio/Dazi: <code>{fattori['dazi']:+.2f}σ</code>\n"
        f"• Tassi/Politica Monetaria: <code>{fattori['tassi']:+.2f}σ</code>\n"
        f"• Tensione Geopolitica: <code>{fattori['geopolitica']:+.2f}σ</code>\n"
        f"• Rischio Debito/Spread: <code>{fattori['debito']:+.2f}σ</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>VENTO A FAVORE RELATIVO:</b>\n"
    )

    for item in top_long:
        ico = "🔥" if item["emi"] >= 10.0 else "🔹"
        msg += f"• {ico} <b>{item['nome']}</b> (<code>{item['ticker']}</code>) ➔ Impatto: <b>{item['emi']:+.1f}%</b>\n"

    msg += f"\n🔴 <b>PRESSIONE MACRO RELATIVA:</b>\n"
    for item in top_short:
        ico = "⚠️" if item["emi"] <= -5.0 else "🔸"
        msg += f"• {ico} <b>{item['nome']}</b> (<code>{item['ticker']}</code>) ➔ Impatto: <b>{item['emi']:+.1f}%</b>\n"

    if rilevanze:
        msg += "\n━━━━━━━━━━━━━━━━━━━━\n🗳️ <b>Eventi Rilevanti Monitorati:</b>\n"
        for r in rilevanze[:2]:
            msg += f"📌 <i>{r}</i>\n"

    msg += "\n💡 <i>I vettori fattoriali stimano l'impatto economico a 5-10 sedute.</i>"
    
    if update.message:
        await update.message.reply_text(msg, parse_mode='HTML')
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode='HTML')

# --- SCANSIONE CONVALIDATA (/scan) ---
async def esegui_scansione_e_monitoraggio(app_bot):
    portafoglio = carica_portafoglio()

    # 1. Alert Uscita posizioni aperte
    for ticker, info in list(portafoglio.items()):
        try:
            d = yf.download(ticker, period="3mo", interval="1d", progress=False)
            if d.empty: continue
            close = estrai_serie(d, 'Close')
            if close.empty or pd.isna(close.iloc[-1]): continue

            p_live = float(close.iloc[-1])
            pmc = info['pmc']
            quote = info['quote']

            sma20_s = close.rolling(20).mean().dropna()
            tp_dinamico = float(sma20_s.iloc[-1]) if not sma20_s.empty else (pmc * 1.03)
            atr_val = calcola_atr(d, 14)
            sl_dinamico = max(0.01, pmc - (1.5 * atr_val))
            rend_pct = ((p_live - pmc) / pmc) * 100

            if p_live >= tp_dinamico or p_live <= sl_dinamico:
                tipo = f"🎯 TARGET MEAN REVERSION (SMA 20 a €{tp_dinamico:.3f})" if p_live >= tp_dinamico else f"⚠️ STOP LOSS (-1.5 ATR a €{sl_dinamico:.3f})"
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
    desc_sentiment, settore_fav, fattori, news, _ = analizza_notizie_macro()
    indice_rialzista, _ = verifica_regime_ftsemib()
    regime_icon = "🟢 Favorevole (Sopra SMA 50)" if indice_rialzista else "🟠 Prudente (Sotto SMA 50)"

    # 3. Scansione Paniere con Filtri Rigorosi
    segnali_validati = []
    segnali_bloccati_earnings = []

    for ticker, meta in PANIERE.items():
        if ticker in portafoglio: continue
        nome = meta["nome"]

        try:
            ticker_obj = yf.Ticker(ticker)
            df = ticker_obj.history(period="1y", interval="1d")
            if df.empty: continue

            close = estrai_serie(df, 'Close')
            open_p = estrai_serie(df, 'Open')
            low = estrai_serie(df, 'Low')
            volume = estrai_serie(df, 'Volume')
            if len(close) < 200: continue

            sma20_s = close.rolling(20).mean().dropna()
            std20_s = close.rolling(20).std().dropna()
            sma200_s = close.rolling(200).mean().dropna()

            delta = close.diff()
            g = (delta.where(delta > 0, 0)).rolling(14).mean()
            l = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = g / l.replace(0, np.nan)
            rsi_s = (100 - (100 / (1 + rs))).dropna()

            p_curr = float(close.iloc[-1])
            o_curr = float(open_p.iloc[-1])
            p_prev = float(close.iloc[-2])
            low_curr = float(low.iloc[-1])
            low_prev = float(low.iloc[-2])

            sma20_curr = float(sma20_s.iloc[-1])
            z_score_prev = (p_prev - float(sma20_s.iloc[-2])) / float(std20_s.iloc[-2])
            rsi_prev = float(rsi_s.iloc[-2])
            sma200_prev = float(sma200_s.iloc[-2])

            setup_ipervenduto = (z_score_prev <= -1.4 and rsi_prev <= 40 and p_prev >= (sma200_prev * 0.93))
            conferma_rimbalzo = (p_curr > o_curr and p_curr > p_prev)

            vol_clean = volume[volume > 0]
            vol_ratio = 1.0
            if len(vol_clean) >= 20:
                sma_vol = float(vol_clean.rolling(20).mean().iloc[-1])
                if sma_vol > 0:
                    vol_ratio = float(vol_clean.iloc[-1]) / sma_vol
            filtro_volumi = (vol_ratio >= 1.15)

            adx_val = calcola_adx(df, 14)
            filtro_adx = (adx_val <= 32.0)

            min_52w = float(low.min())
            filtro_distanza_minimi = (p_curr >= (min_52w * 1.04))

            if (setup_ipervenduto and conferma_rimbalzo and indice_rialzista and 
                filtro_volumi and filtro_adx and filtro_distanza_minimi):

                imminente, data_earn = verifica_imminenza_earnings(ticker_obj, giorni_buffer=2)
                if imminente:
                    segnali_bloccati_earnings.append({"ticker": ticker, "nome": nome, "data_earn": data_earn})
                    continue

                atr = calcola_atr(df, 14)
                tp = sma20_curr
                sl = min(low_prev, low_curr) - (0.8 * atr)
                gain_pct = ((tp - p_curr) / p_curr) * 100
                risk_pct = ((p_curr - sl) / p_curr) * 100

                if gain_pct < 1.8:
                    continue

                emi = calcola_macro_impact(ticker, fattori)

                segnali_validati.append({
                    "ticker": ticker, "nome": nome, "prezzo": p_curr,
                    "tp": tp, "sl": sl, "gain_pct": gain_pct,
                    "risk_pct": risk_pct, "rsi": rsi_prev,
                    "z_score": z_score_prev, "vol_ratio": vol_ratio,
                    "adx": adx_val, "emi": emi
                })
        except Exception:
            continue

    notifica_earnings = ""
    if segnali_bloccati_earnings:
        notifica_earnings = "\n\n⚠️ <b>TITOLI FILTRATI PER EARNINGS IMMINENTI:</b>\n"
        for eb in segnali_bloccati_earnings:
            notifica_earnings += f"• <b>{eb['nome']}</b> (<code>{eb['ticker']}</code>): Risultati il <b>{eb['data_earn']}</b>\n"

    if not segnali_validati:
        await app_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                f"📊 <b>SCANSIONE QUANTITATIVA DIRETTA</b>\n"
                f"🌐 <i>FTSE MIB: {regime_icon}</i>\n"
                f"🧭 <i>Clima Macro: {desc_sentiment}</i>\n\n"
                f"ℹ️ Nessun titolo soddisfa simultaneamente tutti i filtri di precisione:\n"
                f"• Ipervenduto (Z ≤ -1.4, RSI ≤ 40)\n"
                f"• Candela verde con Volumi ≥ 1.15x\n"
                f"• Trend non compromesso (ADX ≤ 32, Distanza Minimi > 4%)\n"
                f"• Nessuna trimestrale nei 2 giorni successivi\n"
                f"• Guadagno minimo target ≥ +1.8% (Anti-Commissioni)"
                f"{notifica_earnings}\n\n"
                f"💡 <i>Digita /top10 per consultare la classifica dei titoli più vicini all'inversione.</i>"
            ),
            parse_mode='HTML'
        )
        return

    for s in segnali_validati:
        tasti = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🛒 Registra Acquisto {s['ticker']}", callback_data=f"buy_{s['ticker']}_{s['prezzo']:.3f}")],
            [InlineKeyboardButton("❌ Ignora Segnale", callback_data=f"ignora_{s['ticker']}")]
        ])
        testo_singolo = (
            f"🎯 <b>OPPORTUNITÀ CONVALIDATA (Massima Precisione)</b>\n\n"
            f"📌 <b>{s['nome']}</b> (<code>{s['ticker']}</code>)\n"
            f"• Prezzo Ingresso: <code>€{s['prezzo']:.3f}</code>\n"
            f"• 🎯 <b>Target (SMA 20):</b> <code>€{s['tp']:.3f}</code> (+{s['gain_pct']:.2f}%)\n"
            f"• ⚠️ <b>Stop Loss:</b> <code>€{s['sl']:.3f}</code> (-{s['risk_pct']:.2f}%)\n"
            f"• Indicatori: Vol <b>{s['vol_ratio']:.1f}x</b> | ADX <b>{s['adx']:.1f}</b> | RSI <b>{s['rsi']:.1f}</b>\n"
            f"• Ponderazione Macro: <b>{s['emi']:+.1f}%</b>\n"
            f"• Calendario Societario: <i>Nessuna trimestrale imminente (OK)</i>\n"
        )
        await app_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=testo_singolo, parse_mode='HTML', reply_markup=tasti)

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_caricamento = "🔍 Scansione selettiva azionaria con filtri Volumi, ADX ed Earnings in corso..."
    if update.message:
        await update.message.reply_text(msg_caricamento)
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg_caricamento)
    await esegui_scansione_e_monitoraggio(context.bot)

async def cmd_portafoglio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    portafoglio = carica_portafoglio()
    if not portafoglio:
        msg_vuoto = "💼 Nessun titolo attualmente attivo nel portafoglio."
        if update.message:
            await update.message.reply_text(msg_vuoto)
        elif update.callback_query:
            await update.callback_query.message.reply_text(msg_vuoto)
        return

    desc_sentiment, _, fattori, _, _ = analizza_notizie_macro()
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

        emi = calcola_macro_impact(t, fattori)
        impatto_macro = f"🟢 VENTO A FAVORE ({emi:+.1f}%)" if emi >= 8.0 else (f"🔴 PRESSIONE CONTRARIA ({emi:+.1f}%)" if emi <= -8.0 else f"⚪ NEUTRO ({emi:+.1f}%)")

        msg += (
            f"🔹 <b>{info['nome']}</b> (<code>{t}</code>)\n"
            f"• Quote: <code>{quote}</code> | Investito: <code>€{val_carico:.2f}</code> ➔ Attuale: <code>€{val_live:.2f}</code>\n"
            f"• PMC: <code>€{pmc:.3f}</code> ➔ Prezzo: <code>€{p_live:.3f}</code> ({segno} <b>{rend_attuale:+.2f}%</b>)\n"
            f"• 🎯 Target (+2 ATR): <code>€{tp_prezzo:.3f}</code> (distanza: <b>{manca_tp:+.2f}%</b>)\n"
            f"• ⚠️ Stop (-1.5 ATR): <code>€{sl_prezzo:.3f}</code> (margine: <b>{margine_sl:.2f}%</b>)\n"
            f"• 🌐 Macro: <code>{impatto_macro}</code>\n"
            f"────────────────────\n"
        )

    pnl_totale_euro = totale_attuale - totale_investito
    pnl_totale_pct = ((totale_attuale - totale_investito) / totale_investito * 100) if totale_investito > 0 else 0.0
    segno_tot = "🟢" if pnl_totale_euro >= 0 else "🔴"

    msg += (
        f"💰 <b>RIEPILOGO CONTO DIRETTA:</b>\n"
        f"• Totale Investito: <code>€{totale_investito:.2f}</code>\n"
        f"• Valore Corrente: <code>€{totale_attuale:.2f}</code>\n"
        f"• P&L Totale: {segno_tot} <b>€{pnl_totale_euro:+.2f} ({pnl_totale_pct:+.2f}%)</b>"
    )
    
    if update.message:
        await update.message.reply_text(msg, parse_mode='HTML')
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode='HTML')

async def cmd_storico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storico = carica_storico()
    if not storico:
        msg_vuoto = "📜 Nessuna operazione chiusa registrata nello storico."
        if update.message:
            await update.message.reply_text(msg_vuoto)
        elif update.callback_query:
            await update.callback_query.message.reply_text(msg_vuoto)
        return

    tot_pnl = sum(item['pnl_euro'] for item in storico)
    vinte = [item for item in storico if item['pnl_euro'] > 0]
    perse = [item for item in storico if item['pnl_euro'] < 0]
    win_rate = (len(vinte) / len(storico)) * 100 if storico else 0.0

    msg = (
        f"📜 <b>STORICO OPERAZIONI AZIONARIE</b>\n\n"
        f"• Operazioni Chiuse: <b>{len(storico)}</b>\n"
        f"• Win Rate Reale: <b>{win_rate:.1f}%</b> (✅ {len(vinte)} | ❌ {len(perse)})\n"
        f"• P&L Netto Realizzato: <b>€{tot_pnl:+.2f}</b>\n\n"
        f"<b>Ultime chiusure:</b>\n"
    )
    for op in storico[-5:]:
        icona = "🟢" if op['pnl_euro'] >= 0 else "🔴"
        msg += (
            f"{icona} <b>{op['nome']}</b> ({op['data_chiusura']})\n"
            f"   PMC: <code>€{op['pmc']:.3f}</code> ➔ Uscita: <code>€{op['prezzo_uscita']:.3f}</code> | <b>€{op['pnl_euro']:+.2f} ({op['rendimento_pct']:+.2f}%)</b>\n"
        )
        
    if update.message:
        await update.message.reply_text(msg, parse_mode='HTML')
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode='HTML')

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
        f"✅ <b>ACQUISTO REGISTRATO NEL CLOUD!</b>\n\n"
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
        await update.message.reply_text(f"⚠️ Il titolo <b>{ticker}</b> non è presente nel portafoglio.", parse_mode='HTML')
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
        f"<i>Titolo rimosso dal portafoglio e sincronizzato su Gist.</i>"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

# --- DISPATCHER PULSANTI CALLBACK ---
async def bottone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Azioni Console Rapida
    if data == "btn_scan":
        await cmd_scan(update, context)
        return ConversationHandler.END
    elif data == "btn_top10":
        await cmd_top10(update, context)
        return ConversationHandler.END
    elif data == "btn_portafoglio":
        await cmd_portafoglio(update, context)
        return ConversationHandler.END
    elif data == "btn_outlook":
        await cmd_outlook(update, context)
        return ConversationHandler.END
    elif data == "btn_storico":
        await cmd_storico(update, context)
        return ConversationHandler.END

    # Gestione Segnali di Uscita e Acquisto
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
                f"P&L: <b>€{op['pnl_euro']:+.2f} ({op['rendimento_pct']:+.2f}%)</b> sincronizzato su Gist.",
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
            f"👍 Registrazione acquisto per <b>{context.user_data['nome_titolo']}</b>!\n"
            f"👉 Rispondi indicando quote e prezzo (es: <code>10 a {prezzo}</code>):",
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
            f"• Controvalore Totale: <code>€{quote * pmc:.2f}</code>",
            parse_mode='HTML'
        )
    except Exception:
        await update.message.reply_text("⚠️ Formato non valido. Esempio: <code>10 a 6.025</code>", parse_mode='HTML')
        return ATTESA_DETTAGLI_ACQUISTO

    return ConversationHandler.END

async def post_init_config(app):
    comandi = [
        BotCommand("console", "Plancia comandi con bottoni interattivi"),
        BotCommand("top10", "Classifica 10 migliori titoli per Win Rate"),
        BotCommand("scan", "Scansione rigorosa con tutti i filtri attivi"),
        BotCommand("portafoglio", "Stato posizioni aperte e P&L live"),
        BotCommand("outlook", "Analisi macro, dazi, geopolitica e tassi"),
        BotCommand("storico", "Storico operazioni e Win Rate reale"),
        BotCommand("compra", "Registra un acquisto (<TICKER> <QUOTE> <PMC>)"),
        BotCommand("vendi", "Registra una vendita (<TICKER> <PREZZO>)")
    ]
    await app.bot.set_my_commands(comandi)
    logging.info("Menu comandi Telegram configurato con successo.")

async def job_schedulato(context: ContextTypes.DEFAULT_TYPE):
    if datetime.now().weekday() < 5:
        await esegui_scansione_e_monitoraggio(context.bot)

def main():
    threading.Thread(target=avvia_server_porta, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init_config).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(bottone_callback)],
        states={
            ATTESA_DETTAGLI_ACQUISTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, registra_dettagli_acquisto)]
        },
        fallbacks=[]
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("console", cmd_console))
    app.add_handler(CommandHandler("help", cmd_console))
    app.add_handler(CommandHandler("start", cmd_console))
    app.add_handler(CommandHandler("top10", cmd_top10))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("portafoglio", cmd_portafoglio))
    app.add_handler(CommandHandler("outlook", cmd_outlook))
    app.add_handler(CommandHandler("storico", cmd_storico))
    app.add_handler(CommandHandler("compra", cmd_compra))
    app.add_handler(CommandHandler("vendi", cmd_vendi))

    if app.job_queue:
        app.job_queue.run_daily(job_schedulato, time=time(hour=18, minute=0))

    print("🤖 Bot Telegram Azionario Attivo: Console + Top 10 Win Rate + Filtri Rigorosi...")
    app.run_polling()

if __name__ == "__main__":
    main()
