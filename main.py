
import os
import requests


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    channel = os.getenv("TELEGRAM_CHANNEL_ID")

    if not token or not channel:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHANNEL_ID before running."
        )

    message = (
        "TREQUARTISTA — BETWEEN THE LINES.\n\n"
        "Our football intelligence project is "
        "being built. ⚽\n\n"
        "More football insights coming soon."
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": channel,
            "text": message,
        },
        timeout=20,
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message.")

    print("Test message published successfully.")


if __name__ == "__main__":
    main()
