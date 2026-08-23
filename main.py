from telethon import TelegramClient
import os
import asyncio

# Fetch from Env Vars set in Railway
api_id = os.environ.get("API_ID")
api_hash = os.environ.get("API_HASH")

async def main():
    if not api_id or not api_hash:
        print("CRITICAL: API_ID or API_HASH not found in environment!")
        return

    # Use /data/userbot.session for persistence in the volume
    client = TelegramClient('/data/userbot.session', int(api_id), api_hash)
    
    print("Userbot starting...")
    await client.start()
    print("Userbot is running!")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
