import os
import re
import html
import json
import psutil
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import yfinance as yf
from flask import Flask, jsonify, render_template
from dotenv import load_dotenv
import anthropic

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

app = Flask(__name__)

MAX_HISTORY = 20
temp_history = []
ram_history = []

ai_summary_cache = {
    "text": "Načítání AI shrnutí...",
    "last_updated": None
}

YAHOO_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/122.0.0.0 Safari/537.36'
    )
}

T212_EXCHANGE_YAHOO = {
    'US': '',
    'UK': '.L',
    'GB': '.L',
    'LSE': '.L',
    'DE': '.DE',
    'NL': '.AS',
    'FR': '.PA',
    'IT': '.MI',
    'ES': '.MC',
    'BE': '.BR',
    'PT': '.LS',
    'IE': '.IR',
    'CH': '.SW',
    'AT': '.VI',
    'SE': '.ST',
    'NO': '.OL',
    'DK': '.CO',
    'FI': '.HE',
    'PL': '.WA',
    'CZ': '.PR',
    'AU': '.AX',
    'CA': '.TO',
    'JP': '.T',
    'HK': '.HK',
    'SG': '.SI',
}

NAME_STOPWORDS = {
    'inc', 'inc.', 'corp', 'corp.', 'corporation', 'ltd', 'ltd.', 'limited',
    'plc', 'sa', 'ag', 'nv', 'se', 'class', 'ordinary', 'ordinary-shs',
    'shares', 'share', 'stock', 'common', 'the', 'co', 'co.', 'company',
    'holdings', 'holding', 'group', 'adr', 'ads'
}

ETF_TYPES = {
    'ETF', 'ETFS', 'ETC', 'ETN', 'FUND', 'FUNDS', 'MUTUALFUND',
    'MUTUAL_FUND', 'INVESTMENTTRUST', 'INVESTMENT_TRUST'
}


def get_temperature():
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            return round(float(f.read()) / 1000.0, 1)
    except:
        return 0.0

t212_cache = {"data": None, "last_updated": None}
T212_SNAPSHOT_FILE = os.path.join(BASE_DIR, 't212_midnight.json')

def get_trading212_data():
    global t212_cache
    now = datetime.now()

    if t212_cache["last_updated"] and (now - t212_cache["last_updated"]).total_seconds() < 60:
        return t212_cache["data"]

    key = os.getenv('TRADING212_API_KEY')
    secret = os.getenv('TRADING212_API_SECRET')
    if not key or not secret:
        return None

    try:
        res = requests.get(
            'https://live.trading212.com/api/v0/equity/account/summary',
            auth=(key, secret),
            timeout=4
        )
        res.raise_for_status()
        inv = res.json().get('investments', {})

        invested = inv.get('totalCost', 0) or 0
        current = inv.get('currentValue', 0) or 0
        profit = inv.get('unrealizedProfitLoss', 0) or 0

        alltime_percent = (profit / invested * 100) if invested else 0

        today = now.strftime('%Y-%m-%d')
        snapshot = None
        try:
            with open(T212_SNAPSHOT_FILE, 'r') as f:
                snapshot = json.load(f)
        except Exception:
            pass

        if not snapshot or snapshot.get('date') != today:
            snapshot = {'date': today, 'profit': profit, 'current': current}
            with open(T212_SNAPSHOT_FILE, 'w') as f:
                json.dump(snapshot, f)

        base = snapshot['current']
        today_percent = ((profit - snapshot['profit']) / base * 100) if base else 0

        data = {
            'alltime_percent': alltime_percent,
            'today_percent': today_percent
        }
        t212_cache["data"] = data
        t212_cache["last_updated"] = now
        return data
    except Exception as e:
        print(f"Trading212 chyba: {e}")
        return t212_cache["data"]



T212_SECTOR_CACHE_FILE = os.path.join(BASE_DIR, 't212_sector_cache.json')
T212_INSTRUMENTS_CACHE_FILE = os.path.join(BASE_DIR, 't212_instruments_cache.json')
SECTOR_MISS_RETRY_HOURS = 24
SECTOR_LOOKUP_BUDGET_SECONDS = 12
t212_sector_cache = None
t212_sector_cache_loaded = False
t212_sectors_result_cache = {"data": None, "last_updated": None}
t212_instruments_mem = {"data": None, "last_updated": None}


SECTOR_NAMES = {
    'Technology': 'Technologie',
    'Communication Services': 'Komunikační služby',
    'Consumer Cyclical': 'Spotřební zboží',
    'Consumer Defensive': 'Základní spotřeba',
    'Healthcare': 'Zdravotnictví',
    'Financial Services': 'Finance',
    'Financial': 'Finance',
    'Industrials': 'Průmysl',
    'Industrial': 'Průmysl',
    'Basic Materials': 'Základní materiály',
    'Energy': 'Energetika',
    'Real Estate': 'Nemovitosti',
    'Utilities': 'Veřejné služby'
}


def load_sector_cache():
    global t212_sector_cache, t212_sector_cache_loaded

    if t212_sector_cache_loaded:
        return t212_sector_cache

    try:
        with open(T212_SECTOR_CACHE_FILE, 'r', encoding='utf-8') as f:
            t212_sector_cache = json.load(f)
            if not isinstance(t212_sector_cache, dict):
                t212_sector_cache = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        t212_sector_cache = {}

    t212_sector_cache_loaded = True
    return t212_sector_cache


def save_sector_cache():
    try:
        with open(T212_SECTOR_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(t212_sector_cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"Chyba ukládání sektorové cache: {e}")


def parse_cache_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def cached_classification(ticker):
    cache = load_sector_cache()
    entry = cache.get(ticker)
    if not isinstance(entry, dict):
        return None, False

    sector = entry.get('sector')
    status = entry.get('status')
    if not status:
        status = 'ok' if sector else 'miss'

    if status == 'ok' and sector:
        return sector, True

    updated = parse_cache_time(entry.get('updated'))
    if updated and datetime.now() - updated < timedelta(hours=SECTOR_MISS_RETRY_HOURS):
        return None, True

    return None, False


def store_classification(ticker, sector, name):
    cache = load_sector_cache()
    cache[ticker] = {
        'sector': sector,
        'name': name or ticker,
        'status': 'ok' if sector else 'miss',
        'updated': datetime.now().isoformat(timespec='seconds')
    }
    save_sector_cache()


def t212_auth():
    key = os.getenv('TRADING212_API_KEY')
    secret = os.getenv('TRADING212_API_SECRET')
    if not key or not secret:
        return None
    return (key, secret)


def t212_get_json(url, timeout=10):
    auth = t212_auth()
    if not auth:
        return None

    response = requests.get(url, auth=auth, timeout=timeout)
    response.raise_for_status()
    return response.json()


def normalize_name(value):
    if not value:
        return ''
    value = html.unescape(str(value)).lower()
    value = re.sub(r'[^a-z0-9]+', ' ', value)
    tokens = [t for t in value.split() if t and t not in NAME_STOPWORDS]
    return ' '.join(tokens)


def names_compatible(left, right):
    a = normalize_name(left)
    b = normalize_name(right)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return False
    overlap = tokens_a & tokens_b
    return len(overlap) / min(len(tokens_a), len(tokens_b)) >= 0.5


def unique_keep_order(items):
    seen = set()
    result = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def yahoo_symbols_from_t212(ticker):
    if not ticker:
        return []

    raw = str(ticker).strip()
    core = re.sub(r'(_EQ)+$', '', raw, flags=re.IGNORECASE)
    exch = None

    if '_' in core:
        maybe_symbol, maybe_exch = core.rsplit('_', 1)
        if maybe_exch.upper() in T212_EXCHANGE_YAHOO:
            exch = maybe_exch.upper()
            core = maybe_symbol

    dotted = core.replace('_', '.')
    hyphen = core.replace('.', '-').replace('_', '-')
    suffix = T212_EXCHANGE_YAHOO.get(exch, '')

    symbols = []
    for base in (hyphen, dotted, core):
        if suffix:
            symbols.append(base + suffix)
        symbols.append(base)

    return unique_keep_order(symbols)


def translate_sector(sector):
    if not sector:
        return None
    return SECTOR_NAMES.get(sector, sector)


def classification_from_quote_meta(quote_type, sector, t212_type):
    qt = (quote_type or '').upper()
    tt = (t212_type or '').upper()

    if qt in ETF_TYPES or tt in ETF_TYPES:
        return 'ETF / fondy'
    if qt in ('EQUITY', 'STOCK', '') or tt in ('STOCK', 'EQUITY', 'SHARES', ''):
        return translate_sector(sector)
    if sector and qt not in ('INDEX', 'CURRENCY', 'CRYPTOCURRENCY', 'OPTION', 'FUTURE'):
        return translate_sector(sector)
    return None


def yahoo_quote_summary(symbol):
    try:
        response = requests.get(
            f'https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}',
            params={'modules': 'assetProfile,summaryProfile,price'},
            headers=YAHOO_HEADERS,
            timeout=8
        )
        if response.status_code != 200:
            return None

        payload = response.json() or {}
        result = (payload.get('quoteSummary') or {}).get('result') or []
        if not result:
            return None

        item = result[0]
        profile = item.get('assetProfile') or item.get('summaryProfile') or {}
        price = item.get('price') or {}
        return {
            'symbol': price.get('symbol') or symbol,
            'quoteType': price.get('quoteType'),
            'shortName': price.get('shortName'),
            'longName': price.get('longName'),
            'sector': profile.get('sector'),
            'isin': profile.get('isin')
        }
    except Exception as e:
        print(f"Yahoo quoteSummary chyba ({symbol}): {e}")
        return None


def yfinance_ticker_meta(symbol):
    try:
        info = yf.Ticker(symbol).info or {}
        if not info:
            return None
        if not any(info.get(k) for k in ('symbol', 'shortName', 'longName', 'sector', 'quoteType')):
            return None
        return {
            'symbol': info.get('symbol') or symbol,
            'quoteType': info.get('quoteType'),
            'shortName': info.get('shortName') or info.get('shortname'),
            'longName': info.get('longName') or info.get('longname'),
            'sector': info.get('sector') or info.get('sectorDisp'),
            'isin': info.get('isin')
        }
    except Exception as e:
        print(f"yfinance chyba ({symbol}): {e}")
        return None


def yahoo_search_quotes(query):
    if not query:
        return []

    quotes = []

    try:
        response = requests.get(
            'https://query1.finance.yahoo.com/v1/finance/search',
            params={'q': query, 'quotesCount': 10, 'newsCount': 0},
            headers=YAHOO_HEADERS,
            timeout=8
        )
        if response.status_code == 200:
            quotes = (response.json() or {}).get('quotes') or []
    except Exception as e:
        print(f"Yahoo search chyba ({query}): {e}")

    if not quotes:
        try:
            search = yf.Search(query, max_results=10)
            quotes = search.quotes or []
        except Exception as e:
            print(f"yfinance Search chyba ({query}): {e}")

    return quotes


def meta_matches_instrument(meta, name, isin, require_identity):
    if not meta:
        return False

    meta_isin = (meta.get('isin') or '').replace(' ', '').upper()
    if isin and meta_isin and meta_isin == isin.replace(' ', '').upper():
        return True

    yahoo_names = [meta.get('longName'), meta.get('shortName')]
    if name and any(names_compatible(name, candidate) for candidate in yahoo_names if candidate):
        return True

    return not require_identity


def classify_from_meta(meta, name, isin, t212_type, require_identity):
    if not meta_matches_instrument(meta, name, isin, require_identity):
        return None
    return classification_from_quote_meta(
        meta.get('quoteType'),
        meta.get('sector'),
        t212_type
    )


def classify_yahoo_symbol(symbol, name, isin, t212_type, require_identity):
    meta = yahoo_quote_summary(symbol)
    sector = classify_from_meta(meta, name, isin, t212_type, require_identity)
    if sector:
        return sector

    meta = yfinance_ticker_meta(symbol)
    return classify_from_meta(meta, name, isin, t212_type, require_identity)


def best_search_matches(quotes, name, isin):
    ranked = []
    for quote in quotes:
        quote_type = (quote.get('quoteType') or '').upper()
        if quote_type not in ('EQUITY', 'ETF', 'MUTUALFUND', 'ETC', 'INDEX', ''):
            continue

        symbol = quote.get('symbol')
        if not symbol:
            continue

        score = 0
        quote_isin = (quote.get('isin') or '').replace(' ', '').upper()
        if isin and quote_isin and quote_isin == isin.replace(' ', '').upper():
            score += 5

        longname = quote.get('longname') or quote.get('longName') or ''
        shortname = quote.get('shortname') or quote.get('shortName') or ''
        if name and names_compatible(name, longname):
            score += 3
        if name and names_compatible(name, shortname):
            score += 2

        if score <= 0:
            continue

        ranked.append((score, symbol, quote_type))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def find_instrument_sector(ticker, name, isin, inst_type):
    t212_type = (inst_type or '').upper()
    if t212_type in ETF_TYPES:
        return 'ETF / fondy'

    for symbol in yahoo_symbols_from_t212(ticker):
        sector = classify_yahoo_symbol(
            symbol,
            name,
            isin,
            t212_type,
            require_identity=False
        )
        if sector:
            return sector

    queries = unique_keep_order([
        (isin or '').replace(' ', ''),
        name,
        ticker
    ])

    for query in queries:
        quotes = yahoo_search_quotes(query)
        matches = best_search_matches(quotes, name, isin)
        for _score, symbol, quote_type in matches[:3]:
            if (quote_type or '').upper() in ETF_TYPES:
                return 'ETF / fondy'
            sector = classify_yahoo_symbol(
                symbol,
                name,
                isin,
                t212_type,
                require_identity=True
            )
            if sector:
                return sector

    return None


def load_instruments_cache_file():
    try:
        with open(T212_INSTRUMENTS_CACHE_FILE, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        if isinstance(payload, dict) and isinstance(payload.get('data'), dict):
            return payload
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def save_instruments_cache_file(data):
    try:
        with open(T212_INSTRUMENTS_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(
                {
                    'last_updated': datetime.now().isoformat(timespec='seconds'),
                    'data': data
                },
                f,
                ensure_ascii=False
            )
    except OSError as e:
        print(f"Chyba ukládání cache instrumentů: {e}")


def get_t212_instruments_map():
    now = datetime.now()
    if (
        t212_instruments_mem["data"] is not None
        and t212_instruments_mem["last_updated"]
        and (now - t212_instruments_mem["last_updated"]).total_seconds() < 6 * 3600
    ):
        return t212_instruments_mem["data"]

    file_cache = load_instruments_cache_file()
    if file_cache:
        updated = parse_cache_time(file_cache.get('last_updated'))
        if updated and now - updated < timedelta(hours=24):
            t212_instruments_mem["data"] = file_cache['data']
            t212_instruments_mem["last_updated"] = updated
            return file_cache['data']

    mapping = {}
    try:
        payload = t212_get_json(
            'https://live.trading212.com/api/v0/equity/metadata/instruments',
            timeout=20
        )
        instruments = payload
        if isinstance(payload, dict):
            instruments = (
                payload.get('items')
                or payload.get('instruments')
                or payload.get('data')
                or []
            )
        if isinstance(instruments, list):
            for item in instruments:
                ticker = item.get('ticker')
                if ticker:
                    mapping[ticker] = item
            save_instruments_cache_file(mapping)
            t212_instruments_mem["data"] = mapping
            t212_instruments_mem["last_updated"] = now
            return mapping
    except Exception as e:
        print(f"Trading212 instrumenty chyba: {e}")

    if file_cache and file_cache.get('data'):
        t212_instruments_mem["data"] = file_cache['data']
        t212_instruments_mem["last_updated"] = now
        return file_cache['data']

    t212_instruments_mem["data"] = mapping
    t212_instruments_mem["last_updated"] = now
    return mapping


def fetch_t212_positions():
    urls = (
        'https://live.trading212.com/api/v0/equity/positions',
        'https://live.trading212.com/api/v0/equity/portfolio',
    )
    last_error = None

    for url in urls:
        try:
            payload = t212_get_json(url, timeout=10)
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict):
                positions = (
                    payload.get('positions')
                    or payload.get('items')
                    or payload.get('data')
                    or payload.get('portfolio')
                )
                if isinstance(positions, list):
                    return positions
        except Exception as e:
            last_error = e
            print(f"Trading212 pozice chyba ({url}): {e}")

    if last_error:
        raise last_error
    return None


def position_market_value(position):
    for key in ('currentValue', 'value', 'result', 'marketValue'):
        if position.get(key) not in (None, ''):
            try:
                return float(position.get(key) or 0)
            except (TypeError, ValueError):
                pass

    wallet = position.get('walletImpact')
    if isinstance(wallet, dict) and wallet.get('currentValue') not in (None, ''):
        try:
            return float(wallet.get('currentValue') or 0)
        except (TypeError, ValueError):
            pass

    try:
        quantity = float(position.get('quantity') or 0)
        price = float(position.get('currentPrice') or 0)
        return quantity * price
    except (TypeError, ValueError):
        return 0.0


def extract_position_instrument(position, instruments_map):
    instrument = position.get('instrument')
    if not isinstance(instrument, dict):
        instrument = {}

    ticker = (
        position.get('ticker')
        or instrument.get('ticker')
        or position.get('code')
        or instrument.get('code')
    )
    meta = instruments_map.get(ticker, {}) if ticker else {}

    name = (
        instrument.get('name')
        or position.get('name')
        or meta.get('name')
        or meta.get('shortName')
        or ''
    )
    isin = (
        instrument.get('isin')
        or position.get('isin')
        or meta.get('isin')
        or ''
    )
    inst_type = (
        instrument.get('type')
        or position.get('type')
        or meta.get('type')
        or ''
    )

    return ticker, name, isin, inst_type


def round_sector_percents(sector_totals, portfolio_total):
    items = [
        (name, value / portfolio_total * 100)
        for name, value in sector_totals.items()
        if value > 0
    ]
    if not items:
        return []

    rounded = [round(percent, 1) for _, percent in items]
    drift = round(100 - sum(rounded), 1)
    if rounded:
        largest = max(range(len(rounded)), key=lambda i: items[i][1])
        rounded[largest] = round(rounded[largest] + drift, 1)

    result = [
        {'name': items[i][0], 'percent': rounded[i]}
        for i in range(len(items))
        if rounded[i] > 0
    ]
    return sorted(result, key=lambda item: item['percent'], reverse=True)


def get_trading212_sectors():
    global t212_sector_cache

    now = datetime.now()
    cached_result = t212_sectors_result_cache["data"]
    last_updated = t212_sectors_result_cache["last_updated"]
    if cached_result is not None and last_updated and (now - last_updated).total_seconds() < 60:
        return cached_result

    try:
        if not t212_auth():
            return None

        positions = fetch_t212_positions()
        if not isinstance(positions, list):
            return None

        instruments_map = get_t212_instruments_map()
        load_sector_cache()

        sector_totals = {}
        portfolio_total = 0
        lookup_started = datetime.now()

        for position in positions:
            ticker, name, isin, inst_type = extract_position_instrument(
                position,
                instruments_map
            )
            value = position_market_value(position)

            if not ticker or value <= 0:
                continue

            portfolio_total += value

            sector, trusted = cached_classification(ticker)
            if not trusted:
                elapsed = (datetime.now() - lookup_started).total_seconds()
                if elapsed < SECTOR_LOOKUP_BUDGET_SECONDS:
                    sector = find_instrument_sector(ticker, name, isin, inst_type)
                    store_classification(ticker, sector, name)
                else:
                    sector = None

            if not sector:
                sector = 'Nezařazeno'

            sector_totals[sector] = sector_totals.get(sector, 0) + value

        if portfolio_total <= 0:
            result = []
        else:
            result = round_sector_percents(sector_totals, portfolio_total)

        t212_sectors_result_cache["data"] = result
        t212_sectors_result_cache["last_updated"] = now
        return result

    except Exception as e:
        print(f"Trading212 sektory chyba: {e}")
        if t212_sectors_result_cache["data"] is not None:
            return t212_sectors_result_cache["data"]
        return None


def get_crypto_data():
    try:
        url = 'https://api.coingecko.com/api/v3/coins/markets'
        params = {'vs_currency': 'usd', 'ids': 'bitcoin', 'price_change_percentage': '7d'}
        d = requests.get(url, params=params, timeout=3).json()[0]
        return {
            'price': d['current_price'],
            'change_24h': d['price_change_percentage_24h'],
            'change_7d': d.get('price_change_percentage_7d_in_currency'),
            'ath': d['ath'],
            'ath_change': d['ath_change_percentage']
        }
    except:
        return None

def get_system_stats():
    try:
        disk = psutil.disk_usage('/')
        disk_total_gb = round(disk.total / (1024**3), 1)
        disk_used_gb = round(disk.used / (1024**3), 1)
        disk_percent = disk.percent

        boot_time = datetime.fromtimestamp(psutil.boot_time())
        uptime_delta = datetime.now() - boot_time
        days = uptime_delta.days
        hours = uptime_delta.seconds // 3600
        uptime_str = f"{days}d {hours}h"

        return {
            'disk_percent': disk_percent,
            'disk_used': disk_used_gb,
            'disk_total': disk_total_gb,
            'uptime': uptime_str
        }
    except:
        return {'disk_percent': 0, 'disk_used': 0, 'disk_total': 0, 'uptime': 'N/A'}

def get_news():
    try:
        url = 'https://www.seznamzpravy.cz/rss/nejnovejsi'
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=3)
        
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            item = root.find('.//item')
            if item is not None:
                title = item.find('title')
                desc = item.find('description')
                
                title_text = title.text.strip() if title is not None and title.text else "Aktuální zpráva"
                desc_text = desc.text.strip() if desc is not None and desc.text else ""
                
                return {
                    'title': title_text,
                    'description': desc_text[:300] + '...' if len(desc_text) > 300 else desc_text
                }
    except:
        pass
    
    return {'title': 'Zprávy jsou online', 'description': 'Sledujeme aktuální dění na Seznam Zprávách.'}

def get_weather():
    try:
        url = 'https://api.open-meteo.com/v1/forecast?latitude=50.0755&longitude=14.4378&current=temperature_2m,weather_code'
        res = requests.get(url, timeout=3).json()
        temp = res['current']['temperature_2m']
        code = res['current']['weather_code']
        
        weather_desc = "Jasno ☀️"
        if code in [1, 2, 3]:
            weather_desc = "Polojasno ⛅"
        elif code in [45, 48]:
            weather_desc = "Mlha 🌫️"
        elif code in [51, 53, 55, 61, 63, 65, 80, 81]:
            weather_desc = "Déšť 🌧️"
        elif code >= 71:
            weather_desc = "Sněžení ❄️"
            
        return {
            'temp': round(temp, 1),
            'desc': weather_desc
        }
    except:
        return {'temp': '--', 'desc': 'Offline'}


def get_calendar_events():
    try:
        ics_url = os.getenv('CALENDAR_ICS_URL')

        if not ics_url:
            return [{
                'title': 'Chybí CALENDAR_ICS_URL v .env',
                'time': 'Zkontroluj konfiguraci'
            }]

        res = requests.get(ics_url, timeout=4)

        if res.status_code != 200:
            return [{'title': 'Kalendář nedostupný', 'time': ''}]

        lines = res.text.replace('\r\n ', '').replace('\n ', '').splitlines()

        events = []
        current_event = None
        now = datetime.now().astimezone()

        def parse_ics_date(value):
            if not value:
                return None, False

            value = value.strip()

            if 'T' not in value:
                dt = datetime.strptime(value, '%Y%m%d')
                return dt.date(), True

            if value.endswith('Z'):
                dt = datetime.strptime(value, '%Y%m%dT%H%M%SZ')
                return dt.replace(tzinfo=timezone.utc).astimezone(), False

            dt = datetime.strptime(value, '%Y%m%dT%H%M%S')
            return dt.astimezone(), False

        for line in lines:
            if line.startswith('BEGIN:VEVENT'):
                current_event = {}

            elif line.startswith('END:VEVENT') and current_event is not None:
                summary = current_event.get('SUMMARY', 'Bez názvu')
                start_value = current_event.get('DTSTART', '')
                end_value = current_event.get('DTEND', '')

                try:
                    start, all_day = parse_ics_date(start_value)
                    end, _ = parse_ics_date(end_value)

                    if start is None:
                        current_event = None
                        continue

                    if all_day:
                        if end:
                            display_end = end - timedelta(days=1)
                        else:
                            display_end = start

                        if display_end < start:
                            display_end = start

                        if display_end > start:
                            time_str = (
                                f"{start.strftime('%d.%m.')} – "
                                f"{display_end.strftime('%d.%m.')} (Celý den)"
                            )
                        else:
                            time_str = start.strftime('%d.%m. (Celý den)')

                        event_start = datetime.combine(
                            start, datetime.min.time()
                        ).astimezone()

                        event_end = datetime.combine(
                            display_end + timedelta(days=1),
                            datetime.min.time()
                        ).astimezone()

                    else:
                        if end is None:
                            end = start

                        if end > start:
                            time_str = (
                                f"{start.strftime('%d.%m. %H:%M')} – "
                                f"{end.strftime('%d.%m. %H:%M')}"
                            )
                        else:
                            time_str = start.strftime('%d.%m. %H:%M')

                        event_start = start
                        event_end = end

                    if event_end >= now:
                        events.append({
                            'title': summary,
                            'time': time_str,
                            'raw': event_start,
                            'start_date': start.isoformat(),
                            'end_date': (
                                end.isoformat() if end else start.isoformat()
                            ) if not all_day else display_end.isoformat()
                        })

                except Exception as e:
                    print(f"Chyba zpracování události: {e}")

                current_event = None

            elif current_event is not None and ':' in line:
                key, val = line.split(':', 1)
                key = key.split(';')[0]
                current_event[key] = val

        events.sort(key=lambda x: x['raw'])

        cleaned_events = [
            {
                'title': e['title'],
                'time': e['time'],
                'start_date': e['start_date'],
                'end_date': e['end_date']
            }
            for e in events[:6]
        ]

        return cleaned_events if cleaned_events else [
            {'title': 'Žádné nadcházející události', 'time': ''}
        ]

    except Exception as e:
        print(f"Chyba načítání kalendáře: {e}")
        return [{'title': 'Chyba načítání kalendáře', 'time': ''}]


def get_stock_news():
    try:
        url = 'https://www.kurzy.cz/zpravy/util/forext.dat?type=rss&col=wzAkcieSvet'
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=3)

        if response.status_code == 200:
            try:
                text = response.content.decode('utf-8')
            except UnicodeDecodeError:
                text = response.content.decode('windows-1250', errors='replace')

            items = re.findall(r'<item>(.*?)</item>', text, re.DOTALL)

            if items:
                item = items[0]
                title = re.search(r'<title>(.*?)</title>', item, re.DOTALL)
                desc = re.search(r'<description>(.*?)</description>', item, re.DOTALL)

                def clean(text):
                    text = text.replace('<![CDATA[', '').replace(']]>', '')
                    text = re.sub(r'<[^>]+>', '', text)
                    return html.unescape(text).strip()

                title_text = clean(title.group(1)) if title else "Aktuální zpráva"
                desc_text = clean(desc.group(1)) if desc else ""

                return {
                    'title': title_text,
                    'description': desc_text[:300] + '...' if len(desc_text) > 300 else desc_text
                }
    except Exception as e:
        print(f"Chyba při načítání akciových zpráv: {e}")

    return {'title': 'Trhy jsou v pohybu', 'description': 'Sledujeme aktuální vývoj na akciových trzích.'}

def get_ai_daily_brief():
    global ai_summary_cache
    now = datetime.now()
    
    if ai_summary_cache["last_updated"] and (now - ai_summary_cache["last_updated"]).total_seconds() < 600:
        return ai_summary_cache["text"]

    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        return "Chybí ANTHROPIC_API_KEY v .env"

    try:
        weather = get_weather()
        stock_news = get_stock_news()
        news = get_news()
        calendar = get_calendar_events()

        now_dt = datetime.now()
        today = now_dt.date()
        now_str = now_dt.strftime("%d.%m.%Y %H:%M")


        
        cal_lines = []
        today_date = datetime.now().date()

        for ev in calendar:
            try:
                start_date = datetime.fromisoformat(ev['start_date']).date()
                end_date = datetime.fromisoformat(ev['end_date']).date()

                if start_date > today_date + timedelta(days=3):
                    continue

                if end_date < today_date:
                    continue

                if start_date < today_date:
                    when = "PRÁVĚ PROBÍHÁ"
                elif start_date == today_date:
                    when = "DNES"
                elif start_date == today_date + timedelta(days=1):
                    when = "ZÍTRA"
                elif start_date == today_date + timedelta(days=2):
                    when = "POZÍTŘÍ"
                else:
                    when = "ZA 3 DNY"

                cal_lines.append(
                    f"- {when}: {ev['title']} ({ev['time']})"
                )

            except Exception as e:
                print(f"Chyba kalendáře pro AI: {e}")

        calendar_text = "\n".join(cal_lines) if cal_lines else "Nic v nejbližších třech dnech."



        user_name = os.getenv('USER_NAME', '')
        
        prompt = f"""
Jsi osobní parťák a AI asistent uživatele {user_name} na TV dashboardu.
Právě teď je: {now_str}

Data z mého systému:
- Počasí: 
{weather['temp']}°C, {weather['desc']}
- Zprávy z akciových trhů:
{stock_news['title']}: {stock_news['description']}
- Aktuální zpráva: 
{news['title']}: {news['description']}
- Kalendář:
{calendar_text}

Pravidla pro výstup:
1. TÓN: Buď neformální parťák, tykej mi, piš přirozeně a lidsky.
2. FOKUS: Zaměř se HLAVNĚ na dnešek a nejbližší 2-3 dny. Do shrnutí krátce zahrň i novinky z akciových trhů.
3. FAKTA: Vycházej STRICTNĚ a POUZE z poskytnutých dat. Nic si nevymýšlej.
4. KALENDÁŘ: Zachovej přesně označení dne z kalendáře (PRÁVĚ PROBÍHÁ, DNES, ZÍTRA, POZÍTŘÍ, ZA 3 DNY). Neodvozuj den z data a nepřepočítávej ho. Nikdy nezaměň pozítří za zítra.
5. FORMÁT: Piš čistě obyčejným textem. NIKDY nepoužívej hvězdičky ani tučné písmo (**). Pro lepší přehlednost můžeš používat odrážky (-).
6. DÉLKA: Piš podle potřeby, minimálně však 4 věty, horní limit není dán."""

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-5-5",
            max_tokens=1500,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}]
        )

        summary_text = "".join(
            b.text for b in response.content if getattr(b, 'type', None) == 'text'
        ).strip()

        if not summary_text:
            print(f"Prázdná odpověď, stop_reason: {response.stop_reason}")
            return "AI shrnutí se nepodařilo přečíst."

        ai_summary_cache["text"] = summary_text
        ai_summary_cache["last_updated"] = now
        return summary_text

    except Exception as e:
        print(f"Claude API Chyba: {e}")
        return ai_summary_cache["text"] if ai_summary_cache["last_updated"] else "AI shrnutí dočasně nedostupné."

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/hardware')
def hardware_stats():
    ram = psutil.virtual_memory()
    current_temp = get_temperature()
    current_ram = ram.percent

    temp_history.push(current_temp) if hasattr(temp_history, 'push') else temp_history.append(current_temp)
    ram_history.push(current_ram) if hasattr(ram_history, 'push') else ram_history.append(current_ram)

    if len(temp_history) > MAX_HISTORY:
        temp_history.pop(0)
    if len(ram_history) > MAX_HISTORY:
        ram_history.pop(0)

    return jsonify({
        'temperature': current_temp,
        'ram_used': round(ram.used / (1024 ** 3), 1),
        'ram_total': round(ram.total / (1024 ** 3), 1),
        'ram_percent': current_ram,
        'temp_history': temp_history,
        'ram_history': ram_history,
        'system': get_system_stats()
    })

@app.route('/api/external')
def external_stats():
    return jsonify({
        'btc': get_crypto_data(),
        'news': get_news(),
        'stock_news': get_stock_news(),
        'weather': get_weather(),
        'calendar': get_calendar_events(),
        'trading212': get_trading212_data(),
        'trading212_sectors': get_trading212_sectors(),
        'ai_brief': get_ai_daily_brief(),
        'ai_brief_time': ai_summary_cache["last_updated"].strftime("%H:%M") if ai_summary_cache["last_updated"] else ''
        
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)