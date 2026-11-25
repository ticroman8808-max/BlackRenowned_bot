import asyncio
import base64
import gc
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
# CONFIGURACIÓN (NORTHFLANK)
# ============================================================
def required_env(name: str, fallback: str | None = None) -> str:
    value = os.getenv(name, "").strip()
    if not value and fallback:
        value = os.getenv(fallback, "").strip()
    if not value:
        raise RuntimeError(f"Falta la variable de entorno obligatoria: {name}")
    return value


API_ID = int(required_env("API_ID"))
API_HASH = required_env("API_HASH")
BOT_TOKEN = required_env("BOT_TOKEN")
MONGO_URI = required_env("MONGO_URI", fallback="DATABASE_URL")
DATABASE_NAME = os.getenv("DATABASE_NAME", "BlackRenowned").strip() or "BlackRenowned"

ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "1500"))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024
TMP_ROOT = Path(os.getenv("TMP_DIR", "/tmp/blackrenowned"))
TMP_ROOT.mkdir(parents=True, exist_ok=True)

# Imagen de bienvenida: incluida en WELCOME_IMAGE_B64.
# Video de bienvenida: colocar Bienvenida.mp4 junto a bot.py en Northflank.
WELCOME_IMAGE: Path | None = None
WELCOME_VIDEO = Path(__file__).resolve().parent / "Bienvenida.mp4"
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

# Máximo de archivos procesándose a la vez (cuida RAM y disco en Northflank).
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
        [btn("📚 Multi-PDF + Imagen", "mode_multithumb"), btn("📦 Lote de archivos", "mode_batch")],
        [btn("📜 Historial", "history"), btn("ℹ️ Ayuda", "help")],
        [btn("✖️ Cerrar", "close")],
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
MULTIPDF_NAME_KEYBOARD = InlineKeyboardMarkup([
    [btn("✏️ Cambiar nombre", "mpdf_rename"), btn("📄 Dejar nombre actual", "mpdf_keep")],
    [btn("🏁 Terminar sesión", "reset")],
])
MULTIPDF_ACTIVE_KEYBOARD = InlineKeyboardMarkup([
    [btn("🏁 Terminar sesión", "reset")],
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
    [btn("📨 Solicitudes pendientes", "access_pending")],
    [btn("✅ Usuarios permitidos", "access_allowed")],
    [btn("⛔ Rechazados / revocados", "access_blocked")],
    [btn("🔄 Reiniciar bot", "restart")],
    [btn("⬅️ Volver", "menu"), btn("✖️ Cerrar", "close")],
])
RESTART_CONFIRM_KEYBOARD = InlineKeyboardMarkup([
    [btn("✅ Sí, reiniciar", "restart_yes"), btn("↩️ No", "admin")],
])


def access_decision_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        btn("✅ ACEPTAR", f"access_approve:{user_id}"),
        btn("❌ RECHAZAR", f"access_reject:{user_id}")
    ]])


def access_list_keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    rows.append([btn("⬅️ Panel admin", "admin")])
    return InlineKeyboardMarkup(rows)


WELCOME_TEXT = (
    "🖤 **BLACK RENOWNED BOT**\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "Renombrá y personalizá tus archivos de forma rápida y prolija.\n\n"
    "✦ Renombrar cualquier archivo\n"
    "✦ Miniatura personalizada + metadatos PDF\n"
    "✦ Multi-PDF con una misma imagen (elegí nombre o dejá el actual)\n"
    "✦ Procesamiento por lotes\n"
    "✦ Limpieza inmediata de temporales y RAM\n\n"
    "Elegí una opción 👇"
)

HELP_TEXT = (
    "ℹ️ **Ayuda**\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "✏️ **Renombrar:** enviás el archivo y el nuevo nombre.\n"
    "🖼️ **Miniatura + PDF:** elegís portada y, si querés, metadatos.\n"
    "📚 **Multi-PDF + Imagen:** mandás 1 imagen y procesás varios PDFs eligiendo en cada uno si cambiás el nombre o dejás el que tiene.\n"
    "📦 **Lote:** varios archivos con un prefijo común.\n\n"
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
# UTILIDADES Y LIMPIEZA ESTRICTA DE RAM / DISCO
# ============================================================
def md_safe(text: str) -> str:
    """Evita que un acento grave rompa el formato markdown."""
    return text.replace("`", "'")


async def safe_edit(message: Message, text: str, **kwargs):
    """Edita un mensaje (de texto o foto) sin romperse si no cambió."""
    try:
        if (message.photo or message.video or message.animation or message.document
                or message.audio or message.voice or message.video_note):
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


def is_pdf_message(message: Message) -> bool:
    if not message.document:
        return False
    mime = (message.document.mime_type or "").lower()
    fname = (message.document.file_name or "").lower()
    return mime == "application/pdf" or fname.endswith(".pdf")


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


def remove_path_tree(path_str: str | Path | None):
    if not path_str:
        return
    p = Path(path_str)
    try:
        if p.is_file() or p.is_symlink():
            p.unlink(missing_ok=True)
            parent = p.parent
            if parent != TMP_ROOT and parent.exists() and not any(parent.iterdir()):
                shutil.rmtree(parent, ignore_errors=True)
        elif p.exists():
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        logger.debug("Error limpiando ruta temporal %s", p, exc_info=True)


def cleanup_thumbnail(state: dict, force: bool = False):
    thumb = state.pop("thumbnail", None)
    if not thumb:
        return
    if force or not state.get("keep_thumbnail_session"):
        folder = Path(thumb).parent
        if folder != TMP_ROOT and folder.name.startswith("thumb_"):
            shutil.rmtree(folder, ignore_errors=True)
        else:
            remove_path_tree(thumb)


def purge_user_ephemeral(user_id: int, force_thumb: bool = True):
    """Limpia referencias de estado y memoria efímera del usuario."""
    last_progress_edit.pop(user_id, None)
    state = user_states.get(user_id)
    if state:
        cleanup_thumbnail(state, force=force_thumb)
        state.pop("file_message", None)
        state.pop("files", None)
        state.pop("pending_pdf", None)
        state.pop("new_name", None)
        state.clear()
    gc.collect()


def reset_state(user_id: int, force_thumb: bool = True):
    """Vuelve al estado inicial limpiando datos efímeros."""
    state = user_states.get(user_id, {})
    if state.get("stage") != "processing":
        cleanup_thumbnail(state, force=force_thumb)
    purge_user_ephemeral(user_id, force_thumb=force_thumb)
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
        {"$set": {
            "username": user.username,
            "first_name": user.first_name,
            "last_seen": datetime.now(timezone.utc)
        }, "$setOnInsert": {
            "created_at": datetime.now(timezone.utc),
            "access_status": "pending",
            "access_requested_at": datetime.now(timezone.utc),
            "access_request_notified": False
        }},
        True
    )


async def get_access_status(user_id: int) -> str | None:
    if is_admin(user_id):
        return "approved"
    user_doc = await asyncio.to_thread(users_collection.find_one, {"user_id": user_id})
    if not user_doc:
        return None
    status = user_doc.get("access_status")
    if status is None:
        await asyncio.to_thread(
            users_collection.update_one,
            {"user_id": user_id, "access_status": {"$exists": False}},
            {"$set": {
                "access_status": "pending",
                "access_requested_at": datetime.now(timezone.utc),
                "access_request_notified": False
            }}
        )
        return "pending"
    return str(status)


async def is_authorized(user_id: int) -> bool:
    return await get_access_status(user_id) == "approved"


async def notify_admins_access_request(user):
    doc = await asyncio.to_thread(users_collection.find_one, {"user_id": user.id})
    if not doc or doc.get("access_status") != "pending" or doc.get("access_request_notified"):
        return
    await asyncio.to_thread(
        users_collection.update_one,
        {"user_id": user.id, "access_status": "pending"},
        {"$set": {
            "access_request_notified": True,
            "access_requested_at": datetime.now(timezone.utc)
        }}
    )
    label = f"@{user.username}" if user.username else (user.first_name or "Sin nombre")
    text = (
        "📨 **Nueva solicitud de acceso**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 Usuario: **{md_safe(label)}**\n"
        f"🆔 ID: `{user.id}`\n\n"
        "¿Querés autorizarlo permanentemente?"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=access_decision_keyboard(user.id))
        except Exception:
            logger.warning("No se pudo notificar la solicitud al administrador %s", admin_id, exc_info=True)


async def build_access_list(status: str) -> tuple[str, InlineKeyboardMarkup]:
    if status == "allowed":
        query_filter = {"access_status": "approved"}
        title = "✅ **Usuarios permitidos**"
    elif status == "blocked":
        query_filter = {"access_status": {"$in": ["rejected", "revoked"]}}
        title = "⛔ **Usuarios rechazados / revocados**"
    else:
        query_filter = {"access_status": "pending"}
        title = "📨 **Solicitudes pendientes**"

    docs = await asyncio.to_thread(
        lambda: list(users_collection.find(query_filter).sort("last_seen", -1).limit(20))
    )
    if not docs:
        return f"{title}\n\nNo hay usuarios en esta lista.", access_list_keyboard([])
    lines = [title, "━━━━━━━━━━━━━━━━━━"]
    buttons = []
    for item in docs:
        target_id = int(item["user_id"])
        name = item.get("username") or item.get("first_name") or "Sin nombre"
        lines.append(f"• **{md_safe(str(name))}** — `{target_id}`")
        if status == "pending":
            buttons.append([
                btn(f"✅ Aceptar {target_id}", f"access_approve:{target_id}"),
                btn(f"❌ Rechazar {target_id}", f"access_reject:{target_id}")
            ])
        elif status == "allowed" and not is_admin(target_id):
            buttons.append([btn(f"🚫 Revocar {target_id}", f"access_revoke:{target_id}")])
    return "\n".join(lines), access_list_keyboard(buttons)


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
    pending = await asyncio.to_thread(users_collection.count_documents, {"access_status": "pending"})
    allowed = await asyncio.to_thread(users_collection.count_documents, {"access_status": "approved"})
    blocked = await asyncio.to_thread(users_collection.count_documents, {"access_status": {"$in": ["rejected", "revoked"]}})
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
        f"👥 Usuarios registrados: **{users}**\n"
        f"📨 Solicitudes pendientes: **{pending}**\n"
        f"✅ Usuarios aprobados: **{allowed}**\n"
        f"⛔ Rechazados / revocados: **{blocked}**\n"
        f"📦 Trabajos: **{jobs}** (✅ {successes})\n"
        f"⚙️ En curso ahora: **{running}**\n"
        f"💽 Espacio libre: **{free}**\n"
        f"⏱️ Activo hace: **{format_duration(time.time() - BOT_STARTED_AT)}**"
    )


def prepare_welcome_image() -> Path | None:
    """Devuelve la ruta de la imagen de bienvenida."""
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
    """Envía primero el video de bienvenida y conserva la imagen como alternativa."""
    if WELCOME_VIDEO.exists():
        try:
            await message.reply_video(
                video=str(WELCOME_VIDEO),
                caption=WELCOME_TEXT,
                reply_markup=main_keyboard(user_id)
            )
            return
        except Exception:
            logger.warning("No se pudo enviar el video de bienvenida", exc_info=True)
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


async def deny_unapproved(message: Message, user_id: int) -> bool:
    status = await get_access_status(user_id)
    if status == "approved":
        return False
    if status == "pending":
        text = "⏳ Tu solicitud de acceso está pendiente de revisión. Te avisaremos cuando sea aprobada."
    elif status == "rejected":
        text = "⛔ Tu solicitud fue rechazada. No tenés autorización para utilizar este bot."
    elif status == "revoked":
        text = "⛔ Tu acceso fue revocado. No tenés autorización para utilizar este bot."
    else:
        text = "⛔ No tenés autorización para utilizar este bot. Escribí /start para solicitar acceso."
    await message.reply_text(text)
    return True


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
# PROCESAMIENTO DE ARCHIVOS (LIMPIEZA INMEDIATA DE RAM / DISCO)
# ============================================================
async def download_and_send(
    user_id: int,
    source_message: Message,
    new_name: str,
    mode: str,
    thumbnail: str | None = None,
    metadata: dict | None = None,
    position: tuple[int, int] | None = None,
    keep_thumbnail_after: bool = False,
    on_complete_callback=None
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
                reply_markup=MULTIPDF_ACTIVE_KEYBOARD if keep_thumbnail_after else HOME_KEYBOARD
            )
            if on_complete_callback:
                await on_complete_callback()
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
        # Borrado inmediato del archivo descargado y carpeta de trabajo.
        shutil.rmtree(workdir, ignore_errors=True)
        last_progress_edit.pop(user_id, None)
        if thumbnail and not keep_thumbnail_after:
            thumb_dir = Path(thumbnail).parent
            if thumb_dir != TMP_ROOT and thumb_dir.exists() and thumb_dir.name.startswith("thumb_"):
                shutil.rmtree(thumb_dir, ignore_errors=True)
            else:
                remove_path_tree(thumbnail)
        gc.collect()


def start_task(user_id: int, coroutine, after_done=None):
    old = active_tasks.get(user_id)
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(coroutine)
    active_tasks[user_id] = task

    def cleanup(done_task):
        if active_tasks.get(user_id) is done_task:
            active_tasks.pop(user_id, None)
        if after_done:
            after_done()
        gc.collect()

    task.add_done_callback(cleanup)


def launch_rename(user_id: int, original: Message, new_name: str):
    user_states[user_id] = {"stage": "processing", "mode": "rename"}

    def finish_cb():
        purge_user_ephemeral(user_id, force_thumb=True)
        user_states[user_id] = {"stage": "idle"}

    start_task(user_id, download_and_send(user_id, original, new_name, "rename"), after_done=finish_cb)


def launch_full(user_id: int, state: dict, metadata: dict):
    original = state.get("file_message")
    new_name = state.get("new_name", "")
    thumbnail = state.get("thumbnail")
    user_states[user_id] = {"stage": "processing", "mode": "full"}

    def finish_cb():
        purge_user_ephemeral(user_id, force_thumb=True)
        user_states[user_id] = {"stage": "idle"}

    start_task(
        user_id,
        download_and_send(user_id, original, new_name, "full", thumbnail, metadata),
        after_done=finish_cb
    )


def launch_multithumb_pdf(user_id: int, pdf_message: Message, final_name: str, thumbnail: str):
    processed_count = user_states.get(user_id, {}).get("processed_count", 0)
    user_states[user_id] = {
        "stage": "processing",
        "mode": "multithumb",
        "thumbnail": thumbnail,
        "keep_thumbnail_session": True,
        "processed_count": processed_count,
    }

    async def after_send_prompt():
        count = processed_count + 1
        user_states[user_id] = {
            "stage": "multithumb_wait_pdf",
            "mode": "multithumb",
            "thumbnail": thumbnail,
            "keep_thumbnail_session": True,
            "processed_count": count,
        }
        await bot.send_message(
            user_id,
            f"🖼️ **Miniatura activa ({count} PDF listo)**\n\n"
            "Enviame el **siguiente PDF** para usar esta misma imagen, o tocá **Terminar sesión** para limpiar todo.",
            reply_markup=MULTIPDF_ACTIVE_KEYBOARD
        )

    def finish_cb():
        if user_states.get(user_id, {}).get("stage") == "processing":
            user_states[user_id] = {
                "stage": "multithumb_wait_pdf",
                "mode": "multithumb",
                "thumbnail": thumbnail,
                "keep_thumbnail_session": True,
                "processed_count": processed_count + 1,
            }
        gc.collect()

    start_task(
        user_id,
        download_and_send(
            user_id,
            pdf_message,
            final_name,
            "multithumb",
            thumbnail=thumbnail,
            keep_thumbnail_after=True,
            on_complete_callback=after_send_prompt
        ),
        after_done=finish_cb
    )


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

    def finish_cb():
        purge_user_ephemeral(user_id, force_thumb=True)
        user_states[user_id] = {"stage": "idle"}

    start_task(user_id, process_batch(), after_done=finish_cb)


def finish_batch_stage(user_id: int) -> str | None:
    """Pasa el lote a la etapa de prefijo. Devuelve un error en texto, o None si salió bien."""
    state = user_states.get(user_id, {})
    if state.get("stage") != "batch_collect":
        return "No hay un lote pendiente. Elegí 📦 Lote de archivos en el menú."
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
    """Cierra el proceso. Northflank lo levanta automáticamente."""
    logger.warning("Reiniciando el bot. Motivo: %s", reason)
    os._exit(1)


async def watchdog():
    """Si Telegram deja de responder varias veces seguidas, reinicia el bot."""
    failures = 0
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
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
        status = await get_access_status(uid)
    except Exception:
        logger.exception("No se pudo registrar o verificar al usuario en MongoDB")
        await message.reply_text("⚠️ No pude verificar tu acceso en este momento. Intentá nuevamente en unos minutos.")
        return

    if status == "approved":
        reset_state(uid)
        await send_welcome(message, uid)
    elif status == "pending":
        await notify_admins_access_request(message.from_user)
        await message.reply_text(
            "🕒 **Solicitud de acceso enviada**\n\n"
            "Tu solicitud está pendiente de aprobación por el administrador. "
            "No necesitás volver a solicitarla; te avisaremos cuando se resuelva."
        )
    elif status == "rejected":
        await message.reply_text("⛔ Tu solicitud fue rechazada. No tenés autorización para utilizar este bot.")
    else:
        await message.reply_text("⛔ Tu acceso fue revocado. Contactá al administrador si necesitás recuperarlo.")


@bot.on_message(filters.command("cancel"))
async def cancel_command(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if await deny_unapproved(message, uid):
        return
    task = active_tasks.get(uid)
    if task and not task.done():
        task.cancel()
    reset_state(uid, force_thumb=True)
    await message.reply_text("🛑 **Operación detenida y memoria limpia.**", reply_markup=HOME_KEYBOARD)


@bot.on_message(filters.command("finish"))
async def finish_batch_command(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if await deny_unapproved(message, uid):
        return
    error = finish_batch_stage(uid)
    if error:
        await message.reply_text(error)
        return
    await message.reply_text(prefix_prompt(uid), reply_markup=PREFIX_KEYBOARD)


@bot.on_message(filters.command("history"))
async def history_command(client: Client, message: Message):
    if not message.from_user:
        return
    if await deny_unapproved(message, message.from_user.id):
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

    # ----- Gestión de accesos (exclusiva de administradores) -----
    if data.startswith("access_"):
        if not is_admin(uid):
            await query.answer("Acceso denegado.", show_alert=True)
            return

        if data in ("access_pending", "access_allowed", "access_blocked"):
            status_key = {
                "access_pending": "pending",
                "access_allowed": "allowed",
                "access_blocked": "blocked"
            }[data]
            text, keyboard = await build_access_list(status_key)
            await query.answer()
            await safe_edit(message, text, reply_markup=keyboard)
            return

        match = re.fullmatch(r"access_(approve|reject|revoke):(\d+)", data)
        if match:
            action, target_id_text = match.groups()
            target_id = int(target_id_text)
            if target_id in ADMIN_IDS:
                await query.answer("No se puede modificar el acceso de un administrador.", show_alert=True)
                return
            new_status = {"approve": "approved", "reject": "rejected", "revoke": "revoked"}[action]
            update = {"access_status": new_status, "access_updated_at": datetime.now(timezone.utc)}
            stamp = datetime.now(timezone.utc)
            update[{"approve": "approved_at", "reject": "rejected_at", "revoke": "revoked_at"}[action]] = stamp
            update[{"approve": "approved_by", "reject": "rejected_by", "revoke": "revoked_by"}[action]] = uid
            result = await asyncio.to_thread(
                users_collection.update_one, {"user_id": target_id}, {"$set": update}
            )
            if action in ("reject", "revoke"):
                running_task = active_tasks.get(target_id)
                if running_task and not running_task.done():
                    running_task.cancel()
                reset_state(target_id, force_thumb=True)
            if result.matched_count == 0:
                await query.answer("No se encontró ese usuario.", show_alert=True)
                return
            try:
                notice = {
                    "approve": "✅ Tu acceso a Black Renowned Bot fue aprobado permanentemente. Escribí /start para comenzar.",
                    "reject": "⛔ Tu solicitud de acceso a Black Renowned Bot fue rechazada.",
                    "revoke": "⛔ Tu autorización para utilizar Black Renowned Bot fue revocada."
                }[action]
                await bot.send_message(target_id, notice)
            except Exception:
                logger.info("No se pudo notificar el cambio de acceso al usuario %s", target_id)
            label = {"approve": "aprobado", "reject": "rechazado", "revoke": "revocado"}[action]
            await query.answer(f"Acceso {label}.")
            await safe_edit(
                message,
                f"✅ Acceso **{label}** para el usuario `{target_id}`.",
                reply_markup=InlineKeyboardMarkup([[btn("⬅️ Panel admin", "admin")]])
            )
            return

    if not await is_authorized(uid):
        await query.answer("No tenés autorización. Escribí /start para consultar tu acceso.", show_alert=True)
        return

    # ----- Navegación -----
    if data == "menu":
        reset_state(uid, force_thumb=True)
        await query.answer()
        await safe_edit(message, WELCOME_TEXT, reply_markup=main_keyboard(uid))

    elif data == "newmenu":
        reset_state(uid, force_thumb=True)
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
            reset_state(uid, force_thumb=True)
            await query.answer("🛑 Deteniendo la operación…")
        else:
            await query.answer("No hay ninguna operación en curso.", show_alert=True)

    elif data == "reset":
        reset_state(uid, force_thumb=True)
        await query.answer("Listo, sesión cerrada y limpiada.")
        await safe_edit(
            message, "🛑 **Sesión cerrada y temporales borrados.**\n\nVolvé al menú cuando quieras.",
            reply_markup=HOME_KEYBOARD
        )

    # ----- Modos -----
    elif data == "mode_rename":
        reset_state(uid, force_thumb=True)
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
        reset_state(uid, force_thumb=True)
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

    elif data == "mode_multithumb":
        reset_state(uid, force_thumb=True)
        user_states[uid] = {
            "stage": "multithumb_wait_thumb",
            "mode": "multithumb",
            "thumbnail": None,
            "keep_thumbnail_session": True,
            "processed_count": 0,
        }
        await query.answer()
        await safe_edit(
            message,
            "📚 **Multi-PDF con una misma imagen**\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ Enviame **una imagen** (se reutiliza para todos los PDFs)\n"
            "2️⃣ Enviame un **PDF**\n"
            "3️⃣ Elegí si querés **cambiar el nombre** o **dejar el nombre actual**\n"
            "4️⃣ Repetí con más PDFs o tocá **Terminar sesión**",
            reply_markup=NAV_KEYBOARD
        )

    elif data == "mode_batch":
        reset_state(uid, force_thumb=True)
        user_states[uid] = {"stage": "batch_collect", "mode": "batch", "files": []}
        await query.answer()
        await safe_edit(
            message,
            "📦 **Procesamiento por lotes**\n"
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

    elif data == "mpdf_keep":
        state = user_states.get(uid, {})
        pdf_msg = state.get("pending_pdf")
        thumb = state.get("thumbnail")
        if state.get("stage") != "multithumb_choose_name" or not pdf_msg or not thumb:
            await query.answer("Esta opción ya no está disponible.", show_alert=True)
            return
        orig_name = clean_filename(media_name(pdf_msg))
        state.pop("pending_pdf", None)
        await query.answer("Dejando nombre actual…")
        await safe_edit(message, f"📄 Manteniendo `{md_safe(orig_name)}`…")
        launch_multithumb_pdf(uid, pdf_msg, orig_name, thumb)

    elif data == "mpdf_rename":
        state = user_states.get(uid, {})
        pdf_msg = state.get("pending_pdf")
        if state.get("stage") != "multithumb_choose_name" or not pdf_msg:
            await query.answer("Esta opción ya no está disponible.", show_alert=True)
            return
        state["stage"] = "multithumb_wait_name"
        await query.answer()
        await safe_edit(
            message,
            f"✏️ Escribí el **nuevo nombre** para `{md_safe(media_name(pdf_msg))}` (con o sin `.pdf`):",
            reply_markup=MULTIPDF_ACTIVE_KEYBOARD
        )

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
    if await deny_unapproved(message, uid):
        return
    state = user_states.get(uid, {})
    stage = state.get("stage")

    if stage == "rename_wait_name":
        original = state["file_message"]
        suffix = Path(media_name(original)).suffix
        requested = clean_filename(message.text)
        if not Path(requested).suffix and suffix:
            requested += suffix
        launch_rename(uid, original, requested)

    elif stage == "multithumb_wait_name":
        pdf_msg = state.get("pending_pdf")
        thumb = state.get("thumbnail")
        if not pdf_msg or not thumb:
            reset_state(uid, force_thumb=True)
            await message.reply_text("📂 La sesión expiró. Elegí una opción:", reply_markup=main_keyboard(uid))
            return
        requested = clean_filename(message.text)
        if not requested.lower().endswith(".pdf"):
            requested += ".pdf"
        state.pop("pending_pdf", None)
        launch_multithumb_pdf(uid, pdf_msg, requested, thumb)

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

    elif stage == "multithumb_choose_name":
        await message.reply_text(
            "Elegí un botón: **Cambiar nombre** o **Dejar nombre actual**.",
            reply_markup=MULTIPDF_NAME_KEYBOARD
        )

    elif stage == "multithumb_wait_pdf":
        await message.reply_text(
            "📄 Enviame el próximo **PDF** para usar la miniatura guardada, o tocá **Terminar sesión**.",
            reply_markup=MULTIPDF_ACTIVE_KEYBOARD
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
    if await deny_unapproved(message, uid):
        return
    state = user_states.get(uid, {})
    stage = state.get("stage")

    if stage in ("full_wait_thumb", "multithumb_wait_thumb"):
        is_image = bool(message.photo) or (
            bool(message.document) and (message.document.mime_type or "").startswith("image/")
        )
        if not is_image:
            await message.reply_text("🖼️ Necesito una imagen para la miniatura. Enviame una foto o imagen.")
            return
        folder = Path(tempfile.mkdtemp(prefix="thumb_", dir=TMP_ROOT))
        try:
            raw = await bot.download_media(message, file_name=str(folder / "source"))
            thumb_path = str(folder / "thumb.jpg")
            await make_thumbnail_jpeg(raw, thumb_path)
            remove_path_tree(raw)
            state["thumbnail"] = thumb_path
            if stage == "full_wait_thumb":
                state["stage"] = "full_wait_file"
                await message.reply_text(
                    "✅ **Miniatura guardada.**\n\nAhora enviame el archivo que querés procesar.",
                    reply_markup=CANCEL_KEYBOARD
                )
            else:
                state["stage"] = "multithumb_wait_pdf"
                state["keep_thumbnail_session"] = True
                await message.reply_text(
                    "✅ **Miniatura lista para varios PDFs.**\n\n"
                    "Enviame el primer **PDF**.",
                    reply_markup=MULTIPDF_ACTIVE_KEYBOARD
                )
        except Exception as exc:
            shutil.rmtree(folder, ignore_errors=True)
            await message.reply_text(f"❌ No pude preparar esa imagen: {md_safe(str(exc))[:300]}")
        return

    if not (message.document or message.video or message.audio or message.animation or message.voice or message.video_note):
        await message.reply_text("Ese tipo de archivo no es compatible. Enviame un documento, video o audio.")
        return

    size = media_size(message)
    if size and size > MAX_FILE_SIZE:
        await message.reply_text(f"❌ El archivo supera el límite configurado de {MAX_FILE_SIZE_MB} MB.")
        return

    if stage == "multithumb_wait_pdf":
        if not is_pdf_message(message):
            await message.reply_text(
                "📄 Este modo trabaja con archivos **PDF**. Enviame un PDF o tocá **Terminar sesión**.",
                reply_markup=MULTIPDF_ACTIVE_KEYBOARD
            )
            return
        state["pending_pdf"] = message
        state["stage"] = "multithumb_choose_name"
        await message.reply_text(
            f"📄 PDF recibido: `{md_safe(media_name(message))}`\n\n"
            "¿Querés cambiarle el nombre o dejar el que está puesto?",
            reply_markup=MULTIPDF_NAME_KEYBOARD
        )
    elif stage == "rename_wait_file":
        state["file_message"] = message
        state["stage"] = "rename_wait_name"
        await message.reply_text(
            f"📄 Recibido: `{md_safe(media_name(message))}`\n\n"
            "Ahora escribí el **nuevo nombre**, con o sin extensión.",
            reply_markup=CANCEL_KEYBOARD
        )
    elif stage == "full_wait_file":
        state["file_message"] = message
        state["stage"] = "full_wait_name"
        await message.reply_text(
            f"📄 Recibido: `{md_safe(media_name(message))}`\n\n"
            "Escribí el **nombre final** del archivo o conservá el original.",
            reply_markup=KEEPNAME_KEYBOARD
        )
    elif stage == "batch_collect":
        state.setdefault("files", []).append(message)
        await message.reply_text(
            f"➕ `{md_safe(media_name(message))}`\n"
            f"Total en el lote: **{len(state['files'])}**",
            reply_markup=BATCH_KEYBOARD
        )
    elif stage == "processing":
        await message.reply_text("⏳ Estoy procesando un archivo. Esperá a que termine o detenelo con 🛑 o /cancel.")
    else:
        await message.reply_text("📂 Primero elegí qué querés hacer:", reply_markup=main_keyboard(uid))


# ============================================================
# SERVIDOR HTTP PARA NORTHFLANK
# ============================================================
async def health(request):
    return web.json_response({
        "status": "ok",
        "service": "Black Renowned Bot",
        "platform": "northflank",
        "uptime_seconds": int(time.time() - BOT_STARTED_AT)
    })


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
    for attempt in range(1, 4):
        try:
            await asyncio.to_thread(mongo_client.admin.command, "ping")
            break
        except Exception:
            logger.warning("MongoDB no respondió (intento %s/3)", attempt, exc_info=True)
            if attempt == 3:
                raise
            await asyncio.sleep(5)
    logger.info("Conexión con MongoDB correcta.")

    await asyncio.to_thread(
        users_collection.update_many,
        {"access_status": {"$exists": False}},
        {"$set": {
            "access_status": "pending",
            "access_requested_at": datetime.now(timezone.utc),
            "access_request_notified": False
        }}
    )

    runner = await start_http_server()
    await bot.start()
    me = await bot.get_me()
    logger.info("Black Renowned Bot conectado como @%s", me.username)
    if WELCOME_VIDEO.exists():
        logger.info("Video de bienvenida listo: %s", WELCOME_VIDEO)
    else:
        logger.warning("No se encontró Bienvenida.mp4 junto a bot.py; se usará la bienvenida alternativa.")
    if WELCOME_IMAGE:
        logger.info("Imagen de bienvenida alternativa lista.")

    watchdog_task = asyncio.create_task(watchdog())
    try:
        await idle()
    finally:
        watchdog_task.cancel()
        await bot.stop()
        await runner.cleanup()
        mongo_client.close()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        gc.collect()


# ============================================================
# IMAGEN DE BIENVENIDA (incluida en el código)
# ============================================================
WELCOME_IMAGE_B64 = """
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
zzzzzzzzyxxdNscFnGP3XCxyzTzzzzzzzyTTzzzzzzzwIbSsrHdCmdhhzzzzzzzzzzzzzzzzzzwhduMce/kBtIHrzzzzzzzzzzyj
zzzzzzzyGHwgiNJzzTRzzzzzzzzzzzzzzzzzzzzzyu+spwjwZVJx97Tzzzzzzzzzzzzzzzzy03zzzznyxzzzzzzzzzzzzzzzzzzz
zzzzyz5f67RIrjgqf4zbTzzyjTzzzyzUVHyxTzzzzzzzxzzzzzzzzggBTzzzzzzzzzzzzzxy5xDxSQAxbWf+SDvzzwBDDzzw302X
zzzzzzzzzzzzzzzzx3nW3zzzzzzzzzzzzzzzzzyiiSihjx87QrL/AI8oEc408sV+U884048o88840oQQ8kR1ht40Qwww08088888
888888s4YE488+Wb7l48UgsI04QR4oEock0o4oQ88sIM4RNJx5YgEMMMAIF5BxNh888888884kA8888mqWhAAYYgc8cBtcp5Yl8s
gsos8QwUgEZlN9ZwwJFNBsB9JBRB088888888v6488NuLZCqU08kw4wkhhRJkc4oQs4c8gQ8sK+mFQEYwBNJIogIhNRR08888888
84SaOiH2+9oms0coNIMUUlNB55lcgEcc8YcsQRl+G6iUs8J4mqWA+qUpl888888888Gym2gNuiNj88F4kNFkYFNNdd1U8Nl88c88
88NAiQiKR889sSEMyqv1c88888w8888+aaJE4HrKQ85BNVNJp0BRxJxg98Q88QgwQg4wwx11VY0hRx9NJB8MNY8888wU088MKqef
Bfx9mR8RhBYd5lRV119l99M88Z95xpwwxI5JRwRxNNN1NNJNtd88884l488sEoEqrOrCUhtoFpVVJ19JBBZ1tN08oNZBd59NJBxx
RJxt999ZxxhRhVN888oNZ088se80B7t50FJtJNVJNJV9hVhNBBc88xjjP6GvXjDTTdefHHzzx72yODR0888BxF888GqkWB+wotBA
lxZZ1NJF91p5JFU8tzKTKOOeOemaiPyC+C2i7v6CaKCS884NNhAJggaY+cEgwV9gMFFlZhxhYw1N1988JG222fSim7H7vfqTnOym
PLLnHymyO88d9j53f77G800oBAHaNM1JRBh8xM04zv0882m67yGuOCS2qPume+a2+KSWKWi2C1szDPCymG3S6sjAN8S+vJNtJCWO
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
"""
WELCOME_IMAGE = prepare_welcome_image()


if __name__ == "__main__":
    # IMPORTANTE: se usa el mismo event loop con el que Pyrogram creó el cliente.
    # Con asyncio.run() el bot conecta pero no procesa ningún mensaje.
    # Funciona con Python 3.11 (fijá PYTHON_VERSION=3.11.9 en Northflank).
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
