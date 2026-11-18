import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web
from PIL import Image
from pymongo import MongoClient
from pypdf import PdfReader, PdfWriter
from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# ============================================================
# CONFIGURACIÓN
# ============================================================
def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Falta la variable de entorno obligatoria: {name}")
    return value

API_ID = int(required_env("API_ID"))
API_HASH = required_env("API_HASH")
BOT_TOKEN = required_env("BOT_TOKEN")
MONGO_URI = required_env("MONGO_URI")
DATABASE_NAME = os.getenv("DATABASE_NAME", "BlackRenowned").strip() or "BlackRenowned"

ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "1500"))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024
TMP_ROOT = Path(os.getenv("TMP_DIR", "/tmp/blackrenowned"))
TMP_ROOT.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("BlackRenowned")

# MongoDB: operaciones síncronas de PyMongo se ejecutan con asyncio.to_thread.
mongo_client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=10000,
    connectTimeoutMS=10000,
    tls=True
)
db = mongo_client[DATABASE_NAME]
users_collection = db["users"]
history_collection = db["processing_history"]

bot = Client(
    "BlackRenownedBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
)

# Estados de conversación y tareas activas, por usuario.
user_states: dict[int, dict] = {}
active_tasks: dict[int, asyncio.Task] = {}
last_progress_edit: dict[int, float] = {}


# ============================================================
# UTILIDADES
# ============================================================
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Renombrar archivo", callback_data="mode_rename")],
        [InlineKeyboardButton("🖼️ Miniatura + metadatos", callback_data="mode_full")],
        [InlineKeyboardButton("📚 Procesar varios archivos", callback_data="mode_batch")],
        [InlineKeyboardButton("📜 Historial", callback_data="history")],
        [InlineKeyboardButton("❌ Cancelar proceso", callback_data="cancel")]
    ])


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def media_size(message: Message) -> int:
    media = (
        message.document or message.video or message.audio or
        message.animation or message.voice or message.video_note
    )
    return int(getattr(media, "file_size", 0) or 0)


def media_name(message: Message) -> str:
    media = (
        message.document or message.video or message.audio or
        message.animation or message.voice or message.video_note
    )
    name = getattr(media, "file_name", None)
    if name:
        return name
    ext = ".mp4" if message.video or message.video_note else ".bin"
    return f"archivo_{message.id}{ext}"


def clean_filename(name: str, fallback: str = "archivo") -> str:
    name = Path(name.strip()).name
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    name = name.strip(" .")
    return name[:180] or fallback


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


async def update_progress(current: int, total: int, status: Message, user_id: int, label: str):
    now = time.monotonic()
    if current < total and now - last_progress_edit.get(user_id, 0) < 2.5:
        return
    last_progress_edit[user_id] = now
    percent = int(current * 100 / total) if total else 0
    filled = min(20, percent // 5)
    bar = "█" * filled + "░" * (20 - filled)
    try:
        await status.edit_text(
            f"{label}\n\n[{bar}] **{percent}%**\n"
            f"{format_bytes(current)} / {format_bytes(total)}"
        )
    except Exception:
        pass


async def db_upsert_user(user):
    await asyncio.to_thread(
        users_collection.update_one,
        {"user_id": user.id},
        {"$set": {
            "username": user.username,
            "first_name": user.first_name,
            "last_seen": datetime.now(timezone.utc)
        }, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
        True
    )


async def save_history(user_id: int, original: str, final_name: str, mode: str, size: int, result: str):
    record = {
        "user_id": user_id,
        "original_name": original,
        "final_name": final_name,
        "mode": mode,
        "size": size,
        "result": result,
        "created_at": datetime.now(timezone.utc)
    }
    await asyncio.to_thread(history_collection.insert_one, record)


async def make_thumbnail_jpeg(source: str, destination: str):
    def convert():
        with Image.open(source) as image:
            image = image.convert("RGB")
            image.thumbnail((320, 320))
            image.save(destination, "JPEG", quality=82, optimize=True)
        if os.path.getsize(destination) >= 200 * 1024:
            with Image.open(destination) as image:
                image.save(destination, "JPEG", quality=65, optimize=True)
    await asyncio.to_thread(convert)
    if os.path.getsize(destination) >= 200 * 1024:
        raise ValueError("La miniatura convertida supera 200 KB. Elegí otra imagen.")
    return destination


async def apply_pdf_metadata(path: str, metadata: dict) -> bool:
    if Path(path).suffix.lower() != ".pdf":
        return False

    def edit_pdf():
        reader = PdfReader(path)
        writer = PdfWriter()
        writer.clone_document_from_reader(reader)
        fields = {}
        mapping = {
            "title": "/Title",
            "author": "/Author",
            "subject": "/Subject",
            "keywords": "/Keywords"
        }
        for key, pdf_key in mapping.items():
            value = metadata.get(key, "").strip()
            if value:
                fields[pdf_key] = value
        if fields:
            writer.add_metadata(fields)
        temp_path = path + ".tmp"
        with open(temp_path, "wb") as output:
            writer.write(output)
        os.replace(temp_path, path)
        return bool(fields)

    return await asyncio.to_thread(edit_pdf)


async def download_and_send(
    user_id: int,
    source_message: Message,
    new_name: str,
    mode: str,
    thumbnail: str | None = None,
    metadata: dict | None = None
):
    workdir = Path(tempfile.mkdtemp(prefix=f"user_{user_id}_", dir=TMP_ROOT))
    status = await source_message.reply_text("📥 Preparando descarga…")
    original_name = media_name(source_message)
    size = media_size(source_message)

    try:
        if size and size > MAX_FILE_SIZE:
            raise ValueError(
                f"El archivo pesa {format_bytes(size)} y supera el límite configurado "
                f"de {MAX_FILE_SIZE_MB} MB."
            )

        free = shutil.disk_usage(TMP_ROOT).free
        if size and free < size * 1.25:
            raise ValueError(
                "No hay espacio temporal suficiente en el servidor para procesar este archivo."
            )

        target = workdir / clean_filename(new_name)
        downloaded = await bot.download_media(
            source_message,
            file_name=str(target),
            progress=update_progress,
            progress_args=(status, user_id, "📥 Descargando…")
        )
        if not downloaded or not os.path.isfile(downloaded):
            raise RuntimeError("Telegram no completó la descarga del archivo.")

        actual_path = str(downloaded)
        actual_name = clean_filename(new_name, original_name)

        await status.edit_text("⚙️ Aplicando cambios y preparando el archivo…")
        metadata_changed = False
        if metadata:
            metadata_changed = await apply_pdf_metadata(actual_path, metadata)

        await status.edit_text("📤 Subiendo el archivo procesado…")
        await bot.send_document(
            chat_id=user_id,
            document=actual_path,
            file_name=actual_name,
            thumb=thumbnail if thumbnail and os.path.exists(thumbnail) else None,
            caption=(
                "✨ **Black Renowned Bot**\n"
                f"📄 `{actual_name}`\n"
                + ("🧾 Metadatos PDF actualizados." if metadata_changed else "")
            ),
            force_document=True,
            progress=update_progress,
            progress_args=(status, user_id, "📤 Subiendo…")
        )
        await save_history(user_id, original_name, actual_name, mode, size, "success")
        await status.edit_text("✅ **Proceso completado.** Tu archivo está arriba.")
    except asyncio.CancelledError:
        try:
            await status.edit_text("🛑 **Proceso cancelado.**")
        except Exception:
            pass
        await save_history(user_id, original_name, new_name, mode, size, "cancelled")
        raise
    except Exception as exc:
        logger.exception("Error procesando archivo para user_id=%s", user_id)
        try:
            await status.edit_text(f"❌ **No se pudo procesar el archivo.**\n\n`{str(exc)[:700]}`")
        except Exception:
            pass
        await save_history(user_id, original_name, new_name, mode, size, f"error: {str(exc)[:300]}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if thumbnail:
            thumb_dir = Path(thumbnail).parent
            if thumb_dir.exists() and thumb_dir.name.startswith("thumb_"):
                shutil.rmtree(thumb_dir, ignore_errors=True)


def start_task(user_id: int, coroutine):
    old = active_tasks.get(user_id)
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(coroutine)
    active_tasks[user_id] = task

    def cleanup(done_task):
        if active_tasks.get(user_id) is done_task:
            active_tasks.pop(user_id, None)
        if user_states.get(user_id, {}).get("stage") == "processing":
            user_states[user_id] = {"stage": "idle"}
    task.add_done_callback(cleanup)


# ============================================================
# COMANDOS
# ============================================================
@bot.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    if not message.from_user:
        return
    await db_upsert_user(message.from_user)
    user_states[message.from_user.id] = {"stage": "idle"}
    await message.reply_text(
        "🖤 **BLACK RENOWNED BOT**\n\n"
        "Elegí qué querés hacer con tus archivos.",
        reply_markup=main_keyboard()
    )


@bot.on_message(filters.command("cancel"))
async def cancel_command(client: Client, message: Message):
    uid = message.from_user.id
    task = active_tasks.get(uid)
    if task and not task.done():
        task.cancel()
    user_states[uid] = {"stage": "idle"}
    await message.reply_text("🛑 Solicitud de cancelación enviada.", reply_markup=main_keyboard())


@bot.on_message(filters.command("finish"))
async def finish_batch_command(client: Client, message: Message):
    uid = message.from_user.id
    state = user_states.get(uid, {})
    if state.get("stage") != "batch_collect":
        await message.reply_text("No hay un lote pendiente.")
        return
    if not state.get("files"):
        await message.reply_text("Todavía no recibí archivos para el lote.")
        return
    state["stage"] = "batch_prefix"
    await message.reply_text(
        f"📚 Recibí {len(state['files'])} archivo(s).\n"
        "Ahora enviá un prefijo para los nombres finales, o escribí `original` para conservarlos."
    )


@bot.on_message(filters.command("history"))
async def history_command(client: Client, message: Message):
    await show_history(message, message.from_user.id)


@bot.on_message(filters.command("admin"))
async def admin_command(client: Client, message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.reply_text("⛔ No tenés permiso para acceder al panel.")
        return
    users = await asyncio.to_thread(users_collection.count_documents, {})
    jobs = await asyncio.to_thread(history_collection.count_documents, {})
    successes = await asyncio.to_thread(history_collection.count_documents, {"result": "success"})
    await message.reply_text(
        "🛡️ **BLACK RENOWNED · ADMIN**\n\n"
        f"👥 Usuarios registrados: **{users}**\n"
        f"📦 Trabajos registrados: **{jobs}**\n"
        f"✅ Procesamientos exitosos: **{successes}**\n"
        f"👮 Administradores configurados: **{len(ADMIN_IDS)}**"
    )


async def show_history(message: Message, user_id: int):
    records = await asyncio.to_thread(
        lambda: list(history_collection.find({"user_id": user_id}).sort("_id", -1).limit(10))
    )
    if not records:
        await message.reply_text("📜 Todavía no hay archivos en tu historial.")
        return
    lines = ["📜 **Últimos 10 trabajos**"]
    for item in records:
        date = item.get("created_at")
        date_text = date.strftime("%d/%m %H:%M") if date else "sin fecha"
        lines.append(
            f"\n• `{str(item.get('final_name', 'archivo'))[:55]}`"
            f"\n  {item.get('result', 'sin estado')} · {date_text}"
        )
    await message.reply_text("\n".join(lines))


# ============================================================
# BOTONES
# ============================================================
@bot.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    uid = query.from_user.id
    data = query.data or ""

    if data == "cancel":
        task = active_tasks.get(uid)
        if task and not task.done():
            task.cancel()
        user_states[uid] = {"stage": "idle"}
        await query.answer("Cancelación solicitada.")
        await query.message.edit_text("🛑 Proceso cancelado o puesto en cancelación.", reply_markup=main_keyboard())
        return

    if data == "history":
        await query.answer()
        await show_history(query.message, uid)
        return

    if data == "admin":
        if not is_admin(uid):
            await query.answer("Acceso denegado.", show_alert=True)
            return
        users = await asyncio.to_thread(users_collection.count_documents, {})
        jobs = await asyncio.to_thread(history_collection.count_documents, {})
        await query.answer()
        await query.message.edit_text(
            f"🛡️ **Panel de administración**\n\nUsuarios: **{users}**\nTrabajos: **{jobs}**",
            reply_markup=main_keyboard()
        )
        return

    if data == "mode_rename":
        user_states[uid] = {"stage": "rename_wait_file", "mode": "rename"}
        await query.answer()
        await query.message.edit_text("✏️ **Renombrar archivo**\n\nAhora enviame el archivo que querés modificar.", reply_markup=main_keyboard())
    elif data == "mode_full":
        user_states[uid] = {"stage": "full_wait_thumb", "mode": "full", "thumbnail": None}
        await query.answer()
        await query.message.edit_text(
            "🖼️ **Procesamiento completo**\n\n"
            "Enviá primero una imagen para usar como miniatura.\n"
            "La imagen se convertirá a JPEG (máximo 320×320 y menos de 200 KB).",
            reply_markup=main_keyboard()
        )
    elif data == "mode_batch":
        user_states[uid] = {"stage": "batch_collect", "mode": "batch", "files": []}
        await query.answer()
        await query.message.edit_text(
            "📚 **Procesamiento por lotes**\n\n"
            "Enviá varios archivos y, cuando termines, escribí /finish.\n"
            "Después podrás asignarles un prefijo común.",
            reply_markup=main_keyboard()
        )


# ============================================================
# MENSAJES DE TEXTO: NOMBRE, METADATOS Y LOTE
# ============================================================
@bot.on_message(filters.text & ~filters.command(["start", "cancel", "finish", "history", "admin"]), group=1)
async def text_handler(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    state = user_states.get(uid, {})
    stage = state.get("stage")

    if stage == "rename_wait_name":
        original = state["file_message"]
        original_name = media_name(original)
        suffix = Path(original_name).suffix
        requested = clean_filename(message.text)
        if not Path(requested).suffix and suffix:
            requested += suffix
        user_states[uid] = {"stage": "processing", "mode": "rename"}
        await message.reply_text("✏️ Nombre recibido. Comenzando…")
        start_task(uid, download_and_send(uid, original, requested, "rename"))
    elif stage == "full_wait_name":
        requested = clean_filename(message.text)
        original_suffix = Path(media_name(state["file_message"])).suffix
        if not Path(requested).suffix and original_suffix:
            requested += original_suffix
        state["new_name"] = requested
        state["stage"] = "full_wait_metadata"
        await message.reply_text(
            "🧾 **Metadatos PDF (opcional)**\n\n"
            "Enviá los datos en líneas separadas, por ejemplo:\n"
            "Título: Mi documento\nAutor: Black Renowned\nAsunto: Archivo\nPalabras clave: manga, pdf\n\n"
            "Si no querés modificarlos, escribí `omitir`."
        )
    elif stage == "full_wait_metadata":
        metadata = {}
        if message.text.strip().lower() != "omitir":
            keys = {
                "título": "title", "titulo": "title",
                "autor": "author", "asunto": "subject",
                "palabras clave": "keywords", "keywords": "keywords"
            }
            for line in message.text.splitlines():
                if ":" in line:
                    label, value = line.split(":", 1)
                    key = keys.get(label.strip().lower())
                    if key:
                        metadata[key] = value.strip()
        original = state["file_message"]
        new_name = state["new_name"]
        thumbnail = state.get("thumbnail")
        user_states[uid] = {"stage": "processing", "mode": "full"}
        await message.reply_text("⚙️ Preparando procesamiento completo…")
        start_task(uid, download_and_send(uid, original, new_name, "full", thumbnail, metadata))
    elif stage == "batch_prefix":
        files = state.get("files", [])
        prefix = message.text.strip()
        user_states[uid] = {"stage": "processing", "mode": "batch"}
        await message.reply_text(f"📦 Procesando {len(files)} archivo(s)…")
        async def process_batch():
            for index, item in enumerate(files, start=1):
                original_name = media_name(item)
                if prefix.lower() == "original":
                    final_name = original_name
                else:
                    final_name = clean_filename(f"{prefix}_{index:02d}{Path(original_name).suffix}")
                await download_and_send(uid, item, final_name, "batch")
        start_task(uid, process_batch())
    elif stage == "batch_collect":
        await message.reply_text("Enviá archivos como documento o multimedia. Cuando termines, escribí /finish.")
    else:
        await message.reply_text("Elegí una función del menú con /start.")


# ============================================================
# ARCHIVOS Y MINIATURAS
# ============================================================
@bot.on_message(
    filters.document | filters.video | filters.audio | filters.animation |
    filters.voice | filters.video_note | filters.photo,
    group=1
)
async def media_handler(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    state = user_states.get(uid, {})
    stage = state.get("stage")

    if stage == "full_wait_thumb":
        if not message.photo:
            await message.reply_text("Necesito una foto para la miniatura. Enviá una imagen.")
            return
        folder = Path(tempfile.mkdtemp(prefix="thumb_", dir=TMP_ROOT))
        try:
            raw = await bot.download_media(message, file_name=str(folder / "source"))
            thumb_path = str(folder / "thumb.jpg")
            await make_thumbnail_jpeg(raw, thumb_path)
            state["thumbnail"] = thumb_path
            state["stage"] = "full_wait_file"
            await message.reply_text("✅ Miniatura guardada. Ahora enviá el archivo que querés procesar.")
        except Exception as exc:
            shutil.rmtree(folder, ignore_errors=True)
            await message.reply_text(f"❌ No pude preparar esa imagen: {str(exc)[:300]}")
        return

    if not (message.document or message.video or message.audio or message.animation or message.voice or message.video_note):
        await message.reply_text("Ese tipo de archivo no es compatible como archivo principal. Enviá un documento, video o audio.")
        return

    size = media_size(message)
    if size and size > MAX_FILE_SIZE:
        await message.reply_text(f"❌ El archivo supera el límite configurado de {MAX_FILE_SIZE_MB} MB.")
        return

    if stage == "rename_wait_file":
        state["file_message"] = message
        state["stage"] = "rename_wait_name"
        await message.reply_text(
            f"📄 Recibido: `{media_name(message)}`\n\n"
            "Ahora escribí el nuevo nombre, con o sin extensión."
        )
    elif stage == "full_wait_file":
        state["file_message"] = message
        state["stage"] = "full_wait_name"
        await message.reply_text(
            f"📄 Recibido: `{media_name(message)}`\n\n"
            "Escribí el nombre final del archivo."
        )
    elif stage == "batch_collect":
        state.setdefault("files", []).append(message)
        await message.reply_text(
            f"➕ Agregado al lote: `{media_name(message)}`\n"
            f"Total: **{len(state['files'])}**. Enviá otro o escribí /finish."
        )
    else:
        await message.reply_text("Primero elegí una función en /start.")


# ============================================================
# SERVIDOR HTTP PARA RENDER
# ============================================================
async def health(request):
    return web.json_response({"status": "ok", "service": "Black Renowned Bot"})


async def start_http_server():
    app_web = web.Application()
    app_web.router.add_get("/", health)
    app_web.router.add_get("/health", health)
    runner = web.AppRunner(app_web)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Servidor HTTP activo en puerto %s", port)
    return runner


# ============================================================
# ARRANQUE
# ============================================================
async def main():
    # Verificar MongoDB antes de iniciar el bot.
    await asyncio.to_thread(mongo_client.admin.command, "ping")
    logger.info("Conexión con MongoDB correcta.")
    runner = await start_http_server()
    await bot.start()
    logger.info("Black Renowned Bot conectado.")
    try:
        await idle()
    finally:
        await bot.stop()
        await runner.cleanup()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())

