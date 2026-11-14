# 🚀 MiniRender PaaS Platform (Full Working Edition)

यह प्रोजेक्ट आपकी Google AI Studio बातचीत से सभी 9 मुख्य फाइलों और अतिरिक्त VPS/Render कॉन्फ़िगरेशन को जोड़कर **100% वर्किंग और रेडी-टू-डिप्लॉय** रूप में तैयार किया गया है।

---

## 📁 प्रोजेक्ट स्ट्रक्चर (Project Structure)

```text
minirender/
├── requirements.txt
├── database.py
├── auth.py
├── notifier.py
├── process_manager.py
├── github_manager.py
├── telegram_bot.py
├── main.py
├── static/
│   └── index.html
├── .env.example
├── render.yaml
├── Dockerfile
└── vps_docker_optional/
```

## ⚡ लोकल कंप्यूटर या VPS पर तुरंत कैसे चलाएं?

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```
ब्राउज़र में खोलें: `http://localhost:8000`
* डिफ़ॉल्ट Login Username: `admin`
* डिफ़ॉल्ट Login Password: `admin123` (या जो आप `ADMIN_PASSWORD` में सेट करें)

## 🌐 Render.com पर फाइनल डिप्लॉय कैसे करें?

1. सभी फाइलें GitHub Repository में push करें।
2. Render.com पर नई Web Service बनाएं:
   * Build Command: `pip install -r requirements.txt`
   * Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
3. Environment Variables में ADMIN_USERNAME, ADMIN_PASSWORD, SECRET_KEY, Telegram और GitHub OAuth variables सेट करें।
4. GitHub OAuth callback URL: `https://आपका-ऐप.onrender.com/api/github/callback`
