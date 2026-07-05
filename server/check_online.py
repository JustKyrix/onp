import bot
osu_id = bot.get_osu_id("kyarixu")
print("osu_id:", osu_id)
online, play = bot.osu_last_play(osu_id)
print("is_online:", online)
print("play:", play['title'] if play else None)