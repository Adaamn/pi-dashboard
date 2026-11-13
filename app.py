import os
import re
import html
import json
import psutil
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template
from dotenv import load_dotenv
import anthropic

load_dotenv()

app = Flask(__name__)

MAX_HISTORY = 20
temp_history = []
ram_history = []

ai_summary_cache = {
    "text": "Načítání AI shrnutí...",
    "last_updated": None
}

def get_temperature():
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            return round(float(f.read()) / 1000.0, 1)
    except:
        return 0.0

t212_cache = {"data": None, "last_updated": None}
T212_SNAPSHOT_FILE = 't212_midnight.json'

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
                        # DTEND je u celodenních událostí v ICS
                        # poslední den EXKLUZIVNÍ.
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

                    # Zobrazit události, které ještě neskončily.
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
        'ai_brief': get_ai_daily_brief(),
        'ai_brief_time': ai_summary_cache["last_updated"].strftime("%H:%M") if ai_summary_cache["last_updated"] else ''
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)