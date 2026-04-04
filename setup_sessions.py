#!/usr/bin/env python3
"""
Script to create Telegram sessions (bot.session and user.session) for UserLixo.
Run this script once to authenticate the bot and userbot.

Compatible with both Docker (SESSIONS_DIR env var) and local execution.
"""
import os
import asyncio
import sys
from pathlib import Path
from dotenv import load_dotenv

# ================================
# ANSI color styles (no emojis)
# ================================
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    RESET = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

def clear_screen():
    """Clear terminal screen (cross-platform)"""
    os.system('cls' if os.name == 'nt' else 'clear')

def print_header(text):
    print(f"\n{Colors.CYAN}{'='*60}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{text.center(60)}{Colors.RESET}")
    print(f"{Colors.CYAN}{'='*60}{Colors.RESET}")

def print_success(text):
    print(f"{Colors.GREEN}[+] {text}{Colors.RESET}")

def print_error(text):
    print(f"{Colors.RED}[X] {text}{Colors.RESET}")

def print_warning(text):
    print(f"{Colors.YELLOW}[!] {text}{Colors.RESET}")

def print_info(text):
    print(f"{Colors.CYAN}[i] {text}{Colors.RESET}")

# ================================
# Get script directory (where this file lives)
# ================================
SCRIPT_DIR = Path(__file__).parent.resolve()
ENV_PATH = SCRIPT_DIR / ".env"

# ================================
# Sessions directory (same logic as config.py)
# ================================
BASE_DIR = SCRIPT_DIR
SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", BASE_DIR / "data" / "sessions"))
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# ================================
# .env file management
# ================================
def save_env_file(api_id, api_hash):
    """Save API credentials to .env file in the script's directory."""
    try:
        with open(ENV_PATH, "w") as f:
            f.write("# Telegram API credentials\n")
            f.write("# Get them at https://my.telegram.org/apps\n")
            f.write(f"API_ID={api_id}\n")
            f.write(f"API_HASH={api_hash}\n")
        print_success(f".env file saved to {ENV_PATH}")
        return True
    except PermissionError:
        print_error(f"Permission denied: cannot write to {ENV_PATH}")
        return False
    except Exception as e:
        print_error(f"Failed to save .env: {e}")
        return False

def load_or_create_env():
    """Load existing .env or create a new one with API credentials."""
    # First, load any existing .env
    load_dotenv(ENV_PATH)
    existing_api_id = os.environ.get("API_ID")
    existing_api_hash = os.environ.get("API_HASH")

    # If .env exists and seems valid, ask if user wants to keep it
    if existing_api_id and existing_api_hash:
        try:
            int(existing_api_id)
            print_warning(f".env file found at {ENV_PATH} with API_ID={existing_api_id}")
            resp = input(f"{Colors.YELLOW}Do you want to use these credentials? (Y/n): {Colors.RESET}").strip().lower()
            if resp == 'n':
                existing_api_id = None
                existing_api_hash = None
            else:
                print_success("API credentials loaded from .env")
                return int(existing_api_id), existing_api_hash
        except (ValueError, TypeError):
            print_error("Invalid API_ID in .env file")
            existing_api_id = None
            existing_api_hash = None

    # If no valid credentials, ask interactively with confirmation loop
    print_info("Let's set up your Telegram API credentials.")
    print("You can obtain them at https://my.telegram.org/apps\n")

    while True:
        # Ask for API_ID
        print(f"{Colors.BOLD}API_ID{Colors.RESET}: ", end="")
        api_id_input = input().strip()
        if not api_id_input:
            print_error("API_ID cannot be empty.")
            continue
        try:
            api_id_int = int(api_id_input)
        except ValueError:
            print_error("API_ID must be an integer.")
            continue

        # Ask for API_HASH
        print(f"{Colors.BOLD}API_HASH{Colors.RESET}: ", end="")
        api_hash_input = input().strip()
        if not api_hash_input:
            print_error("API_HASH cannot be empty.")
            continue

        # Show confirmation
        print("\nPlease confirm the entered values.")
        confirm = input(f"{Colors.YELLOW}Are these correct? (Y/n): {Colors.RESET}").strip().lower()
        if confirm == 'n':
            clear_screen()
            print("Let's try again.\n")
            continue
        else:
            break

    # Save to .env
    if save_env_file(api_id_int, api_hash_input):
        return api_id_int, api_hash_input
    else:
        print_error("Could not save .env. Exiting.")
        sys.exit(1)

# ================================
# Bot setup (token)
# ================================
async def setup_bot(api_id, api_hash):
    """Create bot session by asking for BotFather token."""
    from hydrogram import Client, errors

    clear_screen()
    print_header("BOT SETUP")
    print("You will need the token provided by @BotFather.")
    print("If you don't have a bot, create one at https://t.me/BotFather\n")

    session_path = SESSIONS_DIR / "bot"
    if session_path.with_suffix(".session").exists():
        print_warning("A bot session already exists.")
        resp = input(f"{Colors.YELLOW}Do you want to recreate it? (y/N): {Colors.RESET}").strip().lower()
        if resp != 'y':
            print_info("Keeping existing session. Proceeding to next step.")
            return True

    while True:
        bot_token = input(f"{Colors.BOLD}Bot Token{Colors.RESET}: ").strip()
        if not bot_token:
            print_error("Bot token cannot be empty.")
            continue

        print()  # blank line for separation

        bot = Client(str(session_path), api_id=api_id, api_hash=api_hash, bot_token=bot_token)
        try:
            await bot.start()
            me = await bot.get_me()
            print_success(f"Bot configured successfully!")
            print(f"   Name: {me.first_name}")
            print(f"   Username: @{me.username}")
            await bot.stop()
            return True
        except errors.AccessTokenInvalid:
            print_error("Invalid token. Make sure you copied it correctly.")
            continue
        except Exception as e:
            print_error(f"Unexpected error: {e}")
            continue

# ================================
# Userbot setup (phone number)
# ================================
async def setup_user(api_id, api_hash):
    """Create userbot session by asking for phone number and verification code."""
    from hydrogram import Client, errors

    clear_screen()
    print_header("USERBOT SETUP")
    print("You will need your phone number and the verification code.")
    print("Hydrogram will ask for them interactively.\n")

    session_path = SESSIONS_DIR / "user"
    if session_path.with_suffix(".session").exists():
        print_warning("A userbot session already exists.")
        resp = input(f"{Colors.YELLOW}Do you want to recreate it? (y/N): {Colors.RESET}").strip().lower()
        if resp != 'y':
            print_info("Keeping existing session.")
            return True

    user = Client(str(session_path), api_id=api_id, api_hash=api_hash)
    try:
        await user.start()
        me = await user.get_me()
        print_success(f"Userbot configured successfully!")
        print(f"   Name: {me.first_name} {me.last_name or ''}")
        print(f"   ID: {me.id}")
        await user.stop()
        return True
    except errors.ApiIdInvalid:
        print_error("Invalid API_ID or API_HASH. Check your .env file.")
        return False
    except Exception as e:
        print_error(f"Error during authentication: {e}")
        return False

# ================================
# Main function
# ================================
async def main():
    clear_screen()
    print_header("UserLixo - SESSIONS SETUP")

    # 1. Obtain or create API credentials
    api_id, api_hash = load_or_create_env()

    # 2. Setup bot
    print_info("Starting BOT configuration...")
    bot_ok = await setup_bot(api_id, api_hash)
    if not bot_ok:
        print_error("Bot configuration failed. Aborting.")
        return

    # 3. Setup userbot
    print_info("Starting USERBOT configuration...")
    user_ok = await setup_user(api_id, api_hash)
    if not user_ok:
        print_error("Userbot configuration failed.")
        return

    # 4. Final message
    clear_screen()
    print_header("SETUP COMPLETE")
    print_success("Sessions have been successfully created!")
    print(f"\n{Colors.CYAN}Now you can start the main bot with:{Colors.RESET}")
    print(f"  {Colors.BOLD}docker compose up -d userlixo{Colors.RESET}")
    print(f"\n{Colors.YELLOW}Note: The setup service is finished and can be removed from docker-compose.yml if desired.{Colors.RESET}\n")

if __name__ == "__main__":
    asyncio.run(main())