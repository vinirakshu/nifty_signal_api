"""
One-time setup: asks for your Fyers App ID and Secret ID and writes the .env file.

    python setup_env.py

Find both on https://myapi.fyers.in/dashboard -> your app -> "App credentials & info"
(click the eye icon to reveal, or the copy icons).
"""
import os
import re
import shutil

BASE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(BASE, ".env")
EXAMPLE = os.path.join(BASE, ".env.example")


def set_key(text, key, value):
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        return re.sub(rf"^{key}=.*$", line, text, flags=re.M)
    return text.rstrip("\n") + "\n" + line + "\n"


def main():
    print("Fyers API setup\n" + "-" * 40)
    print("Open https://myapi.fyers.in/dashboard -> your app -> App credentials & info.\n")
    app_id = input("App ID (looks like XB12345-100): ").strip()
    while not re.fullmatch(r"[A-Za-z0-9]+-\d+", app_id):
        app_id = input("That doesn't look like an App ID (e.g. XB12345-100). Try again: ").strip()
    secret = input("Secret ID: ").strip()
    while len(secret) < 6:
        secret = input("Secret ID looks too short. Try again: ").strip()
    redirect = input("Redirect URL [http://127.0.0.1:5110/auth/callback]: ").strip() \
        or "http://127.0.0.1:5110/auth/callback"

    if os.path.exists(ENV):
        text = open(ENV, encoding="utf-8").read()
        shutil.copy(ENV, ENV + ".bak")
        print(f"(Existing .env backed up to {ENV}.bak)")
    elif os.path.exists(EXAMPLE):
        text = open(EXAMPLE, encoding="utf-8").read()
    else:
        text = ""
    for k, v in (("FYERS_CLIENT_ID", app_id), ("FYERS_SECRET_KEY", secret), ("FYERS_REDIRECT_URI", redirect)):
        text = set_key(text, k, v)
    with open(ENV, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"\nSaved {ENV}")
    print("The Redirect URL must match the one in your Fyers app exactly (including /auth/callback).")
    print("Now restart the server (python app.py) and click 'Log in to Fyers' on http://127.0.0.1:5110")


if __name__ == "__main__":
    main()
