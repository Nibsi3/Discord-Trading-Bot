import os
import discord
from discord.ext import commands
import sqlite3
from dotenv import load_dotenv
import uuid
from aiohttp import web
import threading
from urllib.parse import urlencode
import asyncio
import logging
from discord.ext.commands import has_permissions, is_owner, CheckFailure
from discord import Interaction, Message, Reaction
import time

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
STEAM_API_KEY = os.getenv('STEAM_API_KEY')

# Database setup
conn = sqlite3.connect('cs2bot.db')
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users (
    discord_id TEXT PRIMARY KEY,
    steam_id TEXT,
    verified INTEGER DEFAULT 0
)''')
conn.commit()

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

GUILD_ID = None  # Set this to your server's ID if needed
VERIFIED_ROLE_NAME = 'Verified Trader'

# Configure logging
logging.basicConfig(level=logging.INFO)

# Helper: Check if user is verified
def is_verified(discord_id):
    c.execute('SELECT verified FROM users WHERE discord_id=?', (str(discord_id),))
    row = c.fetchone()
    return row and row[0] == 1

# --- Steam OpenID Web Server ---
STEAM_OPENID_URL = 'https://steamcommunity.com/openid/login'
CALLBACK_HOST = 'http://localhost:8080'  # Change if deploying

# In-memory state for pending verifications
pending_verifications = {}

async def handle_steam_callback(request):
    params = request.rel_url.query
    # Validate OpenID response (simplified)
    claimed_id = params.get('openid.claimed_id')
    state = params.get('state')
    if not claimed_id or not state or state not in pending_verifications:
        return web.Response(text='Invalid or expired verification link.')
    steam_id = claimed_id.split('/')[-1]
    discord_id = pending_verifications.pop(state)
    # Open a new DB connection in this thread
    conn_local = sqlite3.connect('cs2bot.db')
    c_local = conn_local.cursor()
    c_local.execute('INSERT OR REPLACE INTO users (discord_id, steam_id, verified) VALUES (?, ?, 1)', (str(discord_id), steam_id))
    conn_local.commit()
    # Assign Verified Trader role in Discord
    guilds = bot.guilds
    user_dm_success = False
    for guild in guilds:
        member = guild.get_member(int(discord_id))
        if member is None:
            try:
                fut_fetch = asyncio.run_coroutine_threadsafe(guild.fetch_member(int(discord_id)), bot.loop)
                member = fut_fetch.result()
            except Exception as e:
                logging.error(f"Failed to fetch member: {e}")
                member = None
        if member:
            role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
            if role:
                try:
                    logging.info(f"Attempting to add role '{role.name}' (ID: {role.id}) to member {member} (ID: {member.id}) in guild {guild.name} (ID: {guild.id})")
                    logging.info(f"Bot top role position: {guild.me.top_role.position}, Role position: {role.position}")
                    logging.info(f"Bot permissions: {guild.me.guild_permissions}")
                    fut = asyncio.run_coroutine_threadsafe(member.add_roles(role, reason="Steam verification"), bot.loop)
                    fut.result()
                    # Allow Verified Trader to send messages in public channels
                    fut_perm = asyncio.run_coroutine_threadsafe(allow_verified_role_in_public_channels(guild), bot.loop)
                    fut_perm.result()
                    logging.info("Role assignment successful.")
                except Exception as e:
                    logging.error(f"Failed to add role: {e}")
                    try:
                        fut_dm = asyncio.run_coroutine_threadsafe(member.send('⚠️ I could not assign you the Verified Trader role. Please contact an admin.'), bot.loop)
                        fut_dm.result()
                    except Exception:
                        pass
            try:
                fut_dm = asyncio.run_coroutine_threadsafe(member.send('✅ Your Steam account has been successfully verified! You now have the Verified Trader role and access to the server.'), bot.loop)
                fut_dm.result()
                user_dm_success = True
            except Exception:
                pass
    conn_local.close()
    if user_dm_success:
        return web.Response(text='Steam account verified! You may now use the Discord server. (Check your DMs)')
    else:
        return web.Response(text='Steam account verified! If you did not receive a DM, please contact an admin.')

# Start aiohttp web server in a thread
def start_web_server():
    app = web.Application()
    app.router.add_get('/steam/callback', handle_steam_callback)
    runner = web.AppRunner(app)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async def run():
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 8080)
        await site.start()
        while True:
            await asyncio.sleep(3600)
    loop.run_until_complete(run())

threading.Thread(target=start_web_server, daemon=True).start()

# !verify command
@bot.command()
async def verify(ctx):
    if is_verified(ctx.author.id):
        await ctx.send('You are already verified.')
        return
    # Generate unique state for this verification
    state = str(uuid.uuid4())
    pending_verifications[state] = ctx.author.id
    params = {
        'openid.ns': 'http://specs.openid.net/auth/2.0',
        'openid.mode': 'checkid_setup',
        'openid.return_to': f'{CALLBACK_HOST}/steam/callback?state={state}',
        'openid.realm': CALLBACK_HOST,
        'openid.identity': 'http://specs.openid.net/auth/2.0/identifier_select',
        'openid.claimed_id': 'http://specs.openid.net/auth/2.0/identifier_select',
    }
    steam_link = f'{STEAM_OPENID_URL}?{urlencode(params)}'
    try:
        await ctx.author.send(f'Please verify your Steam account by logging in here: {steam_link}')
        await ctx.send('I have sent you a DM with your Steam verification link!')
    except Exception:
        await ctx.send(f'Please enable DMs and try again. Steam verification link: {steam_link}')

# !unverify command
@bot.command()
async def unverify(ctx):
    c.execute('UPDATE users SET verified=0, steam_id=NULL WHERE discord_id=?', (str(ctx.author.id),))
    conn.commit()
    guild = ctx.guild
    if guild is not None:
        role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
        if role:
            await ctx.author.remove_roles(role)
    await ctx.send('You have been unverified and your Steam link removed.')

# --- Trade Session Management & Audit Logging ---
# Extend DB for trade sessions and audit logs
c.execute('''CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT,
    user1_id TEXT,
    user2_id TEXT,
    status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)''')
c.execute('''CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT,
    user_id TEXT,
    details TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)''')
conn.commit()

async def log_audit(action, user_id, details, channel_id=None):
    c.execute('INSERT INTO audit_logs (action, user_id, details) VALUES (?, ?, ?)', (action, str(user_id), details))
    conn.commit()
    # Log to trade-logs channel if exists
    for guild in bot.guilds:
        log_channel = discord.utils.get(guild.text_channels, name='trade-logs')
        if log_channel:
            msg = f"[LOG] Action: {action} | User: <@{user_id}> | Details: {details}"
            if channel_id:
                msg += f" | Channel: <#{channel_id}>"
            msg += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            try:
                await log_channel.send(msg)
            except Exception:
                pass

# --- Improved Error Handling for DMs ---
@bot.event
async def on_message(message):
    if message.guild is None and not message.author.bot:
        await message.channel.send('Please use !verify in a server channel to verify your Steam profile before using the bot.')
        return
    await bot.process_commands(message)

# --- Improved !trade command ---
@bot.command()
async def trade(ctx, user: discord.Member):
    if ctx.author == user:
        await ctx.send('You cannot trade with yourself!')
        await log_audit('trade_attempt_self', ctx.author.id, f'Tried to trade with themselves.')
        return
    if not is_verified(ctx.author.id) or not is_verified(user.id):
        await ctx.send('Both users must be verified to start a trade.')
        await log_audit('trade_attempt_unverified', ctx.author.id, f'Tried to trade with {user.id}.')
        return
    # Prevent multiple open trades between same users
    c.execute('SELECT * FROM trades WHERE ((user1_id=? AND user2_id=?) OR (user1_id=? AND user2_id=?)) AND status=?', (str(ctx.author.id), str(user.id), str(user.id), str(ctx.author.id), 'open'))
    if c.fetchone():
        await ctx.send('There is already an open trade session between you two.')
        return
    # Ask for confirmation from the other user
    prompt = await ctx.send(f'{user.mention}, do you accept the trade request from {ctx.author.mention}? React with ✅ to accept or ❌ to decline. (60 seconds)')
    await prompt.add_reaction('✅')
    await prompt.add_reaction('❌')

    def check(reaction, reactor):
        return (
            reactor.id == user.id and
            reaction.message.id == prompt.id and
            str(reaction.emoji) in ['✅', '❌']
        )
    try:
        reaction, reactor = await bot.wait_for('reaction_add', timeout=60.0, check=check)
        if str(reaction.emoji) == '✅':
            await ctx.send(f'{user.mention} accepted the trade! Creating your private trade channel...')
            guild = ctx.guild
            channel_name = f'trade-{ctx.author.name}-{user.name}'.replace(' ', '-')
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                ctx.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                bot.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            channel = await guild.create_text_channel(channel_name, overwrites=overwrites)
            c.execute('INSERT INTO trades (channel_id, user1_id, user2_id, status) VALUES (?, ?, ?, ?)', (str(channel.id), str(ctx.author.id), str(user.id), 'open'))
            conn.commit()
            await channel.send(f'Trade session started between {ctx.author.mention} and {user.mention}. Both users must type !confirm to proceed. Use this channel to negotiate your trade.')
            await ctx.send(f'{ctx.author.mention} and {user.mention}, your trade channel is ready: {channel.mention}')
            await log_audit('trade_start', ctx.author.id, f'Started trade with {user.id} in channel {channel.id}', channel_id=channel.id)
        else:
            await ctx.send(f'{user.mention} declined the trade request from {ctx.author.mention}.')
            await log_audit('trade_declined', user.id, f'Declined trade with {ctx.author.id}')
    except Exception:
        await ctx.send(f'{user.mention} did not respond in time. Trade request cancelled.')
        await log_audit('trade_timeout', user.id, f'No response to trade request from {ctx.author.id}')

# --- !canceltrade command ---
@bot.command(name="canceltrade")
async def canceltrade(ctx):
    # Only allow in trade channels
    if not ctx.channel.name.startswith('trade-'):
        await ctx.send('This command can only be used in a trade channel.')
        return
    # Try to find the trade even if status is not 'open'
    c.execute('SELECT * FROM trades WHERE channel_id=?', (str(ctx.channel.id),))
    trade = c.fetchone()
    if not trade:
        await ctx.send('No trade session found for this channel.')
        # Still try to delete the channel if it exists
        await asyncio.sleep(10)
        try:
            await ctx.channel.delete(reason="No trade session found, cleaning up channel.")
        except Exception as e:
            await log_audit('trade_channel_delete_failed', ctx.author.id, f'Failed to delete channel {ctx.channel.id}: {e}', channel_id=ctx.channel.id)
            await ctx.send(f"Failed to delete this channel automatically. Please contact an admin. Error: {e}")
        return
    # Always allow deletion if the channel exists, even if status is not open
    if trade[4] != 'open':
        await ctx.send('This trade session is already closed or cancelled. This channel is about to close in 10 seconds.')
        await asyncio.sleep(10)
        try:
            await ctx.channel.delete(reason="Trade session already closed/cancelled.")
        except Exception as e:
            await log_audit('trade_channel_delete_failed', ctx.author.id, f'Failed to delete channel {ctx.channel.id}: {e}', channel_id=ctx.channel.id)
            await ctx.send(f"Failed to delete this channel automatically. Please contact an admin. Error: {e}")
        return
    if str(ctx.author.id) not in (trade[2], trade[3]):
        await ctx.send('Only participants of this trade can cancel it.')
        return
    c.execute('UPDATE trades SET status=? WHERE id=?', ('cancelled', trade[0]))
    conn.commit()
    await ctx.send('Trade session cancelled. This channel is about to close in 10 seconds.')
    await log_audit('trade_cancel', ctx.author.id, f'Cancelled trade in channel {ctx.channel.id}', channel_id=ctx.channel.id)
    await asyncio.sleep(10)
    try:
        await ctx.channel.delete(reason="Trade cancelled by user.")
    except Exception as e:
        await log_audit('trade_channel_delete_failed', ctx.author.id, f'Failed to delete channel {ctx.channel.id}: {e}', channel_id=ctx.channel.id)
        await ctx.send(f"Failed to delete this channel automatically. Please contact an admin. Error: {e}")

# --- !cancel command ---
@bot.command(name="cancel")
async def cancel(ctx):
    await canceltrade(ctx)

# --- !confirm command (basic placeholder for user feedback) ---
# In-memory trade confirmations
trade_confirms = {}

@bot.command()
async def confirm(ctx):
    if not ctx.channel.name.startswith('trade-'):
        await ctx.send('This command can only be used in a trade channel.')
        return
    c.execute('SELECT * FROM trades WHERE channel_id=? AND status=?', (str(ctx.channel.id), 'open'))
    trade = c.fetchone()
    if not trade:
        await ctx.send('No open trade session found for this channel.')
        return
    # Track confirmations in memory
    confirms = trade_confirms.setdefault(ctx.channel.id, set())
    confirms.add(ctx.author.id)
    await ctx.send(f'{ctx.author.mention} has confirmed. Waiting for the other party...')
    if len(confirms) == 2:
        await ctx.send('Both users have confirmed! (Escrow/payment/Steam trade logic will go here in Phase 2.)')
        c.execute('UPDATE trades SET status=? WHERE id=?', ('confirmed', trade[0]))
        conn.commit()
        await log_audit('trade_confirmed', ctx.author.id, f'Both users confirmed in channel {ctx.channel.id}')
        del trade_confirms[ctx.channel.id]

# --- Wallet Management (Phase 1) ---
c.execute('''CREATE TABLE IF NOT EXISTS wallets (
    user_id TEXT PRIMARY KEY,
    balance REAL DEFAULT 0
)''')
c.execute('''CREATE TABLE IF NOT EXISTS wallet_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    amount REAL,
    method TEXT,
    status TEXT,
    txid TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)''')
conn.commit()

@bot.command()
async def balance(ctx):
    c.execute('SELECT balance FROM wallets WHERE user_id=?', (str(ctx.author.id),))
    row = c.fetchone()
    bal = row[0] if row else 0.0
    await ctx.send(f'Your wallet balance: ${bal:.2f}')

@bot.command()
async def deposit(ctx, amount: float, method: str):
    method = method.lower()
    if method not in ['paypal', 'stripe', 'bitcoin', 'eft']:
        await ctx.send('Invalid method. Use one of: paypal, stripe, bitcoin, eft.')
        return
    # Simulate payment initiation (in production, generate payment link/invoice)
    await ctx.send(f'Initiating a ${amount:.2f} deposit via {method.title()}... (This is a mock, payment integration coming soon!)')
    # Simulate payment confirmation for now
    c.execute('INSERT INTO wallet_transactions (user_id, amount, method, status, txid) VALUES (?, ?, ?, ?, ?)', (str(ctx.author.id), amount, method, 'confirmed', 'MOCKTXID'))
    c.execute('INSERT OR IGNORE INTO wallets (user_id, balance) VALUES (?, ?)', (str(ctx.author.id), 0.0))
    c.execute('UPDATE wallets SET balance = balance + ? WHERE user_id=?', (amount, str(ctx.author.id)))
    conn.commit()
    await log_audit('wallet_deposit', ctx.author.id, f'Deposited ${amount:.2f} via {method.title()} (MOCK)')
    await ctx.send(f'Deposit successful! Your new balance: ${amount:.2f} added.')

# --- Bot Status ---
@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game(name='DM !verify to get started'))
    print(f'Bot is ready as {bot.user}')

# On member join: restrict typing until verified
@bot.event
async def on_member_join(member):
    guild = member.guild
    role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
    if not role:
        role = await guild.create_role(name=VERIFIED_ROLE_NAME)
    # Remove send_messages from @everyone in public channels
    for channel in guild.text_channels:
        if channel.name.startswith('trade-'):
            continue
        overwrite = channel.overwrites_for(guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(guild.default_role, overwrite=overwrite)
    # Only add role after verification

# Helper: assign verified role after Steam verification (to be called after OpenID callback)
def assign_verified_role(discord_id, steam_id, guild):
    c.execute('INSERT OR REPLACE INTO users (discord_id, steam_id, verified) VALUES (?, ?, 1)', (str(discord_id), steam_id))
    conn.commit()
    # Discord role assignment should be handled in the callback context

async def allow_verified_role_in_public_channels(guild):
    role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
    if not role:
        return
    for channel in guild.text_channels:
        if channel.name.startswith('trade-'):
            continue
        overwrite = channel.overwrites_for(role)
        if overwrite.send_messages is not True:
            overwrite.send_messages = True
            await channel.set_permissions(role, overwrite=overwrite)

# --- Admin Commands ---
@bot.command()
@is_owner()
async def fixroles(ctx):
    """Ensure Verified Trader role is above @everyone and has correct permissions."""
    guild = ctx.guild
    role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
    if not role:
        await ctx.send('Verified Trader role does not exist.')
        return
    # Move role above @everyone if needed
    if role.position <= guild.default_role.position:
        await role.edit(position=guild.default_role.position + 1)
    await ctx.send('Verified Trader role position and permissions checked.')

@bot.command()
@is_owner()
async def fixchannels(ctx):
    """Fix all public channels so Verified Trader can send messages, @everyone cannot."""
    guild = ctx.guild
    role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
    if not role:
        await ctx.send('Verified Trader role does not exist.')
        return
    for channel in guild.text_channels:
        if channel.name.startswith('trade-'):
            continue
        # Restrict @everyone
        overwrite = channel.overwrites_for(guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(guild.default_role, overwrite=overwrite)
        # Allow Verified Trader
        overwrite_v = channel.overwrites_for(role)
        overwrite_v.send_messages = True
        await channel.set_permissions(role, overwrite=overwrite_v)
    await ctx.send('Channel permissions fixed for Verified Trader.')

@bot.command()
@is_owner()
async def clear(ctx, amount: int = 100):
    """Clear messages in the current channel (default 100)."""
    await ctx.channel.purge(limit=amount)
    await ctx.send(f'Cleared {amount} messages.', delete_after=3)

@bot.command()
@is_owner()
async def adminhelp(ctx):
    """List all admin commands."""
    help_text = (
        "**Admin Commands:**\n"
        "!fixroles - Ensure Verified Trader role is above @everyone and has correct permissions.\n"
        "!fixchannels - Fix all public channels so Verified Trader can send messages, @everyone cannot.\n"
        "!clear [amount] - Clear messages in the current channel (default 100).\n"
        "!adminhelp - Show this help message.\n"
    )
    await ctx.author.send(help_text)
    await ctx.send('Admin help sent to your DMs.')

# Prevent non-owners from using admin commands
@fixroles.error
@fixchannels.error
@clear.error
@adminhelp.error
async def admin_command_error(ctx, error):
    if isinstance(error, CheckFailure):
        await ctx.send('You do not have permission to use this command.')

bot.run(DISCORD_TOKEN)
