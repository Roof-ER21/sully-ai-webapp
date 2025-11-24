#!/usr/bin/env python3
"""
Sully AI - Production Web App
Zero setup for Boss Man - just send him the URL!
"""

from flask import Flask, render_template_string, request, jsonify, Response, stream_with_context, session, send_file
import requests
from datetime import datetime
import json
from typing import Dict, List, Any
from groq import Groq
import pytz
import os
from dotenv import load_dotenv
import sqlite3
from functools import wraps
import secrets

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))  # Session management

# Configuration from environment (will be set in Railway)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")  # Optional: for live news search
STOCK_SYMBOLS = os.getenv("STOCK_SYMBOLS", "TSLA,AAPL,NVDA,MSFT,GOOGL,AMZN,META,DJT").split(',')
BOSTON_INTENSITY = int(os.getenv("BOSTON_INTENSITY", "2"))
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_turbo_v2")

# VIP Personalities to track
VIP_TRACKING = {
    'tom_brady': {
        'name': 'Tom Brady',
        'keywords': ['Tom Brady', 'TB12', 'Brady'],
        'businesses': ['TB12 Sports', 'Brady Brand', 'NFL Fox Sports'],
        'emoji': '🐐'
    },
    'elon_musk': {
        'name': 'Elon Musk',
        'keywords': ['Elon Musk', 'Tesla', 'SpaceX', 'X Twitter'],
        'stocks': ['TSLA'],
        'businesses': ['Tesla', 'SpaceX', 'X (Twitter)', 'Neuralink', 'Boring Company'],
        'emoji': '🚀'
    },
    'trump': {
        'name': 'Donald Trump',
        'keywords': ['Donald Trump', 'Trump', 'Truth Social'],
        'stocks': ['DJT'],  # Trump Media & Technology Group
        'businesses': ['Trump Media', 'Truth Social', 'Trump Organization'],
        'emoji': '🇺🇸'
    }
}

# ===== DATABASE SETUP =====
DB_PATH = 'sully_data.db'

def get_db():
    """Get database connection"""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row  # Enable column access by name
    return db

def init_db():
    """Initialize database with schema"""
    db = get_db()
    cursor = db.cursor()

    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )
    ''')

    # User preferences table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            theme TEXT DEFAULT 'dark',
            boston_intensity INTEGER DEFAULT 2,
            voice_enabled BOOLEAN DEFAULT 1,
            voice_rate REAL DEFAULT 0.95,
            voice_pitch REAL DEFAULT 0.85,
            alert_threshold REAL DEFAULT 5.0,
            auto_refresh BOOLEAN DEFAULT 1,
            refresh_interval INTEGER DEFAULT 300,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    # Custom watchlists table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watchlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id),
            UNIQUE(user_id, symbol)
        )
    ''')

    # Conversation history table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            response TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    # Saved briefings table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS briefings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            briefing_type TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    # Achievements table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            achievement_type TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    # User stats table for context awareness
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            total_chats INTEGER DEFAULT 0,
            total_briefings INTEGER DEFAULT 0,
            favorite_stocks TEXT,
            last_active TIMESTAMP,
            streak_days INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    # Portfolio holdings table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            shares REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, symbol),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    db.commit()
    db.close()

def get_or_create_user(username='boss'):
    """Get or create default user"""
    db = get_db()
    cursor = db.cursor()

    # Try to get user
    cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
    user = cursor.fetchone()

    if not user:
        # Create default user
        cursor.execute('INSERT INTO users (username, email) VALUES (?, ?)',
                      (username, f'{username}@rooferdocs.com'))
        user_id = cursor.lastrowid

        # Create default preferences
        cursor.execute('''
            INSERT INTO preferences (user_id) VALUES (?)
        ''', (user_id,))

        db.commit()
        cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
        user = cursor.fetchone()

    db.close()
    return dict(user)

def login_required(f):
    """Decorator to require login for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            # Auto-login as default user for now
            user = get_or_create_user('boss')
            session['user_id'] = user['id']
            session['username'] = user['username']
        return f(*args, **kwargs)
    return decorated_function

# Initialize database on startup
init_db()

# Global state
aggregator = None
sully = None
current_data = None
last_update = None

# ===== NEWS AGGREGATOR =====
class NewsAggregator:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

    def get_stock_data(self, symbols: List[str]) -> Dict[str, Any]:
        stock_data = {}
        for symbol in symbols:
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                params = {'interval': '1d', 'range': '30d'}
                response = self.session.get(url, params=params, timeout=10)

                if response.status_code == 200:
                    data = response.json()
                    result = data['chart']['result'][0]
                    quote = result['meta']
                    current_price = quote.get('regularMarketPrice', 0)
                    previous_close = quote.get('previousClose', 0)
                    change = current_price - previous_close
                    change_percent = (change / previous_close * 100) if previous_close else 0

                    # Extract historical prices for chart
                    history = []
                    if 'indicators' in result and 'quote' in result['indicators']:
                        closes = result['indicators']['quote'][0].get('close', [])
                        history = [price for price in closes if price is not None]

                    stock_data[symbol] = {
                        'symbol': symbol,
                        'price': round(current_price, 2),
                        'change': round(change, 2),
                        'change_percent': round(change_percent, 2),
                        'previous_close': round(previous_close, 2),
                        'volume': quote.get('regularMarketVolume', 0),
                        'history': history[-30:] if history else []  # Last 30 days
                    }
            except Exception as e:
                stock_data[symbol] = {'error': str(e), 'symbol': symbol}
        return stock_data

    def get_full_briefing(self, stock_symbols: List[str]) -> Dict[str, Any]:
        return {
            'stocks': self.get_stock_data(stock_symbols),
            'timestamp': datetime.now().isoformat()
        }

    def search_live_news(self, query: str) -> str:
        """Search for real-time news using News API or web scraping"""
        if NEWS_API_KEY:
            try:
                url = "https://newsapi.org/v2/everything"
                params = {
                    'q': query,
                    'apiKey': NEWS_API_KEY,
                    'language': 'en',
                    'sortBy': 'publishedAt',
                    'pageSize': 5
                }
                response = self.session.get(url, params=params, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    articles = data.get('articles', [])
                    if articles:
                        news_summary = f"\n📰 LATEST NEWS FOR '{query.upper()}':\n\n"
                        for i, article in enumerate(articles[:5], 1):
                            title = article.get('title', 'N/A')
                            source = article.get('source', {}).get('name', 'Unknown')
                            published = article.get('publishedAt', '')
                            news_summary += f"{i}. {title} - {source}\n"
                        return news_summary
            except Exception as e:
                pass

        # Fallback: ESPN/sports scraping for Patriots/Celtics (free, no API needed)
        try:
            if 'patriots' in query.lower() or 'pats' in query.lower():
                url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/ne"
                response = self.session.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    team = data.get('team', {})
                    record = team.get('record', {}).get('items', [{}])[0].get('summary', 'N/A')
                    return f"\n🏈 NEW ENGLAND PATRIOTS\nRecord: {record}\nNote: For today's latest news, check patriots.com/news"

            elif 'celtics' in query.lower():
                url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/bos"
                response = self.session.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    team = data.get('team', {})
                    record = team.get('record', {}).get('items', [{}])[0].get('summary', 'N/A')
                    return f"\n🏀 BOSTON CELTICS\nRecord: {record}\nNote: For today's latest news, check celtics.com"
        except Exception:
            pass

        return f"\n📰 For the latest on {query}, check ESPN.com or team websites!"

    def search_vip_news(self, vip_key: str) -> str:
        """Search for news about Tom Brady, Elon Musk, or Trump"""
        vip = VIP_TRACKING.get(vip_key)
        if not vip:
            return ""

        # Use News API if available
        if NEWS_API_KEY:
            try:
                url = "https://newsapi.org/v2/everything"
                params = {
                    'q': ' OR '.join(vip['keywords']),
                    'apiKey': NEWS_API_KEY,
                    'language': 'en',
                    'sortBy': 'publishedAt',
                    'pageSize': 5
                }
                response = self.session.get(url, params=params, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    articles = data.get('articles', [])
                    if articles:
                        news_summary = f"\n{vip['emoji']} LATEST NEWS - {vip['name'].upper()}:\n\n"
                        for i, article in enumerate(articles[:5], 1):
                            title = article.get('title', 'N/A')
                            source = article.get('source', {}).get('name', 'Unknown')
                            news_summary += f"{i}. {title} - {source}\n"

                        # Add stock info if available
                        if 'stocks' in vip:
                            stock_symbols = vip['stocks']
                            stock_data = self.get_stock_data(stock_symbols)
                            news_summary += f"\n📈 STOCKS:\n"
                            for symbol, data in stock_data.items():
                                if 'error' not in data:
                                    price = data['price']
                                    change_pct = data['change_percent']
                                    indicator = "📈" if change_pct > 0 else "📉"
                                    news_summary += f"{symbol}: ${price:.2f} {indicator} {change_pct:+.2f}%\n"

                        # Add businesses
                        if 'businesses' in vip:
                            news_summary += f"\n🏢 BUSINESSES:\n"
                            for business in vip['businesses']:
                                news_summary += f"• {business}\n"

                        return news_summary
            except Exception as e:
                pass

        # Fallback message
        return f"\n{vip['emoji']} For latest {vip['name']} news, check major news sites!"

# ===== SULLY AI =====
class SullyAI:
    def __init__(self, api_key: str, boston_intensity: int = 7):
        self.client = Groq(api_key=api_key)
        self.boston_intensity = boston_intensity
        self.conversation_history: List[Dict[str, str]] = []
        self.system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        return f"""You are Sully, a knowledgeable AI assistant from Boston. You work for Roof ER (The Roof Docs),
a leading storm restoration roofing company serving Virginia to Pennsylvania (DMV area). You're helping
the boss stay informed about stocks, the Patriots, the Celtics, and fantasy football.

PERSONALITY TRAITS:
- Professional but friendly - you're smart about markets and sports
- Subtle Boston influence: {self.boston_intensity}/10 - Keep it natural and professional, just a hint of personality
- You're passionate about New England sports - Patriots and Celtics fan
- You're proud to work for Roof ER and serve the DMV territory (VA/MD/PA)
- You're helpful and respectful - provide clear, actionable insights

COMMUNICATION STYLE:
- Be conversational but professional
- Use occasional terms like "boss", "looking good", "solid"
- Keep it real and direct - no excessive slang
- Focus on being helpful and informative first
- You can be enthusiastic about sports wins without going overboard

DMV AREA AWARENESS:
- You serve the DMV region (DC/Maryland/Virginia/Pennsylvania)
- Professional knowledge of the roofing and storm restoration business
- Bridge New England sports passion with regional business insights

VIP PERSONALITIES YOU TRACK:
TOM BRADY:
- Patriots legend, 6 Super Bowl rings with New England
- Now retired, working at Fox Sports as analyst
- TB12 Sports (fitness/nutrition) and Brady Brand (clothing)
- Considered one of the greatest QBs of all time

ELON MUSK:
- Tesla CEO (stock symbol: TSLA)
- SpaceX, X (Twitter), Neuralink, Boring Company
- Major market influencer, track TSLA stock movements
- News about Elon often impacts market sentiment

DONALD TRUMP:
- Former President and businessman
- Trump Media & Technology Group (stock: DJT)
- Truth Social platform and Trump Organization
- Major news maker with market impact

ROOFING CONNECTION:
- Storm season drives roofing business
- Roof ER handles hail, wind, storm damage across VA/MD/PA
- Connect weather patterns to business insights when relevant

SPORTS & MARKET INSIGHTS:
- Provide clear, objective market analysis
- Be enthusiastic about Patriots and Celtics but stay professional
- Focus on actionable insights rather than hype
- Keep analysis grounded and realistic

RESPONSE FORMATTING (CRITICAL):
- Break up responses into SHORT paragraphs (2-3 sentences max)
- Use line breaks between thoughts
- NO emojis in stock/financial data (voice reads them)
- Use simple text symbols: UP/DOWN/FLAT instead of emojis
- NO long run-on sentences
- Make it scannable and easy to read on mobile

STOCK DATA FORMATTING:
When discussing stocks, format like this:
"TSLA
Price: $245.50
Change: UP $12.30 (+5.2%)

That's wicked good news, boss!"

NOT like this:
"📊 TSLA: $245.50 📈 +$12.30 (+5.2%) 🚀"

Example Good Format:
"TSLA is showing strong performance today.

TSLA
Price: $245.50
Change: UP $12.30 (+5.2%)
Status: Strong momentum

Good news overall. Worth keeping an eye on earnings next week."

Example Bad Format (DON'T DO THIS):
"Hey boss 📈 TSLA is up 5% today at $245.50 which is up $12.30 🚀 and the volume is looking solid 📊 and the momentum is strong so the market is treating you right today 💰"

Keep it professional and helpful - provide clear insights with easy-to-read formatting."""

    def chat(self, user_message: str, current_data: Dict[str, Any] = None) -> str:
        messages = [{"role": "system", "content": self.system_prompt}]

        if current_data:
            context_parts = [f"CURRENT MARKET DATA: {json.dumps(current_data.get('stocks', {}), indent=2)}"]
            messages.append({"role": "system", "content": f"CURRENT DATA:\n" + "\n\n".join(context_parts)})

        for msg in self.conversation_history[-6:]:
            messages.append({"role": msg["role"], "content": msg["content"]})

        messages.append({"role": "user", "content": user_message})

        response = self.client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.8,
            max_tokens=1500
        )

        reply = response.choices[0].message.content
        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})

        return reply

# ===== HTML TEMPLATE =====
HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0, user-scalable=yes">

    <!-- PWA Meta Tags -->
    <meta name="description" content="Your AI-powered executive assistant for stocks, sports, and insights">
    <meta name="theme-color" content="#c8102e">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="Sully AI">
    <link rel="manifest" href="/manifest.json">
    <link rel="apple-touch-icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'><rect width='512' height='512' rx='128' fill='%233b82f6'/><text x='50%' y='50%' dominant-baseline='middle' text-anchor='middle' font-size='280' fill='white'>📊</text></svg>">

    <title>Sully AI - Executive Dashboard | Roof ER</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📊</text></svg>">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        :root {
            --bg-primary: #1a1a1a;
            --bg-secondary: #2a2a2a;
            --bg-card: rgba(255, 255, 255, 0.05);
            --border-color: rgba(255, 255, 255, 0.1);
            --text-primary: #ffffff;
            --text-secondary: #9ca3af;
            --accent-green: #10b981;
            --accent-red: #c8102e;
            --accent-blue: #3d3d3d;
            --accent-purple: #c8102e;
            --glass-bg: rgba(61, 61, 61, 0.15);
            --glass-border: rgba(200, 16, 46, 0.2);
            --roofer-red: #c8102e;
            --roofer-charcoal: #3d3d3d;
        }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(135deg, #1a1a1a 0%, #2a2a2a 50%, #3d3d3d 100%);
            background-attachment: fixed;
            color: var(--text-primary);
            min-height: 100vh;
            padding: 0;
            margin: 0;
            overflow-x: hidden;
        }

        /* Animated gradient mesh background */
        body::before {
            content: '';
            position: fixed;
            top: -50%;
            left: -50%;
            width: 200%;
            height: 200%;
            background:
                radial-gradient(circle at 20% 80%, rgba(200, 16, 46, 0.15) 0%, transparent 50%),
                radial-gradient(circle at 80% 20%, rgba(61, 61, 61, 0.2) 0%, transparent 50%),
                radial-gradient(circle at 40% 40%, rgba(200, 16, 46, 0.08) 0%, transparent 50%);
            animation: meshMove 20s ease-in-out infinite;
            z-index: 0;
            pointer-events: none;
        }

        @keyframes meshMove {
            0%, 100% { transform: translate(0, 0) rotate(0deg); }
            33% { transform: translate(5%, 5%) rotate(5deg); }
            66% { transform: translate(-5%, 5%) rotate(-5deg); }
        }

        .app-container {
            position: relative;
            width: 100%;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            z-index: 1;
        }

        /* Modern Glassmorphism Header */
        .header {
            background: var(--glass-bg);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid var(--glass-border);
            border-radius: 24px;
            padding: 24px;
            margin-bottom: 24px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
        }

        /* Welcome Message Banner */
        .welcome-banner {
            background: linear-gradient(135deg, var(--roofer-red) 0%, rgba(200, 16, 46, 0.8) 100%);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 2px solid rgba(200, 16, 46, 0.4);
            border-radius: 16px;
            padding: 20px 28px;
            margin-bottom: 24px;
            box-shadow: 0 8px 32px rgba(200, 16, 46, 0.3);
            animation: slideInDown 0.6s ease-out;
        }

        @keyframes slideInDown {
            from {
                opacity: 0;
                transform: translateY(-20px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .welcome-content {
            display: flex;
            align-items: center;
            gap: 16px;
        }

        .welcome-icon {
            font-size: 40px;
            animation: wave 2s ease-in-out infinite;
        }

        @keyframes wave {
            0%, 100% { transform: rotate(0deg); }
            25% { transform: rotate(14deg); }
            75% { transform: rotate(-14deg); }
        }

        .welcome-text {
            flex: 1;
        }

        .welcome-text h2 {
            font-size: 22px;
            font-weight: 700;
            color: white;
            margin-bottom: 6px;
            text-shadow: 0 2px 4px rgba(0, 0, 0, 0.3);
        }

        .welcome-text p {
            font-size: 15px;
            color: rgba(255, 255, 255, 0.95);
            line-height: 1.5;
            text-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
        }

        .roofer-badge {
            background: rgba(255, 255, 255, 0.15);
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 600;
            color: white;
            display: inline-block;
            margin-left: 8px;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
        }

        .header-content {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
        }

        .header-left {
            display: flex;
            align-items: center;
            gap: 16px;
        }

        .header-right {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .logo-section {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 15px;
            margin-bottom: 15px;
            position: relative;
            z-index: 2;
        }

        .logo-circle {
            width: 48px;
            height: 48px;
            background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%);
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 24px;
        }

        .title-section h1 {
            font-size: 24px;
            font-weight: 700;
            margin: 0;
            color: var(--text-primary);
        }

        .subtitle {
            font-size: 14px;
            font-weight: 400;
            color: var(--text-secondary);
            margin-top: 4px;
        }

        /* Portfolio Summary Section */
        .portfolio-summary {
            background: var(--glass-bg);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid var(--glass-border);
            border-radius: 24px;
            padding: 32px;
            margin-bottom: 24px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
        }

        .portfolio-value {
            text-align: center;
            margin-bottom: 24px;
        }

        .portfolio-label {
            font-size: 14px;
            font-weight: 500;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 8px;
        }

        .portfolio-amount {
            font-size: 56px;
            font-weight: 800;
            color: var(--text-primary);
            line-height: 1;
            margin-bottom: 12px;
        }

        .portfolio-change {
            font-size: 20px;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 16px;
            border-radius: 12px;
        }

        .portfolio-change.positive {
            background: rgba(16, 185, 129, 0.1);
            color: var(--accent-green);
        }

        .portfolio-change.negative {
            background: rgba(239, 68, 68, 0.1);
            color: var(--accent-red);
        }

        .portfolio-stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 16px;
            margin-top: 24px;
        }

        .stat-card {
            text-align: center;
            padding: 16px;
            background: rgba(255, 255, 255, 0.02);
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.05);
        }

        .stat-label {
            font-size: 12px;
            color: var(--text-secondary);
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .stat-value {
            font-size: 24px;
            font-weight: 700;
            color: var(--text-primary);
        }

        /* Stock Cards Grid */
        .stocks-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 24px;
        }

        .stock-card {
            background: var(--glass-bg);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid var(--glass-border);
            border-radius: 20px;
            padding: 24px;
            cursor: pointer;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
            overflow: hidden;
        }

        .stock-card:hover {
            transform: translateY(-4px);
            box-shadow: 0 12px 40px rgba(0, 0, 0, 0.4);
            border-color: rgba(255, 255, 255, 0.15);
        }

        .stock-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 16px;
        }

        .stock-symbol {
            font-size: 20px;
            font-weight: 700;
            color: var(--text-primary);
        }

        .stock-price {
            font-size: 32px;
            font-weight: 800;
            color: var(--text-primary);
            margin-bottom: 8px;
        }

        .stock-change {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 12px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 600;
        }

        .stock-change.positive {
            background: rgba(16, 185, 129, 0.15);
            color: var(--accent-green);
        }

        .stock-change.negative {
            background: rgba(239, 68, 68, 0.15);
            color: var(--accent-red);
        }

        .stock-chart {
            height: 120px;
            margin-top: 16px;
            position: relative;
        }

        /* Sparkline for quick trends */
        .sparkline {
            width: 100%;
            height: 60px;
            margin-top: 12px;
        }

        /* Portfolio Holdings */
        .stock-holdings {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-top: 12px;
            padding: 12px;
            background: rgba(255, 255, 255, 0.03);
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.05);
        }

        .holdings-label {
            font-size: 13px;
            color: rgba(255, 255, 255, 0.6);
            font-weight: 600;
        }

        .shares-input {
            flex: 1;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 8px;
            padding: 8px 12px;
            color: white;
            font-size: 14px;
            font-weight: 600;
            transition: all 0.2s;
            min-width: 80px;
            max-width: 120px;
        }

        .shares-input:focus {
            outline: none;
            border-color: var(--roofer-red);
            background: rgba(255, 255, 255, 0.08);
        }

        .save-shares-btn {
            background: var(--roofer-red);
            color: white;
            border: none;
            border-radius: 8px;
            padding: 8px 16px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }

        .save-shares-btn:hover {
            background: #a00d24;
            transform: translateY(-1px);
        }

        .save-shares-btn:active {
            transform: translateY(0);
        }

        .holdings-value {
            font-size: 13px;
            font-weight: 700;
            color: var(--roofer-red);
        }

        /* Chat Section */
        .chat-section {
            background: var(--glass-bg);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid var(--glass-border);
            border-radius: 24px;
            overflow: hidden;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
        }

        .chat-header {
            padding: 20px 24px;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .chat-header h2 {
            font-size: 18px;
            font-weight: 600;
            color: var(--text-primary);
            margin: 0;
        }

        .chat-area {
            min-height: 300px;
            max-height: 400px;
            overflow-y: auto;
            padding: 24px;
        }

        /* Quick Actions */
        .quick-actions {
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            margin-bottom: 24px;
        }

        .action-pill {
            padding: 12px 20px;
            border-radius: 12px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            border: 1px solid var(--border-color);
            background: var(--glass-bg);
            backdrop-filter: blur(10px);
            color: var(--text-primary);
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }

        .action-pill:hover {
            transform: translateY(-2px) scale(1.02);
            border-color: var(--accent-blue);
            box-shadow: 0 8px 24px rgba(59, 130, 246, 0.2);
            background: rgba(59, 130, 246, 0.1);
        }

        .action-pill:active {
            transform: translateY(0) scale(0.98);
        }

        /* Insights & Alerts Section */
        .insights-section {
            background: var(--glass-bg);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid var(--glass-border);
            border-radius: 24px;
            padding: 24px;
            margin-bottom: 24px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
        }

        .insights-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }

        .insights-header h2 {
            font-size: 20px;
            font-weight: 700;
            color: var(--text-primary);
            margin: 0;
        }

        .briefing-button {
            padding: 10px 20px;
            border-radius: 12px;
            background: linear-gradient(135deg, var(--accent-blue) 0%, var(--accent-purple) 100%);
            border: none;
            color: white;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
        }

        .briefing-button:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 24px rgba(59, 130, 246, 0.4);
        }

        /* Icon Button */
        .icon-button {
            width: 40px;
            height: 40px;
            border-radius: 12px;
            background: var(--glass-bg);
            border: 1px solid var(--glass-border);
            color: var(--text-primary);
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.3s;
        }

        .icon-button:hover {
            background: rgba(255, 255, 255, 0.1);
            transform: translateY(-2px);
            box-shadow: 0 8px 16px rgba(0, 0, 0, 0.3);
        }

        /* Settings & History Modals */
        .modal-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.7);
            backdrop-filter: blur(10px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }

        .modal-overlay.active {
            display: flex;
        }

        .modal-content {
            background: var(--glass-bg);
            backdrop-filter: blur(20px);
            border: 1px solid var(--glass-border);
            border-radius: 24px;
            padding: 32px;
            max-width: 600px;
            width: 90%;
            max-height: 80vh;
            overflow-y: auto;
            box-shadow: 0 24px 64px rgba(0, 0, 0, 0.5);
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
        }

        .modal-title {
            font-size: 24px;
            font-weight: 700;
            color: var(--text-primary);
        }

        .close-button {
            width: 32px;
            height: 32px;
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.05);
            border: none;
            color: var(--text-secondary);
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s;
        }

        .close-button:hover {
            background: rgba(255, 255, 255, 0.1);
            color: var(--text-primary);
        }

        .setting-group {
            margin-bottom: 24px;
        }

        .setting-label {
            font-size: 14px;
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 8px;
            display: block;
        }

        .setting-description {
            font-size: 12px;
            color: var(--text-secondary);
            margin-bottom: 12px;
        }

        .setting-input {
            width: 100%;
            padding: 12px;
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            font-size: 14px;
        }

        .setting-input:focus {
            outline: none;
            border-color: var(--accent-blue);
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
        }

        .setting-range {
            width: 100%;
            height: 6px;
            border-radius: 3px;
            background: rgba(255, 255, 255, 0.1);
            outline: none;
            -webkit-appearance: none;
        }

        .setting-range::-webkit-slider-thumb {
            -webkit-appearance: none;
            width: 20px;
            height: 20px;
            border-radius: 50%;
            background: var(--accent-blue);
            cursor: pointer;
        }

        .setting-toggle {
            position: relative;
            width: 48px;
            height: 24px;
            background: rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            cursor: pointer;
            transition: all 0.3s;
        }

        .setting-toggle.active {
            background: var(--accent-green);
        }

        .setting-toggle::after {
            content: '';
            position: absolute;
            top: 2px;
            left: 2px;
            width: 20px;
            height: 20px;
            border-radius: 50%;
            background: white;
            transition: all 0.3s;
        }

        .setting-toggle.active::after {
            transform: translateX(24px);
        }

        .save-button {
            width: 100%;
            padding: 14px;
            border-radius: 12px;
            background: linear-gradient(135deg, var(--accent-blue) 0%, var(--accent-purple) 100%);
            border: none;
            color: white;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
        }

        .save-button:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 24px rgba(59, 130, 246, 0.4);
        }

        .history-item {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 12px;
        }

        .history-timestamp {
            font-size: 12px;
            color: var(--text-secondary);
            margin-bottom: 8px;
        }

        .history-message {
            font-size: 14px;
            color: var(--accent-blue);
            margin-bottom: 8px;
        }

        .history-response {
            font-size: 14px;
            color: var(--text-primary);
            line-height: 1.6;
        }

        /* Alerts Banner */
        .alerts-banner {
            background: linear-gradient(135deg, rgba(239, 68, 68, 0.1) 0%, rgba(220, 38, 38, 0.05) 100%);
            border: 1px solid rgba(239, 68, 68, 0.3);
            border-radius: 16px;
            padding: 16px 20px;
            margin-bottom: 16px;
            display: none;
        }

        .alerts-banner.active {
            display: block;
            animation: slideDown 0.4s ease;
        }

        @keyframes slideDown {
            from {
                opacity: 0;
                transform: translateY(-10px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .alert-item {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 12px;
            background: rgba(255, 255, 255, 0.03);
            border-radius: 12px;
            margin-bottom: 8px;
        }

        .alert-item:last-child {
            margin-bottom: 0;
        }

        .alert-icon {
            font-size: 24px;
            flex-shrink: 0;
        }

        .alert-content {
            flex: 1;
        }

        .alert-message {
            font-size: 14px;
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 4px;
        }

        .alert-severity {
            font-size: 12px;
            color: var(--text-secondary);
        }

        /* Insight Cards */
        .insights-grid {
            display: grid;
            gap: 12px;
        }

        .insight-card {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 16px;
            transition: all 0.3s;
        }

        .insight-card:hover {
            background: rgba(255, 255, 255, 0.04);
            border-color: var(--accent-blue);
        }

        .insight-card.positive {
            border-left: 4px solid var(--accent-green);
        }

        .insight-card.negative {
            border-left: 4px solid var(--accent-red);
        }

        .insight-card.neutral {
            border-left: 4px solid var(--text-secondary);
        }

        .insight-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
        }

        .insight-symbol {
            font-size: 14px;
            font-weight: 700;
            color: var(--accent-blue);
        }

        .insight-type {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-secondary);
        }

        .insight-message {
            font-size: 14px;
            color: var(--text-primary);
            margin-bottom: 8px;
            line-height: 1.5;
        }

        .insight-action {
            font-size: 12px;
            color: var(--accent-blue);
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 4px;
        }

        /* Briefing Modal */
        .briefing-modal {
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.7);
            backdrop-filter: blur(10px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 9999;
            padding: 20px;
        }

        .briefing-modal.active {
            display: flex;
            animation: fadeIn 0.3s ease;
        }

        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }

        .briefing-content {
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 24px;
            padding: 32px;
            max-width: 600px;
            width: 100%;
            max-height: 80vh;
            overflow-y: auto;
            position: relative;
        }

        .briefing-close {
            position: absolute;
            top: 20px;
            right: 20px;
            background: rgba(255, 255, 255, 0.1);
            border: none;
            width: 36px;
            height: 36px;
            border-radius: 50%;
            color: var(--text-primary);
            font-size: 20px;
            cursor: pointer;
            transition: all 0.3s;
        }

        .briefing-close:hover {
            background: rgba(255, 255, 255, 0.2);
            transform: rotate(90deg);
        }

        .briefing-title {
            font-size: 24px;
            font-weight: 700;
            color: var(--text-primary);
            margin-bottom: 8px;
        }

        .briefing-subtitle {
            font-size: 14px;
            color: var(--text-secondary);
            margin-bottom: 24px;
        }

        .briefing-text {
            font-size: 15px;
            line-height: 1.7;
            color: var(--text-primary);
            white-space: pre-wrap;
        }

        /* Responsive Design */
        @media (max-width: 768px) {
            .portfolio-amount {
                font-size: 40px;
            }

            .stocks-grid {
                grid-template-columns: 1fr;
            }

            .portfolio-stats {
                grid-template-columns: 1fr 1fr;
            }

            .header-content {
                flex-direction: column;
                align-items: flex-start;
            }

            .briefing-content {
                padding: 24px;
            }
        }

        /* Message Bubble */
        .message {
            display: flex;
            gap: 12px;
            margin-bottom: 20px;
            animation: slideIn 0.3s ease;
        }

        @keyframes slideIn {
            from {
                opacity: 0;
                transform: translateY(10px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .message.user {
            flex-direction: row-reverse;
        }

        .avatar {
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: linear-gradient(135deg, #c41e3a, #8b1528);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
            flex-shrink: 0;
            border: 2px solid #1a1a1a;
        }

        .message.user .avatar {
            background: linear-gradient(135deg, #6b7280, #4b5563);
        }

        .message-content {
            flex: 1;
        }

        .message-name {
            font-size: 12px;
            font-weight: 700;
            color: #c41e3a;
            margin-bottom: 5px;
        }

        .message.user .message-name {
            color: #9ca3af;
            text-align: right;
        }

        .message-bubble {
            background: linear-gradient(135deg, #1a1a1a 0%, #262626 100%);
            padding: 15px 18px;
            border-radius: 18px;
            border: 1px solid #2a2a2a;
            font-size: 14px;
            line-height: 1.6;
            color: #e4e4e7;
        }

        .message.user .message-bubble {
            background: linear-gradient(135deg, #c41e3a, #8b1528);
            color: white;
            border-color: #c41e3a;
        }

        /* Input Section */
        .input-section {
            padding: 20px;
            background: #0a0a0a;
            border-top: 2px solid #1a1a1a;
            display: flex;
            gap: 12px;
            align-items: center;
        }

        .input-wrapper {
            flex: 1;
            position: relative;
        }

        .input-field {
            width: 100%;
            padding: 14px 20px;
            background: #1a1a1a;
            border: 2px solid #2a2a2a;
            border-radius: 25px;
            color: #ffffff;
            font-size: 14px;
            font-family: 'Inter', sans-serif;
            transition: all 0.3s ease;
        }

        .input-field:focus {
            outline: none;
            border-color: #c41e3a;
            background: #262626;
        }

        .input-field::placeholder {
            color: #6b7280;
        }

        .mic-button {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            background: linear-gradient(135deg, #16a34a 0%, #15803d 100%);
            border: none;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
            transition: all 0.3s ease;
            box-shadow: 0 4px 15px rgba(22, 163, 74, 0.3);
        }

        .mic-button:hover {
            transform: scale(1.05);
            box-shadow: 0 6px 20px rgba(22, 163, 74, 0.5);
        }

        .mic-button:active {
            transform: scale(0.95);
        }

        .mic-button.listening {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
            animation: pulse 1.5s ease-in-out infinite;
        }

        .stop-button {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
            border: none;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
            transition: all 0.3s ease;
            box-shadow: 0 4px 15px rgba(239, 68, 68, 0.3);
            animation: pulse-soft 2s ease-in-out infinite;
        }

        .stop-button:hover {
            transform: scale(1.05);
            box-shadow: 0 6px 20px rgba(239, 68, 68, 0.5);
        }

        .stop-button:active {
            transform: scale(0.95);
        }

        @keyframes pulse {
            0%, 100% { transform: scale(1); }
            50% { transform: scale(1.1); }
        }

        @keyframes pulse-soft {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.8; }
        }

        .send-button {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            background: linear-gradient(135deg, #c41e3a 0%, #8b1528 100%);
            border: none;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
            transition: all 0.3s ease;
            box-shadow: 0 4px 15px rgba(196, 30, 58, 0.3);
        }

        .send-button:hover {
            transform: scale(1.05);
            box-shadow: 0 6px 20px rgba(196, 30, 58, 0.5);
        }

        .send-button:active {
            transform: scale(0.95);
        }

        .send-button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        .voice-status {
            position: absolute;
            bottom: 100px;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(0, 0, 0, 0.9);
            color: #16a34a;
            padding: 10px 20px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            display: none;
            z-index: 100;
        }

        .voice-status.active {
            display: block;
            animation: fadeIn 0.3s ease;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateX(-50%) translateY(10px); }
            to { opacity: 1; transform: translateX(-50%) translateY(0); }
        }

        /* Scrollbar */
        .chat-area::-webkit-scrollbar {
            width: 6px;
        }

        .chat-area::-webkit-scrollbar-track {
            background: transparent;
        }

        .chat-area::-webkit-scrollbar-thumb {
            background: #2a2a2a;
            border-radius: 3px;
        }

        .chat-area::-webkit-scrollbar-thumb:hover {
            background: #3a3a3a;
        }

        .pill-icon {
            font-size: 16px;
        }

        .loading {
            display: none;
            text-align: center;
            padding: 15px;
            color: #c41e3a;
            font-style: italic;
            font-weight: 600;
        }

        .loading.active {
            display: block;
        }

        /* Pull-to-Refresh Indicator */
        .pull-to-refresh {
            position: fixed;
            top: -60px;
            left: 50%;
            transform: translateX(-50%);
            width: 60px;
            height: 60px;
            background: var(--glass-bg);
            backdrop-filter: blur(20px);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.3s;
            z-index: 999;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
        }

        .pull-to-refresh.active {
            top: 20px;
        }

        .pull-to-refresh.refreshing {
            animation: spin 1s linear infinite;
        }

        @keyframes spin {
            from { transform: translateX(-50%) rotate(0deg); }
            to { transform: translateX(-50%) rotate(360deg); }
        }

        /* PWA Install Banner */
        .install-banner {
            position: fixed;
            bottom: -100px;
            left: 50%;
            transform: translateX(-50%);
            width: 90%;
            max-width: 400px;
            background: var(--glass-bg);
            backdrop-filter: blur(20px);
            border: 1px solid var(--glass-border);
            border-radius: 16px;
            padding: 16px;
            display: flex;
            align-items: center;
            gap: 12px;
            transition: bottom 0.3s ease-out;
            z-index: 1001;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
        }

        .install-banner.show {
            bottom: 20px;
        }

        .install-icon {
            font-size: 32px;
        }

        .install-text {
            flex: 1;
        }

        .install-text h4 {
            font-size: 14px;
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 4px;
        }

        .install-text p {
            font-size: 12px;
            color: var(--text-secondary);
        }

        .install-button {
            padding: 8px 16px;
            border-radius: 8px;
            background: var(--accent-blue);
            border: none;
            color: white;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            white-space: nowrap;
        }

        .close-install {
            padding: 8px;
            background: none;
            border: none;
            color: var(--text-secondary);
            cursor: pointer;
            font-size: 20px;
            line-height: 1;
        }

        /* Mobile-Responsive Design */
        @media (max-width: 768px) {
            .app-container {
                padding: 12px;
            }

            .header {
                padding: 16px;
                border-radius: 16px;
                margin-bottom: 16px;
            }

            .welcome-banner {
                padding: 16px 20px;
                border-radius: 12px;
                margin-bottom: 16px;
            }

            .welcome-icon {
                font-size: 32px;
            }

            .welcome-text h2 {
                font-size: 18px;
            }

            .welcome-text p {
                font-size: 14px;
            }

            .roofer-badge {
                display: block;
                margin-left: 0;
                margin-top: 6px;
                width: fit-content;
            }

            .header-content {
                flex-direction: column;
                align-items: flex-start;
            }

            .header-right {
                width: 100%;
                justify-content: space-between;
                margin-top: 12px;
            }

            .logo-circle {
                width: 40px;
                height: 40px;
                font-size: 20px;
            }

            h1 {
                font-size: 20px;
            }

            .subtitle {
                font-size: 12px;
            }

            .portfolio-summary {
                padding: 20px;
                border-radius: 16px;
                margin-bottom: 16px;
            }

            .portfolio-amount {
                font-size: 36px;
            }

            .portfolio-change {
                font-size: 14px;
            }

            .portfolio-stats {
                flex-direction: column;
                gap: 12px;
            }

            .stat-card {
                padding: 12px;
            }

            .stat-value {
                font-size: 18px;
            }

            .stat-label {
                font-size: 11px;
            }

            .stocks-grid {
                grid-template-columns: 1fr;
                gap: 12px;
                margin-bottom: 16px;
            }

            .stock-card {
                padding: 16px;
            }

            .insights-section {
                padding: 20px;
                border-radius: 16px;
                margin-bottom: 16px;
            }

            .insights-header h2 {
                font-size: 18px;
            }

            .briefing-button {
                padding: 8px 16px;
                font-size: 12px;
            }

            .chat-section {
                padding: 16px;
                border-radius: 16px;
            }

            .quick-actions {
                gap: 8px;
                margin-bottom: 12px;
                flex-wrap: nowrap;
                overflow-x: auto;
                padding-bottom: 8px;
            }

            .quick-btn {
                padding: 8px 16px;
                font-size: 12px;
                white-space: nowrap;
                flex-shrink: 0;
            }

            .chat-messages {
                max-height: 300px;
                margin-bottom: 12px;
            }

            .message {
                padding: 12px;
                font-size: 14px;
            }

            .input-section {
                gap: 8px;
            }

            .mic-button,
            .stop-button,
            .send-button {
                width: 44px;
                height: 44px;
                font-size: 18px;
            }

            .input-field {
                padding: 12px 16px;
                font-size: 14px;
            }

            .modal-content {
                width: 95%;
                padding: 24px;
                max-height: 85vh;
            }

            .modal-title {
                font-size: 20px;
            }

            .setting-group {
                margin-bottom: 20px;
            }

            .history-item {
                padding: 12px;
            }

            .icon-button {
                width: 36px;
                height: 36px;
            }
        }

        /* Extra small devices */
        @media (max-width: 480px) {
            .app-container {
                padding: 8px;
            }

            .header {
                padding: 12px;
            }

            h1 {
                font-size: 18px;
            }

            .portfolio-amount {
                font-size: 28px;
            }

            .stock-symbol {
                font-size: 16px;
            }

            .stock-price {
                font-size: 18px;
            }

            .quick-actions {
                gap: 6px;
            }

            .quick-btn {
                padding: 6px 12px;
                font-size: 11px;
            }
        }

        /* Landscape mode on mobile */
        @media (max-height: 500px) and (orientation: landscape) {
            .chat-messages {
                max-height: 150px;
            }

            .portfolio-summary {
                display: none;
            }

            .insights-section {
                display: none;
            }
        }

        /* Touch-friendly improvements */
        @media (hover: none) and (pointer: coarse) {
            .stock-card,
            .quick-btn,
            .mic-button,
            .send-button,
            .icon-button,
            .briefing-button {
                min-height: 44px;
                min-width: 44px;
            }

            .input-field {
                font-size: 16px; /* Prevent zoom on iOS */
            }

            .stock-card:active {
                transform: scale(0.98);
            }

            .quick-btn:active,
            .mic-button:active,
            .send-button:active,
            .icon-button:active,
            .briefing-button:active {
                transform: scale(0.95);
            }
        }

        /* Safe area insets for notched devices */
        @supports (padding: max(0px)) {
            body {
                padding-top: max(0px, env(safe-area-inset-top));
                padding-bottom: max(0px, env(safe-area-inset-bottom));
                padding-left: max(0px, env(safe-area-inset-left));
                padding-right: max(0px, env(safe-area-inset-right));
            }

            .install-banner.show {
                bottom: max(20px, env(safe-area-inset-bottom));
            }
        }

    </style>
</head>
<body>
    <!-- Pull-to-Refresh Indicator -->
    <div class="pull-to-refresh" id="pull-indicator">
        🔄
    </div>

    <!-- PWA Install Banner -->
    <div class="install-banner" id="install-banner">
        <div class="install-icon">📱</div>
        <div class="install-text">
            <h4>Install Sully AI</h4>
            <p>Add to home screen for quick access</p>
        </div>
        <button class="install-button" id="install-btn">Install</button>
        <button class="close-install" id="close-install">×</button>
    </div>

    <div class="app-container">
        <!-- Modern Header -->
        <div class="header">
            <div class="header-content">
                <div class="header-left">
                    <div class="logo-circle">📊</div>
                    <div class="title-section">
                        <h1>Sully AI</h1>
                        <div class="subtitle">Executive Dashboard</div>
                    </div>
                </div>
                <div class="header-right">
                    <div class="subtitle" id="last-update">Updated: Just now</div>
                    <button class="icon-button" onclick="openHistory()" title="Conversation History">
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
                            <path d="M3 3v5h5"/>
                            <path d="M12 7v5l4 2"/>
                        </svg>
                    </button>
                    <button class="icon-button" onclick="openSettings()" title="Settings">
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <circle cx="12" cy="12" r="3"/>
                            <path d="M12 1v6m0 6v6M5.64 5.64l4.24 4.24m4.24 4.24l4.24 4.24M1 12h6m6 0h6m-14.36-.36l4.24 4.24m4.24-4.24l4.24 4.24"/>
                        </svg>
                    </button>
                </div>
            </div>
        </div>

        <!-- Daily Motivation for Oliver Brown -->
        <div class="welcome-banner">
            <div class="welcome-content">
                <div class="welcome-icon" id="daily-icon">💼</div>
                <div class="welcome-text">
                    <h2 id="daily-greeting">Welcome, Oliver Brown! <span class="roofer-badge">RoofER Owner</span></h2>
                    <p id="daily-quote">Loading today's message...</p>
                </div>
            </div>
        </div>

        <!-- Portfolio Summary Dashboard -->
        <div class="portfolio-summary">
            <div class="portfolio-value">
                <div class="portfolio-label">Total Portfolio Value</div>
                <div class="portfolio-amount" id="portfolio-total">Loading...</div>
                <div class="portfolio-change positive" id="portfolio-change">
                    <span>↑</span>
                    <span>+$0 (0%)</span>
                </div>
            </div>
            <div class="portfolio-stats">
                <div class="stat-card">
                    <div class="stat-label">Today's Gain</div>
                    <div class="stat-value" id="today-gain">$0</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Best Performer</div>
                    <div class="stat-value" id="best-stock">—</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Total Stocks</div>
                    <div class="stat-value" id="stock-count">8</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Market Status</div>
                    <div class="stat-value" id="market-status">Open</div>
                </div>
            </div>
        </div>

        <!-- Alerts Banner -->
        <div class="alerts-banner" id="alerts-banner">
            <!-- Alerts will be inserted here -->
        </div>

        <!-- AI Insights Section -->
        <div class="insights-section">
            <div class="insights-header">
                <h2>🧠 AI Insights</h2>
                <button class="briefing-button" onclick="generateBriefing()">
                    <span>📋</span> Get Daily Briefing
                </button>
            </div>
            <div class="insights-grid" id="insights-grid">
                <div style="text-align: center; color: var(--text-secondary); padding: 20px;">
                    Loading insights...
                </div>
            </div>
        </div>

        <!-- Quick Actions -->
        <div class="quick-actions">
            <button class="action-pill" onclick="loadStockData()">
                <span>🔄</span> Refresh Data
            </button>
            <button class="action-pill" onclick="loadInsights()">
                <span>🧠</span> Refresh Insights
            </button>
            <button class="action-pill" onclick="sendQuick('How are the Celtics doing?')">
                <span>🍀</span> Celtics
            </button>
            <button class="action-pill" onclick="sendQuick('What\\'s the latest Patriots news?')">
                <span>🏈</span> Patriots
            </button>
        </div>

        <!-- Stock Cards Grid -->
        <div class="stocks-grid" id="stocks-grid">
            <!-- Stock cards will be dynamically inserted here -->
        </div>

        <!-- Briefing Modal -->
        <div class="briefing-modal" id="briefing-modal" onclick="closeBriefing(event)">
            <div class="briefing-content" onclick="event.stopPropagation()">
                <button class="briefing-close" onclick="closeBriefing()">×</button>
                <div class="briefing-title" id="briefing-title">Daily Briefing</div>
                <div class="briefing-subtitle" id="briefing-subtitle">Generated just now</div>
                <div class="briefing-text" id="briefing-text">Loading...</div>
            </div>
        </div>

        <!-- Chat Section -->
        <div class="chat-section">
            <div class="chat-header">
                <span>💬</span>
                <h2>Ask Sully</h2>
            </div>
            <div class="chat-area" id="messages">
                <div class="message">
                    <div class="avatar">🎩</div>
                    <div class="message-content">
                        <div class="message-name">Sully</div>
                        <div class="message-bubble">Hey there! I'm Sully, your executive assistant. I've loaded your portfolio dashboard above. What would you like to know?</div>
                    </div>
                </div>
            </div>

            <!-- Loading Indicator -->
            <div class="loading" id="loading">🎩 Sully is thinking...</div>

            <!-- Voice Status Indicator -->
            <div class="voice-status" id="voice-status">🎤 Listening...</div>

            <!-- Input Section -->
            <div class="input-section">
                <button class="mic-button" id="mic-btn" onclick="toggleVoice()" title="Click to talk">🎤</button>
                <button class="stop-button" id="stop-btn" onclick="stopSpeaking()" title="Stop speaking" style="display: none;">🔇</button>
                <div class="input-wrapper">
                    <input type="text" id="user-input" class="input-field" placeholder="Ask Sully anything or click 🎤 to talk..." onkeypress="handleKeyPress(event)" autocomplete="off">
                </div>
                <button class="send-button" id="send-btn" onclick="sendMessage()">➤</button>
            </div>
        </div>
    </div>

    <!-- Settings Modal -->
    <div class="modal-overlay" id="settings-modal">
        <div class="modal-content">
            <div class="modal-header">
                <div class="modal-title">⚙️ Settings</div>
                <button class="close-button" onclick="closeSettings()">×</button>
            </div>

            <div class="setting-group">
                <label class="setting-label">Boston Intensity</label>
                <div class="setting-description">Control how much Boston personality Sully shows (1-10)</div>
                <input type="range" class="setting-range" id="boston-intensity" min="1" max="10" value="2" oninput="document.getElementById('boston-value').textContent = this.value">
                <div style="text-align: center; margin-top: 8px; color: var(--text-secondary);">
                    <span id="boston-value">2</span> / 10
                </div>
            </div>

            <div class="setting-group">
                <label class="setting-label">Voice Rate</label>
                <div class="setting-description">How fast Sully speaks (0.5 = slow, 1.5 = fast)</div>
                <input type="range" class="setting-range" id="voice-rate" min="0.5" max="1.5" step="0.05" value="0.95" oninput="document.getElementById('rate-value').textContent = this.value">
                <div style="text-align: center; margin-top: 8px; color: var(--text-secondary);">
                    <span id="rate-value">0.95</span>x
                </div>
            </div>

            <div class="setting-group">
                <label class="setting-label">Voice Pitch</label>
                <div class="setting-description">How deep Sully's voice is (0.5 = deep, 2.0 = high)</div>
                <input type="range" class="setting-range" id="voice-pitch" min="0.5" max="2.0" step="0.05" value="0.85" oninput="document.getElementById('pitch-value').textContent = this.value">
                <div style="text-align: center; margin-top: 8px; color: var(--text-secondary);">
                    <span id="pitch-value">0.85</span>
                </div>
            </div>

            <div class="setting-group">
                <label class="setting-label">Alert Threshold</label>
                <div class="setting-description">Show alerts when stocks move more than this percentage</div>
                <input type="range" class="setting-range" id="alert-threshold" min="1" max="10" step="0.5" value="5.0" oninput="document.getElementById('alert-value').textContent = this.value">
                <div style="text-align: center; margin-top: 8px; color: var(--text-secondary);">
                    <span id="alert-value">5.0</span>%
                </div>
            </div>

            <div class="setting-group">
                <label class="setting-label">Voice Enabled</label>
                <div class="setting-description">Enable text-to-speech responses</div>
                <div class="setting-toggle active" id="voice-enabled" onclick="toggleSetting(this)"></div>
            </div>

            <div class="setting-group">
                <label class="setting-label">Auto Refresh</label>
                <div class="setting-description">Automatically refresh stock data</div>
                <div class="setting-toggle active" id="auto-refresh" onclick="toggleSetting(this)"></div>
            </div>

            <button class="save-button" onclick="saveSettings()">💾 Save Settings</button>
        </div>
    </div>

    <!-- History Modal -->
    <div class="modal-overlay" id="history-modal">
        <div class="modal-content">
            <div class="modal-header">
                <div class="modal-title">🕐 Conversation History</div>
                <button class="close-button" onclick="closeHistory()">×</button>
            </div>
            <div id="history-list">
                <div style="text-align: center; padding: 40px; color: var(--text-secondary);">
                    Loading history...
                </div>
            </div>
        </div>
    </div>

    <script>
        // Server TTS flag injected by Flask
        window.SERVER_TTS_ENABLED = {{ 'true' if server_tts else 'false' }};

        // Daily Owner Motivation System - Sports Focused
        const dailyQuotes = [
            // Game Day Mentality
            { icon: "🏈", greeting: "Fourth Quarter", quote: "Winners are made in the fourth quarter. Time to execute." },
            { icon: "⚾", greeting: "At Bat", quote: "Step up to the plate. Control what you can control. Win your at-bats." },
            { icon: "🏀", greeting: "Clutch Time", quote: "Great players demand the ball in crunch time. Make your shots count." },
            { icon: "🥊", greeting: "Round by Round", quote: "Boxing is won round by round. So is business. Win today." },
            { icon: "🏁", greeting: "Race Mode", quote: "The race is long. Stay focused on your lane. Finish first." },

            // Competition & Performance
            { icon: "🎯", greeting: "Lock In", quote: "Distractions lose games. Lock in on what matters. Execute." },
            { icon: "💪", greeting: "Training Day", quote: "Champions train when others rest. You show up. That's the difference." },
            { icon: "🔥", greeting: "Heat Check", quote: "Momentum is real. RoofER has it. Keep pushing the advantage." },
            { icon: "⚡", greeting: "Fast Break", quote: "Speed kills in sports and business. Move faster than the competition." },
            { icon: "🏆", greeting: "Championship DNA", quote: "Championships are won in preparation. You put in the work." },

            // Strategy & Execution
            { icon: "♟️", greeting: "Next Move", quote: "Think three moves ahead. Your strategy separates you from the pack." },
            { icon: "📊", greeting: "Film Study", quote: "Winners study the game. Your data is your film. Use it." },
            { icon: "🎲", greeting: "Calculated Play", quote: "Smart risks win games. Stupid risks lose seasons. Know the difference." },
            { icon: "🧭", greeting: "Game Plan", quote: "Stick to the game plan. Adjust when needed. Never panic." },
            { icon: "⚙️", greeting: "System Check", quote: "Champions build systems. Your infrastructure wins games for you." },

            // Team & Leadership
            { icon: "🤝", greeting: "Team First", quote: "Great teams trust each other. RoofER Family backs you up." },
            { icon: "🛡️", greeting: "Got Your Back", quote: "Your team protects you. You protect them. That's how dynasties work." },
            { icon: "📣", greeting: "Captain Mode", quote: "Lead from the front. Your team follows your energy." },
            { icon: "🌊", greeting: "Rising Tide", quote: "Lift your team up. Winning teams win together." },
            { icon: "💼", greeting: "Owner Mentality", quote: "You own this. Act like it. That mindset changes everything." },

            // Market & Money
            { icon: "💹", greeting: "Market Watch", quote: "Bulls make money. Bears make money. Pigs get slaughtered. Stay sharp." },
            { icon: "📈", greeting: "Upside Play", quote: "Your portfolio reflects your conviction. Back your winners." },
            { icon: "💰", greeting: "Capital Moves", quote: "Money follows performance. Perform and the money comes." },
            { icon: "⏰", greeting: "Prime Time", quote: "Your time is your most valuable asset. Invest it wisely." },
            { icon: "🎖️", greeting: "Earned Respect", quote: "Respect is earned through results. You earn yours daily." },

            // Intensity & Focus
            { icon: "🔨", greeting: "Grind Mode", quote: "Building something real takes relentless effort. Keep hammering." },
            { icon: "🎾", greeting: "Point by Point", quote: "Win the point in front of you. Championships follow." },
            { icon: "🏋️", greeting: "Weight Room", quote: "Strength compounds daily. So does your business." },
            { icon: "🚀", greeting: "Launch Ready", quote: "RoofER is positioned to dominate. Time to take off." },
            { icon: "⚔️", greeting: "Battle Ready", quote: "Competition never sleeps. Neither should your edge." },

            // Execution & Results
            { icon: "✅", greeting: "Execute", quote: "Plans are worthless without execution. Make it happen today." },
            { icon: "🌅", greeting: "Day One", quote: "Every morning is game day. Come ready to compete." },
            { icon: "📱", greeting: "Live Feed", quote: "Real-time data. Real-time decisions. That's your advantage." },
            { icon: "🧠", greeting: "Mental Edge", quote: "The mental game separates good from great. Stay focused." },
            { icon: "🎪", greeting: "Show Time", quote: "This is your stage. Perform like you own it. Because you do." }
        ];

        // Get quote of the day based on current date
        function getDailyQuote() {
            const today = new Date();
            const dayOfYear = Math.floor((today - new Date(today.getFullYear(), 0, 0)) / 1000 / 60 / 60 / 24);
            const quoteIndex = dayOfYear % dailyQuotes.length;
            return dailyQuotes[quoteIndex];
        }

        // Update welcome banner with daily quote
        function updateDailyMotivation() {
            const dailyContent = getDailyQuote();
            document.getElementById('daily-icon').textContent = dailyContent.icon;
            document.getElementById('daily-greeting').innerHTML = `${dailyContent.greeting}, Oliver! <span class="roofer-badge">RoofER Owner</span>`;
            document.getElementById('daily-quote').textContent = dailyContent.quote;
        }

        // Dashboard Stock Data Management
        let stockData = {};
        let charts = {};
        let portfolio = {}; // User's actual holdings {symbol: shares}

        // Load portfolio holdings
        async function loadPortfolio() {
            try {
                const response = await fetch('/api/portfolio');
                if (response.ok) {
                    const data = await response.json();
                    portfolio = data.holdings || {};
                }
            } catch (error) {
                console.error('Error loading portfolio:', error);
                portfolio = {};
            }
        }

        // Save portfolio holding
        async function saveHolding(symbol, shares) {
            try {
                const response = await fetch('/api/portfolio', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({symbol, shares})
                });

                if (response.ok) {
                    portfolio[symbol] = shares;
                    renderDashboard(); // Re-render with new values
                }
            } catch (error) {
                console.error('Error saving holding:', error);
            }
        }

        // Load stock data and populate dashboard
        async function loadStockData() {
            try {
                const response = await fetch('/api/stocks');

                if (!response.ok) {
                    console.error('Failed to load stock data');
                    return;
                }

                const data = await response.json();

                // Convert stocks object to our format
                stockData = {};
                Object.keys(data.stocks).forEach(symbol => {
                    const stock = data.stocks[symbol];

                    // Skip stocks with errors
                    if (stock.error) {
                        console.error(`Error loading ${symbol}:`, stock.error);
                        return;
                    }

                    stockData[symbol] = {
                        symbol: stock.symbol,
                        price: parseFloat(stock.price).toFixed(2),
                        change: parseFloat(stock.change).toFixed(2),
                        change_percent: parseFloat(stock.change_percent).toFixed(2),
                        history: stock.history || []
                    };
                });

                renderDashboard();
                updateLastUpdate();
            } catch (error) {
                console.error('Error loading stock data:', error);
            }
        }

        // Render the entire dashboard
        function renderDashboard() {
            renderPortfolioSummary();
            renderStockCards();
        }

        // Calculate and render portfolio summary
        function renderPortfolioSummary() {
            let totalValue = 0;
            let totalChange = 0;
            let totalChangePercent = 0;
            let bestStock = { symbol: '—', change: -Infinity };

            Object.values(stockData).forEach(stock => {
                const shares = portfolio[stock.symbol] || 0; // Use actual holdings
                const stockValue = parseFloat(stock.price) * shares;
                const stockChange = parseFloat(stock.change) * shares;

                totalValue += stockValue;
                totalChange += stockChange;

                if (parseFloat(stock.change) > bestStock.change) {
                    bestStock = {
                        symbol: stock.symbol,
                        change: parseFloat(stock.change),
                        percent: parseFloat(stock.change_percent)
                    };
                }
            });

            totalChangePercent = (totalChange / (totalValue - totalChange)) * 100;

            // Update portfolio total
            document.getElementById('portfolio-total').textContent =
                `$${totalValue.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ",")}`;

            // Update portfolio change
            const changeEl = document.getElementById('portfolio-change');
            const isPositive = totalChange >= 0;
            changeEl.className = `portfolio-change ${isPositive ? 'positive' : 'negative'}`;
            changeEl.innerHTML = `
                <span>${isPositive ? '↑' : '↓'}</span>
                <span>${isPositive ? '+' : ''}$${Math.abs(totalChange).toFixed(2)} (${isPositive ? '+' : ''}${totalChangePercent.toFixed(2)}%)</span>
            `;

            // Update stats
            document.getElementById('today-gain').textContent =
                `${totalChange >= 0 ? '+' : ''}$${totalChange.toFixed(2)}`;
            document.getElementById('today-gain').style.color =
                totalChange >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';

            document.getElementById('best-stock').textContent =
                `${bestStock.symbol} ${bestStock.percent > 0 ? '+' : ''}${bestStock.percent.toFixed(1)}%`;
            document.getElementById('best-stock').style.color = 'var(--accent-green)';

            document.getElementById('stock-count').textContent = Object.keys(stockData).length;
        }

        // Render individual stock cards with charts
        function renderStockCards() {
            const grid = document.getElementById('stocks-grid');
            grid.innerHTML = '';

            Object.values(stockData).forEach(stock => {
                const card = createStockCard(stock);
                grid.appendChild(card);
            });
        }

        // Create a single stock card
        function createStockCard(stock) {
            const isPositive = parseFloat(stock.change) >= 0;
            const shares = portfolio[stock.symbol] || 0;
            const totalValue = (parseFloat(stock.price) * shares).toFixed(2);

            const card = document.createElement('div');
            card.className = 'stock-card';
            card.innerHTML = `
                <div class="stock-header">
                    <div>
                        <div class="stock-symbol">${stock.symbol}</div>
                        <div class="stock-price">$${stock.price}</div>
                    </div>
                    <div class="stock-change ${isPositive ? 'positive' : 'negative'}">
                        <span>${isPositive ? '↑' : '↓'}</span>
                        <span>${isPositive ? '+' : ''}${stock.change} (${isPositive ? '+' : ''}${stock.change_percent}%)</span>
                    </div>
                </div>
                <div class="stock-holdings">
                    <span class="holdings-label">Shares:</span>
                    <input type="number"
                           class="shares-input"
                           id="shares-${stock.symbol}"
                           value="${shares}"
                           min="0"
                           step="0.01"
                           placeholder="0">
                    <button class="save-shares-btn" onclick="saveShares('${stock.symbol}')">Save</button>
                    ${shares > 0 ? `<div class="holdings-value">= $${totalValue.replace(/\\B(?=(\\d{3})+(?!\\d))/g, ",")}</div>` : ''}
                </div>
                <div class="stock-chart">
                    <canvas id="chart-${stock.symbol}"></canvas>
                </div>
            `;

            // Add chart after card is added to DOM
            setTimeout(() => {
                createStockChart(stock);
            }, 100);

            return card;
        }

        // Global function to save shares (called from onclick)
        window.saveShares = async function(symbol) {
            const input = document.getElementById(`shares-${symbol}`);
            const shares = parseFloat(input.value) || 0;
            await saveHolding(symbol, shares);
        };

        // Create Chart.js chart for stock
        function createStockChart(stock) {
            const canvas = document.getElementById(`chart-${stock.symbol}`);
            if (!canvas) return;

            const ctx = canvas.getContext('2d');
            const isPositive = parseFloat(stock.change) >= 0;

            if (charts[stock.symbol]) {
                charts[stock.symbol].destroy();
            }

            charts[stock.symbol] = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: stock.history.map((_, i) => ''),
                    datasets: [{
                        data: stock.history,
                        borderColor: isPositive ? '#10b981' : '#ef4444',
                        backgroundColor: isPositive ?
                            'rgba(16, 185, 129, 0.1)' :
                            'rgba(239, 68, 68, 0.1)',
                        borderWidth: 2,
                        fill: true,
                        tension: 0.4,
                        pointRadius: 0,
                        pointHoverRadius: 4
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            enabled: true,
                            mode: 'index',
                            intersect: false
                        }
                    },
                    scales: {
                        x: { display: false },
                        y: { display: false }
                    },
                    interaction: {
                        mode: 'nearest',
                        axis: 'x',
                        intersect: false
                    }
                }
            });
        }

        // Update last update time
        function updateLastUpdate() {
            const now = new Date();
            const timeStr = now.toLocaleTimeString('en-US', {
                hour: 'numeric',
                minute: '2-digit'
            });
            document.getElementById('last-update').textContent = `Updated: ${timeStr}`;
        }

        // Load insights and alerts
        async function loadInsights() {
            try {
                const response = await fetch('/api/insights');
                const data = await response.json();

                if (data.insights) {
                    renderInsights(data.insights);
                }

                if (data.alerts && data.alerts.length > 0) {
                    renderAlerts(data.alerts);
                }
            } catch (error) {
                console.error('Error loading insights:', error);
            }
        }

        // Render insights cards
        function renderInsights(insights) {
            const grid = document.getElementById('insights-grid');

            if (!insights || insights.length === 0) {
                grid.innerHTML = `
                    <div style="text-align: center; color: var(--text-secondary); padding: 20px;">
                        No insights available at this time. Check back soon!
                    </div>
                `;
                return;
            }

            grid.innerHTML = '';

            insights.forEach(insight => {
                const card = document.createElement('div');
                card.className = `insight-card ${insight.severity}`;
                card.innerHTML = `
                    <div class="insight-header">
                        <span class="insight-symbol">${insight.symbol}</span>
                        <span class="insight-type">${insight.type.replace('_', ' ')}</span>
                    </div>
                    <div class="insight-message">${insight.message}</div>
                    <div class="insight-action">→ ${insight.action}</div>
                `;
                grid.appendChild(card);
            });
        }

        // Render alerts banner
        function renderAlerts(alerts) {
            const banner = document.getElementById('alerts-banner');

            if (!alerts || alerts.length === 0) {
                banner.classList.remove('active');
                return;
            }

            banner.innerHTML = '';
            alerts.forEach(alert => {
                const alertItem = document.createElement('div');
                alertItem.className = 'alert-item';
                alertItem.innerHTML = `
                    <div class="alert-icon">${alert.severity === 'urgent' ? '🚨' : '⚠️'}</div>
                    <div class="alert-content">
                        <div class="alert-message">${alert.message}</div>
                        <div class="alert-severity">${alert.severity.toUpperCase()} · ${new Date(alert.timestamp).toLocaleTimeString()}</div>
                    </div>
                `;
                banner.appendChild(alertItem);
            });

            banner.classList.add('active');
        }

        // Generate and show daily briefing
        async function generateBriefing() {
            const modal = document.getElementById('briefing-modal');
            const briefingText = document.getElementById('briefing-text');
            const briefingTitle = document.getElementById('briefing-title');
            const briefingSubtitle = document.getElementById('briefing-subtitle');

            // Determine time of day
            const hour = new Date().getHours();
            let timeOfDay = 'morning';
            let title = '☀️ Morning Briefing';

            if (hour >= 12 && hour < 17) {
                timeOfDay = 'afternoon';
                title = '🌤️ Afternoon Update';
            } else if (hour >= 17) {
                timeOfDay = 'evening';
                title = '🌙 Evening Briefing';
            }

            briefingTitle.textContent = title;
            briefingSubtitle.textContent = 'Generating your personalized briefing...';
            briefingText.textContent = 'Please wait while Sully analyzes your portfolio and generates insights...';

            modal.classList.add('active');

            try {
                const response = await fetch('/api/briefing', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ time: timeOfDay })
                });

                const data = await response.json();

                if (data.briefing) {
                    briefingText.textContent = data.briefing;
                    briefingSubtitle.textContent = `Generated at ${new Date(data.generated_at).toLocaleTimeString()}`;

                    // Speak the briefing
                    speak(data.briefing);

                    // Update insights and alerts if provided
                    if (data.insights) {
                        renderInsights(data.insights);
                    }
                    if (data.alerts) {
                        renderAlerts(data.alerts);
                    }
                } else {
                    briefingText.textContent = 'Unable to generate briefing at this time. Please try again later.';
                }
            } catch (error) {
                console.error('Error generating briefing:', error);
                briefingText.textContent = 'Error generating briefing. Please check your API configuration and try again.';
            }
        }

        // Close briefing modal
        function closeBriefing(event) {
            if (!event || event.target.id === 'briefing-modal' || event.target.className.includes('briefing-close')) {
                document.getElementById('briefing-modal').classList.remove('active');
            }
        }

        // Load data on page load
        window.addEventListener('DOMContentLoaded', async () => {
            updateDailyMotivation();  // Load today's CEO message
            await loadPortfolio();  // Load portfolio first
            await loadStockData();  // Then load stock data
            loadInsights();

            // Refresh every 5 minutes
            setInterval(async () => {
                await loadPortfolio();
                await loadStockData();
            }, 300000);
            setInterval(loadInsights, 300000);
        });

        // Speech Recognition Setup
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        let recognition = null;
        let isListening = false;

        if (SpeechRecognition) {
            recognition = new SpeechRecognition();
            recognition.continuous = false;
            recognition.interimResults = false;
            recognition.lang = 'en-US';

            recognition.onstart = function() {
                isListening = true;
                document.getElementById('mic-btn').classList.add('listening');
                document.getElementById('voice-status').classList.add('active');
            };

            recognition.onend = function() {
                isListening = false;
                document.getElementById('mic-btn').classList.remove('listening');
                document.getElementById('voice-status').classList.remove('active');
            };

            recognition.onresult = function(event) {
                const transcript = event.results[0][0].transcript;
                document.getElementById('user-input').value = transcript;
                sendMessage();
            };

            recognition.onerror = function(event) {
                console.error('Speech recognition error:', event.error);
                isListening = false;
                document.getElementById('mic-btn').classList.remove('listening');
                document.getElementById('voice-status').classList.remove('active');
            };
        }

        // Voice selection - automatically pick best natural-sounding male voice
        // Prioritize voices that sound more like Elon Musk or Trump (deeper, more natural)
        const PREFERRED_VOICES = [
            'Alex',              // macOS - natural male voice
            'Fred',              // macOS - deeper male voice
            'Daniel',            // Natural British male
            'Microsoft David',   // Windows - natural
            'Microsoft Mark',    // Windows - US male
            'Google US English Male', // Chrome - natural
            'Google UK English Male'  // Chrome - deeper
        ];

        function getBestMaleUsVoice() {
            if (!('speechSynthesis' in window)) return null;
            const voices = window.speechSynthesis.getVoices();
            if (!voices || !voices.length) return null;

            // Try preferred voices first (best quality)
            for (const preferredName of PREFERRED_VOICES) {
                const voice = voices.find(v =>
                    v.name === preferredName ||
                    v.name.includes(preferredName)
                );
                if (voice) return voice;
            }

            // Fallback: Find any deeper, natural-sounding male voice
            const deepMaleVoice = voices.find(v =>
                v.lang.startsWith('en') &&
                /(male|alex|daniel|fred|mark|david|guy|james|aaron)/i.test(v.name) &&
                !/(female|samantha|victoria|karen|susan|junior|compact)/i.test(v.name)
            );
            if (deepMaleVoice) return deepMaleVoice;

            // Last resort: any English voice
            return voices.find(v => v.lang.startsWith('en')) || voices[0];
        }

        // Clean text for natural speech (remove emojis, symbols, etc.)
        function cleanTextForSpeech(text) {
            // Remove emojis by filtering out characters in emoji ranges
            let cleaned = '';
            for (let i = 0; i < text.length; i++) {
                const code = text.codePointAt(i);

                // Skip emoji ranges
                if (
                    (code >= 0x1F300 && code <= 0x1F9FF) || // Misc Symbols and Pictographs
                    (code >= 0x2600 && code <= 0x26FF) ||   // Misc symbols
                    (code >= 0x2700 && code <= 0x27BF) ||   // Dingbats
                    (code >= 0x1F1E0 && code <= 0x1F1FF) || // Flags
                    (code >= 0x1F600 && code <= 0x1F64F) || // Emoticons
                    (code >= 0x1F680 && code <= 0x1F6FF) || // Transport symbols
                    (code >= 0x2300 && code <= 0x23FF) ||   // Misc Technical
                    (code >= 0x25A0 && code <= 0x25FF) ||   // Geometric shapes
                    (code >= 0x2190 && code <= 0x21FF)      // Arrows
                ) {
                    // Skip surrogate pairs (emojis use 2 chars)
                    if (code > 0xFFFF) i++;
                    continue;
                }

                cleaned += text[i];
            }

            return cleaned
                // Remove bullet points and list markers
                .replace(/^[•\-\*]\s*/gm, '')
                // Remove extra whitespace
                .replace(/\s+/g, ' ')
                // Remove standalone numbers at start of lines (list numbers)
                .replace(/^\d+\.\s+/gm, '')
                // Remove markdown symbols
                .replace(/[*_~`#]/g, '')
                // Remove extra punctuation
                .replace(/\.{2,}/g, '.')
                // Remove parentheses with single chars (often emoji descriptions)
                .replace(/\([a-zA-Z0-9]\)/g, '')
                // Clean up spacing around punctuation
                .replace(/\s+([,.!?:;])/g, '$1')
                .replace(/([,.!?:;])\s*([,.!?:;])/g, '$1')
                .trim();
        }

        // Track current audio/speech
        let currentAudio = null;
        let isSpeaking = false;

        // Speech Synthesis with natural male voice (optimized for natural sound)
        function speak(text) {
            // Clean text for natural speech
            const cleanText = cleanTextForSpeech(text);
            if (!cleanText) return;

            // Stop any current speech
            stopSpeaking();

            // Show stop button
            const stopBtn = document.getElementById('stop-btn');
            if (stopBtn) stopBtn.style.display = 'flex';
            isSpeaking = true;

            // Prefer server TTS if enabled (ElevenLabs)
            if (window.SERVER_TTS_ENABLED) {
                try {
                    const ttsUrl = '/tts?text=' + encodeURIComponent(cleanText);
                    const audio = new Audio(ttsUrl);
                    currentAudio = audio;
                    let hasFailedOver = false; // Prevent double fallback

                    // Handle audio loading errors
                    audio.addEventListener('error', (e) => {
                        if (!hasFailedOver) {
                            hasFailedOver = true;
                            console.warn('Server TTS audio load failed, falling back to Web Speech:', e);
                            currentAudio = null;
                            fallbackToWebSpeech(cleanText);
                        }
                    });

                    // Handle audio end
                    audio.addEventListener('ended', () => {
                        currentAudio = null;
                        isSpeaking = false;
                        if (stopBtn) stopBtn.style.display = 'none';
                    });

                    // Try to play
                    audio.play().catch(err => {
                        if (!hasFailedOver) {
                            hasFailedOver = true;
                            console.warn('Audio play blocked or failed, falling back to Web Speech:', err);
                            currentAudio = null;
                            fallbackToWebSpeech(cleanText);
                        }
                    });
                    return;
                } catch (e) {
                    console.warn('Server TTS error, falling back to Web Speech:', e);
                    currentAudio = null;
                }
            }

            // Fallback to browser Web Speech API
            fallbackToWebSpeech(cleanText);
        }

        function stopSpeaking() {
            // Stop audio element if playing
            if (currentAudio) {
                currentAudio.pause();
                currentAudio.currentTime = 0;
                currentAudio = null;
            }

            // Stop Web Speech API
            if (window.speechSynthesis) {
                window.speechSynthesis.cancel();
            }

            // Hide stop button
            const stopBtn = document.getElementById('stop-btn');
            if (stopBtn) stopBtn.style.display = 'none';
            isSpeaking = false;
        }

        function fallbackToWebSpeech(text) {
            if (!window.speechSynthesis) {
                console.warn('Speech synthesis not supported');
                return;
            }

            // Limit text length for browser TTS (some browsers have limits)
            if (text.length > 1000) {
                text = text.substring(0, 1000) + '...';
                console.log('Truncated text to 1000 chars for TTS');
            }

            // Use browser's speech synthesis with optimized settings for natural sound
            window.speechSynthesis.cancel();

            const utterance = new SpeechSynthesisUtterance(text);

            // Optimized settings for natural, confident male voice
            utterance.rate = 0.95;      // Slightly slower than default (more natural)
            utterance.pitch = 0.85;     // Deeper pitch (more masculine)
            utterance.volume = 1.0;     // Full volume
            utterance.lang = 'en-US';

            // Try to select best natural male voice
            const voice = getBestMaleUsVoice();
            if (voice) {
                utterance.voice = voice;
                console.log('Using voice:', voice.name);
            } else {
                console.log('Using default system voice');
            }

            // Handle speech end
            utterance.onend = () => {
                console.log('Speech ended successfully');
                isSpeaking = false;
                const stopBtn = document.getElementById('stop-btn');
                if (stopBtn) stopBtn.style.display = 'none';
            };

            // Handle speech error with detailed logging
            utterance.onerror = (e) => {
                console.error('Speech error:', e.error, 'Type:', e.type, 'Char index:', e.charIndex);
                isSpeaking = false;
                const stopBtn = document.getElementById('stop-btn');
                if (stopBtn) stopBtn.style.display = 'none';

                // Show user-friendly error
                if (e.error === 'not-allowed') {
                    console.warn('Speech blocked by browser. User needs to interact with page first.');
                } else if (e.error === 'network') {
                    console.warn('Speech failed due to network issue');
                }
            };

            // Start speaking
            try {
                window.speechSynthesis.speak(utterance);
                console.log('Speech started, text length:', text.length);
            } catch (err) {
                console.error('Failed to start speech:', err);
                isSpeaking = false;
                const stopBtn = document.getElementById('stop-btn');
                if (stopBtn) stopBtn.style.display = 'none';
            }
        }

        // Load voices (needed for some browsers)
        if ('speechSynthesis' in window) {
            window.speechSynthesis.onvoiceschanged = function() {
                window.speechSynthesis.getVoices();
            };
        }

        function toggleVoice() {
            if (!recognition) {
                alert('Voice recognition not supported in this browser. Try Chrome or Edge!');
                return;
            }

            if (isListening) {
                recognition.stop();
            } else {
                recognition.start();
            }
        }

        function addMessage(text, sender) {
            const messagesDiv = document.getElementById('messages');
            const messageDiv = document.createElement('div');
            messageDiv.className = `message ${sender}`;

            if (sender === 'user') {
                messageDiv.innerHTML = `
                    <div class="avatar">👤</div>
                    <div class="message-content">
                        <div class="message-name">You</div>
                        <div class="message-bubble">${text}</div>
                    </div>
                `;
            } else {
                messageDiv.innerHTML = `
                    <div class="avatar">🎩</div>
                    <div class="message-content">
                        <div class="message-name">Sully</div>
                        <div class="message-bubble">${text}</div>
                    </div>
                `;
            }

            messagesDiv.appendChild(messageDiv);
            messagesDiv.scrollTop = messagesDiv.scrollHeight;
        }

        function setLoading(active) {
            document.getElementById('loading').className = active ? 'loading active' : 'loading';
            document.getElementById('send-btn').disabled = active;
            document.getElementById('user-input').disabled = active;
            document.getElementById('mic-btn').disabled = active;
        }

        async function sendMessage() {
            const input = document.getElementById('user-input');
            const message = input.value.trim();
            if (!message) return;

            addMessage(message, 'user');
            input.value = '';
            setLoading(true);

            try {
                const response = await fetch('/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: message})
                });
                const data = await response.json();
                setLoading(false);

                if (data.response) {
                    addMessage(data.response, 'sully');
                    // Speak Sully's response
                    speak(data.response);
                } else {
                    const errorMsg = 'Sorry, I hit a snag there. Please try again.';
                    addMessage(errorMsg, 'sully');
                    speak(errorMsg);
                }
            } catch (error) {
                setLoading(false);
                const errorMsg = 'Connection issue. Please try again.';
                addMessage(errorMsg, 'sully');
                speak(errorMsg);
            }
        }

        function sendQuick(message) {
            document.getElementById('user-input').value = message;
            sendMessage();
        }

        function handleKeyPress(event) {
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                sendMessage();
            }
        }

        // ===== SETTINGS FUNCTIONS =====
        let userPreferences = {};

        async function loadUserPreferences() {
            try {
                const response = await fetch('/api/preferences');
                const prefs = await response.json();
                userPreferences = prefs;

                // Update UI with loaded preferences
                if (prefs.id) {
                    document.getElementById('boston-intensity').value = prefs.boston_intensity || 2;
                    document.getElementById('boston-value').textContent = prefs.boston_intensity || 2;
                    document.getElementById('voice-rate').value = prefs.voice_rate || 0.95;
                    document.getElementById('rate-value').textContent = prefs.voice_rate || 0.95;
                    document.getElementById('voice-pitch').value = prefs.voice_pitch || 0.85;
                    document.getElementById('pitch-value').textContent = prefs.voice_pitch || 0.85;
                    document.getElementById('alert-threshold').value = prefs.alert_threshold || 5.0;
                    document.getElementById('alert-value').textContent = prefs.alert_threshold || 5.0;

                    if (prefs.voice_enabled) {
                        document.getElementById('voice-enabled').classList.add('active');
                    } else {
                        document.getElementById('voice-enabled').classList.remove('active');
                    }

                    if (prefs.auto_refresh) {
                        document.getElementById('auto-refresh').classList.add('active');
                    } else {
                        document.getElementById('auto-refresh').classList.remove('active');
                    }
                }
            } catch (error) {
                console.error('Failed to load preferences:', error);
            }
        }

        function openSettings() {
            document.getElementById('settings-modal').classList.add('active');
            loadUserPreferences();
        }

        function closeSettings() {
            document.getElementById('settings-modal').classList.remove('active');
        }

        function toggleSetting(element) {
            element.classList.toggle('active');
        }

        async function saveSettings() {
            const settings = {
                boston_intensity: parseInt(document.getElementById('boston-intensity').value),
                voice_rate: parseFloat(document.getElementById('voice-rate').value),
                voice_pitch: parseFloat(document.getElementById('voice-pitch').value),
                alert_threshold: parseFloat(document.getElementById('alert-threshold').value),
                voice_enabled: document.getElementById('voice-enabled').classList.contains('active'),
                auto_refresh: document.getElementById('auto-refresh').classList.contains('active')
            };

            try {
                const response = await fetch('/api/preferences', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(settings)
                });

                if (response.ok) {
                    userPreferences = {...userPreferences, ...settings};
                    alert('✅ Settings saved successfully!');
                    closeSettings();

                    // Apply voice settings immediately
                    window.voiceRate = settings.voice_rate;
                    window.voicePitch = settings.voice_pitch;
                } else {
                    alert('❌ Failed to save settings');
                }
            } catch (error) {
                console.error('Error saving settings:', error);
                alert('❌ Failed to save settings');
            }
        }

        // ===== HISTORY FUNCTIONS =====
        async function openHistory() {
            document.getElementById('history-modal').classList.add('active');
            await loadHistory();
        }

        function closeHistory() {
            document.getElementById('history-modal').classList.remove('active');
        }

        async function loadHistory() {
            try {
                const response = await fetch('/api/history?limit=20');
                const data = await response.json();
                const historyList = document.getElementById('history-list');

                if (data.history && data.history.length > 0) {
                    historyList.innerHTML = data.history.map(item => `
                        <div class="history-item">
                            <div class="history-timestamp">${new Date(item.timestamp).toLocaleString()}</div>
                            <div class="history-message"><strong>You:</strong> ${item.message}</div>
                            <div class="history-response"><strong>Sully:</strong> ${item.response}</div>
                        </div>
                    `).join('');
                } else {
                    historyList.innerHTML = `
                        <div style="text-align: center; padding: 40px; color: var(--text-secondary);">
                            No conversation history yet. Start chatting with Sully!
                        </div>
                    `;
                }
            } catch (error) {
                console.error('Error loading history:', error);
                document.getElementById('history-list').innerHTML = `
                    <div style="text-align: center; padding: 40px; color: var(--accent-red);">
                        Failed to load history
                    </div>
                `;
            }
        }

        // Close modals when clicking outside
        document.addEventListener('click', function(event) {
            if (event.target.classList.contains('modal-overlay')) {
                event.target.classList.remove('active');
            }
        });

        // Initialize preferences on page load
        loadUserPreferences();

        // ===== PWA SERVICE WORKER =====
        if ('serviceWorker' in navigator) {
            window.addEventListener('load', () => {
                navigator.serviceWorker.register('/service-worker.js')
                    .then(registration => {
                        console.log('ServiceWorker registered:', registration.scope);

                        // Check for updates periodically
                        setInterval(() => {
                            registration.update();
                        }, 60000); // Check every minute
                    })
                    .catch(error => {
                        console.log('ServiceWorker registration failed:', error);
                    });
            });
        }

        // ===== PWA INSTALLATION PROMPT =====
        let deferredPrompt;
        const installBanner = document.getElementById('install-banner');
        const installBtn = document.getElementById('install-btn');
        const closeInstallBtn = document.getElementById('close-install');

        window.addEventListener('beforeinstallprompt', (e) => {
            // Prevent Chrome 76+ from automatically showing prompt
            e.preventDefault();
            deferredPrompt = e;

            // Show install banner after 3 seconds
            setTimeout(() => {
                const isInstalled = window.matchMedia('(display-mode: standalone)').matches;
                if (!isInstalled && !localStorage.getItem('install-dismissed')) {
                    installBanner.classList.add('show');
                }
            }, 3000);
        });

        installBtn.addEventListener('click', async () => {
            if (!deferredPrompt) return;

            // Show the install prompt
            deferredPrompt.prompt();

            // Wait for the user's response
            const { outcome } = await deferredPrompt.userChoice;
            console.log(`User response: ${outcome}`);

            // Clear the deferredPrompt
            deferredPrompt = null;
            installBanner.classList.remove('show');
        });

        closeInstallBtn.addEventListener('click', () => {
            installBanner.classList.remove('show');
            localStorage.setItem('install-dismissed', 'true');
        });

        // Detect if app is running as PWA
        window.addEventListener('appinstalled', () => {
            console.log('PWA installed successfully');
            installBanner.classList.remove('show');
        });

        // ===== PULL-TO-REFRESH =====
        let startY = 0;
        let currentY = 0;
        let pulling = false;
        const pullIndicator = document.getElementById('pull-indicator');
        const PULL_THRESHOLD = 80;

        document.addEventListener('touchstart', (e) => {
            if (window.scrollY === 0) {
                startY = e.touches[0].clientY;
                pulling = true;
            }
        }, { passive: true });

        document.addEventListener('touchmove', (e) => {
            if (!pulling) return;

            currentY = e.touches[0].clientY;
            const pullDistance = currentY - startY;

            if (pullDistance > 0 && pullDistance < PULL_THRESHOLD * 2) {
                pullIndicator.style.top = `${Math.min(pullDistance / 2, PULL_THRESHOLD)}px`;

                if (pullDistance > PULL_THRESHOLD) {
                    pullIndicator.classList.add('active');
                } else {
                    pullIndicator.classList.remove('active');
                }
            }
        }, { passive: true });

        document.addEventListener('touchend', async () => {
            if (!pulling) return;

            const pullDistance = currentY - startY;

            if (pullDistance > PULL_THRESHOLD) {
                // Trigger refresh
                pullIndicator.classList.add('refreshing');

                try {
                    // Reload stock data
                    await loadStockData();
                    await loadInsights();

                    // Success feedback
                    setTimeout(() => {
                        pullIndicator.classList.remove('active', 'refreshing');
                        pullIndicator.style.top = '-60px';
                    }, 500);
                } catch (error) {
                    console.error('Refresh failed:', error);
                    pullIndicator.classList.remove('active', 'refreshing');
                    pullIndicator.style.top = '-60px';
                }
            } else {
                pullIndicator.classList.remove('active');
                pullIndicator.style.top = '-60px';
            }

            pulling = false;
            startY = 0;
            currentY = 0;
        });

        // ===== PUSH NOTIFICATIONS =====
        async function requestNotificationPermission() {
            if ('Notification' in window && 'serviceWorker' in navigator) {
                const permission = await Notification.requestPermission();

                if (permission === 'granted') {
                    console.log('Notification permission granted');

                    // Subscribe to push notifications
                    const registration = await navigator.serviceWorker.ready;
                    const subscription = await registration.pushManager.subscribe({
                        userVisibleOnly: true,
                        applicationServerKey: null // Add your VAPID public key here if needed
                    });

                    console.log('Push subscription:', subscription);
                    // Send subscription to server if needed
                }
            }
        }

        // Request notification permission after 10 seconds (if not already granted)
        setTimeout(() => {
            if ('Notification' in window && Notification.permission === 'default') {
                requestNotificationPermission();
            }
        }, 10000);

        // ===== OFFLINE DETECTION =====
        window.addEventListener('online', () => {
            console.log('Back online');
            // Optionally show a toast notification
        });

        window.addEventListener('offline', () => {
            console.log('Gone offline');
            // Optionally show a toast notification
        });

        // ===== TOUCH GESTURES FOR MOBILE =====
        let touchStartX = 0;
        let touchStartY = 0;

        document.addEventListener('touchstart', (e) => {
            touchStartX = e.touches[0].clientX;
            touchStartY = e.touches[0].clientY;
        }, { passive: true });

        document.addEventListener('touchend', (e) => {
            const touchEndX = e.changedTouches[0].clientX;
            const touchEndY = e.changedTouches[0].clientY;

            const deltaX = touchEndX - touchStartX;
            const deltaY = touchEndY - touchStartY;

            // Swipe gestures (optional - can be extended)
            if (Math.abs(deltaX) > 100 && Math.abs(deltaY) < 50) {
                if (deltaX > 0) {
                    console.log('Swipe right');
                    // Add swipe right action if needed
                } else {
                    console.log('Swipe left');
                    // Add swipe left action if needed
                }
            }
        }, { passive: true });

        document.getElementById('user-input').focus();
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    server_tts = bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID)
    return render_template_string(HTML, server_tts=server_tts)

@app.route('/manifest.json')
def manifest():
    """Serve PWA manifest"""
    return send_file('manifest.json', mimetype='application/json')

@app.route('/service-worker.js')
def service_worker():
    """Serve service worker"""
    return send_file('service-worker.js', mimetype='application/javascript')

@app.route('/chat', methods=['POST'])
@login_required
def chat():
    global current_data, last_update, aggregator, sully

    # Initialize on first request
    if aggregator is None:
        aggregator = NewsAggregator()
    if sully is None:
        if not GROQ_API_KEY:
            return jsonify({'response': 'API key not configured yet. Please contact support.', 'error': True})
        sully = SullyAI(GROQ_API_KEY, BOSTON_INTENSITY)

    try:
        data = request.get_json()
        user_message = data.get('message', '')

        # Refresh data if needed
        if not current_data or not last_update or (datetime.now() - last_update).seconds > 1800:
            current_data = aggregator.get_full_briefing(STOCK_SYMBOLS)
            last_update = datetime.now()

        # Handle special commands
        if 'stock' in user_message.lower():
            # Format stock data in a clean, readable way
            stocks_text = "\n=== PORTFOLIO UPDATE ===\n\n"
            for symbol, stock_data in current_data['stocks'].items():
                if 'error' not in stock_data:
                    price = stock_data['price']
                    change = stock_data['change']
                    change_pct = stock_data['change_percent']

                    # Determine trend
                    if change > 0:
                        trend = "UP"
                        arrow = "↑"
                    elif change < 0:
                        trend = "DOWN"
                        arrow = "↓"
                    else:
                        trend = "FLAT"
                        arrow = "→"

                    stocks_text += f"{symbol}\n"
                    stocks_text += f"  Price: ${price:.2f}\n"
                    stocks_text += f"  Change: {arrow} ${abs(change):.2f} ({change_pct:+.2f}%)\n"
                    stocks_text += f"  Status: {trend}\n\n"

            stocks_text += "======================\n"
            response = sully.chat(f"Give me your Boston take on these stocks:\n{stocks_text}", current_data)

        # Handle VIP personality queries (Brady, Elon, Trump)
        elif any(keyword in user_message.lower() for keyword in ['brady', 'tb12', 'elon', 'musk', 'tesla', 'trump', 'djt']):
            vip_context = ""

            # Tom Brady
            if any(keyword in user_message.lower() for keyword in ['brady', 'tb12']):
                vip_context = aggregator.search_vip_news('tom_brady')

            # Elon Musk / Tesla
            elif any(keyword in user_message.lower() for keyword in ['elon', 'musk', 'tesla']):
                vip_context = aggregator.search_vip_news('elon_musk')

            # Trump
            elif any(keyword in user_message.lower() for keyword in ['trump', 'djt']):
                vip_context = aggregator.search_vip_news('trump')

            response = sully.chat(f"{user_message}\n\n{vip_context}", current_data)

        # Handle news queries for Patriots, Celtics, or general searches
        elif any(keyword in user_message.lower() for keyword in ['patriots', 'pats', 'celtics', 'news', 'latest']):
            # Determine search query
            if 'patriots' in user_message.lower() or 'pats' in user_message.lower():
                search_query = 'New England Patriots'
            elif 'celtics' in user_message.lower():
                search_query = 'Boston Celtics'
            else:
                search_query = user_message  # Use user's full query

            # Fetch live news
            news_context = aggregator.search_live_news(search_query)
            response = sully.chat(f"{user_message}\n\n{news_context}", current_data)

        else:
            response = sully.chat(user_message, current_data)

        # Save conversation to history
        try:
            db = get_db()
            cursor = db.cursor()
            cursor.execute('''
                INSERT INTO conversations (user_id, message, response)
                VALUES (?, ?, ?)
            ''', (session['user_id'], user_message, response))
            db.commit()
            db.close()
        except Exception as history_error:
            # Don't fail the response if history save fails
            print(f"Failed to save conversation history: {history_error}")

        return jsonify({'response': response})

    except Exception as e:
        return jsonify({'response': f"Sorry, encountered an error: {str(e)}", 'error': True})

@app.route('/tts')
def tts():
    """Server-side neural TTS proxy (ElevenLabs streaming).
    Requires ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID.
    """
    if not ELEVENLABS_API_KEY:
        print("❌ TTS Error: ELEVENLABS_API_KEY not set")
        return Response("TTS not configured (missing API key)", status=400, mimetype='text/plain')

    text = request.args.get('text', '')
    if not text:
        return Response("Missing text", status=400, mimetype='text/plain')

    # keep payload reasonable
    text = text.strip()
    if len(text) > 1200:
        text = text[:1200]

    # Allow temporary override via query param for quick testing
    voice_id = request.args.get('voice_id') or ELEVENLABS_VOICE_ID
    if not voice_id:
        print("❌ TTS Error: ELEVENLABS_VOICE_ID not set")
        return Response("TTS not configured (missing voice id)", status=400, mimetype='text/plain')

    print(f"🎤 TTS Request: text='{text[:50]}...' voice_id={voice_id}")

    def generate():
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?optimize_streaming_latency=2"
        headers = {
            'xi-api-key': ELEVENLABS_API_KEY,
            'Accept': 'audio/mpeg',
            'Content-Type': 'application/json'
        }
        payload = {
            'text': text,
            'model_id': ELEVENLABS_MODEL_ID,
            'voice_settings': {
                'stability': 0.2,
                'similarity_boost': 0.85,
                'style': 0.2,
                'use_speaker_boost': True
            }
        }
        try:
            with requests.post(url, headers=headers, json=payload, stream=True, timeout=60) as r:
                if r.status_code != 200:
                    error_msg = f"ElevenLabs API error: {r.status_code} - {r.text[:200]}"
                    print(f"❌ TTS Error: {error_msg}")
                    return
                r.raise_for_status()
                print("✅ TTS streaming audio from ElevenLabs")
                for chunk in r.iter_content(chunk_size=4096):
                    if chunk:
                        yield chunk
        except Exception as e:
            # surface error to client; do not crash server
            err = f"TTS upstream error: {str(e)}"
            print(f"❌ TTS Exception: {err}")
            yield b''

    return Response(stream_with_context(generate()), mimetype='audio/mpeg')

@app.route('/api/briefing', methods=['POST'])
def get_briefing():
    """Generate AI-powered daily briefing"""
    global current_data, aggregator, sully

    if aggregator is None:
        aggregator = NewsAggregator()
    if sully is None:
        if not GROQ_API_KEY:
            return jsonify({'error': 'API key not configured'}), 400
        sully = SullyAI(GROQ_API_KEY, BOSTON_INTENSITY)

    try:
        data = request.get_json()
        time_of_day = data.get('time', 'morning')  # morning, afternoon, evening

        # Get fresh market data
        if not current_data or not last_update or (datetime.now() - last_update).seconds > 1800:
            current_data = aggregator.get_full_briefing(STOCK_SYMBOLS)

        # Analyze portfolio performance
        portfolio_analysis = analyze_portfolio_performance(current_data['stocks'])

        # Fetch sports news for Patriots and Celtics
        patriots_news = aggregator.search_live_news('patriots')
        celtics_news = aggregator.search_live_news('celtics')

        # Fetch VIP news for Elon Musk and Trump
        elon_news = aggregator.search_vip_news('elon_musk')
        trump_news = aggregator.search_vip_news('trump')

        # Combine all news sections
        news_section = f"""
{patriots_news}
{celtics_news}
{elon_news}
{trump_news}
"""

        # Create briefing prompt based on time of day
        if time_of_day == 'morning':
            briefing_prompt = f"""Generate a concise morning briefing for a busy executive. Include:

PORTFOLIO PERFORMANCE:
{portfolio_analysis}

BREAKING NEWS & UPDATES:
{news_section}

1. Portfolio Status: Quick summary of overall performance
2. Top 3 Insights: Most important things to know today
3. Key Movers: Stocks with significant changes (>3%)
4. Sports & VIP Updates: Brief mention of Patriots, Celtics, Elon, and Trump news highlights
5. Action Items: What to watch today

Keep it under 250 words. Be direct and actionable. Use bullet points."""

        else:
            briefing_prompt = f"""Generate an end-of-day briefing for a busy executive. Include:

PORTFOLIO PERFORMANCE:
{portfolio_analysis}

BREAKING NEWS & UPDATES:
{news_section}

1. Daily Performance: How did the portfolio perform today?
2. Key Winners & Losers: Top 3 of each
3. Notable Events: Any significant market moves or news
4. Sports & VIP Updates: Brief mention of Patriots, Celtics, Elon, and Trump news highlights
5. Tomorrow's Watch List: What to monitor

Keep it under 250 words. Be direct and insightful."""

        # Generate briefing using AI
        briefing = sully.chat(briefing_prompt, current_data)

        # Extract insights
        insights = extract_insights(current_data['stocks'])
        alerts = detect_alerts(current_data['stocks'])

        return jsonify({
            'briefing': briefing,
            'insights': insights,
            'alerts': alerts,
            'time': time_of_day,
            'generated_at': datetime.now().isoformat()
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/insights', methods=['GET'])
def get_insights():
    """Get AI-powered portfolio insights"""
    global current_data, aggregator

    if aggregator is None:
        aggregator = NewsAggregator()

    try:
        # Get fresh data
        if not current_data or not last_update or (datetime.now() - last_update).seconds > 1800:
            current_data = aggregator.get_full_briefing(STOCK_SYMBOLS)

        insights = extract_insights(current_data['stocks'])
        alerts = detect_alerts(current_data['stocks'])

        return jsonify({
            'insights': insights,
            'alerts': alerts,
            'timestamp': datetime.now().isoformat()
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===== USER PREFERENCES API =====
@app.route('/api/preferences', methods=['GET'])
@login_required
def get_preferences():
    """Get user preferences"""
    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute('SELECT * FROM preferences WHERE user_id = ?', (session['user_id'],))
        prefs = cursor.fetchone()
        db.close()

        if prefs:
            return jsonify(dict(prefs))
        else:
            return jsonify({'error': 'Preferences not found'}), 404

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/preferences', methods=['POST'])
@login_required
def update_preferences():
    """Update user preferences"""
    try:
        data = request.get_json()
        db = get_db()
        cursor = db.cursor()

        # Update preferences
        updates = []
        values = []
        for key in ['theme', 'boston_intensity', 'voice_enabled', 'voice_rate',
                   'voice_pitch', 'alert_threshold', 'auto_refresh', 'refresh_interval']:
            if key in data:
                updates.append(f"{key} = ?")
                values.append(data[key])

        if updates:
            values.append(session['user_id'])
            query = f"UPDATE preferences SET {', '.join(updates)} WHERE user_id = ?"
            cursor.execute(query, values)
            db.commit()

        db.close()
        return jsonify({'success': True, 'message': 'Preferences updated'})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===== WATCHLIST API =====
@app.route('/api/watchlist', methods=['GET'])
@login_required
def get_watchlist():
    """Get user's custom watchlist"""
    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            SELECT symbol, added_at, notes
            FROM watchlists
            WHERE user_id = ?
            ORDER BY added_at DESC
        ''', (session['user_id'],))
        watchlist = [dict(row) for row in cursor.fetchall()]
        db.close()

        return jsonify({'watchlist': watchlist})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/watchlist', methods=['POST'])
@login_required
def add_to_watchlist():
    """Add stock to watchlist"""
    try:
        data = request.get_json()
        symbol = data.get('symbol', '').upper()
        notes = data.get('notes', '')

        if not symbol:
            return jsonify({'error': 'Symbol required'}), 400

        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO watchlists (user_id, symbol, notes)
            VALUES (?, ?, ?)
        ''', (session['user_id'], symbol, notes))
        db.commit()
        db.close()

        return jsonify({'success': True, 'message': f'{symbol} added to watchlist'})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/watchlist/<symbol>', methods=['DELETE'])
@login_required
def remove_from_watchlist(symbol):
    """Remove stock from watchlist"""
    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            DELETE FROM watchlists
            WHERE user_id = ? AND symbol = ?
        ''', (session['user_id'], symbol.upper()))
        db.commit()
        db.close()

        return jsonify({'success': True, 'message': f'{symbol} removed from watchlist'})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===== CONVERSATION HISTORY API =====
@app.route('/api/history', methods=['GET'])
@login_required
def get_history():
    """Get conversation history"""
    try:
        limit = request.args.get('limit', 50, type=int)
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            SELECT message, response, timestamp
            FROM conversations
            WHERE user_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (session['user_id'], limit))
        history = [dict(row) for row in cursor.fetchall()]
        db.close()

        return jsonify({'history': history})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

def fetch_stock_data_from_yahoo(symbols):
    """Fetch real-time stock data from Yahoo Finance without requiring Groq"""
    stock_data = {}
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })

    for symbol in symbols:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            params = {'interval': '1d', 'range': '30d'}
            response = session.get(url, params=params, timeout=10)

            if response.status_code == 200:
                data = response.json()
                result = data['chart']['result'][0]
                quote = result['meta']
                current_price = quote.get('regularMarketPrice', 0)
                previous_close = quote.get('previousClose', 0)
                change = current_price - previous_close
                change_percent = (change / previous_close * 100) if previous_close else 0

                # Extract historical prices for chart
                history = []
                if 'indicators' in result and 'quote' in result['indicators']:
                    closes = result['indicators']['quote'][0].get('close', [])
                    history = [price for price in closes if price is not None]

                stock_data[symbol] = {
                    'symbol': symbol,
                    'price': round(current_price, 2),
                    'change': round(change, 2),
                    'change_percent': round(change_percent, 2),
                    'previous_close': round(previous_close, 2),
                    'volume': quote.get('regularMarketVolume', 0),
                    'history': history[-30:] if history else []
                }
        except Exception as e:
            stock_data[symbol] = {'error': str(e), 'symbol': symbol}

    return stock_data

@app.route('/api/stocks', methods=['GET'])
def get_stocks():
    """Get real-time stock data from Yahoo Finance"""
    try:
        # Get stocks from query param or use default
        symbols_param = request.args.get('symbols', '')
        symbols = [s.strip() for s in symbols_param.split(',')] if symbols_param else STOCK_SYMBOLS

        # Fetch real stock data directly
        stock_data = fetch_stock_data_from_yahoo(symbols)

        return jsonify({'stocks': stock_data, 'count': len(stock_data)})

    except Exception as e:
        print(f"Error fetching stocks: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/portfolio', methods=['GET'])
@login_required
def get_portfolio():
    """Get user's portfolio holdings"""
    try:
        user = get_or_create_user()
        db = get_db()
        cursor = db.cursor()

        cursor.execute('''
            SELECT symbol, shares FROM portfolio
            WHERE user_id = ?
        ''', (user['id'],))

        holdings = {}
        for row in cursor.fetchall():
            holdings[row[0]] = row[1]

        db.close()
        return jsonify({'holdings': holdings})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/portfolio', methods=['POST'])
@login_required
def update_portfolio():
    """Update portfolio holdings for a symbol"""
    try:
        data = request.get_json()
        symbol = data.get('symbol', '').upper()
        shares = float(data.get('shares', 0))

        if not symbol:
            return jsonify({'error': 'Symbol required'}), 400

        user = get_or_create_user()
        db = get_db()
        cursor = db.cursor()

        # Insert or update holding
        cursor.execute('''
            INSERT INTO portfolio (user_id, symbol, shares, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, symbol) DO UPDATE SET
                shares = excluded.shares,
                updated_at = CURRENT_TIMESTAMP
        ''', (user['id'], symbol, shares))

        db.commit()
        db.close()

        return jsonify({'success': True, 'symbol': symbol, 'shares': shares})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/history', methods=['POST'])
@login_required
def save_conversation():
    """Save conversation to history"""
    try:
        data = request.get_json()
        message = data.get('message')
        response = data.get('response')

        if not message or not response:
            return jsonify({'error': 'Message and response required'}), 400

        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            INSERT INTO conversations (user_id, message, response)
            VALUES (?, ?, ?)
        ''', (session['user_id'], message, response))
        db.commit()
        db.close()

        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Stock symbol to company name mapping for better TTS
STOCK_NAMES = {
    'AAPL': 'Apple',
    'GOOGL': 'Google',
    'MSFT': 'Microsoft',
    'AMZN': 'Amazon',
    'TSLA': 'Tesla',
    'META': 'Meta',
    'NVDA': 'NVIDIA',
    'DJT': 'Truth Social'
}

def get_user_portfolio_holdings():
    """Get actual portfolio holdings for the default user"""
    try:
        user = get_or_create_user()
        db = get_db()
        cursor = db.cursor()
        cursor.execute('SELECT symbol, shares FROM portfolio WHERE user_id = ?', (user['id'],))
        holdings = {}
        for row in cursor.fetchall():
            holdings[row[0]] = row[1]
        db.close()
        return holdings
    except:
        return {}

def analyze_portfolio_performance(stocks, portfolio_holdings=None):
    """Analyze portfolio performance using actual holdings"""
    if portfolio_holdings is None:
        portfolio_holdings = get_user_portfolio_holdings()

    total_value = 0
    total_change = 0
    gainers = []
    losers = []

    for symbol, data in stocks.items():
        if 'error' in data:
            continue

        shares = portfolio_holdings.get(symbol, 0)
        if shares == 0:
            continue  # Skip stocks user doesn't own

        price = data.get('price', 0)
        change = data.get('change', 0)
        change_pct = data.get('change_percent', 0)

        # Use actual shares owned
        position_value = price * shares
        position_change = change * shares

        total_value += position_value
        total_change += position_change

        stock_name = STOCK_NAMES.get(symbol, symbol)
        if change > 0:
            gainers.append({'symbol': symbol, 'name': stock_name, 'change': change, 'change_pct': change_pct, 'shares': shares})
        elif change < 0:
            losers.append({'symbol': symbol, 'name': stock_name, 'change': change, 'change_pct': change_pct, 'shares': shares})

    # Sort by change percentage
    gainers.sort(key=lambda x: x['change_pct'], reverse=True)
    losers.sort(key=lambda x: x['change_pct'])

    total_change_pct = (total_change / (total_value - total_change)) * 100 if total_value > total_change else 0

    if total_value == 0:
        return "No portfolio holdings entered yet. Add shares to track your portfolio."

    summary = f"""Portfolio Value: ${total_value:,.2f}
Today's Change: ${total_change:+,.2f} ({total_change_pct:+.2f}%)

Top Gainers:
"""
    for stock in gainers[:3]:
        summary += f"  {stock['name']} ({stock['shares']} shares): {stock['change_pct']:+.2f}%\n"

    if losers:
        summary += "\nTop Losers:\n"
        for stock in losers[:3]:
            summary += f"  {stock['name']} ({stock['shares']} shares): {stock['change_pct']:+.2f}%\n"

    return summary

def extract_insights(stocks):
    """Extract actionable insights from stock data"""
    insights = []

    # Detect strong performers (>5% gain)
    for symbol, data in stocks.items():
        if 'error' in data:
            continue

        change_pct = data.get('change_percent', 0)

        if change_pct > 5:
            insights.append({
                'type': 'strong_gain',
                'symbol': symbol,
                'message': f"{symbol} up {change_pct:+.2f}% - Strong performance",
                'severity': 'positive',
                'action': f"Research what's driving {symbol}'s momentum"
            })
        elif change_pct < -5:
            insights.append({
                'type': 'sharp_decline',
                'symbol': symbol,
                'message': f"{symbol} down {change_pct:+.2f}% - Significant drop",
                'severity': 'negative',
                'action': f"Review {symbol} position and news"
            })
        elif abs(change_pct) > 3:
            insights.append({
                'type': 'notable_move',
                'symbol': symbol,
                'message': f"{symbol} moved {change_pct:+.2f}% today",
                'severity': 'neutral',
                'action': f"Monitor {symbol} for continued volatility"
            })

    # Add general insights if portfolio is doing well
    total_gainers = sum(1 for s, d in stocks.items() if 'error' not in d and d.get('change_percent', 0) > 0)
    total_stocks = sum(1 for s, d in stocks.items() if 'error' not in d)

    if total_gainers > total_stocks * 0.75:
        insights.append({
            'type': 'broad_rally',
            'symbol': 'PORTFOLIO',
            'message': f"{total_gainers} of {total_stocks} stocks are up - Strong market day",
            'severity': 'positive',
            'action': 'Consider taking profits on overextended positions'
        })

    return insights[:5]  # Return top 5 insights

def detect_alerts(stocks):
    """Detect alerts for unusual activity"""
    alerts = []

    for symbol, data in stocks.items():
        if 'error' in data:
            continue

        change_pct = data.get('change_percent', 0)
        volume = data.get('volume', 0)

        # Alert on extreme moves
        if abs(change_pct) > 10:
            alerts.append({
                'type': 'extreme_move',
                'symbol': symbol,
                'message': f"{symbol}: {change_pct:+.2f}% move - Extreme volatility!",
                'severity': 'urgent',
                'timestamp': datetime.now().isoformat()
            })

        # Alert on significant moves
        elif abs(change_pct) > 5:
            alerts.append({
                'type': 'significant_move',
                'symbol': symbol,
                'message': f"{symbol}: {change_pct:+.2f}% - Significant movement",
                'severity': 'warning',
                'timestamp': datetime.now().isoformat()
            })

    return alerts

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
