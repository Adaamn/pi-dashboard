import os
import psutil
import requests
from flask import Flask, jsonify, render_template

app = Flask(__name__)

# Tvoje uživatelské jméno na GitHubu
GITHUB_USERNAME = "Adaamn"

def get_temperature():
    try:
        # Cesta platná pro Raspberry Pi (Linux)
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            return round(float(f.read()) / 1000.0, 1)
    except:
        # Pokud neběží na Pi (např. test na Windows), vrátí 0.0
        return 0.0

def get_crypto_price():
    try:
        url = 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd'
        res = requests.get(url, timeout=3).json()
        return res['bitcoin']['usd']
    except:
        return 'N/A'

def get_github_stats():
    try:
        url = f'https://api.github.com/users/{GITHUB_USERNAME}'
        # GitHub vyžaduje User-Agent hlavičku, jinak blokuje přístup
        headers = {'User-Agent': 'Pi-Dashboard-App'}
        res = requests.get(url, headers=headers, timeout=3).json()
        
        return {
            'public_repos': res.get('public_repos', 0),
            'followers': res.get('followers', 0),
            'name': res.get('name', GITHUB_USERNAME),
        }
    except:
        return {'public_repos': 0, 'followers': 0, 'name': GITHUB_USERNAME}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/stats')
def stats():
    ram = psutil.virtual_memory()
    data = {
        'temperature': get_temperature(),
        'ram_used': round(ram.used / (1024 * 1024 * 1024), 1),
        'ram_percent': ram.percent,
        'btc_price': get_crypto_price(),
        'github': get_github_stats(),
    }
    return jsonify(data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)