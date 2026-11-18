
import os

# Telegram
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

# MongoDB
DATABASE_URL = os.environ["DATABASE_URL"]
DATABASE_NAME = os.environ.get(
    "DATABASE_NAME",
    "BlackRenowned"
)
