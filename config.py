import os

# Tu Token de BotFather
BOT_TOKEN = os.environ.get("BOT_TOKEN", "9121190040:AAEXzvcYC_0eY7Mec0HhNfVxBKu07xJXfes")

# Tu ID de API de Telegram (número - sin comillas porque es entero)
API_ID = int(os.environ.get("API_ID", 32647589))

# Tu Hash de API de Telegram
API_HASH = os.environ.get("API_HASH", "ad2d19acbf8fdbfa2a093241662ba692")

# Tu URL completa de MongoDB (con comillas obligatorias)
DATABASE_URL = os.environ.get("DATABASE_URL", "mongodb+srv://ticroman8808_db_user:IanMa088@cluster0.i3tnanf.mongodb.net/")

# Nombre de tu base de datos en MongoDB
DATABASE_NAME = os.environ.get("DATABASE_NAME", "BlackRenowned")
