import asyncio
import base64
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from PIL import Image
from pymongo import MongoClient
from pypdf import PdfReader, PdfWriter
from pyrogram import Client, filters, idle
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

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

# Imagen de bienvenida: va incluida al final de este archivo (WELCOME_IMAGE_B64),
# no hace falta subir ningún archivo aparte. Se prepara al arrancar.
WELCOME_IMAGE: Path | None = None
# Zona horaria para las fechas del historial (Argentina = -3).
LOCAL_TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET_HOURS", "-3"))))
# Cada cuántos segundos el vigilante comprueba la conexión con Telegram.
WATCHDOG_INTERVAL = int(os.getenv("WATCHDOG_INTERVAL", "120"))
BOT_STARTED_AT = time.time()

COMMANDS = ["start", "cancel", "finish", "history", "admin", "restart"]

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

# Máximo de archivos procesándose a la vez (cuida RAM y disco en Render).
PROCESS_SEMAPHORE = asyncio.Semaphore(2)


# ============================================================
# TECLADOS Y TEXTOS
# ============================================================
def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [btn("✏️ Renombrar", "mode_rename"), btn("🖼️ Miniatura + PDF", "mode_full")],
        [btn("📚 Lote de archivos", "mode_batch"), btn("📜 Historial", "history")],
        [btn("ℹ️ Ayuda", "help"), btn("✖️ Cerrar", "close")],
    ]
    if user_id is not None and is_admin(user_id):
        rows.append([btn("🛡️ Panel admin", "admin")])
    return InlineKeyboardMarkup(rows)


STOP_KEYBOARD = InlineKeyboardMarkup([[btn("🛑 Detener operación", "stop")]])
HOME_KEYBOARD = InlineKeyboardMarkup([[btn("🏠 Menú principal", "newmenu")]])
NAV_KEYBOARD = InlineKeyboardMarkup([[btn("⬅️ Volver", "menu"), btn("✖️ Cerrar", "close")]])
CANCEL_KEYBOARD = InlineKeyboardMarkup([[btn("✖️ Cancelar", "reset")]])
KEEPNAME_KEYBOARD = InlineKeyboardMarkup([
    [btn("📄 Conservar nombre original", "keepname")],
    [btn("✖️ Cancelar", "reset")],
])
META_KEYBOARD = InlineKeyboardMarkup([
    [btn("⏭️ Omitir metadatos", "skipmeta")],
    [btn("✖️ Cancelar", "reset")],
])
PREFIX_KEYBOARD = InlineKeyboardMarkup([
    [btn("📄 Conservar nombres originales", "batchorig")],
    [btn("✖️ Cancelar", "reset")],
])
BATCH_KEYBOARD = InlineKeyboardMarkup([
    [btn("✅ Terminar lote", "finish")],
    [btn("✖️ Cancelar", "reset")],
])
ADMIN_KEYBOARD = InlineKeyboardMarkup([
    [btn("🔄 Reiniciar bot", "restart")],
    [btn("⬅️ Volver", "menu"), btn("✖️ Cerrar", "close")],
])
RESTART_CONFIRM_KEYBOARD = InlineKeyboardMarkup([
    [btn("✅ Sí, reiniciar", "restart_yes"), btn("↩️ No", "admin")],
])

WELCOME_TEXT = (
    "🖤 **BLACK RENOWNED BOT**\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "Renombrá y personalizá tus archivos de forma rápida y prolija.\n\n"
    "✦ Renombrar cualquier archivo\n"
    "✦ Miniatura personalizada\n"
    "✦ Metadatos para PDF\n"
    "✦ Procesamiento por lotes\n"
    "✦ Progreso en tiempo real\n\n"
    "Elegí una opción 👇"
)

HELP_TEXT = (
    "ℹ️ **Ayuda**\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "✏️ **Renombrar:** enviás el archivo y el nuevo nombre.\n"
    "🖼️ **Miniatura + PDF:** elegís una portada y, si querés, metadatos.\n"
    "📚 **Lote:** varios archivos con un prefijo común.\n\n"
    "🛑 Podés detener cualquier operación con el botón del progreso o con /cancel.\n"
    "🏠 Si algo se traba, /start vuelve al menú."
)

META_TEXT = (
    "🧾 **Metadatos PDF (opcional)**\n\n"
    "Enviá los datos en líneas separadas:\n\n"
    "`Título: Mi documento`\n"
    "`Autor: Black Renowned`\n"
    "`Asunto: Archivo`\n"
    "`Palabras clave: manga, pdf`\n\n"
    "O tocá **Omitir** para dejarlos como están."
)


# ============================================================
# UTILIDADES
# ============================================================
def md_safe(text: str) -> str:
    """Evita que un acento grave rompa el formato markdown."""
    return text.replace("`", "'")


async def safe_edit(message: Message, text: str, **kwargs):
    """Edita un mensaje (de texto o foto) sin romperse si no cambió."""
    try:
        if message.photo:
            await message.edit_caption(text, **kwargs)
        else:
            await message.edit_text(text, **kwargs)
    except MessageNotModified:
        pass
    except Exception:
        logger.warning("No se pudo editar un mensaje", exc_info=True)


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
    if message.voice:
        ext = ".ogg"
    elif message.audio:
        ext = ".mp3"
    elif message.video or message.video_note or message.animation:
        ext = ".mp4"
    else:
        ext = ".bin"
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


def format_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def progress_bar(percent: int, width: int = 20) -> str:
    filled = min(width, max(0, percent) * width // 100)
    return "▰" * filled + "▱" * (width - filled)


async def update_progress(
    current: int, total: int, status: Message,
    user_id: int, label: str, filename: str, started: float
):
    now = time.monotonic()
    if current < total and now - last_progress_edit.get(user_id, 0) < 2.5:
        return
    last_progress_edit[user_id] = now
    percent = int(current * 100 / total) if total else 0
    elapsed = max(now - started, 0.001)
    speed = current / elapsed
    eta = (total - current) / speed if speed > 0 and total else 0
    text = (
        f"{label}\n"
        f"`{md_safe(filename)[:45]}`\n\n"
        f"{progress_bar(percent)}  **{percent}%**\n"
        f"{format_bytes(current)} / {format_bytes(total)}\n"
        f"⚡ {format_bytes(int(speed))}/s · ⏳ {format_duration(eta)}"
    )
    try:
        await status.edit_text(text, reply_markup=STOP_KEYBOARD)
    except Exception:
        pass


def cleanup_thumbnail(state: dict):
    thumb = state.get("thumbnail")
    if thumb:
        folder = Path(thumb).parent
        if folder.name.startswith("thumb_"):
            shutil.rmtree(folder, ignore_errors=True)


def reset_state(user_id: int):
    """Vuelve al estado inicial. Si hay un proceso en marcha, éste limpia lo suyo."""
    state = user_states.get(user_id, {})
    if state.get("stage") != "processing":
        cleanup_thumbnail(state)
    user_states[user_id] = {"stage": "idle"}


def result_icon(result: str) -> str:
    if result == "success":
        return "✅"
    if result == "cancelled":
        return "🛑"
    return "❌"


async def db_upsert_user(user):
    await asyncio.to_thread(
        users_collection.update_one,
        {"user_id": user.id},
        {"$set": { "username": user.username, "first_name": user.first_name, "last_seen": datetime.now(timezone.utc) }, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
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
    try:
        await asyncio.to_thread(history_collection.insert_one, record)
    except Exception:
        logger.exception("No se pudo guardar el historial")


async def build_history_text(user_id: int) -> str:
    records = await asyncio.to_thread(
        lambda: list(history_collection.find({"user_id": user_id}).sort("_id", -1).limit(8))
    )
    if not records:
        return "📜 **Historial**\n\nTodavía no procesaste ningún archivo."
    lines = ["📜 **Últimos trabajos**\n"]
    for item in records:
        date = item.get("created_at")
        if date:
            date = date.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
            date_text = date.strftime("%d/%m %H:%M")
        else:
            date_text = "sin fecha"
        name = md_safe(str(item.get("final_name", "archivo")))[:34]
        icon = result_icon(str(item.get("result", "")))
        lines.append(f"{icon} `{name}`\n      {date_text}")
    return "\n".join(lines)


async def build_admin_text() -> str:
    users = await asyncio.to_thread(users_collection.count_documents, {})
    jobs = await asyncio.to_thread(history_collection.count_documents, {})
    successes = await asyncio.to_thread(history_collection.count_documents, {"result": "success"})
    running = sum(1 for task in active_tasks.values() if not task.done())
    try:
        free = format_bytes(shutil.disk_usage(TMP_ROOT).free)
    except Exception:
        free = "desconocido"
    return (
        "🛡️ **Panel de administración**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Usuarios: **{users}**\n"
        f"📦 Trabajos: **{jobs}** (✅ {successes})\n"
        f"⚙️ En curso ahora: **{running}**\n"
        f"💽 Espacio libre: **{free}**\n"
        f"⏱️ Activo hace: **{format_duration(time.time() - BOT_STARTED_AT)}**"
    )


def prepare_welcome_image() -> Path | None:
    """Devuelve la ruta de la imagen de bienvenida (la incluida en el código u otra propia)."""
    custom = os.getenv("WELCOME_IMAGE", "").strip()
    if custom:
        path = Path(__file__).resolve().parent / custom
        if path.exists():
            return path
    try:
        data = base64.b64decode("".join(WELCOME_IMAGE_B64.split()))
        if data:
            path = TMP_ROOT / "welcome_embedded.jpg"
            path.write_bytes(data)
            return path
    except Exception:
        logger.warning("No se pudo preparar la imagen de bienvenida incluida", exc_info=True)
    fallback = Path(__file__).resolve().parent / "welcome.jpg"
    return fallback if fallback.exists() else None


async def send_welcome(message: Message, user_id: int):
    """Envía la tarjeta de bienvenida: imagen + texto + botones."""
    if WELCOME_IMAGE and WELCOME_IMAGE.exists():
        try:
            await message.reply_photo(
                photo=str(WELCOME_IMAGE),
                caption=WELCOME_TEXT,
                reply_markup=main_keyboard(user_id)
            )
            return
        except Exception:
            logger.warning("No se pudo enviar la imagen de bienvenida", exc_info=True)
    await message.reply_text(WELCOME_TEXT, reply_markup=main_keyboard(user_id))


async def make_thumbnail_jpeg(source: str, destination: str):
    def convert():
        with Image.open(source) as image:
            image = image.convert("RGB")
            image.thumbnail((320, 320))
            for quality in (82, 65, 50, 35):
                image.save(destination, "JPEG", quality=quality, optimize=True)
                if os.path.getsize(destination) < 200 * 1024:
                    return
        raise ValueError("La miniatura convertida supera 200 KB. Elegí otra imagen.")
    await asyncio.to_thread(convert)
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


def parse_metadata(text: str) -> dict:
    keys = {
        "título": "title", "titulo": "title", "title": "title",
        "autor": "author", "author": "author",
        "asunto": "subject", "subject": "subject",
        "palabras clave": "keywords", "keywords": "keywords"
    }
    metadata = {}
    for line in text.splitlines():
        if ":" in line:
            label, value = line.split(":", 1)
            key = keys.get(label.strip().lower())
            if key:
                metadata[key] = value.strip()
    return metadata


# ============================================================
# PROCESAMIENTO DE ARCHIVOS
# ============================================================
async def download_and_send(
    user_id: int,
    source_message: Message,
    new_name: str,
    mode: str,
    thumbnail: str | None = None,
    metadata: dict | None = None,
    position: tuple[int, int] | None = None
):
    workdir = Path(tempfile.mkdtemp(prefix=f"user_{user_id}_", dir=TMP_ROOT))
    tag = f" ({position[0]}/{position[1]})" if position else ""
    status = await source_message.reply_text(
        f"📥 Preparando descarga…{tag}", reply_markup=STOP_KEYBOARD
    )
    original_name = media_name(source_message)
    size = media_size(source_message)

    try:
        if PROCESS_SEMAPHORE.locked():
            await safe_edit(
                status,
                f"⏳ **En cola**{tag}\n\nHay otros archivos procesándose. "
                "Empiezo en cuanto se libere un lugar.",
                reply_markup=STOP_KEYBOARD
            )

        async with PROCESS_SEMAPHORE:
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

            actual_name = clean_filename(new_name, original_name)
            target = workdir / actual_name

            started = time.monotonic()
            downloaded = await bot.download_media(
                source_message,
                file_name=str(target),
                progress=update_progress,
                progress_args=(status, user_id, f"📥 **Descargando**{tag}", actual_name, started)
            )
            if not downloaded or not os.path.isfile(downloaded):
                raise RuntimeError("Telegram no completó la descarga del archivo.")

            actual_path = str(downloaded)

            await safe_edit(
                status, f"⚙️ **Aplicando cambios**{tag}\n\nPreparando tu archivo…",
                reply_markup=STOP_KEYBOARD
            )
            metadata_changed = False
            if metadata:
                try:
                    metadata_changed = await apply_pdf_metadata(actual_path, metadata)
                except Exception:
                    logger.warning("No se pudieron aplicar los metadatos PDF", exc_info=True)

            final_size = os.path.getsize(actual_path)
            caption = (
                "✨ **Black Renowned Bot**\n"
                f"📄 `{md_safe(actual_name)}`\n"
                f"💾 {format_bytes(final_size)}"
            )
            if metadata_changed:
                caption += "\n🧾 Metadatos PDF actualizados"

            started = time.monotonic()
            await bot.send_document(
                chat_id=user_id,
                document=actual_path,
                file_name=actual_name,
                thumb=thumbnail if thumbnail and os.path.exists(thumbnail) else None,
                caption=caption,
                force_document=True,
                progress=update_progress,
                progress_args=(status, user_id, f"📤 **Subiendo**{tag}", actual_name, started)
            )
            await save_history(user_id, original_name, actual_name, mode, size, "success")
            await safe_edit(
                status, f"✅ **¡Listo!**{tag}\n\nTu archivo está arriba.",
                reply_markup=HOME_KEYBOARD
            )
    except asyncio.CancelledError:
        await safe_edit(status, "🛑 **Operación detenida.**", reply_markup=HOME_KEYBOARD)
        await save_history(user_id, original_name, new_name, mode, size, "cancelled")
        raise
    except Exception as exc:
        logger.exception("Error procesando archivo para user_id=%s", user_id)
        await safe_edit(
            status,
            f"❌ **No se pudo procesar el archivo.**\n\n`{md_safe(str(exc))[:600]}`",
            reply_markup=HOME_KEYBOARD
        )
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


def launch_rename(user_id: int, original: Message, new_name: str):
    user_states[user_id] = {"stage": "processing", "mode": "rename"}
    start_task(user_id, download_and_send(user_id, original, new_name, "rename"))


def launch_full(user_id: int, state: dict, metadata: dict):
    original = state["file_message"]
    new_name = state["new_name"]
    thumbnail = state.get("thumbnail")
    user_states[user_id] = {"stage": "processing", "mode": "full"}
    start_task(user_id, download_and_send(user_id, original, new_name, "full", thumbnail, metadata))


def launch_batch(user_id: int, files: list, prefix: str):
    user_states[user_id] = {"stage": "processing", "mode": "batch"}
    total = len(files)

    async def process_batch():
        for index, item in enumerate(files, start=1):
            original_name = media_name(item)
            if prefix.lower() == "original":
                final_name = original_name
            else:
                final_name = clean_filename(f"{prefix}_{index:02d}{Path(original_name).suffix}")
            await download_and_send(
                user_id, item, final_name, "batch", position=(index, total)
            )

    start_task(user_id, process_batch())


def finish_batch_stage(user_id: int) -> str | None:
    """Pasa el lote a la etapa de prefijo. Devuelve un error en texto, o None si salió bien."""
    state = user_states.get(user_id, {})
    if state.get("stage") != "batch_collect":
        return "No hay un lote pendiente. Elegí 📚 Lote de archivos en el menú."
    if not state.get("files"):
        return "Todavía no recibí archivos para el lote."
    state["stage"] = "batch_prefix"
    return None


def prefix_prompt(user_id: int) -> str:
    count = len(user_states.get(user_id, {}).get("files", []))
    return (
        f"📚 **Lote listo: {count} archivo(s)**\n\n"
        "Escribí un **prefijo** para los nombres finales "
        "(por ejemplo `Capitulo` genera `Capitulo_01.pdf`, `Capitulo_02.pdf`…) "
        "o tocá el botón para conservar los nombres originales."
    )


# ============================================================
# REINICIO Y VIGILANTE
# ============================================================
def restart_process(reason: str):
    """Cierra el proceso. Render lo vuelve a levantar solo en unos segundos."""
    logger.warning("Reiniciando el bot. Motivo: %s", reason)
    os._exit(1)


async def watchdog():
    """Si Telegram deja de responder varias veces seguidas, reinicia el bot."""
    failures = 0
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        # Con archivos en proceso la conexión puede estar ocupada: no se mide.
        if any(not task.done() for task in active_tasks.values()):
            failures = 0
            continue
        try:
            await asyncio.wait_for(bot.get_me(), timeout=45)
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            logger.warning("Vigilante: fallo %s/3 al comprobar Telegram", failures, exc_info=True)
            if failures >= 3:
                restart_process("sin conexión con Telegram")


# ============================================================
# DEPURACIÓN (solo se ve con LOG_LEVEL=DEBUG)
# ============================================================
@bot.on_message(group=-1)
async def debug_logger(client: Client, message: Message):
    logger.debug(
        "Mensaje recibido de %s: %s",
        message.from_user.id if message.from_user else None,
        message.text or "[sin texto]"
    )


# ============================================================
# COMANDOS
# ============================================================
@bot.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    try:
        await db_upsert_user(message.from_user)
    except Exception:
        logger.exception("No se pudo registrar al usuario en MongoDB")
    reset_state(uid)
    await send_welcome(message, uid)


@bot.on_message(filters.command("cancel"))
async def cancel_command(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    task = active_tasks.get(uid)
    if task and not task.done():
        task.cancel()
    reset_state(uid)
    await message.reply_text("🛑 **Operación detenida.**", reply_markup=HOME_KEYBOARD)


@bot.on_message(filters.command("finish"))
async def finish_batch_command(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    error = finish_batch_stage(uid)
    if error:
        await message.reply_text(error)
        return
    await message.reply_text(prefix_prompt(uid), reply_markup=PREFIX_KEYBOARD)


@bot.on_message(filters.command("history"))
async def history_command(client: Client, message: Message):
    if not message.from_user:
        return
    await message.reply_text(
        await build_history_text(message.from_user.id), reply_markup=NAV_KEYBOARD
    )


@bot.on_message(filters.command("admin"))
async def admin_command(client: Client, message: Message):
    if not message.from_user:
        return
    if not is_admin(message.from_user.id):
        await message.reply_text("⛔ No tenés permiso para acceder al panel.")
        return
    await message.reply_text(await build_admin_text(), reply_markup=ADMIN_KEYBOARD)


@bot.on_message(filters.command("restart"))
async def restart_command(client: Client, message: Message):
    if not message.from_user:
        return
    if not is_admin(message.from_user.id):
        await message.reply_text("⛔ No tenés permiso para reiniciar el bot.")
        return
    await message.reply_text(
        "⚠️ **¿Reiniciar el bot?**\n\n"
        "Se detienen los procesos en curso y vuelve en menos de un minuto.",
        reply_markup=RESTART_CONFIRM_KEYBOARD
    )


# ============================================================
# BOTONES
# ============================================================
@bot.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    uid = query.from_user.id
    data = query.data or ""
    message = query.message
    if message is None:
        await query.answer("Este mensaje ya no está disponible. Escribí /start.", show_alert=True)
        return

    # ----- Navegación -----
    if data == "menu":
        reset_state(uid)
        await query.answer()
        await safe_edit(message, WELCOME_TEXT, reply_markup=main_keyboard(uid))

    elif data == "newmenu":
        reset_state(uid)
        await query.answer()
        await send_welcome(message, uid)

    elif data == "close":
        await query.answer()
        try:
            await message.delete()
        except Exception:
            pass

    elif data == "help":
        await query.answer()
        await safe_edit(message, HELP_TEXT, reply_markup=NAV_KEYBOARD)

    elif data == "history":
        await query.answer()
        await safe_edit(message, await build_history_text(uid), reply_markup=NAV_KEYBOARD)

    # ----- Detener / cancelar -----
    elif data == "stop":
        task = active_tasks.get(uid)
        if task and not task.done():
            task.cancel()
            user_states[uid] = {"stage": "idle"}
            await query.answer("🛑 Deteniendo la operación…")
        else:
            await query.answer("No hay ninguna operación en curso.", show_alert=True)

    elif data == "reset":
        reset_state(uid)
        await query.answer("Cancelado.")
        await safe_edit(
            message, "🛑 **Cancelado.**\n\nVolvé al menú cuando quieras.",
            reply_markup=HOME_KEYBOARD
        )

    # ----- Modos -----
    elif data == "mode_rename":
        reset_state(uid)
        user_states[uid] = {"stage": "rename_wait_file", "mode": "rename"}
        await query.answer()
        await safe_edit(
            message,
            "✏️ **Renombrar archivo**\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"1️⃣ Enviame el archivo (hasta {MAX_FILE_SIZE_MB} MB)\n"
            "2️⃣ Escribí el nuevo nombre, con o sin extensión",
            reply_markup=NAV_KEYBOARD
        )

    elif data == "mode_full":
        reset_state(uid)
        user_states[uid] = {"stage": "full_wait_thumb", "mode": "full", "thumbnail": None}
        await query.answer()
        await safe_edit(
            message,
            "🖼️ **Miniatura + metadatos**\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ Enviame una imagen: será la miniatura\n"
            f"2️⃣ Enviame el archivo (hasta {MAX_FILE_SIZE_MB} MB)\n"
            "3️⃣ Elegí el nombre final\n"
            "4️⃣ Metadatos PDF (opcional)\n\n"
            "La imagen se convierte a JPEG (máx. 320×320 y menos de 200 KB).",
            reply_markup=NAV_KEYBOARD
        )

    elif data == "mode_batch":
        reset_state(uid)
        user_states[uid] = {"stage": "batch_collect", "mode": "batch", "files": []}
        await query.answer()
        await safe_edit(
            message,
            "📚 **Procesamiento por lotes**\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ Enviame todos los archivos\n"
            "2️⃣ Tocá **Terminar lote** (o escribí /finish)\n"
            "3️⃣ Elegí un prefijo común para los nombres",
            reply_markup=NAV_KEYBOARD
        )

    # ----- Pasos dentro de un modo -----
    elif data == "keepname":
        state = user_states.get(uid, {})
        if state.get("stage") != "full_wait_name":
            await query.answer("Esta opción ya no está disponible.", show_alert=True)
            return
        state["new_name"] = clean_filename(media_name(state["file_message"]))
        state["stage"] = "full_wait_metadata"
        await query.answer()
        await safe_edit(message, META_TEXT, reply_markup=META_KEYBOARD)

    elif data == "skipmeta":
        state = user_states.get(uid, {})
        if state.get("stage") != "full_wait_metadata":
            await query.answer("Esta opción ya no está disponible.", show_alert=True)
            return
        await query.answer("Procesando…")
        await safe_edit(message, "⚙️ **Preparando todo…**")
        launch_full(uid, state, {})

    elif data == "finish":
        error = finish_batch_stage(uid)
        if error:
            await query.answer(error, show_alert=True)
            return
        await query.answer()
        await message.reply_text(prefix_prompt(uid), reply_markup=PREFIX_KEYBOARD)

    elif data == "batchorig":
        state = user_states.get(uid, {})
        if state.get("stage") != "batch_prefix":
            await query.answer("Esta opción ya no está disponible.", show_alert=True)
            return
        files = state.get("files", [])
        await query.answer("Procesando…")
        await safe_edit(message, f"📦 **Procesando {len(files)} archivo(s)…**")
        launch_batch(uid, files, "original")

    # ----- Administración -----
    elif data == "admin":
        if not is_admin(uid):
            await query.answer("Acceso denegado.", show_alert=True)
            return
        await query.answer()
        await safe_edit(message, await build_admin_text(), reply_markup=ADMIN_KEYBOARD)

    elif data == "restart":
        if not is_admin(uid):
            await query.answer("Acceso denegado.", show_alert=True)
            return
        await query.answer()
        await safe_edit(
            message,
            "⚠️ **¿Reiniciar el bot?**\n\n"
            "Se detienen los procesos en curso y vuelve en menos de un minuto.",
            reply_markup=RESTART_CONFIRM_KEYBOARD
        )

    elif data == "restart_yes":
        if not is_admin(uid):
            await query.answer("Acceso denegado.", show_alert=True)
            return
        await query.answer("Reiniciando…")
        await safe_edit(
            message,
            "🔄 **Reiniciando el bot…**\n\nEscribí /start dentro de un minuto."
        )
        await asyncio.sleep(1.5)
        restart_process(f"pedido del administrador {uid}")

    else:
        await query.answer()


# ============================================================
# MENSAJES DE TEXTO: NOMBRE, METADATOS Y LOTE
# ============================================================
@bot.on_message(filters.text & ~filters.command(COMMANDS), group=1)
async def text_handler(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    state = user_states.get(uid, {})
    stage = state.get("stage")

    if stage == "rename_wait_name":
        original = state["file_message"]
        suffix = Path(media_name(original)).suffix
        requested = clean_filename(message.text)
        if not Path(requested).suffix and suffix:
            requested += suffix
        launch_rename(uid, original, requested)

    elif stage == "full_wait_name":
        requested = clean_filename(message.text)
        original_suffix = Path(media_name(state["file_message"])).suffix
        if not Path(requested).suffix and original_suffix:
            requested += original_suffix
        state["new_name"] = requested
        state["stage"] = "full_wait_metadata"
        await message.reply_text(META_TEXT, reply_markup=META_KEYBOARD)

    elif stage == "full_wait_metadata":
        text = message.text.strip()
        metadata = {} if text.lower() == "omitir" else parse_metadata(text)
        launch_full(uid, state, metadata)

    elif stage == "batch_prefix":
        files = state.get("files", [])
        prefix = clean_filename(message.text, "archivo")
        await message.reply_text(f"📦 **Procesando {len(files)} archivo(s)…**")
        launch_batch(uid, files, prefix)

    elif stage == "batch_collect":
        await message.reply_text(
            "Enviame los archivos del lote. Cuando termines, tocá **Terminar lote** o escribí /finish.",
            reply_markup=BATCH_KEYBOARD
        )

    elif stage == "processing":
        await message.reply_text("⏳ Estoy procesando un archivo. Podés detenerlo con el botón 🛑 o con /cancel.")

    else:
        await message.reply_text("📂 Primero elegí qué querés hacer:", reply_markup=main_keyboard(uid))


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
        is_image = bool(message.photo) or (
            bool(message.document) and (message.document.mime_type or "").startswith("image/")
        )
        if not is_image:
            await message.reply_text("🖼️ Necesito una imagen para la miniatura. Enviame una foto.")
            return
        folder = Path(tempfile.mkdtemp(prefix="thumb_", dir=TMP_ROOT))
        try:
            raw = await bot.download_media(message, file_name=str(folder / "source"))
            thumb_path = str(folder / "thumb.jpg")
            await make_thumbnail_jpeg(raw, thumb_path)
            state["thumbnail"] = thumb_path
            state["stage"] = "full_wait_file"
            await message.reply_text(
                "✅ **Miniatura guardada.**\n\nAhora enviame el archivo que querés procesar.",
                reply_markup=CANCEL_KEYBOARD
