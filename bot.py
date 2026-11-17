import os
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from motor.motor_asyncio import AsyncIOMotorClient
from config import BOT_TOKEN, API_ID, API_HASH, DATABASE_URL, DATABASE_NAME

# Inicializar el cliente del Bot sin conflictos
app = Client(
    "BlackRenownedBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# Conexión a MongoDB
mongo = AsyncIOMotorClient(DATABASE_URL)
db = mongo[DATABASE_NAME]
users_col = db["users"]

user_data = {}

@app.on_message(filters.command("start"))
async def start_command(client, message: Message):
    user_id = message.from_user.id
    found = await users_col.find_one({"_id": user_id})
    if not found:
        await users_col.insert_one({"_id": user_id})
    
    welcome_text = (
        f"¡Hola {message.from_user.mention}! 👋\n\n"
        "Soy tu bot de renombrado.\n"
        "Envíame cualquier archivo (PDF, video o documento) y te ayudaré a cambiarle el nombre."
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Configuración", callback_data="settings"),
         InlineKeyboardButton("ℹ️ Ayuda", callback_data="help")]
    ])
    
    await message.reply_text(welcome_text, reply_markup=keyboard)

@app.on_message(filters.document | filters.video | filters.audio)
async def receive_file(client, message: Message):
    user_id = message.from_user.id
    user_data[user_id] = {"message": message}
    
    file_name = message.document.file_name if message.document else "Archivo"
    
    await message.reply_text(
        f"📁 **Archivo recibido:** `{file_name}`\n\n"
        "Ahora envíame el **nuevo nombre** que quieres que tenga (ej: `documento_nuevo.pdf`)."
    )

@app.on_message(filters.text & ~filters.command(["start"]))
async def get_new_name(client, message: Message):
    user_id = message.from_user.id
    if user_id not in user_data:
        await message.reply_text("Por favor, primero envíame un archivo.")
        return
    
    new_name = message.text.strip()
    original_msg = user_data[user_id]["message"]
    status_msg = await message.reply_text("⏳ Procesando archivo...")
    
    try:
        file_path = await original_msg.download(file_name=new_name)
        await status_msg.edit_text("⬆️ Subiendo archivo renombrado...")
        
        await client.send_document(
            chat_id=message.chat.id,
            document=file_path,
            caption=f"Renombrado a: `{new_name}`"
        )
        
        if os.path.exists(file_path):
            os.remove(file_path)
            
        await status_msg.delete()
        del user_data[user_id]
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: `{str(e)}`")

@app.on_callback_query()
async def callback_query(client, callback_query):
    if callback_query.data == "settings":
        await callback_query.answer("Configuración en desarrollo.", show_alert=True)
    elif callback_query.data == "help":
        await callback_query.answer("Envía un archivo y luego su nuevo nombre.", show_alert=True)

print("¡Bot iniciado correctamente!")
app.run()

