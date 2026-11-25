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
from pyrogram import Client, filters, idle
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ============================================================
# CONFIGURACIÓN (NORTHFLANK / BAJO CONSUMO)
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

WELCOME_IMAGE: Path | None = None
WELCOME_VIDEO = Path(__file__).resolve().parent / "Bienvenida.mp4"
LOCAL_TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET_HOURS", "-3"))))
WATCHDOG_INTERVAL = int(os.getenv("WATCHDOG_INTERVAL", "120"))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "240"))
MAX_TEMP_AGE_SECONDS = int(os.getenv("MAX_TEMP_AGE_SECONDS", "300"))
MAX_HISTORY_DOCS = int(os.getenv("MAX_HISTORY_DOCS", "25"))
BOT_STARTED_AT = time.time()

COMMANDS = ["start", "cancel", "history", "admin", "restart"]

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
        [btn("🖼️ Miniatura + PDF", "mode_single")],
        [btn("📚 Una imagen + Varios PDFs", "mode_multi")],
        [btn("📜 Historial", "history"), btn("✖️ Cerrar", "close")],
    ]
    if user_id is not None and is_admin(user_id):
        rows.append([btn("🛡️ Panel admin", "admin")])
    return InlineKeyboardMarkup(rows)


STOP_KEYBOARD = InlineKeyboardMarkup([[btn("🛑 Detener operación", "stop")]])
HOME_KEYBOARD = InlineKeyboardMarkup([[btn("🏠 Menú principal", "newmenu")]])
NAV_KEYBOARD = InlineKeyboardMarkup([[btn("⬅️ Volver", "menu"), btn("✖️ Cerrar", "close")]])
CANCEL_KEYBOARD = InlineKeyboardMarkup([[btn("✖️ Cancelar", "reset")]])
KEEPNAME_KEYBOARD = InlineKeyboardMarkup([
    [btn("📄 Dejar nombre actual", "keepname")],
    [btn("✖️ Cancelar", "reset")],
])
MULTI_NAME_KEYBOARD = InlineKeyboardMarkup([
    [btn("✏️ Elegir nombre", "mpdf_rename"), btn("📄 Dejar nombre actual", "mpdf_keep")],
    [btn("🏁 Terminar sesión", "reset")],
])
MULTI_ACTIVE_KEYBOARD = InlineKeyboardMarkup([
    [btn("🏁 Terminar sesión", "reset")],
])
ADMIN_KEYBOARD = InlineKeyboardMarkup([
    [btn("📨 Solicitudes pendientes", "access_pending")],
    [btn("✅ Usuarios permitidos", "access_allowed")],
    [btn("⛔ Rechazados / revocados", "access_blocked")],
    [btn("🧹 Limpiar temporales ahora", "admin_cleanup")],
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
    "Poné miniatura a tus PDFs y devolvelos con el nombre que elijas o el mismo.\n\n"
    "✦ **Miniatura + PDF:** 1 imagen y 1 PDF\n"
    "✦ **Una imagen + Varios PDFs:** reutilizá la misma imagen sin reiniciar\n"
    "✦ Limpieza automática de temporales y cuidado de RAM\n\n"
    "Elegí una opción 👇"
)


# ============================================================
# UTILIDADES Y LIMPIEZA AUTOMÁTICA
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


def cleanup_thumbnail(state: dict, force: bool = False):
    thumb = state.pop("thumbnail", None)
    if not thumb:
        return
    if force or not state.get("keep_thumbnail_session"):
        folder = Path(thumb).parent
        if folder != TMP_ROOT and folder.exists() and folder.name.startswith("thumb_"):
            shutil.rmtree(folder, ignore_errors=True)
        else:
            remove_path_tree(thumb)


def purge_user_ephemeral(user_id: int, force_thumb: bool = True):
    last_progress_edit.pop(user_id, None)
    state = user_states.get(user_id)
    if state:
        cleanup_thumbnail(state, force=force_thumb)
        state.clear()
    gc.collect()


def reset_state(user_id: int, force_thumb: bool = True):
    state = user_states.get(user_id, {})
    if state.get("stage") != "processing":
        cleanup_thumbnail(state, force=force_thumb)
    purge_user_ephemeral(user_id, force_thumb=force_thumb)
    user_states[user_id] = {"stage": "idle"}


def sweep_stale_temp_files(max_age_seconds: int = MAX_TEMP_AGE_SECONDS, force_all_inactive: bool = False) -> int:
    removed = 0
    if not TMP_ROOT.exists():
        return 0
    now = time.time()
    active_thumbs = {
        str(Path(st["thumbnail"]).resolve())
        for st in user_states.values()
        if st.get("thumbnail")
    }
    has_running = any(not t.done() for t in active_tasks.values())

    for entry in TMP_ROOT.iterdir():
        if entry.name == "welcome_embedded.jpg":
            continue
        try:
            entry_resolved = str(entry.resolve())
            if any(th.startswith(entry_resolved) for th in active_thumbs):
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
        gc.collect()
    return removed


async def auto_cleaner_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            deleted = await asyncio.to_thread(sweep_stale_temp_files)
            if deleted:
                logger.info("Autolimpieza: %s elementos residuales eliminados", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Error en ciclo de autolimpieza", exc_info=True)


# ============================================================
# ACCESOS Y REGISTRO MÍNIMO (SOLO ADMIN / HISTORIAL)
# ============================================================
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


def _trim_history_sync(user_id: int, keep_last: int):
    if keep_last <= 0:
        history_collection.delete_many({"user_id": user_id})
        return
    old_docs = list(
        history_collection.find({"user_id": user_id}, {"_id": 1})
        .sort("_id", -1)
        .skip(keep_last)
    )
    if old_docs:
        ids = [d["_id"] for d in old_docs]
        history_collection.delete_many({"_id": {"$in": ids}})


async def save_history(user_id: int, final_name: str, size: int, result: str):
    record = {
        "user_id": user_id,
        "final_name": final_name,
        "size": size,
        "result": result,
        "created_at": datetime.now(timezone.utc)
    }
    try:
        await asyncio.to_thread(history_collection.insert_one, record)
        await asyncio.to_thread(_trim_history_sync, user_id, MAX_HISTORY_DOCS)
    except Exception:
        logger.exception("No se pudo guardar el historial")


async def build_history_text(user_id: int) -> str:
    records = await asyncio.to_thread(
        lambda: list(history_collection.find({"user_id": user_id}).sort("_id", -1).limit(8))
    )
    if not records:
        return "📜 **Historial**\n\nTodavía no procesaste ningún PDF."
    lines = ["📜 **Últimos PDFs**\n"]
    for item in records:
        date = item.get("created_at")
        if date:
            date = date.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
            date_text = date.strftime("%d/%m %H:%M")
        else:
            date_text = "sin fecha"
        name = md_safe(str(item.get("final_name", "archivo.pdf")))[:34]
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
        f"👥 Usuarios: **{users}**\n"
        f"📨 Pendientes: **{pending}**\n"
        f"✅ Permitidos: **{allowed}**\n"
        f"⛔ Rechazados / revocados: **{blocked}**\n"
        f"📄 Historial guardado: **{jobs}** (✅ {successes})\n"
        f"⚙️ En curso: **{running}**\n"
        f"💽 Espacio libre: **{free}**\n"
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


async def send_welcome(message: Message, user_id: int):
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
        text = "⏳ Tu solicitud de acceso está pendiente de revisión."
    elif status == "rejected":
        text = "⛔ Tu solicitud fue rechazada."
    elif status == "revoked":
        text = "⛔ Tu acceso fue revocado."
    else:
        text = "⛔ No tenés autorización. Escribí /start para solicitar acceso."
    await message.reply_text(text)
    return True


async def make_thumbnail_jpeg(source: str, destination: str):
    def convert():
        with Image.open(source) as image:
            with image.convert("RGB") as rgb:
                rgb.thumbnail((320, 320))
                for quality in (82, 65, 50, 35):
                    rgb.save(destination, "JPEG", quality=quality, optimize=True)
                    if os.path.getsize(destination) < 200 * 1024:
                        return
        raise ValueError("La miniatura supera 200 KB. Elegí otra imagen.")

    await asyncio.to_thread(convert)
    return destination


# ============================================================
# ENVÍO DE PDF CON MINIATURA Y LIMPIEZA INMEDIATA
# ============================================================
async def send_pdf_with_thumb(
    user_id: int,
    pdf_message: Message,
    new_name: str,
    thumbnail: str | None,
    keep_thumbnail_after: bool = False,
    on_complete_callback=None
):
    workdir = Path(tempfile.mkdtemp(prefix=f"user_{user_id}_", dir=TMP_ROOT))
    status = await pdf_message.reply_text("📥 Preparando descarga…", reply_markup=STOP_KEYBOARD)
    size = media_size(pdf_message)
    actual_name = clean_pdf_filename(new_name, media_name(pdf_message))

    try:
        if PROCESS_SEMAPHORE.locked():
            await safe_edit(
                status,
                "⏳ **En cola**\n\nEmpiezo en cuanto se libere un lugar.",
                reply_markup=STOP_KEYBOARD
            )

        async with PROCESS_SEMAPHORE:
            if size and size > MAX_FILE_SIZE:
                raise ValueError(f"El archivo supera el límite de {MAX_FILE_SIZE_MB} MB.")

            free = shutil.disk_usage(TMP_ROOT).free
            if size and free < size * 1.25:
                raise ValueError("No hay espacio temporal suficiente en el servidor.")

            target = workdir / actual_name
            started = time.monotonic()
            downloaded = await bot.download_media(
                pdf_message,
                file_name=str(target),
                progress=update_progress,
                progress_args=(status, user_id, "📥 **Descargando PDF**", actual_name, started)
            )
            if not downloaded or not os.path.isfile(downloaded):
                raise RuntimeError("Telegram no completó la descarga del PDF.")

            started = time.monotonic()
            await bot.send_document(
                chat_id=user_id,
                document=str(downloaded),
                file_name=actual_name,
                thumb=thumbnail if thumbnail and os.path.exists(thumbnail) else None,
                caption=f"📄 `{md_safe(actual_name)}`",
                force_document=True,
                progress=update_progress,
                progress_args=(status, user_id, "📤 **Subiendo PDF con miniatura**", actual_name, started)
            )
            await save_history(user_id, actual_name, size, "success")
            await safe_edit(
                status,
                "✅ **¡Listo!** Temporales borrados.",
                reply_markup=MULTI_ACTIVE_KEYBOARD if keep_thumbnail_after else HOME_KEYBOARD
            )
            if on_complete_callback:
                await on_complete_callback()
    except asyncio.CancelledError:
        await safe_edit(status, "🛑 **Operación detenida.**", reply_markup=HOME_KEYBOARD)
        await save_history(user_id, actual_name, size, "cancelled")
        raise
    except Exception as exc:
        logger.exception("Error procesando PDF para user_id=%s", user_id)
        await safe_edit(
            status,
            f"❌ **No se pudo procesar el PDF.**\n\n`{md_safe(str(exc))[:600]}`",
            reply_markup=HOME_KEYBOARD
        )
        await save_history(user_id, actual_name, size, f"error: {str(exc)[:300]}")
    finally:
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


def launch_single(user_id: int, pdf_message: Message, final_name: str, thumbnail: str | None):
    user_states[user_id] = {"stage": "processing", "mode": "single"}

    def finish_cb():
        purge_user_ephemeral(user_id, force_thumb=True)
        user_states[user_id] = {"stage": "idle"}

    start_task(
        user_id,
        send_pdf_with_thumb(user_id, pdf_message, final_name, thumbnail, keep_thumbnail_after=False),
        after_done=finish_cb
    )


def launch_multi_pdf(user_id: int, pdf_message: Message, final_name: str, thumbnail: str):
    processed_count = user_states.get(user_id, {}).get("processed_count", 0)
    user_states[user_id] = {
        "stage": "processing",
        "mode": "multi",
        "thumbnail": thumbnail,
        "keep_thumbnail_session": True,
        "processed_count": processed_count,
    }

    async def after_send_prompt():
        count = processed_count + 1
        user_states[user_id] = {
            "stage": "multi_wait_pdf",
            "mode": "multi",
            "thumbnail": thumbnail,
            "keep_thumbnail_session": True,
            "processed_count": count,
        }
        await bot.send_message(
            user_id,
            f"🖼️ **Miniatura activa ({count} PDF listo)**\n\n"
            "Mandame el **siguiente PDF** para usar esta misma imagen, o tocá **Terminar sesión** para limpiar todo.",
            reply_markup=MULTI_ACTIVE_KEYBOARD
        )

    def finish_cb():
        if user_states.get(user_id, {}).get("stage") == "processing":
            user_states[user_id] = {
                "stage": "multi_wait_pdf",
                "mode": "multi",
                "thumbnail": thumbnail,
                "keep_thumbnail_session": True,
                "processed_count": processed_count + 1,
            }
        gc.collect()

    start_task(
        user_id,
        send_pdf_with_thumb(
            user_id,
            pdf_message,
            final_name,
            thumbnail=thumbnail,
            keep_thumbnail_after=True,
            on_complete_callback=after_send_prompt
        ),
        after_done=finish_cb
    )


# ============================================================
# VIGILANCIA Y REINICIO
# ============================================================
def restart_process(reason: str):
    logger.warning("Reiniciando el bot. Motivo: %s", reason)
    os._exit(1)


async def watchdog():
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
    reset_state(uid, force_thumb=True)
    await message.reply_text("🛑 **Operación detenida y temporales borrados.**", reply_markup=HOME_KEYBOARD)


@bot.on_message(filters.command("history"))
async def history_command(client: Client, message: Message):
    if not message.from_user:
        return
    if await deny_unapproved(message, message.from_user.id):
        return
    await message.reply_text(await build_history_text(message.from_user.id), reply_markup=NAV_KEYBOARD)


@bot.on_message(filters.command("admin"))
async def admin_command(client: Client, message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        if message.from_user:
            await message.reply_text("⛔ No tenés permiso para acceder al panel.")
        return
    await message.reply_text(await build_admin_text(), reply_markup=ADMIN_KEYBOARD)


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
                reset_state(target_id, force_thumb=True)
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

    elif data == "history":
        await query.answer()
        await safe_edit(message, await build_history_text(uid), reply_markup=NAV_KEYBOARD)

    elif data == "stop":
        task = active_tasks.get(uid)
        if task and not task.done():
            task.cancel()
            reset_state(uid, force_thumb=True)
            await query.answer("🛑 Deteniendo…")
        else:
            await query.answer("No hay operación en curso.", show_alert=True)

    elif data == "reset":
        reset_state(uid, force_thumb=True)
        await query.answer("Sesión cerrada y temporales borrados.")
        await safe_edit(
            message,
            "🛑 **Sesión cerrada y temporales borrados.**",
            reply_markup=HOME_KEYBOARD
        )

    elif data == "mode_single":
        reset_state(uid, force_thumb=True)
        user_states[uid] = {"stage": "single_wait_thumb", "mode": "single", "thumbnail": None}
        await query.answer()
        await safe_edit(
            message,
            "🖼️ **Miniatura + PDF**\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ Mandame la **imagen** para la miniatura\n"
            "2️⃣ Mandame el **PDF**\n"
            "3️⃣ Elegí el **nombre final** o dejá el que tiene",
            reply_markup=NAV_KEYBOARD
        )

    elif data == "mode_multi":
        reset_state(uid, force_thumb=True)
        user_states[uid] = {
            "stage": "multi_wait_thumb",
            "mode": "multi",
            "thumbnail": None,
            "keep_thumbnail_session": True,
            "processed_count": 0,
        }
        await query.answer()
        await safe_edit(
            message,
            "📚 **Una imagen + Varios PDFs**\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ Mandame **una imagen** (se usa para todos los PDFs)\n"
            "2️⃣ Mandame un **PDF**\n"
            "3️⃣ Elegí **nombre nuevo** o **dejar nombre actual**\n"
            "4️⃣ Seguí mandando PDFs o tocá **Terminar sesión**",
            reply_markup=NAV_KEYBOARD
        )

    elif data == "keepname":
        state = user_states.get(uid, {})
        pdf_msg = state.get("pdf_message")
        thumb = state.get("thumbnail")
        if state.get("stage") != "single_wait_name" or not pdf_msg:
            await query.answer("Opción no disponible.", show_alert=True)
            return
        orig_name = clean_pdf_filename(media_name(pdf_msg))
        state["thumbnail"] = None
        await query.answer("Dejando nombre actual…")
        await safe_edit(message, f"📄 Nombre: `{md_safe(orig_name)}`")
        launch_single(uid, pdf_msg, orig_name, thumb)

    elif data == "mpdf_keep":
        state = user_states.get(uid, {})
        pdf_msg = state.get("pending_pdf")
        thumb = state.get("thumbnail")
        if state.get("stage") != "multi_choose_name" or not pdf_msg or not thumb:
            await query.answer("Opción no disponible.", show_alert=True)
            return
        orig_name = clean_pdf_filename(media_name(pdf_msg))
        state.pop("pending_pdf", None)
        await query.answer("Dejando nombre actual…")
        await safe_edit(message, f"📄 Nombre: `{md_safe(orig_name)}`")
        launch_multi_pdf(uid, pdf_msg, orig_name, thumb)

    elif data == "mpdf_rename":
        state = user_states.get(uid, {})
        pdf_msg = state.get("pending_pdf")
        if state.get("stage") != "multi_choose_name" or not pdf_msg:
            await query.answer("Opción no disponible.", show_alert=True)
            return
        state["stage"] = "multi_wait_name"
        await query.answer()
        await safe_edit(
            message,
            f"✏️ Escribí el **nombre** para `{md_safe(media_name(pdf_msg))}`:",
            reply_markup=MULTI_ACTIVE_KEYBOARD
        )

    elif data == "admin":
        if not is_admin(uid):
            await query.answer("Acceso denegado.", show_alert=True)
            return
        await query.answer()
        await safe_edit(message, await build_admin_text(), reply_markup=ADMIN_KEYBOARD)

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
            reply_markup=ADMIN_KEYBOARD
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

    if stage == "single_wait_name":
        pdf_msg = state.get("pdf_message")
        thumb = state.get("thumbnail")
        if not pdf_msg:
            reset_state(uid, force_thumb=True)
            await message.reply_text("📂 Elegí una opción:", reply_markup=main_keyboard(uid))
            return
        final_name = clean_pdf_filename(message.text, media_name(pdf_msg))
        state["thumbnail"] = None
        launch_single(uid, pdf_msg, final_name, thumb)

    elif stage == "multi_wait_name":
        pdf_msg = state.get("pending_pdf")
        thumb = state.get("thumbnail")
        if not pdf_msg or not thumb:
            reset_state(uid, force_thumb=True)
            await message.reply_text("📂 La sesión terminó. Elegí una opción:", reply_markup=main_keyboard(uid))
            return
        final_name = clean_pdf_filename(message.text, media_name(pdf_msg))
        state.pop("pending_pdf", None)
        launch_multi_pdf(uid, pdf_msg, final_name, thumb)

    elif stage == "multi_choose_name":
        await message.reply_text(
            "Elegí **Elegir nombre** o **Dejar nombre actual**.",
            reply_markup=MULTI_NAME_KEYBOARD
        )

    elif stage == "multi_wait_pdf":
        await message.reply_text(
            "📄 Mandame el **PDF** para usar la misma imagen, o tocá **Terminar sesión**.",
            reply_markup=MULTI_ACTIVE_KEYBOARD
        )

    elif stage == "processing":
        await message.reply_text("⏳ Estoy procesando el PDF. Podés detenerlo con 🛑 o /cancel.")

    else:
        await message.reply_text("📂 Elegí una opción:", reply_markup=main_keyboard(uid))


@bot.on_message(filters.document | filters.photo, group=1)
async def media_handler(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if await deny_unapproved(message, uid):
        return
    state = user_states.get(uid, {})
    stage = state.get("stage")

    if stage in ("single_wait_thumb", "multi_wait_thumb"):
        is_image = bool(message.photo) or (
            bool(message.document) and (message.document.mime_type or "").startswith("image/")
        )
        if not is_image:
            await message.reply_text("🖼️ Mandame una **imagen** para la miniatura.", reply_markup=CANCEL_KEYBOARD)
            return
        folder = Path(tempfile.mkdtemp(prefix="thumb_", dir=TMP_ROOT))
        try:
            raw = await bot.download_media(message, file_name=str(folder / "source"))
            thumb_path = str(folder / "thumb.jpg")
            await make_thumbnail_jpeg(raw, thumb_path)
            remove_path_tree(raw)
            state["thumbnail"] = thumb_path
            if stage == "single_wait_thumb":
                state["stage"] = "single_wait_pdf"
                await message.reply_text("✅ Miniatura lista. Ahora mandame el **PDF**.", reply_markup=CANCEL_KEYBOARD)
            else:
                state["stage"] = "multi_wait_pdf"
                state["keep_thumbnail_session"] = True
                await message.reply_text(
                    "✅ Miniatura lista para varios PDFs. Mandame el primer **PDF**.",
                    reply_markup=MULTI_ACTIVE_KEYBOARD
                )
        except Exception as exc:
            shutil.rmtree(folder, ignore_errors=True)
            await message.reply_text(f"❌ No pude preparar esa imagen: `{md_safe(str(exc))[:300]}`")
        return

    if not is_pdf_message(message):
        await message.reply_text("📄 Necesito un archivo **PDF**.")
        return

    size = media_size(message)
    if size and size > MAX_FILE_SIZE:
        await message.reply_text(f"❌ El archivo supera el límite de {MAX_FILE_SIZE_MB} MB.")
        return

    if stage == "single_wait_pdf":
        state["pdf_message"] = message
        state["stage"] = "single_wait_name"
        await message.reply_text(
            f"📄 Recibido: `{md_safe(media_name(message))}`\n\n"
            "Escribí el **nombre** que querés o dejá el nombre actual:",
            reply_markup=KEEPNAME_KEYBOARD
        )
    elif stage == "multi_wait_pdf":
        state["pending_pdf"] = message
        state["stage"] = "multi_choose_name"
        await message.reply_text(
            f"📄 PDF recibido: `{md_safe(media_name(message))}`\n\n"
            "¿Querés elegir un nombre o dejar el nombre actual?",
            reply_markup=MULTI_NAME_KEYBOARD
        )
    elif stage == "processing":
        await message.reply_text("⏳ Estoy procesando un PDF. Esperá o detenelo con 🛑 o /cancel.")
    else:
        await message.reply_text("📂 Elegí una opción:", reply_markup=main_keyboard(uid))


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
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner


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

    runner = await start_http_server()
    await bot.start()
    watchdog_task = asyncio.create_task(watchdog())
    cleaner_task = asyncio.create_task(auto_cleaner_loop())
    try:
        await idle()
    finally:
        cleaner_task.cancel()
        watchdog_task.cancel()
        await bot.stop()
        await runner.cleanup()
        mongo_client.close()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        gc.collect()


WELCOME_IMAGE_B64 = ""
WELCOME_IMAGE = prepare_welcome_image()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
