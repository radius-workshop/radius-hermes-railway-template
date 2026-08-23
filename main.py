from telethon import TelegramClient
import os
import asyncio

api_id = int(os.environ.get("API_ID", 0))
api_hash = os.environ.get("API_HASH", "")

async def main():
    if not api_id or not api_hash:
        print("Error: API_ID and API_HASH not set.")
        return

    client = TelegramClient('/data/userbot', api_id, api_hash)
    print("Userbot initializing...")
    await client.start()
    print("Userbot is running!")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
