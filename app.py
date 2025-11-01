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

# ===== SULLY AI =====
class SullyAI:
    def __init__(self, api_key: str, boston_intensity: int = 7):
        self.client = Groq(api_key=api_key)
        self.boston_intensity = boston_intensity
        self.conversation_history: List[Dict[str, str]] = []
        self.system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        return f"""You are Sully, a wicked smaht AI assistant from Boston. You work for Roof ER (The Roof Docs),
the best damn storm restoration roofing company from Virginia to Pennsylvania. You're helping the boss man
stay on top of his stocks, the Patriots, the Celtics, and fantasy football.

PERSONALITY TRAITS:
- Boston accent level: {self.boston_intensity}/10 - Use it naturally, not forced. Drop R's sometimes (cah for car,
  heah for here), use "wicked" as an intensifier, throw in "kid" or "guy"
- You're smart about markets and sports, not just accent jokes
- You LOVE New England sports - Patriots and Celtics are your life
- You're proud to work for Roof ER and sometimes connect business insights to roofing/weather
- You're funny but respectful - boss man is the boss
- When stocks go up, you're hyped. When they tank, you're realistic but optimistic

Keep it natural - you're from Boston, not performing in a sketch. Be genuinely helpful with market analysis and sports insights."""

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
    <title>🏗️ Sully AI - Boston-Born Assistant</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🏗️</text></svg>">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a5490 0%, #0d3a66 100%);
            min-height: 100vh;
            padding: 10px;
        }
        .container { max-width: 1000px; margin: 0 auto; }
        .header {
            background: rgba(255, 255, 255, 0.98);
            padding: 20px;
            border-radius: 15px;
            text-align: center;
            margin-bottom: 15px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.3);
        }
        .header h1 { color: #1a5490; font-size: 2em; margin-bottom: 5px; }
        .header p { color: #666; font-size: 1em; }
        .chat-container {
            background: rgba(255, 255, 255, 0.98);
            border-radius: 15px;
            padding: 20px;
            height: calc(100vh - 200px);
            min-height: 500px;
            display: flex;
            flex-direction: column;
            box-shadow: 0 8px 32px rgba(0,0,0,0.3);
        }
        .messages {
            flex: 1;
            overflow-y: auto;
            margin-bottom: 15px;
            padding: 15px;
            background: #f8f9fa;
            border-radius: 10px;
        }
        .message { margin-bottom: 15px; animation: slideIn 0.3s ease; }
        @keyframes slideIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        .message.user { text-align: right; }
        .message-bubble {
            display: inline-block;
            padding: 12px 18px;
            border-radius: 18px;
            max-width: 80%;
            word-wrap: break-word;
            white-space: pre-wrap;
            font-size: 15px;
            line-height: 1.5;
        }
        .message.user .message-bubble { background: #1a5490; color: white; border-bottom-right-radius: 5px; }
        .message.sully .message-bubble { background: white; color: #333; border: 2px solid #1a5490; border-bottom-left-radius: 5px; }
        .message-label { font-size: 0.8em; color: #666; margin-bottom: 5px; font-weight: 600; }
        .quick-actions { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
        .quick-btn {
            padding: 8px 16px;
            background: rgba(26, 84, 144, 0.1);
            border: 2px solid #1a5490;
            border-radius: 20px;
            cursor: pointer;
            font-size: 13px;
            color: #1a5490;
            font-weight: 600;
            transition: all 0.3s;
        }
        .quick-btn:hover { background: #1a5490; color: white; transform: translateY(-2px); }
        .input-area { display: flex; gap: 10px; }
        #user-input {
            flex: 1;
            padding: 12px 18px;
            border: 2px solid #1a5490;
            border-radius: 25px;
            font-size: 15px;
            outline: none;
        }
        #user-input:focus { border-color: #0d3a66; }
        .btn-send {
            padding: 12px 28px;
            border: none;
            border-radius: 25px;
            font-size: 15px;
            font-weight: 700;
            cursor: pointer;
            background: #1a5490;
            color: white;
            transition: all 0.3s;
        }
        .btn-send:hover { background: #0d3a66; transform: translateY(-2px); box-shadow: 0 5px 15px rgba(26, 84, 144, 0.4); }
        .btn-send:disabled { opacity: 0.5; cursor: not-allowed; }
        .loading { display: none; text-align: center; padding: 15px; color: #1a5490; font-style: italic; font-weight: 600; }
        .loading.active { display: block; }
        @media (max-width: 768px) {
            .header h1 { font-size: 1.5em; }
            .message-bubble { max-width: 90%; font-size: 14px; }
            .quick-actions { flex-direction: column; }
            .quick-btn { width: 100%; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🏗️ Sully AI 🏗️</h1>
            <p><strong>Boston-Born AI Assistant for Boss Man</strong></p>
            <p style="font-size: 0.85em; margin-top: 8px; color: #999;">Built for Roof ER - The Roof Docs</p>
        </div>
        <div class="chat-container">
            <div class="quick-actions">
                <button class="quick-btn" onclick="sendQuick('How are my stocks looking?')">📊 Stocks</button>
                <button class="quick-btn" onclick="sendQuick('What about the Patriots?')">🏈 Patriots</button>
                <button class="quick-btn" onclick="sendQuick('How are the Celtics doing?')">🏀 Celtics</button>
                <button class="quick-btn" onclick="sendQuick('Any fantasy football tips?')">🏈 Fantasy</button>
            </div>
            <div class="messages" id="messages">
                <div class="message sully">
                    <div class="message-label">🤖 Sully</div>
                    <div class="message-bubble">Hey boss! Sully heah, ready to help ya out. Ask me about your stocks, the Pats, the Celtics, or anything else. I'm wicked smaht and here for ya!</div>
                </div>
            </div>
            <div class="loading" id="loading">🤖 Sully's thinkin'...</div>
            <div class="input-area">
                <input type="text" id="user-input" placeholder="Ask Sully anything..." onkeypress="handleKeyPress(event)" autocomplete="off">
                <button class="btn-send" id="send-btn" onclick="sendMessage()">Send</button>
            </div>
        </div>
    </div>
    <script>
        function addMessage(text, sender) {
            const messagesDiv = document.getElementById('messages');
            const messageDiv = document.createElement('div');
            messageDiv.className = `message ${sender}`;
            const label = document.createElement('div');
            label.className = 'message-label';
            label.textContent = sender === 'user' ? '👤 You' : '🤖 Sully';
            const bubble = document.createElement('div');
            bubble.className = 'message-bubble';
            bubble.textContent = text;
            messageDiv.appendChild(label);
            messageDiv.appendChild(bubble);
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
        else:
            response = sully.chat(user_message, current_data)

        return jsonify({'response': response})

    except Exception as e:
        return jsonify({'response': f"Ah jeez, hit a snag: {str(e)}", 'error': True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
