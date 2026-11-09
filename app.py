import os
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

def get_crypto_price():
    try:
        url = 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd'
        res = requests.get(url, timeout=3).json()
        return res['bitcoin']['usd']
    except:
        return 'N/A'

def get_f1_next_race():
    try:
        url = 'https://api.jolpi.ca/ergast/f1/current/next.json'
        res = requests.get(url, timeout=3).json()
        race = res['MRData']['RaceTable']['Races'][0]
        
        date_str = race['date']
        time_str = race.get('time', '14:00:00Z')
        iso_datetime = f"{date_str}T{time_str}"
        
        return {
            'name': race['raceName'],
            'datetime': iso_datetime,
            'circuit': race['Circuit']['circuitName']
        }
    except:
        return {'name': 'Sezóna skončila / Offline', 'datetime': '', 'circuit': ''}

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

def get_financial_news():
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
            return [{'title': 'Chybí CALENDAR_ICS_URL v .env', 'time': 'Zkontroluj konfiguraci'}]

        res = requests.get(ics_url, timeout=4)
        if res.status_code != 200:
            return [{'title': 'Kalendář nedostupný', 'time': ''}]

        lines = res.text.splitlines()
        events = []
        current_event = None
        now = datetime.now(timezone.utc)

        for line in lines:
            if line.startswith('BEGIN:VEVENT'):
                current_event = {}
            elif line.startswith('END:VEVENT') and current_event:
                summary = current_event.get('SUMMARY', 'Bez názvu')
                dtstart = current_event.get('DTSTART', '')
                
                parsed_time = None
                try:
                    if 'T' in dtstart:
                        dt_clean = dtstart.split('Z')[0]
                        parsed_time = datetime.strptime(dt_clean, '%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc)
                        time_str = parsed_time.astimezone().strftime('%d.%m. %H:%M')
                    else:
                        parsed_time = datetime.strptime(dtstart, '%Y%m%d').replace(tzinfo=timezone.utc)
                        time_str = parsed_time.strftime('%d.%m. (Celý den)')
                    
                    if parsed_time >= now - timedelta(hours=1):
                        events.append({'title': summary, 'time': time_str, 'raw': parsed_time})
                except:
                    pass
                current_event = None
            elif current_event is not None:
                if ':' in line:
                    key, val = line.split(':', 1)
                    key = key.split(';')[0]
                    current_event[key] = val

        events.sort(key=lambda x: x['raw'])
        cleaned_events = [{'title': e['title'], 'time': e['time']} for e in events[:6]]
        
        return cleaned_events if cleaned_events else [{'title': 'Žádné nadcházející události', 'time': ''}]
    except Exception as e:
        return [{'title': 'Chyba načítání kalendáře', 'time': ''}]

def get_ai_daily_brief():
    global ai_summary_cache
    now = datetime.now()
    
    if ai_summary_cache["last_updated"] and (now - ai_summary_cache["last_updated"]).total_seconds() < 6 * 3600:
        return ai_summary_cache["text"]

    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        return "Chybí ANTHROPIC_API_KEY v .env"

    try:
        weather = get_weather()
        f1 = get_f1_next_race()
        news = get_financial_news()
        calendar = get_calendar_events()

        now_dt = datetime.now()
        today = now_dt.date()
        now_str = now_dt.strftime("%d.%m.%Y %H:%M")

        cal_lines = []
        for ev in calendar:
            try:
                d, m = ev['time'].split(' ')[0].strip('.').split('.')
                diff = (datetime(today.year, int(m), int(d)).date() - today).days
                if diff > 3:
                    continue
                if diff <= 0:
                    when = "DNES"
                elif diff == 1:
                    when = "ZÍTRA"
                elif diff == 2:
                    when = "POZÍTŘÍ"
                else:
                    when = f"za {diff} dny"
                cal_lines.append(f"- {when}: {ev['title']}")
            except Exception:
                cal_lines.append(f"- {ev['title']} ({ev['time']})")
        calendar_text = "\n".join(cal_lines) if cal_lines else "Nic v nejbližších třech dnech."

        user_name = os.getenv('USER_NAME', '')
        
        prompt = f"""
Jsi osobní parťák a AI asistent uživatele {user_name} na TV dashboardu.
Právě teď je: {now_str}

Data z mého systému:
- Počasí: 
{weather['temp']}°C, {weather['desc']}
- Nadcházející závod F1: 
{f1['name']}
- Aktuální zpráva: 
{news['title']}
- Kalendář:
{calendar_text}

Pravidla pro výstup:
1. TÓN: Buď neformální parťák, tykej mi, piš přirozeně a lidsky.
2. FOKUS: Zaměř se HLAVNĚ na dnešek a nejbližší 2-3 dny.
3. FAKTA: Vycházej STRICTNĚ a POUZE z poskytnutých dat. Nic si nevymýšlej.
4. DNY: Ber den přesně tak, jak je napsaný na začátku řádku kalendáře. Pokud tam stojí POZÍTŘÍ, napiš "pozítří", nikdy ne "zítra". Dnes je jediná událost, která není zítra, pokud u ní není ZÍTRA.
5. FORMÁT: Piš čistě obyčejným textem. NIKDY nepoužívej hvězdičky ani tučné písmo (**). Pro lepší přehlednost můžeš používat odrážky (-).
6. DÉLKA: Piš podle potřeby, minimálně však 4 věty, horní limit není dán."""
        
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-5-5",
            max_tokens=1024,
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
        'ram_used': round(ram.used / (1024 * 1024 * 1024), 1),
        'ram_percent': current_ram,
        'temp_history': temp_history,
        'ram_history': ram_history,
        'system': get_system_stats()
    })

@app.route('/api/external')
def external_stats():
    return jsonify({
        'btc_price': get_crypto_price(),
        'f1': get_f1_next_race(),
        'news': get_financial_news(),
        'weather': get_weather(),
        'calendar': get_calendar_events(),
        'ai_brief': get_ai_daily_brief(),
        'ai_brief_time': ai_summary_cache["last_updated"].strftime("%H:%M") if ai_summary_cache["last_updated"] else ''
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)