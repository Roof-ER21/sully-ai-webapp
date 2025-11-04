#!/usr/bin/env python3
"""
Sully AI - Production Web App
Zero setup for Boss Man - just send him the URL!
"""

from flask import Flask, render_template_string, request, jsonify
import requests
from datetime import datetime
import json
from typing import Dict, List, Any
from groq import Groq
import pytz
import os

app = Flask(__name__)

# Configuration from environment (will be set in Railway)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")  # Optional: for live news search
STOCK_SYMBOLS = os.getenv("STOCK_SYMBOLS", "TSLA,AAPL,NVDA,MSFT,GOOGL,AMZN,META").split(',')
BOSTON_INTENSITY = int(os.getenv("BOSTON_INTENSITY", "7"))

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
                params = {'interval': '1d', 'range': '5d'}
                response = self.session.get(url, params=params, timeout=10)

                if response.status_code == 200:
                    data = response.json()
                    quote = data['chart']['result'][0]['meta']
                    current_price = quote.get('regularMarketPrice', 0)
                    previous_close = quote.get('previousClose', 0)
                    change = current_price - previous_close
                    change_percent = (change / previous_close * 100) if previous_close else 0

                    stock_data[symbol] = {
                        'symbol': symbol,
                        'price': current_price,
                        'change': change,
                        'change_percent': change_percent,
                        'previous_close': previous_close,
                        'volume': quote.get('regularMarketVolume', 0)
                    }
            except Exception as e:
                stock_data[symbol] = {'error': str(e)}
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

# ===== SULLY AI =====
class SullyAI:
    def __init__(self, api_key: str, boston_intensity: int = 7):
        self.client = Groq(api_key=api_key)
        self.boston_intensity = boston_intensity
        self.conversation_history: List[Dict[str, str]] = []
        self.system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        return f"""You are Sully, a wicked smaht AI assistant from Boston. You work for Roof ER (The Roof Docs),
the best damn storm restoration roofing company from Virginia to Pennsylvania (DMV area represent!). You're helping
the boss man stay on top of his stocks, the Patriots, the Celtics, and fantasy football.

PERSONALITY TRAITS:
- Boston accent level: {self.boston_intensity}/10 - Use it naturally, not forced. Drop R's (cah, heah, pahk),
  use "wicked" as intensifier, throw in "kid", "guy", "boss"
- You're smart about markets and sports, not just accent jokes
- You LOVE New England sports - Patriots and Celtics are your LIFE. When they win, you're hyped!
- You're proud to work for Roof ER and bridge Boston attitude with DMV territory (VA/MD/PA)
- You're funny but respectful - boss man is the boss

BOSTON HUMOR & SAYINGS (use naturally):
- "That's wicked good/bad" - Intensifier for everything
- "Pahk the cah" - Classic Boston, but don't overdo it
- "Down the cah-pah" - Something's broken/wrong
- "Masshole" pride - Own it with a wink
- "The T" - Boston transit (always late jokes)
- "Lost your khakis? Lost your car keys!" - Boston wordplay
- "Southie chair" - Saving parking spots (local humor)
- Patriots/Tom Brady references - You miss him but Drake Maye is the future
- Celtics dynasty talk - Banner 18 baby!
- Dunkin' Donuts > Starbucks (always)

DMV AREA AWARENESS (you work here now):
- You respect the DMV (DC/Maryland/Virginia) - it's good roofing territory
- "Washing-TON of opportunities" - Weight pun for DMV
- Traffic jokes - "495 at rush hour? Wicked nightmare, kid"
- You bridge Boston sports passion with DMV business savvy
- Maryland crab cakes are "not bad for non-New England seafood"

ROOFING CONNECTION:
- Storm season = business season
- "If it's rainin', we're gainin'" - roofing motto
- Connect weather to stocks ("Stormy markets need solid foundations")
- Roof ER handles hail, wind, storm damage across VA/MD/PA

SPORTS & STOCKS:
- When stocks moon: "We're goin' to the moon, kid! Time to buy that boat!"
- When stocks tank: "Ah, markets are cyclical like New England weather - we'll bounce back"
- Patriots talk: Get hyped about Drake Maye, but realistic about rebuild
- Celtics: Brown/Tatum two-way dominance, Banner 18 energy
- Fantasy: "Start your studs" mentality

RESPONSE FORMATTING (CRITICAL):
- Break up responses into SHORT paragraphs (2-3 sentences max)
- Use line breaks between thoughts
- Add bullet points for lists
- Use emojis as visual breaks
- NO long run-on sentences
- Make it scannable and easy to read on mobile

Example Good Format:
"Hey boss! TSLA is up 5% today. That's wicked good news!

📈 Quick take:
- Price: $245.50 (+$12.30)
- Volume looking solid
- Momentum is strong

The market's treatin' ya right today, kid. Keep an eye on it though - earnings comin' up next week."

Example Bad Format (DON'T DO THIS):
"Hey boss TSLA is up 5% today at $245.50 which is up $12.30 and the volume is looking solid and the momentum is strong so the market is treating you right today kid but keep an eye on it because earnings are coming up next week."

Keep it natural - you're a Boston guy in DMV territory, helping boss man crush it. Be genuinely helpful with sharp wit AND easy-to-read formatting."""

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
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sully AI - Boston Sports Assistant | Roof ER</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎩</text></svg>">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&display=swap" rel="stylesheet">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Inter', sans-serif;
            background: linear-gradient(135deg, #1a1a1a 0%, #0a0a0a 100%);
            color: #ffffff;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }

        .app-container {
            width: 100%;
            max-width: 420px;
            background: #0a0a0a;
            border-radius: 30px;
            overflow: hidden;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.8);
            border: 2px solid #1a1a1a;
        }

        /* Header Section */
        .header {
            background: linear-gradient(135deg, #c41e3a 0%, #8b1528 100%);
            padding: 30px 25px 25px;
            position: relative;
            overflow: hidden;
        }

        .header::before {
            content: '';
            position: absolute;
            top: -50%;
            right: -20%;
            width: 300px;
            height: 300px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 50%;
        }

        .header::after {
            content: '';
            position: absolute;
            bottom: -30%;
            left: -15%;
            width: 250px;
            height: 250px;
            background: rgba(0, 0, 0, 0.1);
            border-radius: 50%;
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
            width: 70px;
            height: 70px;
            background: white;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.3);
            border: 3px solid #1a1a1a;
        }

        .logo-text {
            font-size: 24px;
            font-weight: 900;
            color: #c41e3a;
            text-align: center;
            line-height: 1;
        }

        .title-section {
            text-align: center;
            position: relative;
            z-index: 2;
        }

        .main-title {
            font-size: 32px;
            font-weight: 900;
            margin-bottom: 5px;
            text-shadow: 2px 2px 4px rgba(0, 0, 0, 0.3);
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
        }

        .boston-hat {
            font-size: 28px;
        }

        .subtitle {
            font-size: 14px;
            font-weight: 600;
            opacity: 0.95;
            margin-bottom: 3px;
        }

        .powered-by {
            font-size: 11px;
            opacity: 0.8;
            font-weight: 500;
        }

        /* Chat Area */
        .chat-area {
            background: #0f0f0f;
            min-height: 400px;
            max-height: 500px;
            overflow-y: auto;
            padding: 20px;
        }

        /* Category Pills */
        .category-pills {
            display: flex;
            gap: 10px;
            padding: 20px;
            background: #0f0f0f;
            flex-wrap: wrap;
            justify-content: center;
            border-top: 1px solid #1a1a1a;
        }

        .pill {
            padding: 10px 18px;
            border-radius: 25px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.3s ease;
            border: 2px solid;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .pill.celtics {
            background: linear-gradient(135deg, #007A33 0%, #005a26 100%);
            border-color: #00a84f;
            color: white;
        }

        .pill.celtics:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(0, 122, 51, 0.4);
        }

        .pill.patriots {
            background: linear-gradient(135deg, #002244 0%, #001a33 100%);
            border-color: #0044aa;
            color: white;
        }

        .pill.patriots:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(0, 34, 68, 0.4);
        }

        .pill.news {
            background: linear-gradient(135deg, #6b7280 0%, #4b5563 100%);
            border-color: #9ca3af;
            color: white;
        }

        .pill.news:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(107, 114, 128, 0.4);
        }

        .pill.stocks {
            background: linear-gradient(135deg, #16a34a 0%, #15803d 100%);
            border-color: #22c55e;
            color: white;
        }

        .pill.stocks:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(22, 163, 74, 0.4);
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
    </style>
</head>
<body>
    <div class="app-container">
        <!-- Header -->
        <div class="header">
            <div class="logo-section">
                <div class="logo-circle">
                    <div style="text-align: center;">
                        <div class="logo-text">ROOF</div>
                        <div class="logo-text">ER</div>
                    </div>
                </div>
            </div>
            <div class="title-section">
                <div class="main-title">
                    <span class="boston-hat">🎩</span>
                    Sully AI
                    <span class="boston-hat">🏀</span>
                </div>
                <div class="subtitle">Your Wicked Smaht Boston Assistant</div>
                <div class="powered-by">Powered by Roof ER - The Roof Docs</div>
            </div>
        </div>

        <!-- Category Pills -->
        <div class="category-pills">
            <button class="pill celtics" onclick="sendQuick('How are the Celtics doing? Any latest news?')">
                <span class="pill-icon">🍀</span>
                Celtics
            </button>
            <button class="pill patriots" onclick="sendQuick('What\\'s the latest Patriots news today?')">
                <span class="pill-icon">🏈</span>
                Patriots
            </button>
            <button class="pill news" onclick="sendQuick('Any big news today?')">
                <span class="pill-icon">📰</span>
                News
            </button>
            <button class="pill stocks" onclick="sendQuick('How are my stocks looking?')">
                <span class="pill-icon">📈</span>
                Stocks
            </button>
        </div>

        <!-- Chat Area -->
        <div class="chat-area" id="messages">
            <div class="message">
                <div class="avatar">🎩</div>
                <div class="message-content">
                    <div class="message-name">Sully</div>
                    <div class="message-bubble">Hey there, boss! Sully heah, straight outta Southie! 🍀 Ready to talk Celtics, Pats, or check those stocks? I'm wicked smaht and here to help ya out. What's on your mind today?</div>
                </div>
            </div>
        </div>

        <!-- Loading Indicator -->
        <div class="loading" id="loading">🎩 Sully's thinkin'...</div>

        <!-- Input Section -->
        <div class="input-section">
            <div class="input-wrapper">
                <input type="text" id="user-input" class="input-field" placeholder="Ask Sully anything about Boston sports..." onkeypress="handleKeyPress(event)" autocomplete="off">
            </div>
            <button class="send-button" id="send-btn" onclick="sendMessage()">➤</button>
        </div>
    </div>
    <script>
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
                } else {
                    addMessage('Ah jeez, hit a snag there. Try again, kid.', 'sully');
                }
            } catch (error) {
                setLoading(false);
                addMessage('Down the cah-pah with the connection. Try again, boss.', 'sully');
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

        document.getElementById('user-input').focus();
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/chat', methods=['POST'])
def chat():
    global current_data, last_update, aggregator, sully

    # Initialize on first request
    if aggregator is None:
        aggregator = NewsAggregator()
    if sully is None:
        if not GROQ_API_KEY:
            return jsonify({'response': 'Ah jeez, the API key ain\'t set up yet, kid. Tell the dev team!', 'error': True})
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
            stocks_text = "📊 HERE'S YOUR PORTFOLIO, BOSS:\n\n"
            for symbol, stock_data in current_data['stocks'].items():
                if 'error' not in stock_data:
                    price = stock_data['price']
                    change = stock_data['change']
                    change_pct = stock_data['change_percent']
                    indicator = "📈" if change > 0 else "📉" if change < 0 else "➡️"
                    stocks_text += f"{symbol}: ${price:.2f} {indicator} {change:+.2f} ({change_pct:+.2f}%)\n"
            response = sully.chat(f"Give me a quick take on these stocks: {stocks_text}", current_data)

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

        return jsonify({'response': response})

    except Exception as e:
        return jsonify({'response': f"Ah jeez, hit a snag: {str(e)}", 'error': True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
