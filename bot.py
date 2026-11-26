import asyncio
import base64
import ctypes
import gc
import hashlib
import io
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from PIL import Image
from pymongo import MongoClient
from pyrogram import Client, filters, idle
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ============================================================
# CONFIGURACIÓN (BAJO CONSUMO / SIN HISTORIAL)
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

WELCOME_IMAGE_B64 = ""
WELCOME_VIDEO = Path(__file__).resolve().parent / "Bienvenida.mp4"
WELCOME_FILE_ID: str | None = None
WELCOME_SHA1: str | None = None
WELCOME_LOCK = asyncio.Lock()
LOCAL_TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET_HOURS", "-3"))))
WATCHDOG_INTERVAL = int(os.getenv("WATCHDOG_INTERVAL", "120"))

# Reinicio automático por memoria (en MB). 0 = apagado.
# Activado por defecto en 200 MB: se reinicia solo si supera 200 MB y nadie lo está usando.
# No corta trabajos ni borra un PDF que alguien esté eligiendo.
AUTO_RESTART_MB = int(os.getenv("AUTO_RESTART_MB", "200"))

CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "180"))
MAX_TEMP_AGE_SECONDS = int(os.getenv("MAX_TEMP_AGE_SECONDS", "240"))
# Tope de lo que pueden ocupar los archivos temporales en disco a la vez.
TEMP_BUDGET_MB = int(os.getenv("TEMP_BUDGET_MB", "250"))
# Un PDF esperando que elijas nombre/miniatura se borra solo pasado este tiempo.
PREVIEW_TTL_SECONDS = int(os.getenv("PREVIEW_TTL_SECONDS", "1200"))
# Memoria máxima aproximada al decodificar la imagen de una miniatura, y peso máximo de la imagen que se manda.
MAX_THUMB_SOURCE_MB = int(os.getenv("MAX_THUMB_SOURCE_MB", "70"))
THUMB_MAX_DOWNLOAD_MB = int(os.getenv("THUMB_MAX_DOWNLOAD_MB", "25"))
BOT_STARTED_AT = time.time()
BOT_PAUSED = False

COMMANDS = ["start", "cancel", "admin", "restart"]

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
settings_collection = db["settings"]

bot = Client(
    "BlackRenownedBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
)

user_states: dict[int, dict] = {}
active_tasks: dict[int, asyncio.Task] = {}
last_progress_edit: dict[int, float] = {}
PROCESS_SEMAPHORE = asyncio.Semaphore(1)


# ============================================================
# TECLADOS Y TEXTOS
# ============================================================
def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [btn("📄 Procesar PDF", "start_pdf")],
        [btn("✖️ Cerrar", "close")],
    ]
    if user_id is not None and is_admin(user_id):
        rows.append([btn("🛡️ Panel admin", "admin")])
    return InlineKeyboardMarkup(rows)


def pdf_actions_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [btn("✅ Dejar nombre", "keepname"), btn("✏️ Renombrar", "ask_rename")],
        [btn("🖼️ Poner miniatura", "ask_thumb"), btn("✖️ Cerrar", "reset")],
    ]
    if is_admin(user_id):
        rows.append([btn("🛡️ Panel admin", "admin")])
    return InlineKeyboardMarkup(rows)


STOP_KEYBOARD = InlineKeyboardMarkup([[btn("🛑 Detener operación", "stop")]])
HOME_KEYBOARD = InlineKeyboardMarkup([[btn("🏠 Menú principal", "newmenu")]])
NAV_KEYBOARD = InlineKeyboardMarkup([[btn("⬅️ Volver", "menu"), btn("✖️ Cerrar", "close")]])
CANCEL_KEYBOARD = InlineKeyboardMarkup([[btn("✖️ Cancelar", "reset")]])


def admin_keyboard() -> InlineKeyboardMarkup:
    pause_btn = btn("▶️ Reanudar bot", "admin_toggle_pause") if BOT_PAUSED else btn("⏸️ Pausar bot", "admin_toggle_pause")
    return InlineKeyboardMarkup([
        [btn("📨 Solicitudes pendientes", "access_pending"), btn("✅ Permitidos", "access_allowed")],
        [btn("⛔ Rechazados / revocados", "access_blocked"), pause_btn],
        [btn("🧹 Limpiar temporales", "admin_cleanup"), btn("🔄 Reiniciar bot", "restart")],
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
    "Mandame un **PDF**.\n"
    "Tomo la primera imagen como miniatura y te muestro cómo queda para que elijas:\n"
    "• **Dejar nombre** o **Renombrar**\n"
    "• **Poner miniatura** si querés cambiarla\n\n"
    "Sin historial ni archivos guardados."
)


# ============================================================
# UTILIDADES Y LIMPIEZA INMEDIATA
# ============================================================
def md_safe(text: str) -> str:
    return text.replace("`", "'")


async def safe_edit(message: Message, text: str, **kwargs):
    try:
        if (
            message.photo or message.video or message.animation or message.document
            or message.audio or message.voice or message.video_note
        ):
            await message.edit_caption(text, **kwargs)
        else:
            await message.edit_text(text, **kwargs)
    except MessageNotModified:
        pass
    except Exception:
        logger.warning("No se pudo editar un mensaje", exc_info=True)


def media_size(message: Message) -> int:
    if message.document:
        return int(getattr(message.document, "file_size", 0) or 0)
    return 0


def media_name(message: Message) -> str:
    if message.document and message.document.file_name:
        return message.document.file_name
    return f"archivo_{message.id}.pdf"


def is_pdf_message(message: Message) -> bool:
    if not message.document:
        return False
    mime = (message.document.mime_type or "").lower()
    fname = (message.document.file_name or "").lower()
    return mime == "application/pdf" or fname.endswith(".pdf")


def clean_pdf_filename(name: str, fallback: str = "archivo.pdf") -> str:
    cleaned = Path(name.strip()).name
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", cleaned).strip(" .")
    stem = Path(cleaned).stem if cleaned else ""
    if not stem:
        stem = Path(re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", Path(fallback).name).strip(" .")).stem or "archivo"
    return f"{stem[:170]}.pdf"


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


try:
    _LIBC = ctypes.CDLL("libc.so.6")
except Exception:
    _LIBC = None
_last_trim = 0.0


def release_memory():
    """Libera memoria sobrante: recolecta basura y devuelve al sistema lo que Python ya no usa."""
    global _last_trim
    gc.collect()
    now = time.monotonic()
    if _LIBC is not None and now - _last_trim > 5:
        _last_trim = now
        try:
            _LIBC.malloc_trim(0)
        except Exception:
            pass


def clear_state_files(state: dict):
    for key in ("thumbnail", "pdf_path", "workdir"):
        val = state.pop(key, None)
        if val:
            remove_path_tree(val)


def purge_user_ephemeral(user_id: int, trim_mem: bool = True):
    last_progress_edit.pop(user_id, None)
    state = user_states.get(user_id)
    if state:
        clear_state_files(state)
        state.clear()
    if trim_mem:
        release_memory()


def reset_state(user_id: int, trim_mem: bool = True):
    state = user_states.get(user_id, {})
    if state.get("stage") != "processing":
        clear_state_files(state)
    purge_user_ephemeral(user_id, trim_mem=trim_mem)
    user_states[user_id] = {"stage": "idle"}


def sweep_stale_temp_files(max_age_seconds: int = MAX_TEMP_AGE_SECONDS, force_all_inactive: bool = False) -> int:
    removed = 0
    if not TMP_ROOT.exists():
        return 0
    now = time.time()
    active_paths = set()
    for st in user_states.values():
        for k in ("thumbnail", "pdf_path", "workdir"):
            if st.get(k):
                active_paths.add(str(Path(st[k]).resolve()))
    has_running = any(not t.done() for t in active_tasks.values())

    for entry in TMP_ROOT.iterdir():
        if entry.name == "welcome_embedded.jpg":
            continue
        try:
            entry_resolved = str(entry.resolve())
            if any(ap.startswith(entry_resolved) or entry_resolved.startswith(ap) for ap in active_paths):
                continue
            age = now - entry.stat().st_mtime
            should_remove = (force_all_inactive and not has_running) or (age >= max_age_seconds)
            if should_remove:
                if entry.is_file() or entry.is_symlink():
                    entry.unlink(missing_ok=True)
                else:
                    shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except Exception:
            logger.debug("No se pudo limpiar residuo %s", entry, exc_info=True)
    if removed:
        release_memory()
    return removed


def temp_usage_bytes() -> int:
    total = 0
    if not TMP_ROOT.exists():
        return 0
    for root, _dirs, files in os.walk(TMP_ROOT):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def expire_idle_states() -> int:
    """Borra los PDF que esperan una decisión del usuario hace demasiado tiempo."""
    now = time.monotonic()
    expired = 0
    for uid, st in list(user_states.items()):
        if st.get("stage") in ("pdf_ready", "waiting_name", "waiting_thumb") and st.get("pdf_path"):
            ready_at = st.setdefault("ready_at", now)
            if now - ready_at > PREVIEW_TTL_SECONDS:
                clear_state_files(st)
                st.clear()
                st["stage"] = "idle"
                expired += 1
    return expired


async def auto_cleaner_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            expired = expire_idle_states()
            if expired:
                logger.info("Vencieron %s PDF sin terminar; temporales borrados", expired)
            deleted = await asyncio.to_thread(sweep_stale_temp_files)
            release_memory()
            if deleted:
                logger.info("Autolimpieza: %s elementos residuales eliminados", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Error en ciclo de autolimpieza", exc_info=True)


# ============================================================
# ACCESOS Y ESTADO DE ADMIN
# ============================================================
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
        "¿Querés autorizarlo?"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=access_decision_keyboard(user.id))
        except Exception:
            logger.warning("No se pudo notificar al admin %s", admin_id, exc_info=True)


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


def month_key() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m")


async def record_upload(nbytes: int):
    if not nbytes:
        return
    try:
        await asyncio.to_thread(
            settings_collection.update_one,
            {"_id": f"traffic-{month_key()}"},
            {"$inc": {"uploaded_bytes": int(nbytes)}},
            upsert=True
        )
    except Exception:
        logger.warning("No se pudo registrar el tráfico subido", exc_info=True)


def auto_restart_summary() -> str:
    if AUTO_RESTART_MB > 0:
        return f"Activo (límite: **{AUTO_RESTART_MB} MB**, solo si nadie lo está usando)"
    return "Apagado (`AUTO_RESTART_MB=0`)"


async def build_admin_text() -> str:
    users = await asyncio.to_thread(users_collection.count_documents, {})
    pending = await asyncio.to_thread(users_collection.count_documents, {"access_status": "pending"})
    allowed = await asyncio.to_thread(users_collection.count_documents, {"access_status": "approved"})
    blocked = await asyncio.to_thread(users_collection.count_documents, {"access_status": {"$in": ["rejected", "revoked"]}})
    running = sum(1 for task in active_tasks.values() if not task.done())
    rss = current_rss_mb()
    rss_text = f"{rss} MB" if rss else "desconocido"
    try:
        free = format_bytes(shutil.disk_usage(TMP_ROOT).free)
    except Exception:
        free = "desconocido"
    try:
        traffic = await asyncio.to_thread(
            settings_collection.find_one, {"_id": f"traffic-{month_key()}"}
        )
        uploaded = format_bytes(int((traffic or {}).get("uploaded_bytes", 0)))
    except Exception:
        uploaded = "desconocido"

    modo_bot = "⏸️ PAUSADO para usuarios" if BOT_PAUSED else "▶️ ACTIVO"
    return (
        "🛡️ **Panel de administración**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ Estado: **{modo_bot}**\n"
        f"🧠 Memoria actual: **{rss_text}**\n"
        f"♻️ Reinicio aut. por memoria: **{auto_restart_summary()}**\n"
        f"📦 Tope PDF / Temp: **{MAX_FILE_SIZE_MB} MB / {TEMP_BUDGET_MB} MB**\n"
        f"👥 Usuarios: **{users}**\n"
        f"📨 Pendientes: **{pending}**\n"
        f"✅ Permitidos: **{allowed}**\n"
        f"⛔ Rechazados / revocados: **{blocked}**\n"
        f"⚡ En curso: **{running}**\n"
        f"💽 Espacio libre: **{free}**\n"
        f"📤 Subido este mes: **{uploaded}**\n"
        f"⏱️ Activo hace: **{format_duration(time.time() - BOT_STARTED_AT)}**"
    )


def prepare_welcome_image() -> Path | None:
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
        logger.warning("No se pudo preparar la imagen de bienvenida", exc_info=True)
    fallback = Path(__file__).resolve().parent / "welcome.jpg"
    return fallback if fallback.exists() else None


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def load_welcome_file_id():
    global WELCOME_FILE_ID, WELCOME_SHA1
    if not WELCOME_VIDEO.exists():
        return
    try:
        WELCOME_SHA1 = await asyncio.to_thread(file_sha1, WELCOME_VIDEO)
        doc = await asyncio.to_thread(settings_collection.find_one, {"_id": "welcome_video"})
        if doc and doc.get("sha1") == WELCOME_SHA1 and doc.get("file_id"):
            WELCOME_FILE_ID = doc["file_id"]
    except Exception:
        logger.warning("No se pudo recuperar el file_id del video de bienvenida", exc_info=True)


async def remember_welcome_file_id(sent: Message):
    global WELCOME_FILE_ID
    file_id = getattr(getattr(sent, "video", None), "file_id", None)
    if not file_id:
        return
    WELCOME_FILE_ID = file_id
    try:
        await asyncio.to_thread(
            settings_collection.update_one,
            {"_id": "welcome_video"},
            {"$set": {"file_id": file_id, "sha1": WELCOME_SHA1}},
            upsert=True
        )
    except Exception:
        logger.warning("No se pudo guardar el file_id del video de bienvenida", exc_info=True)


async def send_welcome(message: Message, user_id: int):
    global WELCOME_FILE_ID
    if WELCOME_VIDEO.exists():
        async with WELCOME_LOCK:
            if WELCOME_FILE_ID:
                try:
                    await message.reply_video(
                        video=WELCOME_FILE_ID,
                        caption=WELCOME_TEXT,
                        reply_markup=main_keyboard(user_id)
                    )
                    return
                except Exception:
                    WELCOME_FILE_ID = None
            try:
                sent = await message.reply_video(
                    video=str(WELCOME_VIDEO),
                    caption=WELCOME_TEXT,
                    reply_markup=main_keyboard(user_id)
                )
                await record_upload(WELCOME_VIDEO.stat().st_size)
                await remember_welcome_file_id(sent)
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
        if BOT_PAUSED and not is_admin(user_id):
            await message.reply_text("⏸️ El bot está en pausa en este momento. Intentá más tarde.")
            return True
        return False
    if status == "pending":
        text = "⏳ Tu solicitud de acceso está pendiente de revisión."
    elif status == "rejected":
        text = "⛔ Tu solicitud fue rechazada."
    elif status == "revoked":
        text = "⛔ Tu acceso fue revocado."
    else:
        text = "⛔ No tenés autorización. Escribí /start para solicitar acceso."
    await message.reply_text(text)
    return True


_BYTES_PER_PIXEL = {"1": 4, "L": 1, "P": 4, "RGB": 3, "LA": 4, "RGBA": 4}


def _save_jpeg_thumb(pil_img: Image.Image, destination: str) -> str:
    if pil_img.format == "JPEG":
        pil_img.draft("RGB", (640, 640))
    else:
        has_alpha = pil_img.mode in ("RGBA", "LA", "PA") or "transparency" in pil_img.info
        per_pixel = _BYTES_PER_PIXEL.get(pil_img.mode, 4) * (2 if has_alpha else 1)
        estimated_mb = pil_img.width * pil_img.height * per_pixel / (1024 * 1024)
        if estimated_mb > MAX_THUMB_SOURCE_MB:
            raise ValueError(
                "La imagen es demasiado grande para procesarla en este servidor. "
                "Mandala más chica o en formato JPG."
            )
    if pil_img.mode in ("P", "1"):
        pil_img = pil_img.convert("RGB")
    pil_img.thumbnail((320, 320))
    with pil_img.convert("RGB") as rgb:
        for quality in (82, 65, 50, 35):
            rgb.save(destination, "JPEG", quality=quality, optimize=True)
            if os.path.getsize(destination) < 200 * 1024:
                return destination
    raise ValueError("La miniatura supera 200 KB. Elegí otra imagen.")


async def make_thumbnail_jpeg(source: str, destination: str) -> str:
    def convert():
        with Image.open(source) as image:
            return _save_jpeg_thumb(image, destination)
    return await asyncio.to_thread(convert)


async def extract_first_pdf_image_thumb(pdf_path: str, destination: str) -> tuple[str | None, str]:
    """
    Extrae la primera imagen que aparece en la 1.ª página del PDF para usarla como miniatura.
    Si la 1.ª página no contiene imágenes incrustadas, renderiza la 1.ª página como respaldo.
    """
    def extract():
        import fitz
        doc = fitz.open(pdf_path)
        try:
            if len(doc) == 0:
                return None, "PDF sin páginas"
            page = doc[0]
            imgs = page.get_images(full=True)
            if imgs:
                try:
                    base = doc.extract_image(imgs[0][0])
                    raw_bytes = base.get("image") or b""
                    if len(raw_bytes) <= MAX_THUMB_SOURCE_MB * 1024 * 1024:
                        with Image.open(io.BytesIO(raw_bytes)) as img:
                            _save_jpeg_thumb(img, destination)
                        return destination, "1.ª imagen del PDF"
                except Exception as exc:
                    logger.info("No se pudo usar la 1.ª imagen del PDF (%s); se usa la página", exc)
            longest = max(page.rect.width, page.rect.height) or 1
            zoom = min(100 / 72, 1000 / longest)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            with Image.open(io.BytesIO(pix.tobytes("png"))) as img:
                _save_jpeg_thumb(img, destination)
            return destination, "1.ª página del PDF"
        finally:
            doc.close()

    try:
        return await asyncio.to_thread(extract)
    except Exception as exc:
        logger.warning("No se pudo extraer miniatura del PDF: %s", exc)
        return None, "Sin miniatura automática"


# ============================================================
# DESCARGA DE PDF, VISTA PREVIA DE MINIATURA Y ENVÍO FINAL
# ============================================================
async def prepare_pdf_preview(user_id: int, pdf_message: Message):
    workdir = Path(tempfile.mkdtemp(prefix=f"preview_{user_id}_", dir=TMP_ROOT))
    status = await pdf_message.reply_text("📥 Descargando PDF para ver su miniatura…", reply_markup=STOP_KEYBOARD)
    size = media_size(pdf_message)
    orig_name = clean_pdf_filename(media_name(pdf_message))

    try:
        if PROCESS_SEMAPHORE.locked():
            await safe_edit(status, "⏳ **En cola**…", reply_markup=STOP_KEYBOARD)

        async with PROCESS_SEMAPHORE:
            if size and size > MAX_FILE_SIZE:
                raise ValueError(f"El archivo supera el límite de {MAX_FILE_SIZE_MB} MB.")

            used = await asyncio.to_thread(temp_usage_bytes)
            if size and used + size > TEMP_BUDGET_MB * 1024 * 1024:
                raise ValueError("El servidor está ocupado con otros archivos. Probá de nuevo en unos minutos.")
            free = shutil.disk_usage(TMP_ROOT).free
            if size and free < size * 1.25:
                raise ValueError("No hay espacio temporal suficiente en el servidor.")

            target = workdir / orig_name
            started = time.monotonic()
            downloaded = await bot.download_media(
                pdf_message,
                file_name=str(target),
                progress=update_progress,
                progress_args=(status, user_id, "📥 **Descargando PDF**", orig_name, started)
            )
            if not downloaded or not os.path.isfile(downloaded):
                raise RuntimeError("Telegram no completó la descarga del PDF.")

            thumb_dest = str(workdir / "thumb.jpg")
            thumb_path, thumb_source = await extract_first_pdf_image_thumb(str(downloaded), thumb_dest)

        user_states[user_id] = {
            "stage": "pdf_ready",
            "workdir": str(workdir),
            "pdf_path": str(downloaded),
            "orig_name": orig_name,
            "thumbnail": thumb_path,
            "thumb_source": thumb_source,
            "ready_at": time.monotonic(),
        }
        try:
            await status.delete()
        except Exception:
            pass

        caption = (
            f"📄 **Archivo:** `{md_safe(orig_name)}`\n"
            f"🖼️ **Miniatura:** {thumb_source}\n\n"
            "¿Te gusta cómo queda o querés cambiar algo?"
        )
        if thumb_path and os.path.exists(thumb_path):
            await pdf_message.reply_photo(
                photo=thumb_path,
                caption=caption,
                reply_markup=pdf_actions_keyboard(user_id)
            )
        else:
            await pdf_message.reply_text(
                caption,
                reply_markup=pdf_actions_keyboard(user_id)
            )
    except asyncio.CancelledError:
        shutil.rmtree(workdir, ignore_errors=True)
        reset_state(user_id, trim_mem=False)
        await safe_edit(status, "🛑 **Operación detenida.**", reply_markup=HOME_KEYBOARD)
        raise
    except Exception as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        reset_state(user_id, trim_mem=False)
        logger.exception("Error preparando vista previa para user_id=%s", user_id)
        await safe_edit(
            status,
            f"❌ **No se pudo leer el PDF.**\n\n`{md_safe(str(exc))[:500]}`",
            reply_markup=HOME_KEYBOARD
        )
    finally:
        last_progress_edit.pop(user_id, None)
        release_memory()


async def send_prepared_pdf(user_id: int, anchor_message: Message, final_name: str):
    state = user_states.get(user_id, {})
    pdf_path = state.get("pdf_path")
    workdir = state.get("workdir")
    thumb_path = state.get("thumbnail")

    if not pdf_path or not os.path.exists(pdf_path):
        reset_state(user_id)
        await anchor_message.reply_text("⚠️ El archivo ya no está disponible. Mandá el PDF de nuevo.", reply_markup=HOME_KEYBOARD)
        return

    actual_name = clean_pdf_filename(final_name, state.get("orig_name", "archivo.pdf"))
    user_states[user_id] = {**state, "stage": "processing"}
    status = await anchor_message.reply_text("📤 Preparando envío…", reply_markup=STOP_KEYBOARD)

    try:
        async with PROCESS_SEMAPHORE:
            started = time.monotonic()
            await bot.send_document(
                chat_id=user_id,
                document=pdf_path,
                file_name=actual_name,
                thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
                caption=f"📄 `{md_safe(actual_name)}`",
                force_document=True,
                progress=update_progress,
                progress_args=(status, user_id, "📤 **Subiendo PDF**", actual_name, started)
            )
            await record_upload(os.path.getsize(pdf_path))
            await safe_edit(status, "✅ **¡Listo!** Archivos temporales eliminados.", reply_markup=HOME_KEYBOARD)
    except asyncio.CancelledError:
        await safe_edit(status, "🛑 **Envío detenido.**", reply_markup=HOME_KEYBOARD)
        raise
    except Exception as exc:
        logger.exception("Error enviando PDF para user_id=%s", user_id)
        await safe_edit(
            status,
            f"❌ **Error al enviar el PDF.**\n\n`{md_safe(str(exc))[:500]}`",
            reply_markup=HOME_KEYBOARD
        )
    finally:
        if workdir:
            remove_path_tree(workdir)
        purge_user_ephemeral(user_id, trim_mem=False)
        user_states[user_id] = {"stage": "idle"}
        release_memory()


def start_task(user_id: int, coroutine):
    old = active_tasks.get(user_id)
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(coroutine)
    active_tasks[user_id] = task

    def cleanup(done_task):
        if active_tasks.get(user_id) is done_task:
            active_tasks.pop(user_id, None)
        release_memory()

    task.add_done_callback(cleanup)


# ============================================================
# VIGILANCIA Y REINICIO
# ============================================================
def restart_process(reason: str):
    """Reinicia el bot dentro del mismo proceso para mantener estable el contenedor."""
    logger.warning("Reiniciando el bot. Motivo: %s", reason)
    try:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception:
        logger.exception("No se pudo reiniciar en el mismo proceso; se cierra el proceso")
        os._exit(1)


def current_rss_mb() -> int:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def bot_is_idle() -> bool:
    if PROCESS_SEMAPHORE.locked():
        return False
    if any(not task.done() for task in active_tasks.values()):
        return False
    return all(st.get("stage", "idle") == "idle" for st in user_states.values())


async def watchdog():
    failures = 0
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        if AUTO_RESTART_MB > 0 and bot_is_idle():
            rss = current_rss_mb()
            if rss > AUTO_RESTART_MB:
                restart_process(f"memoria ({rss} MB) por encima de AUTO_RESTART_MB ({AUTO_RESTART_MB} MB) sin trabajos ni PDFs pendientes")
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
        logger.exception("Error verificando acceso en MongoDB")
        await message.reply_text("⚠️ No pude verificar tu acceso en este momento.")
        return

    if status == "approved":
        reset_state(uid)
        await send_welcome(message, uid)
    elif status == "pending":
        await notify_admins_access_request(message.from_user)
        await message.reply_text("🕒 **Solicitud de acceso enviada.** Esperá la aprobación del admin.")
    elif status == "rejected":
        await message.reply_text("⛔ Tu solicitud fue rechazada.")
    else:
        await message.reply_text("⛔ Tu acceso fue revocado.")


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
    reset_state(uid)
    await message.reply_text("🛑 **Operación detenida y temporales borrados.**", reply_markup=HOME_KEYBOARD)


@bot.on_message(filters.command("admin"))
async def admin_command(client: Client, message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        if message.from_user:
            await message.reply_text("⛔ No tenés permiso para acceder al panel.")
        return
    await message.reply_text(await build_admin_text(), reply_markup=admin_keyboard())


@bot.on_message(filters.command("restart"))
async def restart_command(client: Client, message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        if message.from_user:
            await message.reply_text("⛔ No tenés permiso para reiniciar el bot.")
        return
    await message.reply_text("⚠️ **¿Reiniciar el bot?**", reply_markup=RESTART_CONFIRM_KEYBOARD)


# ============================================================
# BOTONES
# ============================================================
@bot.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    global BOT_PAUSED
    uid = query.from_user.id
    data = query.data or ""
    message = query.message
    if message is None:
        await query.answer("Mensaje no disponible. Escribí /start.", show_alert=True)
        return

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
                await query.answer("No se puede modificar a un administrador.", show_alert=True)
                return
            new_status = {"approve": "approved", "reject": "rejected", "revoke": "revoked"}[action]
            stamp = datetime.now(timezone.utc)
            update = {
                "access_status": new_status,
                "access_updated_at": stamp,
                {"approve": "approved_at", "reject": "rejected_at", "revoke": "revoked_at"}[action]: stamp,
                {"approve": "approved_by", "reject": "rejected_by", "revoke": "revoked_by"}[action]: uid
            }
            result = await asyncio.to_thread(
                users_collection.update_one, {"user_id": target_id}, {"$set": update}
            )
            if action in ("reject", "revoke"):
                running_task = active_tasks.get(target_id)
                if running_task and not running_task.done():
                    running_task.cancel()
                reset_state(target_id)
            if result.matched_count == 0:
                await query.answer("Usuario no encontrado.", show_alert=True)
                return
            try:
                notice = {
                    "approve": "✅ Tu acceso fue aprobado. Escribí /start para comenzar.",
                    "reject": "⛔ Tu solicitud de acceso fue rechazada.",
                    "revoke": "⛔ Tu acceso fue revocado."
                }[action]
                await bot.send_message(target_id, notice)
            except Exception:
                logger.info("No se pudo notificar a %s", target_id)
            label = {"approve": "aprobado", "reject": "rechazado", "revoke": "revocado"}[action]
            await query.answer(f"Acceso {label}.")
            await safe_edit(
                message,
                f"✅ Acceso **{label}** para `{target_id}`.",
                reply_markup=InlineKeyboardMarkup([[btn("⬅️ Panel admin", "admin")]])
            )
            return

    if not await is_authorized(uid):
        await query.answer("No tenés autorización. Escribí /start.", show_alert=True)
        return

    if BOT_PAUSED and not is_admin(uid):
        await query.answer("⏸️ El bot está en pausa.", show_alert=True)
        return

    if data == "menu":
        reset_state(uid)
        await query.answer()
        await safe_edit(message, WELCOME_TEXT, reply_markup=main_keyboard(uid))

    elif data == "newmenu":
        reset_state(uid)
        await query.answer()
        await send_welcome(message, uid)

    elif data == "start_pdf":
        reset_state(uid)
        await query.answer()
        await safe_edit(message, "📄 Mandame el **PDF** que querés procesar.", reply_markup=CANCEL_KEYBOARD)

    elif data == "close":
        await query.answer()
        try:
            await message.delete()
        except Exception:
            pass

    elif data == "stop":
        task = active_tasks.get(uid)
        if task and not task.done():
            task.cancel()
            reset_state(uid)
            await query.answer("🛑 Deteniendo…")
        else:
            await query.answer("No hay operación en curso.", show_alert=True)

    elif data == "reset":
        reset_state(uid)
        await query.answer("Cerrado y temporales borrados.")
        await safe_edit(message, "✖️ **Cerrado. Archivos temporales eliminados.**", reply_markup=HOME_KEYBOARD)

    elif data == "keepname":
        state = user_states.get(uid, {})
        if state.get("stage") != "pdf_ready" or not state.get("pdf_path"):
            await query.answer("No hay un PDF listo.", show_alert=True)
            return
        orig_name = state.get("orig_name", "archivo.pdf")
        await query.answer("Enviando con su nombre…")
        start_task(uid, send_prepared_pdf(uid, message, orig_name))

    elif data == "ask_rename":
        state = user_states.get(uid, {})
        if state.get("stage") != "pdf_ready" or not state.get("pdf_path"):
            await query.answer("No hay un PDF listo.", show_alert=True)
            return
        state["stage"] = "waiting_name"
        await query.answer()
        await message.reply_text(
            f"✏️ Escribí el **nuevo nombre** para `{md_safe(state.get('orig_name', 'archivo.pdf'))}`:",
            reply_markup=CANCEL_KEYBOARD
        )

    elif data == "ask_thumb":
        state = user_states.get(uid, {})
        if state.get("stage") != "pdf_ready" or not state.get("pdf_path"):
            await query.answer("No hay un PDF listo.", show_alert=True)
            return
        state["stage"] = "waiting_thumb"
        await query.answer()
        await message.reply_text(
            "🖼️ Mandá la **imagen** que querés usar de miniatura:",
            reply_markup=CANCEL_KEYBOARD
        )

    elif data == "admin":
        if not is_admin(uid):
            await query.answer("Acceso denegado.", show_alert=True)
            return
        await query.answer()
        await safe_edit(message, await build_admin_text(), reply_markup=admin_keyboard())

    elif data == "admin_toggle_pause":
        if not is_admin(uid):
            await query.answer("Acceso denegado.", show_alert=True)
            return
        BOT_PAUSED = not BOT_PAUSED
        await query.answer("Bot pausado." if BOT_PAUSED else "Bot reanudado.")
        await safe_edit(message, await build_admin_text(), reply_markup=admin_keyboard())

    elif data == "admin_cleanup":
        if not is_admin(uid):
            await query.answer("Acceso denegado.", show_alert=True)
            return
        deleted = await asyncio.to_thread(sweep_stale_temp_files, 0, True)
        await query.answer(f"Limpiados {deleted} elementos.")
        await safe_edit(
            message,
            f"🧹 **Limpieza manual ejecutada.** Se eliminaron **{deleted}** elementos residuales.\n\n"
            + await build_admin_text(),
            reply_markup=admin_keyboard()
        )

    elif data == "restart":
        if not is_admin(uid):
            await query.answer("Acceso denegado.", show_alert=True)
            return
        await query.answer()
        await safe_edit(message, "⚠️ **¿Reiniciar el bot?**", reply_markup=RESTART_CONFIRM_KEYBOARD)

    elif data == "restart_yes":
        if not is_admin(uid):
            await query.answer("Acceso denegado.", show_alert=True)
            return
        await query.answer("Reiniciando…")
        await safe_edit(message, "🔄 **Reiniciando el bot…**")
        await asyncio.sleep(1.5)
        restart_process(f"pedido del administrador {uid}")

    else:
        await query.answer()


# ============================================================
# TEXTO Y ARCHIVOS
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

    if stage == "waiting_name":
        final_name = clean_pdf_filename(message.text, state.get("orig_name", "archivo.pdf"))
        start_task(uid, send_prepared_pdf(uid, message, final_name))

    elif stage == "waiting_thumb":
        await message.reply_text("🖼️ Mandame una **imagen** para la miniatura, o tocá Cancelar.", reply_markup=CANCEL_KEYBOARD)

    elif stage == "pdf_ready":
        await message.reply_text(
            "Elegí **Dejar nombre**, **Renombrar** o **Poner miniatura** en los botones del mensaje anterior.",
            reply_markup=pdf_actions_keyboard(uid)
        )

    elif stage == "processing":
        await message.reply_text("⏳ Estoy procesando el PDF. Podés detenerlo con 🛑 o /cancel.")

    else:
        await message.reply_text("📄 Mandame un **PDF** para empezar.", reply_markup=main_keyboard(uid))


@bot.on_message(filters.document | filters.photo, group=1)
async def media_handler(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if await deny_unapproved(message, uid):
        return
    state = user_states.get(uid, {})
    stage = state.get("stage")

    if stage == "waiting_thumb":
        is_image = bool(message.photo) or (
            bool(message.document) and (message.document.mime_type or "").startswith("image/")
        )
        if not is_image:
            await message.reply_text("🖼️ Mandame una **imagen** para la miniatura.", reply_markup=CANCEL_KEYBOARD)
            return

        if media_size(message) > THUMB_MAX_DOWNLOAD_MB * 1024 * 1024:
            await message.reply_text(
                f"🖼️ Esa imagen pesa demasiado. Mandame una de hasta {THUMB_MAX_DOWNLOAD_MB} MB.",
                reply_markup=CANCEL_KEYBOARD
            )
            return

        workdir = Path(state.get("workdir") or tempfile.mkdtemp(prefix=f"preview_{uid}_", dir=TMP_ROOT))
        state["workdir"] = str(workdir)
        raw = await bot.download_media(message, file_name=str(workdir / "custom_raw"))
        custom_thumb = str(workdir / "custom_thumb.jpg")
        try:
            if not raw or not os.path.isfile(raw):
                raise RuntimeError("No se pudo descargar la imagen.")
            await make_thumbnail_jpeg(raw, custom_thumb)
            remove_path_tree(raw)
            old_thumb = state.get("thumbnail")
            if old_thumb and old_thumb != custom_thumb and os.path.exists(old_thumb):
                remove_path_tree(old_thumb)
            state["thumbnail"] = custom_thumb
            state["thumb_source"] = "Miniatura personalizada"
            state["stage"] = "pdf_ready"
            state["ready_at"] = time.monotonic()
            await message.reply_photo(
                photo=custom_thumb,
                caption=(
                    f"🖼️ **Miniatura actualizada** para `{md_safe(state.get('orig_name', 'archivo.pdf'))}`.\n"
                    "¿Dejamos el nombre o lo renombrás?"
                ),
                reply_markup=pdf_actions_keyboard(uid)
            )
        except Exception as exc:
            if raw:
                remove_path_tree(raw)
            state["stage"] = "pdf_ready"
            state["ready_at"] = time.monotonic()
            await message.reply_text(
                f"❌ No pude usar esa imagen: `{md_safe(str(exc))[:300]}`",
                reply_markup=pdf_actions_keyboard(uid)
            )
        finally:
            release_memory()
        return

    if not is_pdf_message(message):
        await message.reply_text("📄 Mandame un archivo **PDF**.")
        return

    if stage == "processing":
        await message.reply_text("⏳ Ya estoy procesando un PDF. Esperá o detenelo con 🛑 o /cancel.")
        return

    reset_state(uid)
    start_task(uid, prepare_pdf_preview(uid, message))


# ============================================================
# SERVIDOR HTTP Y ARRANQUE
# ============================================================
async def health(request):
    return web.json_response({
        "status": "ok",
        "service": "Black Renowned Bot",
        "uptime_seconds": int(time.time() - BOT_STARTED_AT)
    })


async def start_http_server():
    app_web = web.Application()
    app_web.router.add_get("/", health)
    app_web.router.add_get("/health", health)
    runner = web.AppRunner(app_web)
    await runner.setup()
    port = int(os.getenv("PORT") or os.getenv("SERVER_PORT") or "10000")
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner


WELCOME_IMAGE = prepare_welcome_image()


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

    await asyncio.to_thread(
        users_collection.update_many,
        {"access_status": {"$exists": False}},
        {"$set": {
            "access_status": "pending",
            "access_requested_at": datetime.now(timezone.utc),
            "access_request_notified": False
        }}
    )

    await load_welcome_file_id()

    runner = None
    try:
        runner = await start_http_server()
    except Exception:
        logger.warning("No se pudo abrir el servidor HTTP de salud; el bot sigue sin él.", exc_info=True)
    await bot.start()
    watchdog_task = asyncio.create_task(watchdog())
    cleaner_task = asyncio.create_task(auto_cleaner_loop())
    try:
        await idle()
    finally:
        cleaner_task.cancel()
        watchdog_task.cancel()
        await bot.stop()
        if runner:
            await runner.cleanup()
        mongo_client.close()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        release_memory()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
