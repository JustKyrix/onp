import sqlite3
import bot

t = bot.refresh_tokens(bot.load_tokens())
bid, _ = bot.fetch_self(t['access'])
modded = set(bot.fetch_modded_channels(t['access'], bid))
channels = sorted(modded - bot.get_removed())
print("DB file  :", bot.DB_PATH)
print("will mark:", channels)

for ch in channels:
    bot.mark_joined(ch)

rows = sqlite3.connect(bot.DB_PATH).execute(
    "SELECT channel, bot_joined, removed FROM channels").fetchall()
print("after    :", rows)