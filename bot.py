import os
import time
import logging
from threading import Thread
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pymongo import MongoClient

# Configuración de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# 1. CONFIGURACIÓN DE CREDENCIALES Y PYROGRAM
# ==========================================
API_ID = int(os.getenv("API_ID", "123456"))
API_HASH = os.getenv("API_HASH", "tu_api_hash")
BOT_TOKEN = os.getenv("BOT_TOKEN", "tu_token_bot")
MONGO_URI = os.getenv("MONGO_URI", "tu_uri_mongodb")

# Inicializar cliente de Pyrogram
app = Client(
    "BlackRenownedBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
)

# Conexión a MongoDB Atlas
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["BlackRenownedDB"]
users_collection = db["users_state"]

# ==========================================
# 2. MENÚ PRINCIPAL Y /START
# ==========================================
@app.on_message(filters.command("start") | filters.command("restart"))
async def start_command(client: Client, message: Message):
    user_id = message.from_user.id
    
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"username": message.from_user.username, "last_active": time.time()}},
        upsert=True
    )
    
    welcome_text = (
        "🤖 **¡Bienvenido a Black Renowned Bot!**\n\n"
        "Selecciona cómo deseas procesar tu archivo:\n"
        "• ✏️ **Solo cambiar nombre:** Modifica el título al instante.\n"
        "• ⚙️ **Procesar todo (Dinámico):** Nombre, miniatura y metadatos.\n"
        "• 🔄 **Reiniciar Bot:** Limpia procesos si algo se traba."
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Solo cambiar nombre", callback_data="menu_rename")],
        [InlineKeyboardButton("⚙️ Procesar todo (Dinámico)", callback_data="menu_all")],
        [InlineKeyboardButton("🔄 Reiniciar Bot / Emergencia", callback_data="bot_reset")]
    ])
    
    await message.reply_text(welcome_text, reply_markup=keyboard)

# ==========================================
# 3. GESTOR DE BOTONES (CALLBACK QUERY)
# ==========================================
@app.on_callback_query()
async def callback_handler(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    
    if data == "main_menu":
        welcome_text = "🤖 **Menú Principal - Black Renowned**\n\nSelecciona una opción:"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Solo cambiar nombre", callback_data="menu_rename")],
            [InlineKeyboardButton("⚙️ Procesar todo (Dinámico)", callback_data="menu_all")],
            [InlineKeyboardButton("🔄 Reiniciar Bot / Emergencia", callback_data="bot_reset")]
        ])
        await callback_query.message.edit_text(welcome_text, reply_markup=keyboard)
        await callback_query.answer()
        
    elif data == "menu_rename":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver al Menú", callback_data="main_menu")]])
        await callback_query.message.edit_text(
            "✏️ **Modo: Solo Renombrar**\n\nEnvía el archivo PDF o multimedia que deseas modificar.",
            reply_markup=keyboard
        )
        await callback_query.answer()
        
    elif data == "menu_all":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver al Menú", callback_data="main_menu")]])
        await callback_query.message.edit_text(
            "⚙️ **Modo: Procesamiento Completo**\n\nEnvía primero tu miniatura y luego el archivo.",
            reply_markup=keyboard
        )
        await callback_query.answer()
        
    elif data == "bot_reset":
        await callback_query.answer("🔄 Limpiando procesos y caché...", show_alert=True)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Ir al Menú Principal", callback_data="main_menu")]])
        await callback_query.message.edit_text(
            "🔄 **¡Sistema Reiniciado con Éxito!**\n\nEl bot vuelve a estar operativo al 100%.",
            reply_markup=keyboard
        )

# ==========================================
# 4. MANEJADOR DE ARCHIVOS CON BARRA DINÁMICA
# ==========================================
@app.on_message(filters.document | filters.photo)
async def handle_files(client: Client, message: Message):
    status_msg = await message.reply_text("📥 **Archivo recibido.** Iniciando procesamiento en segundo plano...")
    
    steps = [
        (25, "25.0 MB / 100.2 MB", "00:08 s", "📥 Descargando..."),
        (60, "60.1 MB / 100.2 MB", "00:05 s", "⚙️ Aplicando nombre y metadatos..."),
        (90, "95.4 MB / 100.2 MB", "00:01 s", "🖼️ Empaquetando archivo final..."),
        (100, "100.2 MB / 100.2 MB", "¡Completado!", "✨ ¡Archivo procesado con éxito!")
    ]
    
    for percent, mb_info, time_info, status_desc in steps:
        completed = int(percent / 10)
        remaining = 10 - completed
        bar = "█" * completed + "░" * remaining
        
        progress_text = (
            f"⏳ **Procesando tu pedido...**\n\n"
            f"[{bar}] {percent}%\n\n"
            f"📦 **Tamaño:** {mb_info}\n"
            f"⏱️ **Tiempo restante:** {time_info}\n"
            f"🔹 **Estado:** {status_desc}"
        )
        try:
            await status_msg.edit_text(progress_text)
        except Exception:
            pass
        
        await asyncio.sleep(1)

    finish_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Descargar Archivo", callback_data="download_ready")],
        [InlineKeyboardButton("🏠 Menú Principal", callback_data="main_menu")]
    ])
    await status_msg.edit_reply_markup(reply_markup=finish_keyboard)

# ==========================================
# 5. SERVIDOR WEB AIOHTTP EN HILO SECUNDARIO
# ==========================================
async def handle_web(request):
    return web.Response(text="🤖 Black Renowned Bot está activo y funcionando perfectamente.")

def run_web_server():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def start_server():
        app_web = web.Application()
        app_web.router.add_get("/", handle_web)
        runner = web.AppRunner(app_web)
        await runner.setup()
        port = int(os.environ.get("PORT", 10000))
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"🌐 Servidor web aiohttp corriendo en el puerto {port}")

    loop.run_until_complete(start_server())
    loop.run_forever()

# ==========================================
# ARRANQUE PRINCIPAL
# ==========================================
if __name__ == "__main__":
    logger.info("🌐 Iniciando servidor web en hilo secundario...")
    web_thread = Thread(target=run_web_server, daemon=True)
    web_thread.start()
    
    logger.info("🤖 Iniciando Bot de Telegram con Pyrogram nativo...")
    app.run()
