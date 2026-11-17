import os
from flask import Flask
from threading import Thread
import config
from pyrogram import Client, filters
from pymongo import MongoClient

# Servidor web para mantener vivo el Web Service en Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running and ready for heavy files!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

Thread(target=run_web).start()

# Conexión a MongoDB Atlas
mongo_client = MongoClient(config.DATABASE_URL)
db = mongo_client[config.DATABASE_NAME]
states_collection = db["user_states"]

# Inicializar cliente de Pyrogram
bot = Client(
    "BlackRenowned",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN
)

@bot.on_message(filters.command("start"))
def start_command(client, message):
    user_id = message.from_user.id
    states_collection.update_one(
        {"user_id": user_id},
        {"$set": {"step": "waiting_image", "thumb_id": None, "file_id": None}},
        upsert=True
    )
    message.reply(
        "🚀 **Bot Pro de Renombrado y Miniaturas activado**\n\n"
        "1️⃣ Envía la **imagen** que deseas usar como miniatura.\n"
        "2️⃣ Envía el **archivo PDF** (compatible con archivos grandes).\n"
        "3️⃣ Escribe el **nuevo nombre** para recibir tu PDF procesado."
    )

# 1. Recibir la imagen (Miniatura)
@bot.on_message(filters.photo & ~filters.command(["start"]))
def receive_image(client, message):
    user_id = message.from_user.id
    user_state = states_collection.find_one({"user_id": user_id})

    if not user_state or user_state.get("step") != "waiting_image":
        message.reply("⚠️ Envía /start para reiniciar el ciclo o sigue el paso actual.")
        return

    photo_id = message.photo.file_id
    
    states_collection.update_one(
        {"user_id": user_id},
        {"$set": {"step": "waiting_pdf", "thumb_id": photo_id}}
    )
    
    message.reply("✅ **Miniatura guardada con éxito.**\n\nAhora envía el **archivo PDF** que deseas renombrar.")

# 2. Recibir el documento PDF (Soporte avanzado)
@bot.on_message(filters.document & ~filters.command(["start"]))
def receive_pdf(client, message):
    user_id = message.from_user.id
    user_state = states_collection.find_one({"user_id": user_id})

    if not user_state or user_state.get("step") != "waiting_pdf":
        message.reply("⚠️ Primero debes enviar la imagen para la miniatura. Envía /start para comenzar.")
        return

    # Validar extensión PDF
    file_name = message.document.file_name
    if not file_name or not file_name.lower().endswith(".pdf"):
        message.reply("❌ El archivo adjunto no es un documento PDF válido.")
        return

    file_id = message.document.file_id
    file_size_mb = message.document.file_size / (1024 * 1024)

    states_collection.update_one(
        {"user_id": user_id},
        {"$set": {"step": "waiting_name", "file_id": file_id}}
    )

    message.reply(
        f"✅ **PDF recibido** `({file_size_mb:.2f} MB)`.\n\n"
        "Por último, **escribe el nuevo nombre** que tendrá el archivo (sin agregar .pdf al final)."
    )

# 3. Recibir el nombre, procesar y devolver el archivo
@bot.on_message(filters.text & ~filters.command(["start", "help", "info"]))
def receive_name(client, message):
    user_id = message.from_user.id
    user_state = states_collection.find_one({"user_id": user_id})

    if not user_state or user_state.get("step") != "waiting_name":
        return

    custom_name = message.text.strip()
    if not custom_name.lower().endswith(".pdf"):
        new_name = custom_name + ".pdf"
    else:
        new_name = custom_name

    thumb_id = user_state.get("thumb_id")
    file_id = user_state.get("file_id")

    status_msg = message.reply("⏳ **Procesando archivo y aplicando miniatura...** Esto puede tomar unos segundos según el tamaño.")

    try:
        # Descarga optimizada de archivos
        thumb_path = client.download_media(thumb_id)
        pdf_path = client.download_media(file_id)

        # Ruta con el nuevo nombre personalizado
        directory = os.path.dirname(pdf_path)
        new_pdf_path = os.path.join(directory, new_name)
        
        if os.path.exists(new_pdf_path):
            os.remove(new_pdf_path)
        os.rename(pdf_path, new_pdf_path)

        # Envío del documento con miniatura integrada
        client.send_document(
            chat_id=message.chat.id,
            document=new_pdf_path,
            thumb=thumb_path,
            caption=f"✨ **¡Archivo procesado con éxito!**\n📄 Nombre: `{new_name}`"
        )

        # Limpieza de archivos locales temporales en el servidor
        if thumb_path and os.path.exists(thumb_path):
            os.remove(thumb_path)
        if new_pdf_path and os.path.exists(new_pdf_path):
            os.remove(new_pdf_path)

        # Reiniciar estado para el siguiente archivo en bucle continuo
        states_collection.update_one(
            {"user_id": user_id},
            {"$set": {"step": "waiting_image", "thumb_id": None, "file_id": None}}
        )

        status_msg.delete()
        message.reply("🔄 **¡Listo para el siguiente!** Envía una nueva imagen para comenzar otro proceso.")

    except Exception as e:
        status_msg.edit(f"❌ **Error al procesar el archivo:**\n`{str(e)}`")
        states_collection.update_one(
            {"user_id": user_id},
            {"$set": {"step": "waiting_image", "thumb_id": None, "file_id": None}}
        )

if __name__ == "__main__":
    print("Iniciando bot pro en modo agente...")
    bot.run()
