# main.py
import os
import asyncio
import logging
import tempfile
import shutil
from datetime import datetime

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.functions.channels import JoinChannelRequest
from aiohttp import web

load_dotenv()

# --- Configuration (from .env) ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "")  # e.g. @mtproto_channel
BOT_CHANNEL = os.getenv("BOT_CHANNEL", "")        # e.g. @my_own_channel
BOT_PREFIX = os.getenv("BOT_PREFIX", "Shared proxy:")
USER_SESSION = os.getenv("USER_SESSION", "user_session")
BOT_SESSION = os.getenv("BOT_SESSION", "bot_session")
PHONE = os.getenv("PHONE", None)  # optional: your phone for first sign-in

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def safe_text_of(msg):
    # Telethon Message: prefer .text if available, else .message
    return getattr(msg, "text", None) or getattr(msg, "message", "") or ""


async def create_web_server():
    """Create a simple web server for health checks"""
    app = web.Application()
    
    async def health_check(request):
        return web.Response(text="OK", status=200)
    
    async def status_check(request):
        status = {
            "status": "running",
            "timestamp": datetime.now().isoformat(),
            "service": "MTProto Proxy Forwarder"
        }
        return web.json_response(status)
    
    app.router.add_get('/health', health_check)
    app.router.add_get('/status', status_check)
    app.router.add_get('/', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("Web server started on port 8080")
    return runner


async def heartbeat_ping():
    """Send periodic pings to keep the service alive"""
    while True:
        try:
            # Ping multiple services to ensure reliability
            ping_urls = [
                "https://httpbin.org/get",
                "https://api.github.com",
                "https://www.google.com"
            ]
            
            import aiohttp
            async with aiohttp.ClientSession() as session:
                for url in ping_urls:
                    try:
                        async with session.get(url, timeout=10) as response:
                            if response.status == 200:
                                logger.info(f"Heartbeat ping successful to {url}")
                                break
                    except Exception as e:
                        logger.debug(f"Ping failed to {url}: {e}")
                        continue
            
            # Wait 2 minutes before next ping
            await asyncio.sleep(120)
            
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
            await asyncio.sleep(60)  # Wait 1 minute on error


async def main():
    if not API_ID or not API_HASH or not BOT_TOKEN:
        logger.error("Please fill API_ID, API_HASH and BOT_TOKEN in .env")
        return

    # Create both clients
    user_client = TelegramClient(USER_SESSION, API_ID, API_HASH)
    bot_client = TelegramClient(BOT_SESSION, API_ID, API_HASH)

    # --- Start / authorize user client (server-friendly) ---
    await user_client.connect()
    if not await user_client.is_user_authorized():
        if not PHONE:
            logger.error("PHONE environment variable is required for server deployment")
            return
        
        try:
            await user_client.send_code_request(PHONE)
            logger.info("Verification code sent to your phone")
            logger.info("For first-time setup, you may need to manually handle the verification")
            # In production, you might want to implement a more robust verification system
            return
        except Exception as e:
            logger.error(f"Failed to send verification code: {e}")
            return

    # --- Start bot client with bot token (no phone needed) ---
    await bot_client.start(bot_token=BOT_TOKEN)

    # fetch "me" for both clients
    me_user = await user_client.get_me()
    me_bot = await bot_client.get_me()
    logger.info("User account: %s (id=%s)", getattr(me_user, "username", me_user.id), me_user.id)
    logger.info("Bot account:  %s (id=%s)", getattr(me_bot, "username", me_bot.id), me_bot.id)

    # if the user hasn't joined the source channel, we can try to join it
    if SOURCE_CHANNEL:
        try:
            await user_client(JoinChannelRequest(SOURCE_CHANNEL))
            logger.info("Attempted to join %s", SOURCE_CHANNEL)
        except Exception as e:
            # may already be a member or the channel is private & requires invite
            logger.debug("JoinChannelRequest: %s", e)

    # Get numeric identifiers we will use
    bot_entity = me_bot.id
    user_id = me_user.id

    # ============= USER CLIENT: listen to source channel and forward to bot =============
    @user_client.on(events.NewMessage(chats=SOURCE_CHANNEL))
    async def user_handler(event):
        try:
            # Create multi-line message with clickable text
            # Replace \n in BOT_PREFIX with actual newlines and add channel info
            formatted_prefix = BOT_PREFIX.replace('\\n', '\n')
            
            # Extract proxy details from the original message text
            original_text = event.message.text or ""
            logger.info(f"Original message text: {original_text[:200]}...")
            
            server = None
            port = None
            secret = None
            proxy_url = None
            
            # Parse the original message to extract proxy details
            lines = original_text.split('\n')
            for line in lines:
                line = line.strip()
                # Extract server
                if line.startswith('Server:'):
                    server = line.replace('Server:', '').strip()
                elif 'Server:' in line:
                    server = line.split('Server:')[1].strip()
                # Extract port
                elif line.startswith('Port:'):
                    port = line.replace('Port:', '').strip()
                elif 'Port:' in line:
                    port = line.split('Port:')[1].strip()
                # Extract secret
                elif line.startswith('Secret:'):
                    secret = line.replace('Secret:', '').strip()
                elif 'Secret:' in line:
                    secret = line.split('Secret:')[1].strip()
            
            # Extract the proxy URL from the original buttons
            if event.message.buttons:
                for row in event.message.buttons:
                    for button in row:
                        if hasattr(button, 'url') and button.url:
                            # Look for proxy URLs in buttons
                            if 'tg://proxy' in button.url:
                                proxy_url = button.url
                                logger.info(f"Found proxy URL in button: {proxy_url}")
                                break
                            # Also check for HTTP URLs that might redirect to proxy
                            elif 'http' in button.url and ('proxy' in button.url.lower() or 'connect' in button.url.lower()):
                                proxy_url = button.url
                                logger.info(f"Found HTTP proxy URL in button: {proxy_url}")
                                break
                    if proxy_url:
                        break
            
            logger.info(f"Extracted proxy details: server={server}, port={port}, secret={'***' if secret else 'None'}")
            
            # Create message with proxy details and clickable text
            proxy_details = ""
            if server or port or secret:
                proxy_details = "\n\n"
                if server:
                    proxy_details += f"Server: {server}\n"
                if port:
                    proxy_details += f"Port: {port}\n"
                proxy_details = proxy_details.rstrip()  # Remove trailing newline
            
            if proxy_url:
                message_text = f"""{formatted_prefix}

[Your Freedom is here | Connect]({proxy_url})

[اتصال به پروکسی پر سرعت]({proxy_url}){proxy_details}

@{BOT_CHANNEL.replace('@', '')}"""
                logger.info("Created clickable text with proxy URL and details")
            else:
                # Fallback to plain text if no proxy URL found
                message_text = f"""{formatted_prefix}

Your Freedom is Here

اتصال به پروکسی پر سرعت{proxy_details}

@{BOT_CHANNEL.replace('@', '')}"""
                logger.info("No proxy URL found, using plain text with details")
            
            # Only send the formatted prefix with original buttons, no original text
            await bot_client.send_message(
                BOT_CHANNEL,
                message_text,
                parse_mode='Markdown' if proxy_url else None,  # Enable Markdown only if we have clickable text
                buttons=event.message.buttons,   # keep original inline buttons (these work!)
                link_preview=event.message.web_preview  # keep link previews if any
        )
            logging.info("Message forwarded with multi-line prefix, channel ID and buttons only.")
        except Exception as e:
            logging.error(f"Error while forwarding: {e}")

    # ============= BOT CLIENT: listen for messages FROM your user account =============
    @bot_client.on(events.NewMessage(incoming=True))
    async def bot_handler(event):
        try:
            # process only messages that came from your personal account (the forward)
            if event.sender_id != user_id:
                # ignore other users
                logger.debug("Bot received message from %s; ignoring.", event.sender_id)
                return

            msg = event.message
            logger.info("Bot received forwarded message. Processing...")

            # If the forwarded message contains media, download it and re-upload with the caption
            if msg.media:
                tmpdir = tempfile.mkdtemp(prefix="tgfw_")
                try:
                    # download the media to tmpdir
                    file_path = await bot_client.download_media(msg, file=tmpdir)
                    logger.info("Downloaded media to %s", file_path)
                    # upload the file and send to target channel with multi-line caption and buttons
                    await bot_client.send_file(BOT_CHANNEL, file_path, caption=msg.text, buttons=msg.buttons, parse_mode='Markdown')
                    logger.info("Posted media message with multi-line caption to %s", BOT_CHANNEL)
                finally:
                    # cleanup temp files
                    try:
                        shutil.rmtree(tmpdir)
                    except Exception:
                        pass
            else:
                # text-only: forward the multi-line message as-is (already processed by user handler)
                await bot_client.send_message(BOT_CHANNEL, msg.text, buttons=msg.buttons, parse_mode='Markdown')
                logger.info("Posted multi-line text message to %s", BOT_CHANNEL)

        except FloodWaitError as f:
            logger.warning("Bot hit flood-wait: sleeping %s sec", f.seconds)
            await asyncio.sleep(f.seconds + 1)
        except Exception as e:
            logger.exception("Error in bot handler: %s", e)

    # Start web server for health checks
    web_runner = await create_web_server()
    
    # Start heartbeat in background
    heartbeat_task = asyncio.create_task(heartbeat_ping())

    try:
        # Run both clients concurrently with heartbeat
        logger.info("Started handlers with heartbeat. Running until disconnected...")
        await asyncio.gather(
            user_client.run_until_disconnected(),
            bot_client.run_until_disconnected(),
            heartbeat_task
        )
    except Exception as e:
        logger.error(f"Main error: {e}")
    finally:
        heartbeat_task.cancel()
        await web_runner.cleanup()
        logger.info("Cleaned up resources")


if __name__ == "__main__":
    asyncio.run(main())
