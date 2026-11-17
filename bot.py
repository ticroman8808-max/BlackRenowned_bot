import os
import asyncio
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from motor.motor_asyncio import AsyncIOMotorClient
from config import BOT_TOKEN, API_ID, API_HASH, DATABASE_URL, DATABASE_NAME

# Inicializar el cliente del Bot
app = Client(
    "PyroRenameBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# Conexión a MongoDB
mongo = AsyncIOMotorClient(DATABASE_URL)
db = mongo[DATABASE_NAME]
users_col = db["users"]

# Memoria temporal para guardar el nuevo nombre que el usuario escribe
user_data = {}

@app.on_message(filters.command("start"))
async def start_command(client, message: Message):
    user_id = message.from_user.id
    # Guardar usuario en la base de datos si no existe
    found = await users_col.find_one({"_id": user_id})
    if not found:
        await users_col.insert_one({"_id": user_id})
    
    welcome_text = (
        f"¡Hola {message.from_user.mention}! 👋\n\n"
        "Soy tu bot de renombrado avanzado.\n"
        "Envíame cualquier archivo (documento, video o audio) y te ayudaré a cambiarle el nombre."
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Configuración", callback_data="settings"),
         InlineKeyboardButton("ℹ️ Ayuda", callback_data="help")]
    ])
    
    await message.reply_text(welcome_text, reply_markup=keyboard)

@app.on_message(filters.document | filters.video | filters.audio)
async def receive_file(client, message: Message):
    user_id = message.from_user.id
    user_data[user_id] = {
        "message": message,
        "new_name": None
    }
    
    file_name = message.document.file_name if message.document else (message.video.file_name if message.video else "Archivo de audio")
    
    await message.reply_text(
        f"📁 **Archivo detectado:** `{file_name}`\n\n"
        "Por favor, envíame el **nuevo nombre** que quieres que tenga el archivo (con su extensión, ej: `pelicula.mp4`)."
    )

@app.on_message(filters.text & ~filters.command(["start"]))
async def get_new_name(client, message: Message):
    user_id = message.from_user.id
    if user_id not in user_data or user_data[user_id].get("message") is None:
        await message.reply_text("Por favor, primero envíame un archivo multimedia o documento.")
        return
    
    new_name = message.text.strip()
    user_data[user_id]["new_name"] = new_name
    
    original_msg = user_data[user_id]["message"]
    
    status_msg = await message.reply_text("⏳ **Procesando archivo...** Descargando de los servidores de Telegram.")
    
    try:
        # Descargar el archivo al servidor del bot
        file_path = await original_msg.download(file_name=new_name)
        
        await status_msg.edit_text("⬆️ **Subiendo archivo renombrado...**")
        
        # Subir el archivo renombrado de vuelta a Telegram
        if original_msg.document:
            await client.send_document(
                chat_id=message.chat.id,
                document=file_path,
                caption=f"Renombrado a: `{new_name}`"
            )
        elif original_msg.video:
            await client.send_video(
                chat_id=message.chat.id,
                video=file_path,
                caption=f"Renombrado a: `{new_name}`"
            )
        elif original_msg.audio:
            await client.send_audio(
                chat_id=message.chat.id,
                audio=file_path,
                caption=f"Renombrado a: `{new_name}`"
            )
            
        # Borrar el archivo temporal del servidor
        if os.path.exists(file_path):
            os.remove(file_path)
            
        await status_msg.delete()
        del user_data[user_id]
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Ocurrió un error al procesar el archivo:\n`{str(e)}`")

@app.on_callback_query()
async def callback_query(client, callback_query):
    data = callback_query.data
    if data == "settings":
        await callback_query.answer("Menú de configuración en desarrollo.", show_alert=True)
    elif data == "help":
        await callback_query.answer("Simplemente envía un archivo y luego escribe su nuevo nombre.", show_alert=True)

# Iniciar el bot
print("¡Bot iniciado correctamente!")
app.run()
