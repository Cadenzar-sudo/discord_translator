"""One-time interactive setup: bot token, access token, public host.

Writes config.json (preserving anything already there) and prints the
one-line setup code to give to friends plus the bot-invite steps.
"""

import json
import secrets
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from config import DEFAULTS, load_config  # noqa: E402


def mask(s: str) -> str:
    return f"{s[:6]}…{s[-4:]}" if len(s) > 12 else "(set)"


def main():
    cfg = load_config()
    path = APP_DIR / "config.json"

    print("=" * 60)
    print("  Server setup — answers are saved to config.json")
    print("=" * 60)

    # 0. audio source
    src = cfg.get("source", "loopback")
    print(f"0. Audio source — currently: {src}")
    print("   [loopback] a normal Discord client on THIS PC outputs to")
    print("     VB-Cable; works with Discord's E2EE; no per-speaker labels.")
    print("   [bot] a py-cord bot joins the channel for per-user audio;")
    print("     currently broken (py-cord DAVE receive, issue #3139).")
    pick = input("   Which source? [loopback/bot, ENTER keeps current]: "
                 ).strip().lower()
    if pick in ("loopback", "bot"):
        cfg["source"] = pick
    src = cfg.get("source", "loopback")

    if src == "loopback":
        dev = (cfg.get("audio_device") or "").strip()
        new_dev = input(f"1. Recording device substring for the loopback "
                        f"[{dev or 'CABLE Output'}]: ").strip()
        cfg["audio_device"] = new_dev or dev or "CABLE Output"
        qnum = "2"
    else:
        # bot token
        cur = cfg["discord"].get("token") or ""
        if cur:
            if input(f"1. Bot token already set ({mask(cur)}). Keep it? [Y/n] "
                     ).strip().lower() != "n":
                pass
            else:
                cur = ""
        if not cur:
            print("   Get it: https://discord.com/developers/applications")
            print("   -> your application -> Bot -> Reset Token -> Copy")
            while True:
                tok = input("   Paste the bot token: ").strip()
                if len(tok) > 40:
                    cur = tok
                    break
                print("   That looks too short — paste the full token.")
        cfg["discord"]["token"] = cur
        guild = input("2. Your Discord server ID for INSTANT /command "
                      "availability\n   (right-click your server name -> "
                      "Copy Server ID; ENTER to skip): ").strip()
        cfg["discord"]["guild_id"] = int(guild) if guild.isdigit() else None
        qnum = "3"

    # access token
    cur_tok = cfg["server"].get("access_token") or ""
    if cur_tok:
        if input(f"{qnum}. Client access token already set. Keep it? [Y/n] "
                 ).strip().lower() != "n":
            pass
        else:
            cur_tok = ""
    if not cur_tok:
        cur_tok = secrets.token_urlsafe(18)
        print(f"   Generated a new access token: {cur_tok}")
    cfg["server"]["access_token"] = cur_tok
    qnum = str(int(qnum) + 1)

    # public host (tunnel hostname)
    host = (cfg["server"].get("public_host") or "").strip()
    if host:
        if input(f"{qnum}. Public host is {host}. Keep? [Y/n] "
                 ).strip().lower() != "n":
            pass
        else:
            host = ""
    if not host:
        host = input(f"{qnum}. Public hostname friends connect through\n"
                     "   (your Cloudflare tunnel, e.g. subs.example.com;\n"
                     "    ENTER = LAN-only ws://<this-pc-ip>:8765/ws): ").strip()
    cfg["server"]["public_host"] = host
    qnum = str(int(qnum) + 1)

    # cloudflare zero-trust service token (optional, recommended)
    cf = cfg.setdefault("cloudflare", {"client_id": "", "client_secret": ""})
    print(f"{qnum}. Cloudflare Zero Trust service token (optional but "
          f"recommended\n   for internet exposure - blocks strangers at "
          f"Cloudflare's edge;\n   see the README section 'Locking it down "
          f"with Zero Trust').")
    cf_id = (cf.get("client_id") or "").strip()
    cf_sec = (cf.get("client_secret") or "").strip()
    if cf_id and cf_sec:
        if input(f"   Service token already set ({mask(cf_id)}). Keep? [Y/n] "
                 ).strip().lower() != "n":
            pass
        else:
            cf_id = cf_sec = ""
    if not (cf_id and cf_sec):
        cf_id = input("   Access Service Token Client ID (ENTER to skip): ").strip()
        cf_sec = input("   Access Service Token Client Secret: ").strip() \
            if cf_id else ""
    cf["client_id"], cf["client_secret"] = cf_id, cf_sec

    # translation engine
    qnum = str(int(qnum) + 1)
    eng = cfg.get("engine", "nllb")
    print(f"{qnum}. Translation engine — currently: {eng}")
    print("   [nllb] local model, offline, unlimited, decent quality")
    print("   [azure] Azure Translator (2M chars/month free on your student")
    print("     subscription), better quality, NLLB auto-fallback on errors.")
    print("     Create it: portal.azure.com -> Create resource -> Translator")
    print("     -> Free F0 -> Keys and Endpoint.")
    pick = input(f"   Which engine? [nllb/azure, ENTER keeps {eng}]: ").strip().lower()
    if pick in ("nllb", "azure"):
        cfg["engine"] = pick
        eng = pick
    if eng == "azure":
        az = cfg.setdefault("azure", {"key": "", "region": ""})
        if not (az.get("key") and az.get("region")):
            az["key"] = input("   Azure Translator KEY (KEY 1): ").strip()
            az["region"] = input("   Azure Translator REGION (e.g. koreacentral): ").strip()
        cfg["engine"] = "azure"

    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                    encoding="utf-8")

    print()
    print("=" * 60)
    print("  Saved. Next steps:")
    print("=" * 60)
    print("  1. Invite the bot to your Discord server:")
    print("     https://discord.com/developers/applications -> your app ->")
    print("     OAuth2 -> URL Generator -> scopes: bot + applications.commands")
    print("     -> perms: View Channels + Connect -> open the URL.")
    print("     (the server console prints a ready-made invite link on start)")
    print("  2. (Internet friends) expose the port through Cloudflare:")
    print("     cloudflared tunnel --url http://localhost:8765   (named tunnel:")
    print("     route DNS for the hostname above) — see README.md")
    print()
    base = (f"wss://{host}/ws" if host
            else f"ws://127.0.0.1:{cfg['server']['port']}/ws (LAN: swap in this PC's IP)")
    cf = cfg.get("cloudflare") or {}
    frag = f"t={cur_tok}"
    if cf.get("client_id") and cf.get("client_secret"):
        frag += f"&cid={cf['client_id']}&csec={cf['client_secret']}"
    print("  3. Give every friend this ONE-LINE SETUP CODE:")
    print()
    print(f"     {base}#{frag}")
    if len(frag) > 40:
        print("       (includes the Cloudflare Access service token - requests")
        print("        without it are blocked at Cloudflare's edge)")
    print()
    print("  4. Start the server:  start.bat")
    print("=" * 60)


if __name__ == "__main__":
    main()
