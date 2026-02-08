import json
import os
import io
import chat_exporter
import discord
import asyncio

from discord import app_commands
from discord.ext import commands

# ---------- Config ----------
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

TOKEN = CONFIG["token"]
GUILD_ID = int(CONFIG["guild_id"])
TICKET_CATEGORY_ID = int(CONFIG["ticket_category_id"])
SUPPORT_ROLE_ID = int(CONFIG["support_role_id"])
LOG_CHANNEL_ID = int(CONFIG["log_channel_id"])

TICKETS_DB_FILE = "tickets.json"
ticket_lock = asyncio.Lock()

SAVE_TRANSCRIPTS = bool(CONFIG.get("save_transcripts", False))
TRANSCRIPTS_DIR = CONFIG.get("transcripts_dir", "./transcripts")

# ---------- Helpers ----------

def save_transcript_to_disk(channel_name: str, html: str) -> str:
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{channel_name}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path

def load_tickets_db() -> dict:
    if not os.path.exists(TICKETS_DB_FILE):
        return {
            "last_ticket_number": 0,
            "open_tickets_by_user": {},
            "tickets_by_channel": {}
        }

    with open(TICKETS_DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)

    db.setdefault("last_ticket_number", 0)
    db.setdefault("open_tickets_by_user", {})
    db.setdefault("tickets_by_channel", {})

    return db


def save_tickets_db(db: dict) -> None:
    with open(TICKETS_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=4)


async def get_next_ticket_number() -> int:
    async with ticket_lock:
        db = load_tickets_db()
        db["last_ticket_number"] = int(db.get("last_ticket_number", 0)) + 1
        save_tickets_db(db)
        return db["last_ticket_number"]


def format_ticket_name(n: int) -> str:
    return f"ticket-{n:04d}"


# ---------- Bot ----------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")


def build_ticket_overwrites(guild: discord.Guild, opener: discord.Member) -> dict:
    support_role = guild.get_role(SUPPORT_ROLE_ID)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        opener: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    return overwrites


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.green, emoji="🎫", custom_id="ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("This only works in a server.", ephemeral=True)

       
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        opener = interaction.user

        category = guild.get_channel(TICKET_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.followup.send("Ticket category is not set correctly.", ephemeral=True)

        db = load_tickets_db()
        user_key = str(opener.id)
        existing_channel_id = db.get("open_tickets_by_user", {}).get(user_key)
        if existing_channel_id:
            ch = guild.get_channel(int(existing_channel_id))
            if isinstance(ch, discord.TextChannel):
                return await interaction.followup.send(f"You already have a ticket: {ch.mention}", ephemeral=True)

        overwrites = build_ticket_overwrites(guild, opener)
        ticket_no = await get_next_ticket_number()
        channel_name = format_ticket_name(ticket_no)

        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Ticket #{ticket_no} opened by {opener} ({opener.id})",
        )

        open_log = discord.Embed(
        title="🟢 Ticket Opened",
        color=0x2b2d31,
        timestamp=discord.utils.utcnow()
    )

        open_log.add_field(name="Ticket", value=channel.name, inline=True)
        open_log.add_field(name="Opened by", value=opener.mention, inline=True)
        open_log.add_field(name="Status", value="Open", inline=True)

        open_log.set_footer(text="Ticket System")

        db = load_tickets_db()
        db.setdefault("open_tickets_by_user", {})[str(opener.id)] = str(channel.id)
        db.setdefault("tickets_by_channel", {})[str(channel.id)] = {
            "ticket_number": ticket_no,
            "channel_id": str(channel.id),
            "opener_id": str(opener.id),
            "claimed_by": None,
            "status": "open"
        }
        save_tickets_db(db)

        embed = discord.Embed(
            title=f"🎫 Support Ticket #{ticket_no}",
            description=(
                f"Hi {opener.mention}! Explain your issue and a staff member will respond.\n\n"
                "Use the buttons below to manage this ticket."
            ),
        )

        await channel.send(
            content=f"{opener.mention}",
            embed=embed,
            view=TicketInsideView(opener_id=opener.id),
        )

        log_ch = guild.get_channel(LOG_CHANNEL_ID)
        if isinstance(log_ch, discord.TextChannel):
            await log_ch.send(embed=open_log)


class TicketInsideView(discord.ui.View):
    def __init__(self, opener_id: int):
        super().__init__(timeout=None)
        self.opener_id = opener_id

    @discord.ui.button(
        label="Claim Ticket",
        style=discord.ButtonStyle.blurple,
        emoji="✅",
        custom_id="ticket:claim",
    )
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not interaction.channel:
            return await interaction.response.send_message("This only works in a server.", ephemeral=True)

        guild = interaction.guild
        channel_id = str(interaction.channel.id)

        support_role = guild.get_role(SUPPORT_ROLE_ID)
        is_staff = support_role in getattr(interaction.user, "roles", []) if support_role else False
        if not is_staff:
            return await interaction.response.send_message("Only support staff can claim tickets.", ephemeral=True)

        db = load_tickets_db()
        ticket = db["tickets_by_channel"].get(channel_id)
        if not ticket:
            return await interaction.response.send_message(
                "Ticket data not found (did you save it on ticket creation?).",
                ephemeral=True
            )

        claimed_by = ticket.get("claimed_by")
        if claimed_by is not None:
            return await interaction.response.send_message(
                f"This ticket is already claimed by <@{claimed_by}>.",
                ephemeral=True
            )


        ticket["claimed_by"] = str(interaction.user.id)
        ticket["status"] = "claimed"
        db["tickets_by_channel"][channel_id] = ticket
        save_tickets_db(db)


        if interaction.message.embeds:
            updated_embed = interaction.message.embeds[0].copy()
        else:
            updated_embed = discord.Embed(title="🎫 Support Ticket")


        updated_embed.add_field(name="Status", value="Claimed", inline=True)
        updated_embed.add_field(name="Claimed by", value=interaction.user.mention, inline=True)

        await interaction.response.send_message("✅ Ticket claimed.", ephemeral=True)
        await interaction.message.edit(embed=updated_embed, view=self)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.red,
        emoji="🔒",
        custom_id="ticket:close",
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not interaction.channel:
            return await interaction.response.send_message("This only works in a server.", ephemeral=True)

        guild = interaction.guild
        channel = interaction.channel
        channel_id = str(interaction.channel.id)
        

        support_role = guild.get_role(SUPPORT_ROLE_ID)
        is_staff = support_role in getattr(interaction.user, "roles", []) if support_role else False
        is_opener = interaction.user.id == self.opener_id
        db = load_tickets_db()
        ticket = db["tickets_by_channel"].get(channel_id)
        claimed_by = ticket.get("claimed_by")  
        user_id = str(interaction.user.id)
        is_admin = interaction.user.guild_permissions.manage_channels

        if claimed_by is not None:
            if claimed_by != user_id and not is_admin:
                return await interaction.response.send_message(
                    f"This ticket is claimed by <@{claimed_by}>. Only they (or an admin) can close it.",
                    ephemeral=True
                )



        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("Closing ticket in 3 seconds...", ephemeral=True)

        transcript_html = None

        try:
            transcript_html = await chat_exporter.export(
                channel=channel,
                limit=500,
                tz_info="Europe/London",
                bot=interaction.client,
            )
        except Exception as e:
            await interaction.followup.send(f"Transcript error: {e}", ephemeral=True)

        saved_path = None
        if transcript_html and SAVE_TRANSCRIPTS:
            saved_path = save_transcript_to_disk(channel.name, transcript_html)

        ticket = load_tickets_db()["tickets_by_channel"].get(str(channel.id))

        claimed_by = (
            f"<@{ticket['claimed_by']}>"
            if ticket and ticket.get("claimed_by")
            else "Not claimed"
        )

        close_log = discord.Embed(
            title="🔴 Ticket Closed",
            color=0x2b2d31,
            timestamp=discord.utils.utcnow()
        )

        close_log.add_field(name="Ticket", value=channel.name, inline=True)
        close_log.add_field(name="Opened by", value=f"<@{self.opener_id}>", inline=True)
        close_log.add_field(name="Claimed by", value=claimed_by, inline=True)
        close_log.add_field(name="Closed by", value=interaction.user.mention, inline=True)

        close_log.add_field(
            name="Transcript",
            value="✅ Attached" if transcript_html else "❌ Failed",
            inline=False
        )

        close_log.set_footer(text="Ticket System")

        log_ch = guild.get_channel(LOG_CHANNEL_ID)
        if transcript_html:
            buf = io.BytesIO(transcript_html.encode("utf-8"))
            buf.seek(0)
            log_file = discord.File(buf, filename=f"transcript-{channel.name}.html")
            await log_ch.send(embed=close_log, file=log_file)
        else:
            await log_ch.send(embed=close_log)

        if transcript_html:
            try:
                opener = guild.get_member(self.opener_id)
                if opener is None:
                    opener = await interaction.client.fetch_user(self.opener_id)

                buf = io.BytesIO(transcript_html.encode("utf-8"))
                buf.seek(0)
                dm_file = discord.File(buf, filename=f"transcript-{channel.name}.html")

                send_embed = discord.Embed(
                    title="🎫 Ticket Closed",
                    description=(
                        f"Your support ticket **{channel.name}** has been successfully closed.\n\n"
                        "📄 **Transcript**\n"
                        "A full transcript of the conversation is attached above for your records."
                    ),
                    color=0x2b2d31
                )
                send_embed.timestamp = discord.utils.utcnow()
                if interaction.guild.icon:
                    send_embed.set_thumbnail(url=interaction.guild.icon.url)
                send_embed.set_footer(text="Thank you for contacting support")

                await opener.send(embed=send_embed, file=dm_file)

            except discord.Forbidden:
                if isinstance(log_ch, discord.TextChannel):
                    await log_ch.send(f"⚠️ Could not DM transcript to <@{self.opener_id}> (DMs closed).")
            except Exception as e:
                if isinstance(log_ch, discord.TextChannel):
                    await log_ch.send(f"⚠️ Failed to DM transcript to <@{self.opener_id}>: {e}")

        
        db = load_tickets_db()
        db.get("open_tickets_by_user", {}).pop(str(self.opener_id), None)
        db.get("tickets_by_channel", {}).pop(str(channel.id), None)
        save_tickets_db(db)

        await asyncio.sleep(3)
        await channel.delete(reason=f"Ticket closed by {interaction.user} ({interaction.user.id})")


@bot.tree.command(name="panel", description="Post the ticket panel (staff only).")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def panel(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)

    if not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message("You don’t have permission to use this.", ephemeral=True)

    embed = discord.Embed(
        title="Support Tickets",
        description="Press the button below to open a private support ticket.",
    )
    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.response.send_message("✅ Panel posted.", ephemeral=True)


bot.run(TOKEN)
