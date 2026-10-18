"""Discord Web auto-joiner: keeps a browser logged into discord.com connected
to a configured voice channel, so the loopback capture always has audio.

ToS note: automating a user account is against Discord's Terms of Service.
Use a dedicated burner account for this — not your main.

First run (--setup): a visible browser opens; log in manually (QR/password).
The session is saved to discord_web_profile/ and reused afterwards.

Normal use (--join, or automatic when the server starts with source=loopback
and discord_web.auto_join): joins guild/channel from config.json and watches
the connection — re-clicking Join Voice if it drops, and relaunching the
whole browser if it closes or crashes. Ctrl+C to stop.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

APP_DIR = Path(__file__).resolve().parent
PROFILE_DIR = APP_DIR / "discord_web_profile"

SELECTORS = {
    "guild": '[data-list-item-id^="guildsnav___"][aria-label="{guild}"]',
    "channel": '[data-list-item-id^="channels___"][aria-label="{channel}"]',
    "join_voice": '[aria-label*="Join Voice" i]',
    "disconnect": '[aria-label*="Disconnect" i]',
}


def load_cfg() -> dict:
    cfg = json.loads((APP_DIR / "config.json").read_text(encoding="utf-8"))
    dw = cfg.setdefault("discord_web", {})
    dw.setdefault("guild_id", "")     # direct navigation: most reliable
    dw.setdefault("channel_id", "")   # the VOICE channel's id
    dw.setdefault("guild", "")        # sidebar-click fallbacks
    dw.setdefault("channel", "Lobby")
    dw.setdefault("auto_join", True)
    return cfg


async def join_channel(page, guild: str, channel: str) -> bool:
    """Navigate to guild/channel and press Join Voice. True if connected.
    Uses layered locators — Discord's DOM attribute names change often, so
    role/text matching comes first and attribute selectors are fallbacks."""
    from pathlib import Path
    dbg = Path(__file__).resolve().parent / "debug_audio"
    dbg.mkdir(exist_ok=True)

    async def shot(tag):
        try:
            await page.screenshot(path=str(dbg / f"web_{tag}.png"))
        except Exception:
            pass

    # already connected? nothing to do
    try:
        if await page.locator(SELECTORS["disconnect"]).count() > 0:
            print("[web] already connected to voice")
            return True
    except Exception:
        pass

    # primary: navigate straight to the voice channel URL (no sidebar clicking)
    cfg_ids = load_cfg()["discord_web"]
    gid, cid = cfg_ids.get("guild_id"), cfg_ids.get("channel_id")
    if gid and cid:
        url = f"https://discord.com/channels/{gid}/{cid}"
        print(f"[web] navigating to {url}")
        await page.goto(url, timeout=30000)
        await page.wait_for_timeout(2500)
        chan_clicked = True
    else:
        chan_clicked = False

    if guild and not chan_clicked:
        clicked = False
        for attempt in (
            lambda: page.get_by_role("link", name=guild).first.click(timeout=5000),
            lambda: page.get_by_role("button", name=guild).first.click(timeout=5000),
            lambda: page.get_by_text(guild, exact=True).first.click(timeout=5000),
            lambda: page.click(SELECTORS["guild"].format(guild=guild), timeout=5000),
        ):
            try:
                await attempt()
                clicked = True
                await page.wait_for_timeout(1500)
                break
            except Exception:
                continue
        if not clicked:
            print(f"[web] guild '{guild}' not clickable — dumping screenshot, "
                  f"using the currently open guild")
            await shot("no_guild")

    if not chan_clicked:
        chan_clicked = False
        for attempt in (
            lambda: page.get_by_role("link", name=channel, exact=True).first.click(timeout=5000),
            lambda: page.get_by_role("link", name=f"*{channel}*").first.click(timeout=5000),
            lambda: page.get_by_text(channel, exact=True).first.click(timeout=5000),
            lambda: page.click(SELECTORS["channel"].format(channel=channel), timeout=5000),
        ):
            try:
                await attempt()
                chan_clicked = True
                break
            except Exception:
                continue
        if not chan_clicked:
            print(f"[web] channel '{channel}' not found — dumping screenshot "
                  f"(debug_audio/web_no_channel.png)")
            await shot("no_channel")
            return False

    await page.wait_for_timeout(1000)
    try:
        if await page.locator(SELECTORS["disconnect"]).count() > 0:
            print("[web] already connected to voice")
            return True
        for attempt in (
            lambda: page.click(SELECTORS["join_voice"], timeout=6000),
            lambda: page.get_by_role("button", name="Join Voice").first.click(timeout=6000),
        ):
            try:
                await attempt()
                break
            except Exception:
                continue
        print("[web] clicked Join Voice (or it auto-connected)")
        await page.wait_for_timeout(2000)
        return True
    except Exception as e:
        print(f"[web] join button not found: {e}")
        await shot("no_join_button")
        return False


async def run(join_only: bool):
    from playwright.async_api import async_playwright
    cfg = load_cfg()
    dw = cfg["discord_web"]
    PROFILE_DIR.mkdir(exist_ok=True)
    # a force-killed previous browser can leave a stale profile lock that
    # blocks launch; it's only valid while a playwright chrome is alive
    lock = PROFILE_DIR / "lockfile"
    if lock.exists():
        try:
            lock.unlink()
            print("[web] removed stale profile lock")
        except Exception:
            pass

    while True:  # browser-level loop: relaunches whenever the browser dies
        try:
            async with async_playwright() as pw:
                # persistent context: the login session lives in
                # discord_web_profile/
                ctx = await pw.chromium.launch_persistent_context(
                    str(PROFILE_DIR),
                    headless=False,
                    viewport={"width": 1280, "height": 800},
                    permissions=["microphone"],
                )
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto("https://discord.com/channels/@me",
                                timeout=60000)

                if join_only:
                    print("[web] First run: log in to discord.com in this "
                          "browser window. The session is saved — afterwards "
                          "this script joins automatically. Press Ctrl+C "
                          "here when logged in.")
                    input("Press ENTER after you are logged in…")

                print(f"[web] watching for voice channel: "
                      f"{dw['guild'] or '<current>'} / {dw['channel']}")
                await join_channel(page, dw["guild"], dw["channel"])

                # voice watchdog: re-click Join Voice if it drops; any
                # browser/page death breaks out to a full relaunch below
                while True:
                    await asyncio.sleep(30)
                    try:
                        connected = await page.locator(
                            SELECTORS["disconnect"]).count() > 0
                        if not connected:
                            print("[web] voice not connected — rejoining…")
                            await join_channel(page, dw["guild"], dw["channel"])
                    except Exception as e:
                        print(f"[web] page lost ({type(e).__name__}); "
                              f"relaunching browser…")
                        break
                await ctx.close()
        except Exception as e:
            print(f"[web] browser session failed: {type(e).__name__}: {e}; "
                  f"retrying in 15s")
            await asyncio.sleep(15)


def main():
    ap = argparse.ArgumentParser(description="Discord Web auto-joiner")
    ap.add_argument("--setup", action="store_true",
                    help="first run: open the browser and log in manually")
    args = ap.parse_args()
    try:
        asyncio.run(run(join_only=args.setup))
    except KeyboardInterrupt:
        print("\n[web] stopped")


if __name__ == "__main__":
    main()
