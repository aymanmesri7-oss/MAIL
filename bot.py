"""
Bot Discord TempMail — Génère des emails temporaires via temp-mail.org
et extrait les codes de vérification automatiquement.
Même architecture que HeroSMS : panneau persistant, éphémère, logs, quotas.
"""

import os
import asyncio
import logging
import datetime as dt
import hashlib
import random
import string
import re
from collections import defaultdict

import aiohttp
import discord
from discord.ext import commands
from discord import app_commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tempmail-bot")

# ── ENV ──
def env(key, default=""):
    return os.getenv(key, default).strip().strip('"').strip("'")

DISCORD_TOKEN      = env("DISCORD_TOKEN")
GUILD_IDS          = [int(g) for g in env("DISCORD_GUILD_IDS", env("DISCORD_GUILD_ID", "0")).split(",") if g.strip()]
LOG_CHANNEL_ID     = int(env("LOG_CHANNEL_ID", "0"))
ALLOWED_ROLE_ID    = int(env("ALLOWED_ROLE_ID", "0"))

POLL_TIMEOUT       = int(env("POLL_TIMEOUT", "180"))       # secondes d'attente max pour le code
POLL_INTERVAL      = int(env("POLL_INTERVAL", "5"))         # secondes entre chaque check
COOLDOWN_SECONDS   = int(env("COOLDOWN_SECONDS", "5"))
DAILY_QUOTA        = int(env("DAILY_QUOTA", "0"))           # 0 = illimité
DAILY_ATTEMPTS_MAX = int(env("DAILY_ATTEMPTS_MAX", "0"))    # 0 = illimité

# ── API temp-mail.org ──
TEMPMAIL_API = "http://api.temp-mail.org"

# ── COULEURS ──
COLOR_MAIL   = 0x00D166   # vert
COLOR_CODE   = 0xFEE75C   # jaune
COLOR_FAIL   = 0xE74C3C   # rouge
COLOR_CANCEL = 0x95A5A6   # gris
COLOR_LOG    = 0x5865F2   # bleu

# ── COMPTEURS ──
daily_success: dict[int, int] = defaultdict(int)
daily_attempts: dict[int, int] = defaultdict(int)
last_reset: str = dt.date.today().isoformat()

def _reset_if_new_day():
    global last_reset
    today = dt.date.today().isoformat()
    if today != last_reset:
        daily_success.clear()
        daily_attempts.clear()
        last_reset = today

_user_locks: dict[int, float] = {}
_cancel_events: dict[int, asyncio.Event] = {}  # keyed by discord user id

# ── BOT ──
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
http_session: aiohttp.ClientSession | None = None


# ── HELPERS ──

def random_user(length=12):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))

def md5_hash(email: str) -> str:
    return hashlib.md5(email.encode()).hexdigest()

def extract_code(text: str) -> str | None:
    """Cherche un code de vérification (4-8 chiffres) dans le texte."""
    patterns = [
        r"(?:code|código|verification|vérification|confirm|pin|otp)[:\s]*(\d{4,8})",
        r"(\d{4,8})\s*(?:is your|est votre|code)",
        r"(?:enter|entrez|use|utilisez)[:\s]*(\d{4,8})",
        r"\b(\d{6})\b",  # fallback: 6 chiffres isolés
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


# ── TEMPMAIL API ──

async def tempmail_get_domains() -> list[str]:
    """Récupère les domaines dispo sur temp-mail.org."""
    url = f"{TEMPMAIL_API}/request/domains/format/json/"
    try:
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                domains = await r.json()
                return [d for d in domains if isinstance(d, str)]
    except Exception as e:
        log.error("tempmail_get_domains: %s", e)
    return []


async def tempmail_get_messages(email_hash: str) -> list[dict]:
    """Récupère les mails reçus pour une adresse (via son MD5)."""
    url = f"{TEMPMAIL_API}/request/mail/id/{email_hash}/format/json/"
    try:
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json()
                if isinstance(data, list):
                    return data
            # 404 = pas de messages encore
            return []
    except Exception as e:
        log.error("tempmail_get_messages: %s", e)
    return []


async def tempmail_delete_message(msg_id: str):
    """Supprime un mail spécifique."""
    url = f"{TEMPMAIL_API}/request/delete/id/{msg_id}/format/json/"
    try:
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.text()
    except Exception as e:
        log.error("tempmail_delete: %s", e)


# ── LOGS STRUCTURÉS ──

async def log_email_pris(user, address):
    try:
        ch = bot.get_channel(LOG_CHANNEL_ID)
        if not ch:
            return
        _reset_if_new_day()
        t = daily_attempts.get(user.id, 0)
        v = daily_success.get(user.id, 0)
        now = dt.datetime.now().strftime("Aujourd'hui a %H:%M")
        embed = discord.Embed(title="📧 Email pris", color=COLOR_LOG)
        embed.add_field(name="VA", value=f"{user.mention}\n{user.display_name}", inline=True)
        embed.add_field(name="Type", value="Email temporaire", inline=True)
        embed.add_field(name="Valides", value=f"{v}\n({t} pris)", inline=True)
        embed.add_field(name="Email", value=f"`{address}`", inline=False)
        embed.add_field(name="Info", value=f"Gratuit · ID Discord : {user.id} · {now}", inline=False)
        await ch.send(embed=embed)
    except Exception as e:
        log.error("log_email_pris: %s", e)


async def log_code_recu(user, address, code, sender, subject):
    try:
        ch = bot.get_channel(LOG_CHANNEL_ID)
        if not ch:
            return
        _reset_if_new_day()
        t = daily_attempts.get(user.id, 0)
        v = daily_success.get(user.id, 0)
        now = dt.datetime.now().strftime("Aujourd'hui a %H:%M")
        embed = discord.Embed(title="✅ Code recu", color=0x2ECC71)
        embed.add_field(name="VA", value=f"{user.mention}\n{user.display_name}", inline=True)
        embed.add_field(name="Type", value="Email temporaire", inline=True)
        embed.add_field(name="Valides", value=f"{v}\n({t} pris)", inline=True)
        embed.add_field(name="Email", value=f"`{address}`", inline=True)
        embed.add_field(name="Code", value=f"**{code}**", inline=True)
        embed.add_field(name="De", value=sender[:50], inline=True)
        embed.add_field(name="Info", value=f"Objet : {subject[:80]}\nID Discord : {user.id} · {now}", inline=False)
        await ch.send(embed=embed)
    except Exception as e:
        log.error("log_code_recu: %s", e)


async def log_pas_de_code(user, address):
    try:
        ch = bot.get_channel(LOG_CHANNEL_ID)
        if not ch:
            return
        _reset_if_new_day()
        t = daily_attempts.get(user.id, 0)
        v = daily_success.get(user.id, 0)
        now = dt.datetime.now().strftime("Aujourd'hui a %H:%M")
        embed = discord.Embed(title="📧 Aucun code (expire)", color=COLOR_FAIL)
        embed.add_field(name="VA", value=f"{user.mention}\n{user.display_name}", inline=True)
        embed.add_field(name="Type", value="Email temporaire", inline=True)
        embed.add_field(name="Valides", value=f"{v}\n({t} pris)", inline=True)
        embed.add_field(name="Email", value=f"`{address}`", inline=False)
        embed.add_field(name="Info", value=f"Ne compte pas dans le quota.\nID Discord : {user.id} · {now}", inline=False)
        await ch.send(embed=embed)
    except Exception as e:
        log.error("log_pas_de_code: %s", e)


async def log_annule(user, address):
    try:
        ch = bot.get_channel(LOG_CHANNEL_ID)
        if not ch:
            return
        now = dt.datetime.now().strftime("Aujourd'hui a %H:%M")
        embed = discord.Embed(title="📧 Annule par le VA", color=0xF39C12)
        embed.add_field(name="VA", value=f"{user.mention}\n{user.display_name}", inline=True)
        embed.add_field(name="Email", value=f"`{address}`", inline=True)
        embed.add_field(name="Info", value=f"Ne compte pas dans le quota.\nID Discord : {user.id} · {now}", inline=False)
        await ch.send(embed=embed)
    except Exception as e:
        log.error("log_annule: %s", e)


# ── BOUTONS APRÈS GÉNÉRATION ──

class EmailActionsView(discord.ui.View):
    def __init__(self, address: str, email_hash: str, user_id: int):
        super().__init__(timeout=300)
        self.address = address
        self.email_hash = email_hash
        self.user_id = user_id

    @discord.ui.button(label="📬  Vérifier inbox", style=discord.ButtonStyle.grey)
    async def check_inbox(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Ce n'est pas ton email.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        messages = await tempmail_get_messages(self.email_hash)
        if not messages:
            await interaction.followup.send("📭 Aucun mail reçu. Réessaie dans quelques secondes.", ephemeral=True)
            return
        desc = ""
        for msg in messages[:5]:
            sender = msg.get("mail_from", "?")
            subject = msg.get("mail_subject", "(sans objet)")
            desc += f"**De :** {sender}\n**Objet :** {subject[:80]}\n\n"
        embed = discord.Embed(
            title=f"📬 {len(messages)} mail{'s' if len(messages) > 1 else ''} reçu{'s' if len(messages) > 1 else ''}",
            description=desc[:4000],
            color=COLOR_LOG,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="❌  Annuler", style=discord.ButtonStyle.red)
    async def cancel_email(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Ce n'est pas ton email.", ephemeral=True)
            return
        event = _cancel_events.get(self.user_id)
        if event:
            event.set()

        embed_cancelled = discord.Embed(
            title="📧 Email temporaire",
            description=f"`{self.address}` — annulé.\n\n✅ **Email annulé. Tu peux en reprendre un autre.**",
            color=COLOR_CANCEL,
        )
        await interaction.response.edit_message(embed=embed_cancelled, view=None)
        await log_annule(interaction.user, self.address)


# ── LOGIQUE D'ACHAT ──

async def _handle_email(interaction: discord.Interaction):
    user = interaction.user
    uid = user.id

    # Rôle
    if ALLOWED_ROLE_ID:
        if ALLOWED_ROLE_ID not in [r.id for r in user.roles]:
            await interaction.response.send_message("❌ Tu n'as pas le rôle nécessaire.", ephemeral=True)
            return

    # Cooldown
    now = asyncio.get_event_loop().time()
    if now - _user_locks.get(uid, 0) < COOLDOWN_SECONDS:
        await interaction.response.send_message(f"⏳ Attends {COOLDOWN_SECONDS}s.", ephemeral=True)
        return
    _user_locks[uid] = now

    # Quotas
    _reset_if_new_day()
    if DAILY_QUOTA and daily_success[uid] >= DAILY_QUOTA:
        await interaction.response.send_message(f"🚫 Limite de **{DAILY_QUOTA}** codes atteinte.", ephemeral=True)
        return
    if DAILY_ATTEMPTS_MAX and daily_attempts[uid] >= DAILY_ATTEMPTS_MAX:
        await interaction.response.send_message(f"🚫 Limite de **{DAILY_ATTEMPTS_MAX}** tentatives atteinte.", ephemeral=True)
        return

    daily_attempts[uid] += 1
    await interaction.response.defer(ephemeral=True)

    # Récupérer les domaines
    domains = await tempmail_get_domains()
    if not domains:
        await interaction.followup.send("❌ Impossible de récupérer les domaines temp-mail.org. Réessaie.", ephemeral=True)
        return

    # Générer l'adresse
    domain = random.choice(domains)
    if not domain.startswith("@"):
        domain = f"@{domain}"
    address = f"{random_user()}{domain}"
    email_hash = md5_hash(address)

    log.info("Email genere: %s (hash=%s) user=%s", address, email_hash, user)

    # Event annulation
    cancel_event = asyncio.Event()
    _cancel_events[uid] = cancel_event

    # Afficher l'email
    embed_email = discord.Embed(
        title="📧 Email temporaire",
        description=(
            f"# `{address}`\n\n"
            f"📋 Copie cette adresse et utilise-la pour t'inscrire.\n\n"
            f"⏳ **Le bot cherche automatiquement le code...**\n"
            f"Tu recevras le code ici dès qu'il arrive.\n\n"
            f"⚠️ **PAS DE CODE ? ANNULE ET PRENDS-EN UN AUTRE**\n\n"
            f"visible uniquement par toi."
        ),
        color=COLOR_MAIL,
    )
    actions_view = EmailActionsView(address, email_hash, uid)
    await interaction.followup.send(embed=embed_email, view=actions_view, ephemeral=True)

    # DM
    try:
        await user.send(f"📧 **Email temporaire** — Ton adresse : `{address}`")
    except discord.Forbidden:
        pass

    await log_email_pris(user, address)

    # ── Polling automatique du code ──
    code = None
    sender = ""
    subject = ""
    start = asyncio.get_event_loop().time()

    while asyncio.get_event_loop().time() - start < POLL_TIMEOUT:
        if cancel_event.is_set():
            break
        await asyncio.sleep(POLL_INTERVAL)
        if cancel_event.is_set():
            break

        messages = await tempmail_get_messages(email_hash)
        if not messages:
            continue

        # Chercher un code dans chaque mail
        for msg in messages:
            text = msg.get("mail_text_only", "") or msg.get("mail_text", "") or ""
            html = msg.get("mail_html", "") or ""
            html_text = re.sub(r"<[^>]+>", " ", html) if html else ""

            found = extract_code(text) or extract_code(html_text)
            if found:
                code = found
                sender = msg.get("mail_from", "inconnu")
                subject = msg.get("mail_subject", "")
                break

        if code:
            break

    _cancel_events.pop(uid, None)

    if code:
        daily_success[uid] += 1

        embed_code = discord.Embed(
            title="✅ Code reçu — Email",
            description=(
                f"# `{address}`\n\n"
                f"# 🔑  {code}\n\n"
                f"**De :** {sender}\n"
                f"**Objet :** {subject[:80]}\n\n"
                f"Copie le code ci-dessus.\n"
                f"visible uniquement par toi."
            ),
            color=COLOR_CODE,
        )
        await interaction.followup.send(embed=embed_code, ephemeral=True)

        try:
            await user.send(f"✅ **Code email reçu !**\n📧 `{address}`\n🔑 Code : **{code}**")
        except discord.Forbidden:
            pass

        await log_code_recu(user, address, code, sender, subject)

    elif not cancel_event.is_set():
        # Timeout — pas de code reçu
        embed_fail = discord.Embed(
            title="📧 Email temporaire",
            description=(
                f"# `{address}`\n\n"
                f"⏱️ Pas de code reçu après {POLL_TIMEOUT}s.\n\n"
                f"⚠️ **PAS DE CODE ? PRENDS-EN UN AUTRE**\n\n"
                f"visible uniquement par toi."
            ),
            color=COLOR_FAIL,
        )
        await interaction.followup.send(embed=embed_fail, ephemeral=True)

        try:
            await user.send(f"⏱️ **Pas de code email** — `{address}` — Reprends-en un autre.")
        except discord.Forbidden:
            pass

        await log_pas_de_code(user, address)


# ── PANNEAU PERSISTANT ──

class EmailPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📧  Prendre un email temporaire",
        style=discord.ButtonStyle.green,
        custom_id="tempmail_take_email",
    )
    async def take_email(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_email(interaction)


# ── COMMANDES SLASH ──

@bot.tree.command(name="installer_mail", description="Publie le panneau Email temporaire dans ce salon")
@app_commands.default_permissions(administrator=True)
async def cmd_installer(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📧  Email temporaire",
        description=(
            "Clique sur le bouton pour obtenir une adresse email temporaire.\n"
            "Le bot te donnera l'adresse et cherchera automatiquement le code de vérification."
        ),
        color=COLOR_MAIL,
    )
    await interaction.channel.send(embed=embed, view=EmailPanel())
    await interaction.response.send_message("✅ Panneau Email publié.", ephemeral=True)


@bot.tree.command(name="stats_mail", description="Emails pris aujourd'hui par VA")
@app_commands.default_permissions(administrator=True)
async def cmd_stats(interaction: discord.Interaction):
    _reset_if_new_day()
    if not daily_success and not daily_attempts:
        await interaction.response.send_message("Aucune activité aujourd'hui.", ephemeral=True)
        return
    lines = []
    for uid in sorted(set(daily_success) | set(daily_attempts)):
        m = interaction.guild.get_member(uid)
        name = m.display_name if m else str(uid)
        lines.append(f"**{name}** — {daily_success.get(uid, 0)} codes ✅ / {daily_attempts.get(uid, 0)} tentatives")
    embed = discord.Embed(
        title=f"📊  Stats Email — {dt.date.today().strftime('%d/%m/%Y')}",
        description="\n".join(lines),
        color=COLOR_MAIL,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="domaines_mail", description="Domaines disponibles sur temp-mail.org")
@app_commands.default_permissions(administrator=True)
async def cmd_domaines(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    domains = await tempmail_get_domains()
    if not domains:
        await interaction.followup.send("❌ Impossible de récupérer les domaines.", ephemeral=True)
        return
    text = "\n".join(f"`{d}`" for d in domains[:30])
    embed = discord.Embed(
        title=f"📋 Domaines disponibles ({len(domains)})",
        description=text,
        color=COLOR_MAIL,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── EVENTS ──

@bot.event
async def on_ready():
    global http_session
    http_session = aiohttp.ClientSession()
    bot.add_view(EmailPanel())
    if GUILD_IDS and GUILD_IDS != [0]:
        for gid in GUILD_IDS:
            g = discord.Object(id=gid)
            bot.tree.copy_global_to(guild=g)
            await bot.tree.sync(guild=g)
    else:
        await bot.tree.sync()
    log.info("Bot connecte : %s (ID %s)", bot.user, bot.user.id)


@bot.event
async def on_error(event, *args, **kwargs):
    log.exception("Erreur dans %s", event)


async def main():
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN manquant !")
        return
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
