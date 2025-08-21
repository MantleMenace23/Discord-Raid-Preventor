from flask import Flask
import threading

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!", 200

def run():
    app.run(host="0.0.0.0", port=6969)  # Render healthcheck port

def keep_alive():
    t = threading.Thread(target=run)
    t.start()
