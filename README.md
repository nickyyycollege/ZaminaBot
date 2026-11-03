# Telegram bot for schedule changes

A small Python service that checks the college schedule page every day and filters the current active entries to group `21-КІ` (configured as `21-KI`) and posts matching changes to one Telegram group at 18:00 Europe/Kyiv. It reads the HTML iframe/table, not pixels, carries group names across continuation rows, ignores visibly struck-through/cancelled rows, and refuses to post if it cannot parse any entries. A local SQLite record prevents duplicate posts on restarts during the same day.

## Before running

1. Create a bot with Telegram's official BotFather, keep its token private, and add the bot to the target group with permission to send messages.
2. Find the group's numeric chat ID. A common approach: send a test message in the group, then inspect the bot's `getUpdates` response while it is configured to receive updates. Do not paste the bot token into public chat or commit it to Git.
3. Copy `.env.example` to `.env` on a VPS or other always-on host. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` there.
4. Run with Docker Compose: `docker compose up -d --build`.
5. View logs: `docker compose logs -f`.

The host must stay online. The scheduled post is at 18:00 in `Europe/Kyiv` time. Set `RUN_NOW=1` temporarily for a one-time test, then restore it to `0`.

## Local run

Python 3.11+ required. `pip install -r requirements.txt`, configure environment variables, then `python bot.py`.

## Important

The site may change its embedded page or table structure. Check the logs after first deployment and verify the first posted message before relying on it. The bot only posts changes matching `TARGET_GROUP` (default `21-KI`). The site uses Cyrillic `КІ`, while the config can use Latin `KI`; both are normalized for matching. The bot reports changes for **tomorrow** (Europe/Kyiv date). The page lists today and tomorrow, so the bot reads the date heading above each table (`04.11.2026`, `4 листопада 2026`, or a one-cell date row) and only uses rows under tomorrow's date. If tomorrow has no changes for the group, it posts "Для 21-КІ замін на завтра немає." If tomorrow's date is not on the page yet, or no date can be found, the message carries a ⚠️ warning. `RUN_NOW=1` sends a test post and does not block the real 18:00 post.
