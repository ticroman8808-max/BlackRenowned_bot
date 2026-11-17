import os
from flask import Flask
from threading import Thread
import config
from pyrogram import Client

# Servidor web mínimo para satisfacer el puerto HTTP de Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# Iniciar el servidor web en un hilo paralelo para mantener vivo a Render
Thread(target=run_web).start()

# Inicializar el bot de Pyrogram
bot = Client(
    "BlackRenowned",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN
)

@bot.on_message()
def start(client, message):
    message.reply("¡Hola! El bot está activo y funcionando.")

if __name__ == "__main__":
    print("Iniciando bot y servidor web...")
    bot.run()

