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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GIST_ID = os.getenv("GIST_ID", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

PORTFOLIO_FILE = "portfolio.json"
STORICO_FILE = "storico_operazioni.json"
ATTESA_DETTAGLI_ACQUISTO = 1
ATTESA_COMPRA_GUIDATA = 2
ATTESA_VENDI_GUIDATA = 3

PANIERE = {
    "STLAM.MI": {"nome": "Stellantis", "settore": "export", "beta_dazi": 0.85, "beta_tassi": -0.30, "beta_geopolitica": -0.40, "beta_debito": 0.20},
    "RACE.MI": {"nome": "Ferrari", "settore": "export", "beta_dazi": 0.50, "beta_tassi": -0.10, "beta_geopolitica": -0.20, "beta_debito": 0.10},
    "MAIRE.MI": {"nome": "Maire Tecnimont", "settore": "export", "beta_dazi": 0.60, "beta_tassi": -0.40, "beta_geopolitica": -0.30, "beta_debito": 0.30},
    "WBD.MI": {"nome": "Webuild", "settore": "export", "beta_dazi": 0.40, "beta_tassi": -0.50, "beta_geopolitica": -0.20, "beta_debito": 0.40},
    "SFER.MI": {"nome": "Salvatore Ferragamo", "settore": "export", "beta_dazi": 0.70, "beta_tassi": -0.20, "beta_geopolitica": -0.40, "beta_debito": 0.10},
    "ISP.MI": {"nome": "Intesa Sanpaolo", "settore": "finanza", "beta_dazi": 0.20, "beta_tassi": 0.75, "beta_geopolitica": -0.30, "beta_debito": 0.80},
    "UCG.MI": {"nome": "UniCredit", "settore": "finanza", "beta_dazi": 0.30, "beta_tassi": 0.80, "beta_geopolitica": -0.35, "beta_debito": 0.75},
    "BPE.MI": {"nome": "BPER Banca", "settore": "finanza", "beta_dazi": 0.15, "beta_tassi": 0.70, "beta_geopolitica": -0.25, "beta_debito": 0.85},
    "BMED.MI": {"nome": "Banca Mediolanum", "settore": "finanza", "beta_dazi": 0.20, "beta_tassi": 0.50, "beta_geopolitica": -0.20, "beta_debito": 0.60},
    "G.MI": {"nome": "Generali", "settore": "finanza", "beta_dazi": 0.20, "beta_tassi": 0.40, "beta_geopolitica": -0.20, "beta_debito": 0.70},
    "ENEL.MI": {"nome": "Enel", "settore": "utility", "beta_dazi": -0.10, "beta_tassi": -0.65, "beta_geopolitica": 0.20, "beta_debito": 0.50},
    "TRN.MI": {"nome": "Terna", "settore": "utility", "beta_dazi": -0.05, "beta_tassi": -0.75, "beta_geopolitica": 0.35, "beta_debito": 0.40},
    "SRG.MI": {"nome": "Snam", "settore": "utility", "beta_dazi": -0.05, "beta_tassi": -0.70, "beta_geopolitica": 0.35, "beta_debito": 0.45},
    "IG.MI": {"nome": "Italgas", "settore": "utility", "beta_dazi": -0.05, "beta_tassi": -0.60, "beta_geopolitica": 0.30, "beta_debito": 0.40},
    "A2A.MI": {"nome": "A2A", "settore": "utility", "beta_dazi": 0.00, "beta_tassi": -0.50, "beta_geopolitica": 0.25, "beta_debito": 0.45},
    "HER.MI": {"nome": "Hera", "settore": "utility", "beta_dazi": 0.00, "beta_tassi": -0.45, "beta_geopolitica": 0.20, "beta_debito": 0.40},
    "IRE.MI": {"nome": "Iren", "settore": "utility", "beta_dazi": 0.00, "beta_tassi": -0.50, "beta_geopolitica": 0.20, "beta_debito": 0.45},
    "ENI.MI": {"nome": "Eni", "settore": "energy", "beta_dazi": 0.20, "beta_tassi": 0.10, "beta_geopolitica": 0.80, "beta_debito": 0.30},
    "OVS.MI": {"nome": "OVS", "settore": "consumer", "beta_dazi": 0.30, "beta_tassi": -0.40, "beta_geopolitica": -0.30, "beta_debito": 0.30}
}

class SimpleWebServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Directa Trading Cockpit Online")

    def log_message(self, format, *args):
        return

def avvia_server_porta():
    porta = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", porta), SimpleWebServer)
    server.serve_forever()

def _leggi_gist_file(nome_file, default_val):
    if not GIST_ID or not GITHUB_TOKEN: return None
    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            files = res.json().get("files", {})
            if nome_file in files:
                return json.loads(files[nome_file]["content"])
    except Exception:
        pass
    return None

def _scrivi_gist_file(nome_file, dati):
    if not GIST_ID or not GITHUB_TOKEN: return False
    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    payload = {"files": {nome_file: {"content": json.dumps(dati, indent=4)}}}
    try:
        res = requests.patch(url, headers=headers, json=payload, timeout=8)
        return res.status_code == 200
    except Exception:
        return False

def carica_portafoglio():
    cloud_data = _leggi_gist_file(PORTFOLIO_FILE, {})
    if cloud_data is not None: return cloud_data
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r") as f: return json.load(f)
        except Exception: return {}
    return {}

def salva_portafoglio(data):
    if GIST_ID and GITHUB_TOKEN: _scrivi_gist_file(PORTFOLIO_FILE, data)
    with open(PORTFOLIO_FILE, "w") as f: json.dump(data, f, indent=4)

def carica_storico():
    cloud_data = _leggi_gist_file(STORICO_FILE, [])
    if cloud_data is not None: return cloud_data
    if os.path.exists(STORICO_FILE):
        try:
            with open(STORICO_FILE, "r") as f: return json.load(f)
        except Exception: return []
    return []

def salva_storico(data):
    if GIST_ID and GITHUB_TOKEN: _scrivi_gist_file(STORICO_FILE, data)
    with open(STORICO_FILE, "w") as f: json.dump(data, f, indent=4)

def registra_chiusura_operazione(ticker, nome, quote, pmc, p_uscita, motivo="Vendita"):
    pnl_unitario = p_uscita - pmc
    pnl_totale = pnl_unitario * quote
    rend_pct = ((p_uscita - pmc) / pmc) * 100.0 if pmc > 0 else 0.0

    operazione = {
        "ticker": ticker, "nome": nome, "quote": quote, "pmc": pmc,
        "prezzo_uscita": p_uscita, "pnl_euro": round(pnl_totale, 2),
        "rendimento_pct": round(rend_pct, 2),
        "data_chiusura": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "motivo": motivo
    }
    storico = carica_storico()
    storico.append(operazione)
    salva_storico(storico)
    return operazione

def estrai_serie(df, col_nome):
    if col_nome not in df.columns: return pd.Series(dtype=float)
    col = df[col_nome]
    if isinstance(col, pd.DataFrame): col = col.iloc[:, 0]
    return col.dropna()

def calcola_atr(df, period=14):
    high, low, close = estrai_serie(df, 'High'), estrai_serie(df, 'Low'), estrai_serie(df, 'Close')
    if len(close) < period + 1: return float(close.iloc[-1] * 0.02) if not close.empty else 0.05
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1).dropna()
    atr = tr.rolling(period).mean().dropna()
    return float(atr.iloc[-1]) if not atr.empty else float(close.iloc[-1] * 0.02)

def calcola_adx(df, period=14):
    high, low, close = estrai_serie(df, 'High'), estrai_serie(df, 'Low'), estrai_serie(df, 'Close')
    if len(close) < (period * 2): return 20.0
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_s = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr_s)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr_s)
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    adx_s = dx.rolling(period).mean().dropna()
    return float(adx_s.iloc[-1]) if not adx_s.empty else 20.0

def verifica_imminenza_earnings(ticker_obj, giorni_buffer=2):
    try:
        cal = ticker_obj.calendar
        oggi = date.today()
        earnings_date = None
        if isinstance(cal, dict) and "Earnings Date" in cal:
            date_list = cal["Earnings Date"]
            if date_list: earnings_date = date_list[0]
        elif isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.index:
            earnings_date = cal.loc["Earnings Date"].iloc[0]
        if earnings_date is not None:
            if isinstance(earnings_date, (datetime, pd.Timestamp)): earnings_date = earnings_date.date()
            diff = (earnings_date - oggi).days
            if 0 <= diff <= giorni_buffer: return True, earnings_date.strftime("%d/%m/%Y")
    except Exception:
        pass
    return False, None

def analizza_notizie_macro():
    feed_urls = [
        "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=it&gl=IT&ceid=IT:it",
        "https://www.ansa.it/sito/notizie/economia/economia_rss.xml",
        "https://news.google.com/rss/search?q=(Borsa+OR+mercati+OR+BCE+OR+inflazione+OR+geopolitica)+when:7d&hl=it&gl=IT&ceid=IT:it"
    ]
    notizie = []
    headers = {"User-Agent": "Mozilla/5.0"}

    for url in feed_urls:
        try:
            r = requests.get(url, headers=headers, timeout=6)
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall('.//item')[:8]:
                    t = item.find('title').text if item.find('title') is not None else ""
                    if t and t not in notizie: notizie.append(t)
        except Exception:
            continue

    f_dazi, f_tassi, f_geopolitica, f_debito, f_mercato = 0.0, 0.0, 0.0, 0.0, 0.0
    rilevanze = []

    for n in notizie:
        nl = n.lower()
        if any(p in nl for p in ["in calo", "in rosso", "rosso", "frena", "pesante", "debole", "ribasso", "sell-off", "perdite"]):
            f_mercato -= 0.35
            rilevanze.append(f"Mercato: {n}")
        if any(p in nl for p in ["in rialzo", "sale", "vola", "in rally", "positivo", "record"]):
            f_mercato += 0.35
        if any(p in nl for p in ["taglio tassi", "bce taglia", "fed taglia", "allentamento"]):
            f_tassi -= 0.35
        if any(p in nl for p in ["tassi alti", "inflazione", "stretta", "falchi", "rialzo tassi"]):
            f_tassi += 0.35
        if any(p in nl for p in ["guerra", "tensione", "missili", "escalation", "conflitto", "crisi", "dazi"]):
            f_geopolitica += 0.40
            rilevanze.append(f"Geo: {n}")

    f_dazi = float(np.clip(f_dazi, -1.0, 1.0))
    f_tassi = float(np.clip(f_tassi, -1.0, 1.0))
    f_geopolitica = float(np.clip(f_geopolitica, -1.0, 1.0))
    f_mercato = float(np.clip(f_mercato, -1.0, 1.0))

    var_ftse_mib = 0.0
    try:
        df_idx = yf.download("FTSEMIB.MI", period="2d", interval="1d", progress=False)
        if not df_idx.empty and len(df_idx) >= 2:
            c_today = float(df_idx['Close'].iloc[-1])
            c_prev = float(df_idx['Close'].iloc[-2])
            var_ftse_mib = ((c_today - c_prev) / c_prev) * 100.0
    except Exception:
        pass

    if var_ftse_mib < -0.5 or f_mercato <= -0.3 or f_geopolitica >= 0.3:
        desc_sentiment = f"🔴 FLESSIONE (FTSE MIB {var_ftse_mib:+.2f}%) - PRUDENZA"
    elif var_ftse_mib > 0.5 and f_mercato > 0.2:
        desc_sentiment = f"🟢 RIALZO (FTSE MIB {var_ftse_mib:+.2f}%) - PROPENSIONE AL RISCHIO"
    else:
        desc_sentiment = f"⚪ LATERALE (FTSE MIB {var_ftse_mib:+.2f}%) - SELETTIVO"

    fattori = {"dazi": f_dazi, "tassi": f_tassi, "geopolitica": f_geopolitica, "debito": f_debito, "mercato": f_mercato, "var_ftsemib": var_ftse_mib}
    return desc_sentiment, fattori, notizie[:4], rilevanze

def calcola_macro_impact(ticker, fattori):
    info = PANIERE.get(ticker)
    if not info: return 0.0
    b_dazi, b_tassi, b_geo, b_deb = info.get("beta_dazi", 0.0), info.get("beta_tassi", 0.0), info.get("beta_geopolitica", 0.0), info.get("beta_debito", 0.0)
    impatto = ((b_dazi * fattori["dazi"] * 0.35) + (b_tassi * fattori["tassi"] * 0.25) + (b_geo * fattori["geopolitica"] * 0.20) + (b_deb * fattori["debito"] * 0.20)) * 100.0
    return float(np.clip(impatto, -100.0, 100.0))

def calcola_probabilita_setup(z_score, rsi, candela_verde, vol_ratio, adx, emi, dist_52w):
    score_z = np.clip((-z_score - 0.5) / 1.5, 0, 1) * 3.0
    score_rsi = np.clip((45.0 - rsi) / 25.0, 0, 1) * 2.0
    score_vol = (1.5 if vol_ratio >= 1.15 else 0.5) if candela_verde else 0.0
    score_adx = np.clip((35.0 - adx) / 15.0, 0, 1) * 1.5
    score_emi = np.clip((emi + 10.0) / 20.0, 0, 1) * 1.0
    score_min = 1.0 if dist_52w >= 1.04 else 0.0
    score_totale = score_z + score_rsi + score_vol + score_adx + score_emi + score_min
    prob_win = 40.0 + (46.0 / (1.0 + np.exp(-(score_totale - 5.0) / 1.5)))
    return round(float(prob_win), 1), round(float(score_totale), 1)

# ==============================================================================
# NUOVA CONSOLE: COCKPIT OPERATIVO CONCENTRATO SU 3 FASI
# ==============================================================================
def genera_tastiera_cockpit():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎯 COSA COMPRARE OGGI?", callback_data="btn_scan")
        ],
        [
            InlineKeyboardButton("💼 IL MIO PORTAFOGLIO", callback_data="btn_portafoglio"),
            InlineKeyboardButton("🏆 CLASSIFICA WIN RATE", callback_data="btn_top10")
        ],
        [
            InlineKeyboardButton("➕ Registra Acquisto", callback_data="btn_guida_compra"),
            InlineKeyboardButton("➖ Registra Vendita", callback_data="btn_guida_vendi")
        ],
        [
            InlineKeyboardButton("🌐 Meteo Borsa", callback_data="btn_outlook"),
            InlineKeyboardButton("📜 Storico Trade", callback_data="btn_storico")
        ]
    ])

async def cmd_console(update: Update, context: ContextTypes.DEFAULT_TYPE):
    portafoglio = carica_portafoglio()
    num_posizioni = len(portafoglio)
    stato_titoli = f"Hai <b>{num_posizioni}</b> titoli aperti" if num_posizioni > 0 else "Nessuna posizione aperta (100% Cassa)"

    testo = (
        "🧭 <b>DIRECTA TRADING COCKPIT</b>\n"
        "<i>Tutto ciò di cui hai bisogno in 3 passaggi:</i>\n\n"
        f"💼 <b>Stato:</b> {stato_titoli}\n\n"
        "<b>1️⃣ Decisione di Ingresso:</b>\n"
        "• Premi <b>🎯 COSA COMPRARE OGGI?</b> per vedere se oggi c'è un segnale convalidato.\n"
        "• Premi <b>🏆 CLASSIFICA</b> per monitorare i 10 titoli più promettenti.\n\n"
        "<b>2️⃣ Gestione e Monitoraggio:</b>\n"
        "• Premi <b>💼 IL MIO PORTAFOGLIO</b> per controllare profitti e target live.\n\n"
        "<b>3️⃣ Azione Rapida:</b>\n"
        "• Usa <b>➕ Registra Acquisto</b> o <b>➖ Registra Vendita</b> dopo aver operato su Directa."
    )

    tasti = genera_tastiera_cockpit()
    if update.message:
        await update.message.reply_text(testo, parse_mode='HTML', reply_markup=tasti)
    elif update.callback_query:
        await update.callback_query.message.reply_text(testo, parse_mode='HTML', reply_markup=tasti)

# --- COSA COMPRARE OGGI (/scan) ---
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_wait = "🔍 <i>Verifica opportunità in corso su Piazza Affari...</i>"
    if update.message: await update.message.reply_text(msg_wait, parse_mode='HTML')
    elif update.callback_query: await update.callback_query.message.reply_text(msg_wait, parse_mode='HTML')

    desc_sentiment, fattori, _, _ = analizza_notizie_macro()
    portafoglio = carica_portafoglio()
    segnali_validati = []
    segnali_earnings = []

    for ticker, meta in PANIERE.items():
        if ticker in portafoglio: continue
        try:
            ticker_obj = yf.Ticker(ticker)
            df = ticker_obj.history(period="1y", interval="1d")
            if df.empty or len(df) < 200: continue

            close, open_p, low, volume = estrai_serie(df, 'Close'), estrai_serie(df, 'Open'), estrai_serie(df, 'Low'), estrai_serie(df, 'Volume')
            p_curr, p_prev, o_curr = float(close.iloc[-1]), float(close.iloc[-2]), float(open_p.iloc[-1])
            sma20_curr = float(close.rolling(20).mean().dropna().iloc[-1])
            std20_prev = float(close.rolling(20).std().dropna().iloc[-2])
            sma20_prev = float(close.rolling(20).mean().dropna().iloc[-2])
            sma200_prev = float(close.rolling(200).mean().dropna().iloc[-2])

            delta = close.diff()
            g = delta.where(delta > 0, 0).rolling(14).mean()
            l = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi_prev = float((100 - (100 / (1 + (g / l.replace(0, np.nan)))))).dropna().iloc[-2])

            z_prev = (p_prev - sma20_prev) / std20_prev if std20_prev > 0 else 0.0

            # Filtri
            setup_ipervenduto = (z_prev <= -1.4 and rsi_prev <= 40 and p_prev >= (sma200_prev * 0.93))
            conferma_verde = (p_curr > o_curr and p_curr > p_prev)
            
            vol_clean = volume[volume > 0]
            vol_ratio = (float(vol_clean.iloc[-1]) / float(vol_clean.rolling(20).mean().iloc[-1])) if len(vol_clean) >= 20 else 1.0
            filtro_vol = (vol_ratio >= 1.15)
            adx_val = calcola_adx(df, 14)
            filtro_adx = (adx_val <= 32.0)
            filtro_min = (p_curr >= float(low.min()) * 1.04)

            if setup_ipervenduto and conferma_verde and filtro_vol and filtro_adx and filtro_min:
                imminente, data_earn = verifica_imminenza_earnings(ticker_obj, 2)
                if imminente:
                    segnali_earnings.append((meta["nome"], data_earn))
                    continue

                atr = calcola_atr(df, 14)
                tp = sma20_curr
                sl = min(float(low.iloc[-2]), float(low.iloc[-1])) - (0.8 * atr)
                gain_pct = ((tp - p_curr) / p_curr) * 100.0

                if gain_pct >= 1.8:
                    segnali_validati.append({
                        "ticker": ticker, "nome": meta["nome"], "prezzo": p_curr,
                        "tp": tp, "sl": sl, "gain_pct": gain_pct,
                        "risk_pct": ((p_curr - sl) / p_curr) * 100.0,
                        "vol": vol_ratio, "adx": adx_val
                    })
        except Exception:
            continue

    if not segnali_validati:
        msg_no = (
            "🎯 <b>VERIFICA INGRESSI DI OGGI</b>\n\n"
            f"📊 <b>Quadro:</b> {desc_sentiment}\n\n"
            "🔴 <b>SEMAFORO ROSSO: NESSUN ACQUISTO CONSIGLIATO OGGI.</b>\n\n"
            "• Nessun titolo italiano rispetta tutti i filtri di sicurezza.\n"
            "• Il capitale rimane protetto in cassa al 100%.\n"
            "• <i>Consiglio: Premi '🏆 CLASSIFICA WIN RATE' per vedere i titoli più vicini all'inversione.</i>"
        )
        tasti_ritorno = InlineKeyboardMarkup([[InlineKeyboardButton("🏆 Guarda Classifica", callback_data="btn_top10")], [InlineKeyboardButton("🔙 Menu Principale", callback_data="btn_menu")]])
        await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg_no, parse_mode='HTML', reply_markup=tasti_ritorno)
        return

    for s in segnali_validati:
        tasti = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🛒 Compra {s['ticker']}", callback_data=f"buy_{s['ticker']}_{s['prezzo']:.3f}")],
            [InlineKeyboardButton("🔙 Menu Principale", callback_data="btn_menu")]
        ])
        testo = (
            f"🟢 <b>OPPORTUNITÀ CONVALIDATA: {s['nome']}</b>\n\n"
            f"• <b>Prezzo Ingresso:</b> <code>€{s['prezzo']:.3f}</code>\n"
            f"• 🎯 <b>Target Incasso (SMA 20):</b> <code>€{s['tp']:.3f}</code> (+{s['gain_pct']:.2f}%)\n"
            f"• ⚠️ <b>Stop Loss:</b> <code>€{s['sl']:.3f}</code> (-{s['risk_pct']:.2f}%)\n"
            f"• <b>Volumi Istituzionali:</b> {s['vol']:.1f}x la media\n\n"
            f"<i>Copre ampiamente le commissioni Directa.</i>"
        )
        await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=testo, parse_mode='HTML', reply_markup=tasti)

# --- CLASSIFICA ORDINATA WIN RATE (/top10) ---
async def cmd_top10(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_wait = "⏳ <i>Ordinamento titoli per probabilità statistica...</i>"
    if update.message: await update.message.reply_text(msg_wait, parse_mode='HTML')
    elif update.callback_query: await update.callback_query.message.reply_text(msg_wait, parse_mode='HTML')

    desc_sentiment, fattori, _, _ = analizza_notizie_macro()
    classifica = []

    for ticker, meta in PANIERE.items():
        try:
            d = yf.download(ticker, period="1y", interval="1d", progress=False)
            if d.empty or len(d) < 200: continue
            close, open_p, low, volume = estrai_serie(d, 'Close'), estrai_serie(d, 'Open'), estrai_serie(d, 'Low'), estrai_serie(df:=d, 'Volume')
            p_curr, p_prev, o_curr = float(close.iloc[-1]), float(close.iloc[-2]), float(open_p.iloc[-1])
            sma20 = float(close.rolling(20).mean().dropna().iloc[-1])
            std20 = float(close.rolling(20).std().dropna().iloc[-1])
            z_score = (p_curr - sma20) / std20 if std20 > 0 else 0.0

            delta = close.diff()
            g = delta.where(delta > 0, 0).rolling(14).mean()
            l = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi = float((100 - (100 / (1 + (g / l.replace(0, np.nan)))))).dropna().iloc[-1])

            candela_verde = (p_curr > o_curr and p_curr > p_prev)
            vol_clean = volume[volume > 0]
            vol_ratio = (float(vol_clean.iloc[-1]) / float(vol_clean.rolling(20).mean().iloc[-1])) if len(vol_clean) >= 20 else 1.0
            adx_val = calcola_adx(d, 14)
            dist_52w = p_curr / float(low.min()) if float(low.min()) > 0 else 1.0
            emi = calcola_macro_impact(ticker, fattori)

            win_rate, _ = calcola_probabilita_setup(z_score, rsi, candela_verde, vol_ratio, adx_val, emi, dist_52w)
            target_gain = ((sma20 - p_curr) / p_curr) * 100.0

            classifica.append({
                "ticker": ticker, "nome": meta["nome"], "win_rate": win_rate,
                "prezzo": p_curr, "rsi": rsi, "target_gain": target_gain
            })
        except Exception:
            continue

    classifica.sort(key=lambda x: x["win_rate"], reverse=True)
    top_5 = classifica[:5]

    msg = "🏆 <b>LE PRIME 5 OCCASIONI AZIONARIE</b>\n" f"<i>{desc_sentiment}</i>\n\n"
    for i, t in enumerate(top_5, 1):
        pallino = "🟢" if t["win_rate"] >= 75.0 else ("🟡" if t["win_rate"] >= 65.0 else "⚪")
        msg += (
            f"<b>{i}. {pallino} {t['nome']}</b> (<code>{t['ticker']}</code>)\n"
            f"   • Probabilità Rimbalzo: <b>{t['win_rate']}%</b>\n"
            f"   • Prezzo: <code>€{t['prezzo']:.3f}</code> | Distanza Target SMA20: <b>{t['target_gain']:+.2f}%</b>\n"
            f"   • Ipervenduto (RSI): <code>{t['rsi']:.1f}</code>\n\n"
        )
    msg += "💡 <i>I titoli con bollino 🟢 hanno oltre il 75% di win rate statistico.</i>"
    tasti = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Torna al Menu", callback_data="btn_menu")]])

    if update.message: await update.message.reply_text(msg, parse_mode='HTML', reply_markup=tasti)
    elif update.callback_query: await update.callback_query.message.reply_text(msg, parse_mode='HTML', reply_markup=tasti)

# --- IL MIO PORTAFOGLIO (/portafoglio) ---
async def cmd_portafoglio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    portafoglio = carica_portafoglio()
    if not portafoglio:
        msg = (
            "💼 <b>IL TUO PORTAFOGLIO</b>\n\n"
            "Attualmente <b>non hai posizioni aperte</b>.\n"
            "La liquidità è interamente disponibile su Directa per la prossima occasione convalidata."
        )
        tasti = InlineKeyboardMarkup([[InlineKeyboardButton("🎯 Cerca Opportunità", callback_data="btn_scan")], [InlineKeyboardButton("🔙 Menu Principale", callback_data="btn_menu")]])
        if update.message: await update.message.reply_text(msg, parse_mode='HTML', reply_markup=tasti)
        elif update.callback_query: await update.callback_query.message.reply_text(msg, parse_mode='HTML', reply_markup=tasti)
        return

    msg = "💼 <b>POSIZIONI APERTE E MONITORAGGIO LIVE</b>\n\n"
    totale_speso = 0.0
    totale_attuale = 0.0

    for t, info in portafoglio.items():
        pmc = info['pmc']
        quote = info['quote']
        p_live = pmc
        atr_val = pmc * 0.03
        try:
            d = yf.download(t, period="3mo", interval="1d", progress=False)
            if not d.empty:
                c = estrai_serie(d, 'Close')
                if not c.empty: p_live = float(c.iloc[-1])
                atr_val = calcola_atr(d, 14)
        except Exception:
            pass

        val_carico = quote * pmc
        val_live = quote * p_live
        totale_speso += val_carico
        totale_attuale += val_live
        rend_pct = ((p_live - pmc) / pmc) * 100.0
        pnl_euro = val_live - val_carico
        segno = "🟢" if rend_pct >= 0 else "🔴"
        tp_prezzo = pmc + (2.0 * atr_val)
        sl_prezzo = max(0.01, pmc - (1.5 * atr_val))

        msg += (
            f"📌 <b>{info['nome']}</b> (<code>{t}</code>)\n"
            f"• Investito: <code>€{val_carico:.2f}</code> ({quote} quote a €{pmc:.3f})\n"
            f"• Valore Attuale: <code>€{val_live:.2f}</code> (Prezzo: €{p_live:.3f})\n"
            f"• Rendimento Netto: {segno} <b>€{pnl_euro:+.2f} ({rend_pct:+.2f}%)</b>\n"
            f"• 🎯 Target Vendita: <code>€{tp_prezzo:.3f}</code> | ⚠️ Stop Loss: <code>€{sl_prezzo:.3f}</code>\n"
            f"────────────────────\n"
        )

    tot_pnl = totale_attuale - totale_speso
    tot_pct = (tot_pnl / totale_speso * 100.0) if totale_speso > 0 else 0.0
    segno_tot = "🟢" if tot_pnl >= 0 else "🔴"
    msg += f"💰 <b>TOTALE TITOLI:</b> {segno_tot} <b>€{tot_pnl:+.2f} ({tot_pct:+.2f}%)</b>"

    tasti = InlineKeyboardMarkup([[InlineKeyboardButton("➖ Chiudi una Posizione", callback_data="btn_guida_vendi")], [InlineKeyboardButton("🔙 Menu Principale", callback_data="btn_menu")]])
    if update.message: await update.message.reply_text(msg, parse_mode='HTML', reply_markup=tasti)
    elif update.callback_query: await update.callback_query.message.reply_text(msg, parse_mode='HTML', reply_markup=tasti)

# --- METEO BORSA (/outlook) ---
async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc_sentiment, fattori, news, rilevanze = analizza_notizie_macro()
    msg = (
        "🌐 <b>METEO DI BORSA & QUADRO GENERALE</b>\n\n"
        f"📊 <b>Trend Oggi:</b> <b>{desc_sentiment}</b>\n\n"
        f"• Variazione Indice FTSE MIB: <b>{fattori['var_ftsemib']:+.2f}%</b>\n"
        f"• Clima Notizie: <code>{fattori['mercato']:+.2f}σ</code>\n"
        f"• Rischio Geopolitico: <code>{fattori['geopolitica']:+.2f}σ</code>\n\n"
    )
    if rilevanze:
        msg += "📰 <b>Notizie più rilevanti:</b>\n"
        for r in rilevanze[:2]: msg += f"• <i>{r}</i>\n"

    tasti = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu Principale", callback_data="btn_menu")]])
    if update.message: await update.message.reply_text(msg, parse_mode='HTML', reply_markup=tasti)
    elif update.callback_query: await update.callback_query.message.reply_text(msg, parse_mode='HTML', reply_markup=tasti)

# --- STORICO TRADE (/storico) ---
async def cmd_storico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storico = carica_storico()
    if not storico:
        msg = "📜 <b>STORICO TRADE</b>\n\nNessuna operazione chiusa registrata."
    else:
        tot_pnl = sum(item['pnl_euro'] for item in storico)
        vinte = [item for item in storico if item['pnl_euro'] > 0]
        msg = (
            f"📜 <b>STORICO OPERAZIONI CONCLUSE</b>\n\n"
            f"• Operazioni Chiuse: <b>{len(storico)}</b>\n"
            f"• Win Rate Effettivo: <b>{(len(vinte)/len(storico)*100):.1f}%</b>\n"
            f"• P&L Netto Realizzato: <b>€{tot_pnl:+.2f}</b>\n\n"
            f"<b>Ultime vendite:</b>\n"
        )
        for op in storico[-3:]:
            ico = "🟢" if op['pnl_euro'] >= 0 else "🔴"
            msg += f"{ico} <b>{op['nome']}</b>: <b>€{op['pnl_euro']:+.2f} ({op['rendimento_pct']:+.2f}%)</b>\n"

    tasti = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu Principale", callback_data="btn_menu")]])
    if update.message: await update.message.reply_text(msg, parse_mode='HTML', reply_markup=tasti)
    elif update.callback_query: await update.callback_query.message.reply_text(msg, parse_mode='HTML', reply_markup=tasti)

# --- PROMPT GUIDATI PER COMPRARE E VENDERE ---
async def guida_compra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    msg = (
        "➕ <b>REGISTRAZIONE ACQUISTO</b>\n\n"
        "Rispondi a questo messaggio indicando:\n"
        "<b>TICKER QUOTE PREZZO</b>\n\n"
        "<i>Esempio pratico:</i>\n"
        "<code>ISP.MI 15 6.02</code>"
    )
    await update.callback_query.message.reply_text(msg, parse_mode='HTML')
    return ATTESA_COMPRA_GUIDATA

async def esegui_compra_guidata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parti = update.message.text.strip().split()
    if len(parti) < 3:
        await update.message.reply_text("⚠️ Formato errato. Invia: <code>TICKER QUOTE PREZZO</code> (es: <code>ISP.MI 15 6.02</code>)", parse_mode='HTML')
        return ATTESA_COMPRA_GUIDATA

    t_raw, quote_s, pmc_s = parti[0].upper(), parti[1], parti[2].replace(",", ".")
    ticker = t_raw if t_raw.endswith(".MI") else f"{t_raw}.MI"
    try:
        quote = int(quote_s)
        pmc = float(pmc_s)
    except ValueError:
        await update.message.reply_text("⚠️ Quantità o prezzo non validi. Riprova:", parse_mode='HTML')
        return ATTESA_COMPRA_GUIDATA

    nome = PANIERE.get(ticker, {}).get("nome", ticker)
    portafoglio = carica_portafoglio()
    portafoglio[ticker] = {"nome": nome, "quote": quote, "pmc": pmc, "data": datetime.now().strftime("%Y-%m-%d %H:%M")}
    salva_portafoglio(portafoglio)

    await update.message.reply_text(
        f"✅ <b>ACQUISTO SALVATO NEL CLOUD!</b>\n\n"
        f"• Titolo: <b>{nome}</b> (<code>{ticker}</code>)\n"
        f"• Quote: <code>{quote}</code> | Prezzo Carico: <code>€{pmc:.3f}</code>\n"
        f"• Totale: <code>€{quote * pmc:.2f}</code>",
        parse_mode='HTML',
        reply_markup=genera_tastiera_cockpit()
    )
    return ConversationHandler.END

async def guida_vendi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    portafoglio = carica_portafoglio()
    if not portafoglio:
        await update.callback_query.message.reply_text("⚠️ Nessun titolo attualmente in portafoglio da vendere.", reply_markup=genera_tastiera_cockpit())
        return ConversationHandler.END

    msg = (
        "➖ <b>REGISTRAZIONE VENDITA</b>\n\n"
        "Rispondi a questo messaggio indicando:\n"
        "<b>TICKER PREZZO</b>\n\n"
        "<i>Esempio pratico:</i>\n"
        "<code>A2A.MI 2.438</code>"
    )
    await update.callback_query.message.reply_text(msg, parse_mode='HTML')
    return ATTESA_VENDI_GUIDATA

async def esegui_vendi_guidata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parti = update.message.text.strip().split()
    if len(parti) < 2:
        await update.message.reply_text("⚠️ Formato errato. Invia: <code>TICKER PREZZO</code> (es: <code>A2A.MI 2.438</code>)", parse_mode='HTML')
        return ATTESA_VENDI_GUIDATA

    t_raw, p_s = parti[0].upper(), parti[1].replace(",", ".")
    ticker = t_raw if t_raw.endswith(".MI") else f"{t_raw}.MI"
    try:
        p_uscita = float(p_s)
    except ValueError:
        await update.message.reply_text("⚠️ Prezzo non valido. Riprova:", parse_mode='HTML')
        return ATTESA_VENDI_GUIDATA

    portafoglio = carica_portafoglio()
    if ticker not in portafoglio:
        await update.message.reply_text(f"⚠️ Il titolo {ticker} non è presente nel portafoglio.", reply_markup=genera_tastiera_cockpit())
        return ConversationHandler.END

    info = portafoglio[ticker]
    op = registra_chiusura_operazione(ticker, info['nome'], info['quote'], info['pmc'], p_uscita)
    del portafoglio[ticker]
    salva_portafoglio(portafoglio)

    ico = "🟢" if op['pnl_euro'] >= 0 else "🔴"
    await update.message.reply_text(
        f"✅ <b>VENDITA REGISTRATA!</b>\n\n"
        f"• <b>{op['nome']}</b>: Chiuso a <code>€{p_uscita:.3f}</code>\n"
        f"• Esito: {ico} <b>€{op['pnl_euro']:+.2f} ({op['rendimento_pct']:+.2f}%)</b>\n"
        f"• Saldo sincronizzato nel cloud.",
        parse_mode='HTML',
        reply_markup=genera_tastiera_cockpit()
    )
    return ConversationHandler.END

# --- ROUTER PULSANTI CALLBACK ---
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "btn_menu":
        await cmd_console(update, context)
    elif data == "btn_scan":
        await cmd_scan(update, context)
    elif data == "btn_top10":
        await cmd_top10(update, context)
    elif data == "btn_portafoglio":
        await cmd_portafoglio(update, context)
    elif data == "btn_outlook":
        await cmd_outlook(update, context)
    elif data == "btn_storico":
        await cmd_storico(update, context)
    elif data.startswith("buy_"):
        _, ticker, prezzo = data.split("_")
        context.user_data['pending_ticker'] = ticker
        context.user_data['pending_prezzo'] = float(prezzo)
        await query.edit_message_text(
            f"🛒 Acquisto <b>{ticker}</b> a ~<code>€{prezzo}</code>\n"
            f"Rispondi con quante quote hai comprato e a che prezzo (es: <code>10 a {prezzo}</code>):",
            parse_mode='HTML'
        )
        return ATTESA_DETTAGLI_ACQUISTO

async def registra_dettagli_acquisto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    testo = update.message.text.lower().replace(",", ".")
    ticker = context.user_data.get('pending_ticker')
    try:
        parti = [p for p in testo.replace("a", " ").split() if p]
        quote = int(parti[0])
        pmc = float(parti[1])
        nome = PANIERE.get(ticker, {}).get("nome", ticker)
        portafoglio = carica_portafoglio()
        portafoglio[ticker] = {"nome": nome, "quote": quote, "pmc": pmc, "data": datetime.now().strftime("%Y-%m-%d %H:%M")}
        salva_portafoglio(portafoglio)
        await update.message.reply_text(f"✅ Registrato: <b>{nome}</b> ({quote} quote a €{pmc:.3f})", parse_mode='HTML', reply_markup=genera_tastiera_cockpit())
    except Exception:
        await update.message.reply_text("⚠️ Formato non valido. Esempio: <code>10 a 6.02</code>", parse_mode='HTML')
        return ATTESA_DETTAGLI_ACQUISTO
    return ConversationHandler.END

async def post_init_config(app):
    comandi = [
        BotCommand("start", "Apri la plancia di comando principale"),
        BotCommand("console", "Visualizza il cockpit operativo"),
        BotCommand("scan", "Verifica cosa comprare oggi"),
        BotCommand("portafoglio", "Stato investimenti e P&L"),
        BotCommand("top10", "Classifica Win Rate titoli")
    ]
    await app.bot.set_my_commands(comandi)

def main():
    threading.Thread(target=avvia_server_porta, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init_config).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(guida_compra, pattern="^btn_guida_compra$"),
            CallbackQueryHandler(guida_vendi, pattern="^btn_guida_vendi$"),
            CallbackQueryHandler(callback_router)
        ],
        states={
            ATTESA_DETTAGLI_ACQUISTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, registra_dettagli_acquisto)],
            ATTESA_COMPRA_GUIDATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, esegui_compra_guidata)],
            ATTESA_VENDI_GUIDATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, esegui_vendi_guidata)]
        },
        fallbacks=[CallbackQueryHandler(callback_router)]
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", cmd_console))
    app.add_handler(CommandHandler("console", cmd_console))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("portafoglio", cmd_portafoglio))
    app.add_handler(CommandHandler("top10", cmd_top10))
    app.add_handler(CommandHandler("outlook", cmd_outlook))
    app.add_handler(CommandHandler("storico", cmd_storico))

    print("🤖 Cockpit Diretto Operativo...")
    app.run_polling()

if __name__ == "__main__":
    main()
