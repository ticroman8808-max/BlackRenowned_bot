import os


def _get_env(name: str, fallback: str | None = None) -> str:
    val = os.getenv(name, "").strip()
    if not val and fallback:
        val = os.getenv(fallback, "").strip()
    return val


def _get_int(name: str, default: int = 0) -> int:
    raw = _get_env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Telegram
BOT_TOKEN = _get_env("BOT_TOKEN")
API_ID = _get_int("API_ID", 0)
API_HASH = _get_env("API_HASH")

# MongoDB (soporta MONGO_URI o DATABASE_URL en Northflank)
MONGO_URI = _get_env("MONGO_URI", fallback="DATABASE_URL")
DATABASE_URL = MONGO_URI
DATABASE_NAME = _get_env("DATABASE_NAME") or "BlackRenowned"

# Servidor / Temporales
PORT = _get_int("PORT", 10000)
TMP_DIR = _get_env("TMP_DIR") or "/tmp/blackrenowned"
