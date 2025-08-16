# SecureBotPlus v1.4 - 全コマンド「!」/ 高機能ログ / 連投対策 / WL / 重大アクション即BAN / Spotlight
# New in v1.4:
#   - WL限定コマンドシステム
#       * 既定設定に "restricted_commands" を追加
#       * グローバルガード(@bot.check)でWL外の実行をブロック
#       * 管理コマンド: !cmdwl_add / !cmdwl_remove / !cmdwl_list / !cmdwl_clear
# 必要: Python 3.10+ / discord.py 2.4+ / Dev Portalで MESSAGE CONTENT & SERVER MEMBERS をON
# 起動例 (PowerShell):
#   cd C:\bots\securebot
#   $env:TOKEN="YOUR_BOT_TOKEN"
#   py .\secure_bot_plus.py

import os
import re
import json
import random
import asyncio
import os, re, json, io, difflib, random, asyncio
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque, OrderedDict
from typing import Optional, List, Tuple, Dict, Any

import discord
from discord.ext import commands

# ====== 環境 ======
TOKEN = os.getenv("TOKEN")
JST = timezone(timedelta(hours=9))

# ====== Intents ======
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.guild_messages = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ====== 永続設定 ======
DATA_DIR = os.getenv("DATA_DIR", ".")  # ← 環境変数で保存先を差し替え可能に
CONF_FILE = os.path.join(DATA_DIR, "security_conf.json")
def _load_conf() -> dict:
    if not os.path.exists(CONF_FILE):
        return {}
    try:
        with open(CONF_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_conf(data: dict):
    try:
        with open(CONF_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

_all_conf = _load_conf()

# ログ種別
LOG_KINDS = [
    "join", "message_delete", "message_edit", "voice",
    "channel_update", "member_update", "guild_update", "pins"
]

# ====== WL限定にする既定コマンド一覧（必要に応じて編集OK） ======
DEFAULT_RESTRICTED_COMMANDS = [
    # 通知・ログ周り
    "notify_set","notify_clear",
    "log_set","log_clear","log_toggle","log_showcontent","log_cache_size",

    # WL自体の操作
    "wl_add","wl_remove","wl_list","wlrole_add","wlrole_remove","wlrole_list",

    # セキュリティ強度
    "lockdown","captcha","verified_role","probation",
    "burst_set","burst_punish","burst_mute_role",
    "cooldown_set",

    # Spotlight 操作（運用に直結）
    "spotlight_source","spotlight_target","spotlight_label","spotlight_every",
    "spotlight_pick","spotlight_filter","spotlight_role","spotlight_role_clear",
    "spotlight_on","spotlight_off","spotlight_now",
    "spotlight_profile_save","spotlight_profile_load","spotlight_profile_use",
    "spotlight_profile_delete",

    # 閲覧系まで締めたい場合は↓を有効化
    # "spotlight_status","spotlight_profile_show","security_status","security_overview",
]

def default_guild_conf() -> dict:
    return {
        "notify_channel_id": None,
        "whitelist_users": [],
        "whitelist_roles": [],
        "captcha": {
            "enabled": True,
            "verified_role_name": "Verified",
            "quarantine_role_name": "Quarantine",
        },
        "lockdown": False,
        "probation_minutes": 10,
        "antispam": {
            "max_msgs_per_5s": 6,
            "max_urls_per_10s": 4,
            "max_mentions_per_msg": 5,
            "action": "quarantine",
        },
        "cooldown": {"role_name": "CooldownMuted", "duration_sec": 15 * 60, "notify": True},

        # 連投検知（≒1秒ペース×10）
        "burst_guard": {"count": 10, "window_sec": 10, "spacing_min": 0.7, "spacing_max": 1.6},
        # 連投時の処罰（ロール剥奪＋ミュート付与 or クールダウン）
        "burst_punish": {"mode": "strip_and_mute", "mute_role_name": "Muted", "notify": True},

        "hard_ban_actions": True,

        # ログ設定
        "logs": {
            "channels": {},
            "enabled": {k: True for k in LOG_KINDS},
            "include_content": {"message_delete": True, "message_edit": True},
            "message_cache_size": 300
        },

        # Spotlight（今日の○○）
        "spotlight": {
            "enabled": False,
            "source_channel_id": None,     # ネタ元
            "post_channel_id": None,       # 投稿先（未設定は notify）
            "label": "投稿",                # ○○ の文言
            "interval_sec": 24*3600,       # 実行間隔（秒）
            "next_run_ts": None,           # 次回実行（UTC epoch秒）

            # 拾う種類: text / image / text_or_image / text_and_image
            "pick": "text_or_image",
            # キーワード絞り込み: {"mode": None| "contains" | "regex", "query": str|None}
            "filter": {"mode": None, "query": None},
            # 投稿者の必須ロール（@メンバー等）
            "required_role_id": None,
        },

        # --- Spotlight Profiles ---
        "spotlight_profiles": {},           # 名前: 設定スナップショット
        "spotlight_active_profile": None,   # 現在アクティブなプロファイル名

        # ★ 追加：WL限定にするコマンド名一覧（bot.commands の name）
        "restricted_commands": list(DEFAULT_RESTRICTED_COMMANDS),
    }

def guild_conf(gid: int) -> dict:
    if str(gid) not in _all_conf:
        _all_conf[str(gid)] = default_guild_conf()
        _save_conf(_all_conf)
    else:
        base = default_guild_conf()
        merged = base
        merged.update(_all_conf[str(gid)])
        for k in ("captcha","antispam","cooldown","burst_guard","burst_punish","logs","spotlight"):
            if isinstance(base.get(k), dict) and isinstance(_all_conf[str(gid)].get(k), dict):
                tmp = base[k]
                tmp.update(_all_conf[str(gid)][k])
                merged[k] = tmp
        # プロファイル領域が無ければ追加
        if "spotlight_profiles" not in merged: merged["spotlight_profiles"] = {}
        if "spotlight_active_profile" not in merged: merged["spotlight_active_profile"] = None
        if "restricted_commands" not in merged: merged["restricted_commands"] = list(DEFAULT_RESTRICTED_COMMANDS)
        _all_conf[str(gid)] = merged
    return _all_conf[str(gid)]

def update_conf(gid: int, conf: dict):
    _all_conf[str(gid)] = conf
    _save_conf(_all_conf)

# ====== ユーティリティ ======
INVITE_RE = re.compile(r"(?:discord\.gg/|discord\.com/invite/)", re.IGNORECASE)
URL_RE = re.compile(r"https?://", re.IGNORECASE)

def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator

def is_whitelisted(member: discord.Member, conf: dict) -> bool:
    if is_admin(member): return True
    if member.id in conf.get('whitelist_users', []): return True
    wl_role_ids = set(conf.get('whitelist_roles', []))
    if wl_role_ids and any(role.id in wl_role_ids for role in member.roles): return True
    return False

async def get_notify_channel(guild: discord.Guild, conf: dict) -> Optional[discord.TextChannel]:
    ch = None
    if conf.get('notify_channel_id'):
        ch = guild.get_channel(conf['notify_channel_id'])
    if ch is None:
        ch = guild.system_channel
    if ch is None:
        for c in guild.text_channels:
            if c.permissions_for(guild.me).send_messages:
                ch = c; break
    return ch

def log_enabled(conf: dict, kind: str) -> bool:
    return conf.get('logs', {}).get('enabled', {}).get(kind, True)

def get_log_channel_obj(guild: discord.Guild, conf: dict, kind: str) -> Optional[discord.TextChannel]:
    ch_id = conf.get('logs', {}).get('channels', {}).get(kind) or conf.get('notify_channel_id')
    if not ch_id: return None
    return guild.get_channel(ch_id)  # type: ignore

async def send_log(guild: discord.Guild, kind: str, embed: discord.Embed, content: Optional[str] = None):
    conf = guild_conf(guild.id)
    if not log_enabled(conf, kind): return
    ch = get_log_channel_obj(guild, conf, kind) or await get_notify_channel(guild, conf)
    if not ch: return
    try:
        if content and embed: await ch.send(content, embed=embed)
        elif embed:          await ch.send(embed=embed)
        else:                await ch.send(content or "")
    except discord.Forbidden:
        pass

async def notify(guild: discord.Guild, content: Optional[str] = None, embed: Optional[discord.Embed] = None):
    ch = await get_notify_channel(guild, guild_conf(guild.id))
    if not ch: return
    try:
        if embed and content: await ch.send(content, embed=embed)
        elif embed:           await ch.send(embed=embed)
        elif content:         await ch.send(content)
    except discord.Forbidden:
        pass

async def ensure_role(guild: discord.Guild, role_name: str, send_lock: bool = True) -> Optional[discord.Role]:
    role = discord.utils.get(guild.roles, name=role_name)
    if role is None:
        try:
            role = await guild.create_role(name=role_name, reason="Secure role auto-create")
        except discord.Forbidden:
            return None
    if send_lock:
        tasks = []
        for ch in guild.channels:
            try:
                overwrite = ch.overwrites_for(role)
                changed = False
                if isinstance(ch, (discord.TextChannel, discord.Thread, discord.ForumChannel)):
                    if overwrite.send_messages is not False:
                        overwrite.send_messages = False; changed = True
                if changed:
                    tasks.append(ch.set_permissions(role, overwrite=overwrite, reason="SecureBotPlus: send lock"))
            except Exception:
                pass
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
    return role

async def strip_all_roles(member: discord.Member, reason: str):
    removable = [r for r in member.roles if r != member.guild.default_role and r < member.guild.me.top_role]
    if removable:
        try: await member.remove_roles(*removable, reason=f"SecureBotPlus: strip ({reason})")
        except discord.Forbidden: pass

async def punish(member: discord.Member, mode: str, reason: str):
    if mode == "ban":
        try: await member.ban(reason=f"SecureBotPlus: {reason}", delete_message_days=1)
        except discord.Forbidden: pass
    elif mode == "kick":
        try: await member.kick(reason=f"SecureBotPlus: {reason}")
        except discord.Forbidden: pass
    elif mode == "strip":
        await strip_all_roles(member, reason)
    elif mode == "quarantine":
        role = await ensure_role(member.guild, "Quarantined", send_lock=True)
        if role:
            try: await member.add_roles(role, reason=f"SecureBotPlus: {reason}")
            except discord.Forbidden: pass

def role_is_dangerous(role: discord.Role) -> bool:
    p = role.permissions
    return any([p.administrator, p.manage_guild, p.manage_roles, p.manage_channels,
                p.ban_members, p.kick_members, p.mention_everyone, p.manage_messages,
                p.view_audit_log, p.manage_webhooks])

# ====== 連投時の新処罰：全ロール剥奪＋ミュート付与 ======
async def burst_strip_and_mute(member: discord.Member, conf: dict):
    await strip_all_roles(member, "Burst spam (strip + mute)")
    bp = conf.get('burst_punish', {}) or {}
    mute_role_name = bp.get('mute_role_name', "Muted")
    mute_role = await ensure_role(member.guild, mute_role_name, send_lock=True)
    if mute_role:
        try: await member.add_roles(mute_role, reason="Burst spam (mute)")
        except discord.Forbidden: pass
    if bp.get('notify', True):
        emb = discord.Embed(title="🔇 バースト連投を検知",
                            description=f"{member.mention} を **ロール全剥奪**＋ **{mute_role_name} 付与** しました。",
                            color=0x8A2BE2)
        await notify(member.guild, embed=emb)

# ====== メッセージキャッシュ ======
class MsgCache:
    def __init__(self, capacity: int = 300):
        self.capacity = capacity
        self.od: "OrderedDict[int, Dict[str, Any]]" = OrderedDict()
    def set_capacity(self, n: int):
        self.capacity = max(50, int(n))
        while len(self.od) > self.capacity: self.od.popitem(last=False)
    def put(self, message: discord.Message):
        if not message.guild or message.author.bot: return
        attachments = [{"id": a.id, "filename": a.filename, "size": a.size,
                        "content_type": a.content_type, "url": a.url,
                        "is_image": (a.content_type or "").startswith("image/") or str(a.filename).lower().endswith((".png",".jpg",".jpeg",".gif",".webp"))}
                       for a in message.attachments]
        data = {"guild_id": message.guild.id, "channel_id": message.channel.id,
                "author_id": message.author.id, "content": message.content or "",
                "created_at": message.created_at.isoformat(), "attachments": attachments}
        mid = message.id
        if mid in self.od: self.od.move_to_end(mid)
        self.od[mid] = data
        if len(self.od) > self.capacity: self.od.popitem(last=False)
    def get(self, message_id: int) -> Optional[Dict[str, Any]]:
        data = self.od.get(message_id)
        if data: self.od.move_to_end(message_id)
        return data

MSG_CACHE = MsgCache()

# ====== CAPTCHA / 参加直後管理 ======
pending_captcha: dict[Tuple[int,int], str] = {}

async def send_captcha(member: discord.Member, conf: dict):
    if not conf["captcha"]["enabled"]: return
    code = str(1000 + (member.id % 9000))
    pending_captcha[(member.guild.id, member.id)] = code
    try:
        await member.send(f"ようこそ **{member.guild.name}** へ！\n"
                          f"本人確認のため、このメッセージに **{code}** と返信してください（10分以内）。")
    except Exception:
        pass

async def pass_captcha(member: discord.Member, conf: dict):
    pending_captcha.pop((member.guild.id, member.id), None)
    role_name = conf["captcha"]["verified_role_name"]
    role = await ensure_role(member.guild, role_name, send_lock=False)
    if role:
        try: await member.add_roles(role, reason="CAPTCHA passed")
        except discord.Forbidden: pass

async def captcha_watchdog(member: discord.Member, conf: dict):
    await asyncio.sleep(10*60)
    if (member.guild.id, member.id) in pending_captcha:
        await punish(member, "quarantine", "CAPTCHA not completed")

# ====== クールダウン ======
burst_msg_times: defaultdict[Tuple[int,int], deque] = defaultdict(lambda: deque())
cooldown_until: dict[Tuple[int,int], datetime] = {}

def _is_in_cooldown(gid: int, uid: int) -> bool:
    until = cooldown_until.get((gid, uid))
    return bool(until and until > datetime.now(timezone.utc))

def _one_per_second_like(ts: List[datetime], min_gap: float, max_gap: float) -> bool:
    if len(ts) < 2: return False
    for i in range(1, len(ts)):
        gap = (ts[i]-ts[i-1]).total_seconds()
        if gap < min_gap or gap > max_gap: return False
    return True

async def _schedule_cooldown_clear(guild_id: int, user_id: int, role_name: str, until: datetime):
    try:
        while True:
            now = datetime.now(timezone.utc)
            if now >= until: break
            await asyncio.sleep(min(60, (until-now).total_seconds()))
    finally:
        cooldown_until.pop((guild_id, user_id), None)
        guild = bot.get_guild(guild_id)
        if not guild: return
        member = guild.get_member(user_id)
        if not member: return
        role = discord.utils.get(guild.roles, name=role_name)
        if role and role in member.roles:
            try: await member.remove_roles(role, reason="Cooldown ended")
            except discord.Forbidden: pass

async def start_cooldown(member: discord.Member, conf: dict, reason: str = "Burst spam (≈1s pace x N)"):
    cd = conf.get("cooldown", {})
    duration = int(cd.get("duration_sec", 15*60))
    role_name = cd.get("role_name", "CooldownMuted")
    until = datetime.now(timezone.utc) + timedelta(seconds=duration)
    cooldown_until[(member.guild.id, member.id)] = until
    role = await ensure_role(member.guild, role_name, send_lock=True)
    if role:
        try: await member.add_roles(role, reason=reason)
        except discord.Forbidden: pass
    if cd.get("notify", True):
        jst = until.astimezone(JST).strftime("%H:%M")
        emb = discord.Embed(title="⏳ クールダウン開始",
                            color=0x778899,
                            description=f"{member.mention} さんはメッセージ送信を**{duration//60}分**制限中。(解除予定: {jst} JST)")
        await notify(member.guild, embed=emb)
    asyncio.create_task(_schedule_cooldown_clear(member.guild.id, member.id, role_name, until))

# ====== 監査ログ：重大アクション即処罰 ======
@bot.event
async def on_audit_log_entry_create(entry: discord.AuditLogEntry):
    guild = entry.guild
    if guild is None: return
    conf = guild_conf(guild.id)
    if not conf.get("hard_ban_actions", True): return

    executor = entry.user
    if not isinstance(executor, discord.Member):
        try: executor = await guild.fetch_member(executor.id)  # type: ignore
        except Exception: return
    if is_whitelisted(executor, conf): return

    act = entry.action
    BAN_ACTIONS = {
        discord.AuditLogAction.channel_create, discord.AuditLogAction.channel_delete, discord.AuditLogAction.channel_update,
        discord.AuditLogAction.overwrite_create, discord.AuditLogAction.overwrite_delete, discord.AuditLogAction.overwrite_update,
        discord.AuditLogAction.role_create, discord.AuditLogAction.role_delete, discord.AuditLogAction.role_update,
        discord.AuditLogAction.guild_update, discord.AuditLogAction.kick, discord.AuditLogAction.ban,
    }
    KICK_ACTIONS = {discord.AuditLogAction.bot_add}

    if act == discord.AuditLogAction.member_role_update:
        try:
            target = entry.target  # type: ignore
            if isinstance(target, discord.Member):
                dangerous = [r for r in target.roles if role_is_dangerous(r)]
                if dangerous:
                    await punish(executor, "ban", "Dangerous role granted to a member")
                    emb = discord.Embed(title="🚫 BAN: 危険ロールの付与", color=0xFF0000,
                                        description=f"{executor.mention} → {target.mention}\n付与例: " + ", ".join(r.name for r in dangerous[:3]))
                    await notify(guild, embed=emb)
                    return
        except Exception:
            pass

    if act in BAN_ACTIONS:
        await punish(executor, "ban", f"Hard-ban action detected: {act.name}")
        emb = discord.Embed(title="🚫 BAN: 重大アクション実行",
                            color=0xDC143C, description=f"{executor.mention} による `{act.name}` を検知しました。")
        await send_log(guild, "guild_update", emb); await notify(guild, embed=emb); return

    if act in KICK_ACTIONS:
        await punish(executor, "kick", f"Bot added: {act.name}")
        emb = discord.Embed(title="⛔ KICK: Bot追加の実行者",
                            color=0xB22222, description=f"{executor.mention} がBotを追加しました。")
        await send_log(guild, "guild_update", emb); await notify(guild, embed=emb); return

# ====== 削除ログ（実行者推定） ======
async def _guess_deleter_by_audit(guild: discord.Guild, channel: discord.abc.GuildChannel, author_id: Optional[int]) -> Optional[discord.Member]:
    try:
        async for e in guild.audit_logs(action=discord.AuditLogAction.message_delete, limit=6):
            if (datetime.now(timezone.utc)-e.created_at).total_seconds() > 5: break
            extra = getattr(e, "extra", None)
            if not extra or getattr(extra, "channel", None) is None: continue
            if extra.channel.id != channel.id: continue
            if author_id is not None:
                target = getattr(e, "target", None)
                if not target or getattr(target, "id", None) != author_id: continue
            deleter = e.user
            if isinstance(deleter, discord.Member): return deleter
            try: return await guild.fetch_member(deleter.id)  # type: ignore
            except Exception: return None
    except discord.Forbidden:
        return None
    except Exception:
        return None
    return None

# ====== 参加ログ ======
@bot.event
async def on_member_join(member: discord.Member):
    conf = guild_conf(member.guild.id)
    await send_captcha(member, conf)
    asyncio.create_task(captcha_watchdog(member, conf))

    created = member.created_at.astimezone(JST)
    age_days = (datetime.now(JST) - created).days
    emb = discord.Embed(title="👋 メンバー参加", color=0x2E8B57, timestamp=datetime.now(timezone.utc))
    emb.add_field(name="ユーザー", value=f"{member.mention} (`{member.id}`)", inline=False)
    emb.add_field(name="アカウント作成", value=f"{created:%Y-%m-%d %H:%M JST}", inline=True)
    emb.add_field(name="経過", value=f"約 {age_days} 日", inline=True)
    await send_log(member.guild, "join", emb)

# ====== メッセージ監視 ======
user_msg_timestamps: defaultdict[Tuple[int,int], deque] = defaultdict(lambda: deque())

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot: return

    # DMでCAPTCHA応答
    if isinstance(message.channel, discord.DMChannel):
        text = (message.content or "").strip()
        if not text: return
        to_pass = None
        for (gid, uid), code in list(pending_captcha.items()):
            if uid == message.author.id and code == text: to_pass = (gid, uid); break
        if to_pass:
            gid, uid = to_pass
            guild = bot.get_guild(gid); member = None
            if guild:
                member = guild.get_member(uid) or await guild.fetch_member(uid)
                if member:
                    await pass_captcha(member, guild_conf(gid))
                    try: await message.channel.send(f"✅ **{guild.name}** の認証に成功しました。ようこそ！")
                    except Exception: pass
            pending_captcha.pop((gid, uid), None)
        return

    if not message.guild: return

    conf = guild_conf(message.guild.id)
    MSG_CACHE.set_capacity(conf.get("logs", {}).get("message_cache_size", 300))
    MSG_CACHE.put(message)

    author: discord.Member = message.author  # type: ignore

    if is_whitelisted(author, conf):
        return await bot.process_commands(message)

    if _is_in_cooldown(message.guild.id, author.id):
        try: await message.delete()
        except discord.Forbidden: pass
        return

    if conf.get("lockdown"):
        vname = conf["captcha"]["verified_role_name"]
        ver = discord.utils.get(author.roles, name=vname)
        if ver is None:
            await punish(author, "quarantine", "Server lockdown")
            try: await message.delete()
            except discord.Forbidden: pass
            return

    content = message.content or ""

    # 招待リンク → BAN
    if INVITE_RE.search(content):
        await punish(author, "ban", "Invite link posted")
        preview = content[:250] + ("…" if len(content)>250 else "")
        await notify(message.guild, embed=discord.Embed(
            title="🚫 BAN: 招待リンク送信",
            description=f"{author.mention}\n`{preview}`",
            color=0xFF0000
        ))
        try: await message.delete()
        except discord.Forbidden: pass
        return

    # 大量メンション → ロール剥奪
    if len(message.mentions) >= 4:
        await strip_all_roles(author, "Mass mention (>=4)")
        await notify(message.guild, embed=discord.Embed(
            title="⚠️ ロール剥奪: 大量メンション",
            description=f"{author.mention} / 人数: {len(message.mentions)}",
            color=0xFFA500
        ))

    # 1秒ペース×10 → 罰
    burst = conf.get("burst_guard", {})
    count = int(burst.get("count", 10)); window = int(burst.get("window_sec", 10))
    min_gap = float(burst.get("spacing_min", 0.7)); max_gap = float(burst.get("spacing_max", 1.6))

    key = (message.guild.id, author.id); now = datetime.now(timezone.utc)
    dq = burst_msg_times[key]; dq.append(now)
    cutoff = now - timedelta(seconds=window + 2)
    while dq and dq[0] < cutoff: dq.popleft()

    if len(dq) >= count:
        lastN = list(dq)[-count:]
        if (lastN[-1]-lastN[0]).total_seconds() <= window + 1.5 and _one_per_second_like(lastN, min_gap, max_gap):
            try: await message.delete()
            except discord.Forbidden: pass
            bp = conf.get("burst_punish", {}) or {}
            if bp.get("mode","strip_and_mute") == "cooldown":
                await start_cooldown(author, conf, reason="Burst spam detected")
            else:
                await burst_strip_and_mute(author, conf)
            return

    # 参加直後の厳格監視
    joined_ago = (datetime.now(timezone.utc) - author.joined_at) if author.joined_at else timedelta.max
    if joined_ago <= timedelta(minutes=conf.get("probation_minutes", 10)):
        dq2 = user_msg_timestamps[(message.guild.id, author.id)]
        dq2.append(now); cutoff2 = now - timedelta(seconds=5)
        while dq2 and dq2[0] < cutoff2: dq2.popleft()
        too_fast = len(dq2) > conf["antispam"]["max_msgs_per_5s"]
        url_count = len(URL_RE.findall(content))
        mention_count = len(message.mentions) + len(message.role_mentions)
        mass_mention = message.mention_everyone
        bad = (url_count >= conf["antispam"]["max_urls_per_10s"] or
               mention_count > conf["antispam"]["max_mentions_per_msg"] or
               mass_mention or too_fast)
        if bad:
            try: await message.delete()
            except discord.Forbidden: pass
            await punish(author, "quarantine", "Antispam: probation violation")
            return

    await bot.process_commands(message)

# ====== メッセージ削除/編集ログ・ボイス・権限差分 ======
@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild: return
    conf = guild_conf(message.guild.id)
    if not log_enabled(conf, "message_delete"): return
    ch = message.channel; lg_inc = conf["logs"]["include_content"].get("message_delete", True)
    author = getattr(message, "author", None)
    attach_info = [f"[{a.filename}]({a.url})" for a in message.attachments]
    deleter = await _guess_deleter_by_audit(message.guild, ch, getattr(author, "id", None))
    desc = f"チャンネル: {ch.mention}\n"
    if author: desc += f"投稿者: {author.mention} (`{author.id}`)\n"
    if lg_inc:
        if message.content:
            snippet = message.content if len(message.content) <= 800 else message.content[:800] + "…"
            desc += f"本文: ```\n{snippet}\n```\n"
        if attach_info: desc += "添付: " + ", ".join(attach_info) + "\n"
    if deleter: desc += f"削除実行者(推定): {deleter.mention}\n"
    emb = discord.Embed(title="🗑️ メッセージ削除", description=desc, color=0x696969, timestamp=datetime.now(timezone.utc))
    await send_log(message.guild, "message_delete", emb)

@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if payload.guild_id is None: return
    guild = bot.get_guild(payload.guild_id)
    if not guild: return
    conf = guild_conf(guild.id)
    if not log_enabled(conf, "message_delete"): return
    data = MSG_CACHE.get(payload.message_id)
    desc = f"チャンネル: <#{payload.channel_id}>\nメッセージID: `{payload.message_id}`\n"
    if data:
        author_id = data.get("author_id")
        if author_id:
            m = guild.get_member(author_id)
            desc += f"投稿者: {(m.mention if m else f'`{author_id}`')}\n"
        if conf["logs"]["include_content"].get("message_delete", True):
            cont = data.get("content") or ""
            if cont:
                snippet = cont if len(cont) <= 800 else cont[:800] + "…"
                desc += f"本文(キャッシュ): ```\n{snippet}\n```\n"
            atts = data.get("attachments") or []
            if atts:
                links = [f"[{a['filename']}]({a['url']})" for a in atts]
                desc += "添付(キャッシュ): " + ", ".join(links) + "\n"
        # 削除実行者推定
        ch_obj = guild.get_channel(payload.channel_id)
        if isinstance(ch_obj, discord.TextChannel):
            deleter = await _guess_deleter_by_audit(guild, ch_obj, author_id)
            if deleter: desc += f"削除実行者(推定): {deleter.mention}\n"
    emb = discord.Embed(title="🗑️ メッセージ削除（未キャッシュ）", description=desc, color=0x808080, timestamp=datetime.now(timezone.utc))
    await send_log(guild, "message_delete", emb)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not after.guild: return
    conf = guild_conf(after.guild.id)
    if not log_enabled(conf, "message_edit"): return
    if before.author.bot: return
    if before.content == after.content: return
    MSG_CACHE.put(after)
    desc = f"チャンネル: {after.channel.mention}\n投稿者: {after.author.mention} (`{after.author.id}`)\n"
    if conf["logs"]["include_content"].get("message_edit", True):
        old = before.content or ""; new = after.content or ""
        old_snip = old if len(old) <= 800 else old[:800] + "…"
        new_snip = new if len(new) <= 800 else new[:800] + "…"
        desc += f"**編集前:** ```\n{old_snip}\n```\n**編集後:** ```\n{new_snip}\n```"
    jump = f"[メッセージへ]({after.jump_url})"
    emb = discord.Embed(title="✏️ メッセージ編集", description=desc, color=0x1E90FF, timestamp=datetime.now(timezone.utc))
    emb.add_field(name="リンク", value=jump, inline=False)
    await send_log(after.guild, "message_edit", emb)

voice_sessions: dict[Tuple[int,int], Tuple[int, datetime]] = {}

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    conf = guild_conf(guild.id)
    if not log_enabled(conf, "voice"): return

    if before.channel != after.channel:
        now = datetime.now(timezone.utc)
        if before.channel and not after.channel:
            key = (guild.id, member.id)
            sess = voice_sessions.pop(key, None)
            dur_txt = ""
            if sess:
                dur = now - sess[1]
                mins = int(dur.total_seconds()//60); secs = int(dur.total_seconds()%60)
                dur_txt = f"（滞在: {mins}分{secs}秒）"
            emb = discord.Embed(title="👋 ボイス退室",
                                description=f"{member.mention} が **{before.channel.name}** を退出 {dur_txt}",
                                color=0x708090, timestamp=now)
            await send_log(guild, "voice", emb)
        elif not before.channel and after.channel:
            voice_sessions[(guild.id, member.id)] = (after.channel.id, datetime.now(timezone.utc))
            emb = discord.Embed(title="🎧 ボイス入室",
                                description=f"{member.mention} が **{after.channel.name}** に参加",
                                color=0x2E8B57, timestamp=datetime.now(timezone.utc))
            await send_log(guild, "voice", emb)
        else:
            voice_sessions[(guild.id, member.id)] = (after.channel.id, datetime.now(timezone.utc))
            emb = discord.Embed(title="🔀 ボイス移動",
                                description=f"{member.mention} : **{before.channel.name}** → **{after.channel.name}**",
                                color=0x20B2AA, timestamp=datetime.now(timezone.utc))
            await send_log(guild, "voice", emb)

    flags = []
    if before.self_mute != after.self_mute: flags.append(f"Self Mute: {'ON' if after.self_mute else 'OFF'}")
    if before.self_deaf != after.self_deaf: flags.append(f"Self Deaf: {'ON' if after.self_deaf else 'OFF'}")
    if before.mute != after.mute:           flags.append(f"Server Mute: {'ON' if after.mute else 'OFF'}")
    if before.deaf != after.deaf:           flags.append(f"Server Deaf: {'ON' if after.deaf else 'OFF'}")
    if before.self_video != after.self_video: flags.append(f"Video: {'ON' if after.self_video else 'OFF'}")
    if before.streaming != after.streaming:   flags.append(f"Streaming: {'ON' if after.streaming else 'OFF'}")
    if flags and (after.channel or before.channel):
        name = after.channel.name if after.channel else before.channel.name
        emb = discord.Embed(title="🎛️ ボイス状態変更",
                            description=f"{member.mention} @ **{name}**\n" + "\n".join(flags),
                            color=0x6A5ACD, timestamp=datetime.now(timezone.utc))
        await send_log(guild, "voice", emb)

# ====== チャンネル更新（権限差分） ======
def _iter_overwrites(ch: discord.abc.GuildChannel):
    ow = ch.overwrites
    return list(ow.items()) if isinstance(ow, dict) else list(ow)

def _ow_to_state(ow: discord.PermissionOverwrite) -> Dict[str, Optional[bool]]:
    return {k: getattr(ow, k) for k in dir(ow) if not k.startswith("_") and isinstance(getattr(ow, k), (bool, type(None)))}

@bot.event
async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
    guild = after.guild
    conf = guild_conf(guild.id)
    if not log_enabled(conf, "channel_update"): return

    changes = []
    if hasattr(before, "name") and before.name != after.name:
        changes.append(f"名称: `{before.name}` → `{after.name}`")
    if isinstance(before, discord.TextChannel) and isinstance(after, discord.TextChannel):
        if before.topic != after.topic:
            changes.append(f"トピック変更:\n旧: `{before.topic or '（なし）'}`\n新: `{after.topic or '（なし）'}`")
        if before.slowmode_delay != after.slowmode_delay: changes.append(f"スローモード: {before.slowmode_delay}s → {after.slowmode_delay}s")
        if before.nsfw != after.nsfw:                     changes.append(f"NSFW: {'ON' if after.nsfw else 'OFF'}")

    b_map = {t.id: (t, ow) for t, ow in _iter_overwrites(before)}  # type: ignore
    a_map = {t.id: (t, ow) for t, ow in _iter_overwrites(after)}   # type: ignore
    keys = set(b_map.keys()) | set(a_map.keys())
    diff_lines = []
    for k in keys:
        bt = b_map.get(k); at = a_map.get(k)
        b_ow = bt[1] if bt else discord.PermissionOverwrite()
        a_ow = at[1] if at else discord.PermissionOverwrite()
        b_state = _ow_to_state(b_ow); a_state = _ow_to_state(a_ow)
        changed = []
        for p in sorted(a_state.keys()):
            if b_state.get(p) != a_state.get(p):
                def fmt(v): return "✅許可" if v is True else ("❌拒否" if v is False else "—未設定")
                changed.append(f"{p}: {fmt(b_state.get(p))} → {fmt(a_state.get(p))}")
        if changed:
            target_obj = at[0] if at else (bt[0] if bt else None)
            if isinstance(target_obj, discord.Role): target_name = target_obj.mention
            elif isinstance(target_obj, discord.Member): target_name = target_obj.mention
            else: target_name = f"`{k}`"
            diff_lines.append(f"対象: {target_name}\n" + "\n".join("・"+c for c in changed))

    if not changes and not diff_lines: return
    emb = discord.Embed(title="🛠️ チャンネル更新",
                        description=f"{after.mention}（ID: `{after.id}`）",
                        color=0xDAA520, timestamp=datetime.now(timezone.utc))
    if changes: emb.add_field(name="基本変更", value="\n".join("・"+c for c in changes), inline=False)
    if diff_lines:
        text = "\n\n".join(diff_lines)
        if len(text) > 1000: text = text[:1000] + "…"
        emb.add_field(name="権限オーバーライドの差分", value=text, inline=False)
    await send_log(guild, "channel_update", emb)

# ====== メンバー更新 / サーバー更新 / ピン更新 ======
@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    guild = after.guild
    conf = guild_conf(guild.id)
    if not log_enabled(conf, "member_update"): return
    added = [r for r in after.roles if r not in before.roles and r != guild.default_role]
    removed = [r for r in before.roles if r not in after.roles and r != guild.default_role]
    nick_changed = before.nick != after.nick
    if not added and not removed and not nick_changed: return
    lines = []
    if added:   lines.append("付与: " + ", ".join(r.mention for r in added))
    if removed: lines.append("剥奪: " + ", ".join(r.mention for r in removed))
    if nick_changed: lines.append(f"ニックネーム: `{before.nick or '（なし）'}` → `{after.nick or '（なし）'}`")
    emb = discord.Embed(title="👤 メンバー更新",
                        description=f"{after.mention} (`{after.id}`)\n" + "\n".join(lines),
                        color=0x00CED1, timestamp=datetime.now(timezone.utc))
    await send_log(guild, "member_update", emb)

@bot.event
async def on_guild_update(before: discord.Guild, after: discord.Guild):
    conf = guild_conf(after.id)
    if not log_enabled(conf, "guild_update"): return
    changes = []
    if before.name != after.name: changes.append(f"サーバー名: `{before.name}` → `{after.name}`")
    if before.icon != after.icon: changes.append("サーバーアイコン: 更新されました")
    if not changes: return
    emb = discord.Embed(title="🏛️ サーバー更新", description="\n".join("・"+c for c in changes),
                        color=0xCD5C5C, timestamp=datetime.now(timezone.utc))
    await send_log(after, "guild_update", emb)

@bot.event
async def on_guild_channel_pins_update(channel: discord.abc.GuildChannel, last_pin: Optional[datetime]):
    guild = channel.guild
    conf = guild_conf(guild.id)
    if not log_enabled(conf, "pins"): return
    when = last_pin.astimezone(JST).strftime("%Y-%m-%d %H:%M JST") if last_pin else "不明"
    emb = discord.Embed(title="📌 ピン留め更新",
                        description=f"{channel.mention} / 最終ピン: {when}",
                        color=0xFF8C00, timestamp=datetime.now(timezone.utc))
    await send_log(guild, "pins", emb)

# ==================== Spotlight（今日の○○） ====================

spotlight_tasks: Dict[int, asyncio.Task] = {}

def _parse_interval_to_sec(text: str) -> Optional[int]:
    s = text.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days)", s)
    if not m: return None
    n = int(m.group(1)); u = m.group(2)
    if u in ("s","sec","secs"): return n
    if u in ("m","min","mins"): return n*60
    if u in ("h","hr","hrs"):   return n*3600
    if u in ("d","day","days"): return n*86400
    return None

def _has_text(msg: discord.Message) -> bool:
    return bool((msg.content or "").strip())

def _has_image(msg: discord.Message) -> bool:
    for a in msg.attachments:
        ct = (a.content_type or "").lower()
        name = str(a.filename).lower()
        if ct.startswith("image/") or name.endswith((".png",".jpg",".jpeg",".gif",".webp")):
            return True
    return False

def _match_filter(msg: discord.Message, mode: Optional[str], query: Optional[str]) -> bool:
    if not mode or not query:
        return True
    text = (msg.content or "")
    if mode == "contains":
        return query.lower() in text.lower()
    if mode == "regex":
        try:
            return re.search(query, text, re.IGNORECASE | re.DOTALL) is not None
        except re.error:
            return False
    return True

def _pass_pick_mode(msg: discord.Message, pick: str) -> bool:
    t = _has_text(msg); i = _has_image(msg)
    if pick == "text": return t
    if pick == "image": return i
    if pick == "text_or_image": return (t or i)
    if pick == "text_and_image": return (t and i)
    return (t or i)

def _author_has_role(guild: discord.Guild, user_id: int, role_id: int) -> bool:
    m = guild.get_member(user_id)
    if not m: return False
    return any(r.id == role_id for r in m.roles)

async def _spotlight_collect_candidates(ch: discord.TextChannel, pick: str, fmode: Optional[str], fquery: Optional[str],
                                        required_role_id: Optional[int],
                                        limit: int = 2000, cap: int = 400) -> List[discord.Message]:
    candidates: List[discord.Message] = []
    g = ch.guild
    try:
        async for m in ch.history(limit=limit, oldest_first=False):
            if m.author.bot:
                continue
            if required_role_id and not _author_has_role(g, m.author.id, required_role_id):
                continue
            if not _pass_pick_mode(m, pick):
                continue
            if not _match_filter(m, fmode, fquery):
                continue
            if not (_has_text(m) or m.attachments):
                continue
            candidates.append(m)
            if len(candidates) >= cap:
                break
    except discord.Forbidden:
        return []
    return candidates

def _spotlight_build_embed(msg: discord.Message, label: str) -> discord.Embed:
    created = msg.created_at.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    desc = f"投稿者: {msg.author.mention} (`{msg.author.id}`)\n場所: {msg.channel.mention}\n日時: {created}\n\n"
    if _has_text(msg):
        snippet = msg.content if len(msg.content) <= 900 else msg.content[:900] + "…"
        desc += f"本文: ```\n{snippet}\n```"
    emb = discord.Embed(title=f"🎯 今日の{label}はこれ！", description=desc, color=0x00BFFF)
    if _has_image(msg):
        for a in msg.attachments:
            ct = (a.content_type or "").lower(); name = str(a.filename).lower()
            if ct.startswith("image/") or name.endswith((".png",".jpg",".jpeg",".gif",".webp")):
                emb.set_image(url=a.url); break
    if msg.attachments:
        links = ", ".join(f"[{a.filename}]({a.url})" for a in msg.attachments)
        if links:
            emb.add_field(name="添付", value=links[:1000] + ("…" if len(links)>1000 else ""), inline=False)
    emb.add_field(name="元メッセージ", value=f"[ジャンプ]({msg.jump_url})", inline=False)
    return emb

async def _spotlight_run_once(guild_id: int, override_contains: Optional[str] = None):
    guild = bot.get_guild(guild_id)
    if not guild: return
    conf = guild_conf(guild_id)
    sp = conf.get("spotlight", {})
    src_id = sp.get("source_channel_id")
    dst_id = sp.get("post_channel_id") or conf.get("notify_channel_id")
    if not src_id or not dst_id: return
    src = guild.get_channel(src_id); dst = guild.get_channel(dst_id)
    if not isinstance(src, discord.TextChannel) or not isinstance(dst, discord.TextChannel): return

    pick = sp.get("pick", "text_or_image")
    fmode = sp.get("filter", {}).get("mode")
    fquery = sp.get("filter", {}).get("query")
    required_role_id = sp.get("required_role_id")
    if override_contains:
        fmode, fquery = "contains", override_contains

    cand = await _spotlight_collect_candidates(src, pick, fmode, fquery, required_role_id)
    if not cand:
        try:
            role_txt = (f"<@&{required_role_id}>" if required_role_id else "なし")
            msg = f"（Spotlight）条件に合う投稿が見つかりませんでした。\nソース: {src.mention} / pick={pick} / filter={fmode or 'なし'} / role={role_txt}"
            await dst.send(msg)
        except Exception: pass
        return
    chosen = random.choice(cand)
    emb = _spotlight_build_embed(chosen, sp.get("label", "投稿"))
    try:
        await dst.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        pass

async def _spotlight_worker(guild_id: int):
    try:
        while True:
            guild = bot.get_guild(guild_id)
            if not guild: await asyncio.sleep(60); continue
            conf = guild_conf(guild_id)
            sp = conf.get("spotlight", {})
            if not sp.get("enabled", False):
                await asyncio.sleep(60); continue
            interval = int(sp.get("interval_sec", 86400))
            now_ts = datetime.now(timezone.utc).timestamp()
            nxt = sp.get("next_run_ts")
            if not nxt or now_ts >= nxt:
                await _spotlight_run_once(guild_id)
                sp["next_run_ts"] = (datetime.now(timezone.utc) + timedelta(seconds=interval)).timestamp()
                conf["spotlight"] = sp
                update_conf(guild_id, conf)
            await asyncio.sleep(30)
    except asyncio.CancelledError:
        return
    except Exception:
        await asyncio.sleep(60)

def _spotlight_restart_task(guild_id: int):
    t = spotlight_tasks.get(guild_id)
    if t and not t.done(): t.cancel()
    spotlight_tasks[guild_id] = asyncio.create_task(_spotlight_worker(guild_id))

# ==================== WL限定コマンド：グローバルガード ====================
@bot.check
async def _whitelist_command_gate(ctx: commands.Context):
    # DMや不明コマンドは対象外
    if not getattr(ctx, "guild", None) or not getattr(ctx, "command", None):
        return True
    conf = guild_conf(ctx.guild.id)
    restricted = set(conf.get("restricted_commands") or [])
    cmd_name = ctx.command.name
    if cmd_name in restricted and not is_whitelisted(ctx.author, conf):
        try:
            await ctx.reply(
                "🚫 このコマンドは**ホワイトリスト限定**です。\n権限のあるメンバーに依頼してください。",
                mention_author=False
            )
        except Exception:
            pass
        return False
    return True

# ==================== コマンド群（全部 "!"） ====================

def _need_manage_guild(ctx: commands.Context) -> bool:
    return bool(ctx.author.guild_permissions.manage_guild)

async def _deny_manage_guild(ctx: commands.Context):
    await ctx.reply("このコマンドには **Manage Server（サーバー管理）** 権限が必要です。", mention_author=False)

# ---- 基本 & デバッグ ----
@bot.command(name="ping")
async def ping(ctx: commands.Context): await ctx.reply("pong 🏓", mention_author=False)

@bot.command(name="hello")
async def hello_prefix(ctx: commands.Context): await ctx.reply("👋 動いてます！（prefix版）", mention_author=False)

@bot.command(name="version")
async def version_cmd(ctx: commands.Context): await ctx.reply("SecureBotPlus v1.4（WL限定コマンド搭載）", mention_author=False)

@bot.command(name="debug_perms")
async def debug_perms_cmd(ctx: commands.Context):
    me = ctx.guild.me  # type: ignore
    p = ctx.channel.permissions_for(me)  # type: ignore
    fields = [
        ("send_messages", p.send_messages),
        ("manage_messages", p.manage_messages),
        ("read_message_history", p.read_message_history),
        ("embed_links", p.embed_links),
        ("view_audit_log", p.view_audit_log),
        ("manage_channels", p.manage_channels),
        ("manage_roles", p.manage_roles),
        ("kick_members", p.kick_members),
        ("ban_members", p.ban_members),
    ]
    txt = "\n".join([f"{'✅' if v else '❌'} {k}" for k, v in fields])
    await ctx.reply(f"**Bot権限（このチャンネル）**\n{txt}", mention_author=False)

@bot.command(name="debug_intents")
async def debug_intents_cmd(ctx: commands.Context):
    i = bot.intents
    props = [("guilds", i.guilds), ("members", i.members), ("guild_messages", i.guild_messages),
             ("message_content", i.message_content), ("voice_states", i.voice_states)]
    txt = "\n".join([f"{'✅' if v else '❌'} {k}" for k, v in props])
    await ctx.reply(f"**Intents**\n{txt}", mention_author=False)

# ---- 通知チャンネル ----
@bot.command(name="notify_set")
async def notify_set_cmd(ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    if not ctx.guild: return
    conf = guild_conf(ctx.guild.id); channel = channel or ctx.channel  # type: ignore
    conf["notify_channel_id"] = channel.id; update_conf(ctx.guild.id, conf)
    await ctx.reply(f"🔔 通知先を {channel.mention} に設定しました。", mention_author=False)

@bot.command(name="notify_clear")
async def notify_clear_cmd(ctx: commands.Context):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf = guild_conf(ctx.guild.id); conf["notify_channel_id"] = None; update_conf(ctx.guild.id, conf)
    await ctx.reply("🔕 通知チャンネルの設定を解除しました。", mention_author=False)

# ---- ログ保存先/設定 ----
@bot.command(name="log_set")
async def log_set_cmd(ctx: commands.Context, kind: str, channel: Optional[discord.TextChannel] = None):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    if not ctx.guild: return
    kind = kind.lower(); target_ch = channel or ctx.channel  # type: ignore
    conf = guild_conf(ctx.guild.id)
    if kind == "all":
        for k in LOG_KINDS: conf["logs"]["channels"][k] = target_ch.id
        update_conf(ctx.guild.id, conf); return await ctx.reply(f"🧭 すべてのログ種別の保存先を {target_ch.mention} に設定しました。", mention_author=False)
    if kind not in LOG_KINDS: return await ctx.reply(f"未知の種別 `{kind}`。利用可能: {', '.join(LOG_KINDS)}", mention_author=False)
    conf["logs"]["channels"][kind] = target_ch.id; update_conf(ctx.guild.id, conf)
    await ctx.reply(f"🧭 ログ `{kind}` の保存先を {target_ch.mention} に設定しました。", mention_author=False)

@bot.command(name="log_clear")
async def log_clear_cmd(ctx: commands.Context, kind: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf = guild_conf(ctx.guild.id); kind = kind.lower()
    if kind == "all":
        conf["logs"]["channels"] = {}; update_conf(ctx.guild.id, conf)
        return await ctx.reply("🧭 すべてのログ保存先を解除しました（notify先にフォールバック）。", mention_author=False)
    if kind not in LOG_KINDS: return await ctx.reply(f"未知の種別 `{kind}`。利用可能: {', '.join(LOG_KINDS)}", mention_author=False)
    conf["logs"]["channels"].pop(kind, None); update_conf(ctx.guild.id, conf)
    await ctx.reply(f"🧭 ログ `{kind}` の保存先設定を解除しました。", mention_author=False)

@bot.command(name="log_toggle")
async def log_toggle_cmd(ctx: commands.Context, kind: str, mode: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf = guild_conf(ctx.guild.id); mode = mode.lower()
    if mode not in ("on","off"): return await ctx.reply("使い方: !log_toggle <kind|all> on|off", mention_author=False)
    if kind == "all":
        for k in LOG_KINDS: conf["logs"]["enabled"][k] = (mode == "on")
    else:
        if kind not in LOG_KINDS: return await ctx.reply(f"未知の種別 `{kind}`。利用可能: {', '.join(LOG_KINDS)}", mention_author=False)
        conf["logs"]["enabled"][kind] = (mode == "on")
    update_conf(ctx.guild.id, conf); await ctx.reply(f"✅ ログ `{kind}`: **{mode.upper()}**", mention_author=False)

@bot.command(name="log_showcontent")
async def log_showcontent_cmd(ctx: commands.Context, sub: str, mode: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    sub = sub.lower(); mode = mode.lower()
    if sub not in ("delete","edit") or mode not in ("on","off"):
        return await ctx.reply("使い方: !log_showcontent <delete|edit> on|off", mention_author=False)
    conf = guild_conf(ctx.guild.id); key = "message_delete" if sub == "delete" else "message_edit"
    conf["logs"]["include_content"][key] = (mode == "on"); update_conf(ctx.guild.id, conf)
    await ctx.reply(f"📝 `{key}` の本文表示: **{mode.upper()}**", mention_author=False)

@bot.command(name="log_cache_size")
async def log_cache_size_cmd(ctx: commands.Context, num: int):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf = guild_conf(ctx.guild.id); conf["logs"]["message_cache_size"] = max(50, int(num))
    update_conf(ctx.guild.id, conf); MSG_CACHE.set_capacity(conf["logs"]["message_cache_size"])
    await ctx.reply(f"🗄️ メッセージキャッシュ容量を **{conf['logs']['message_cache_size']}** に設定しました。", mention_author=False)

# ---- WL / Lockdown / CAPTCHA / しきい値 ----
@bot.command(name="wl_add")
async def wl_add_cmd(ctx, user: discord.User):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf=guild_conf(ctx.guild.id)
    if user.id not in conf["whitelist_users"]:
        conf["whitelist_users"].append(user.id); update_conf(ctx.guild.id, conf)
    await ctx.reply(f"✅ {user.mention} をホワイトリストに追加しました。", mention_author=False)

@bot.command(name="wl_remove")
async def wl_remove_cmd(ctx, user: discord.User):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf=guild_conf(ctx.guild.id)
    if user.id in conf["whitelist_users"]:
        conf["whitelist_users"].remove(user.id); update_conf(ctx.guild.id, conf)
    await ctx.reply(f"🗑️ {user.mention} をホワイトリストから削除しました。", mention_author=False)

@bot.command(name="wl_list")
async def wl_list_cmd(ctx):
    conf=guild_conf(ctx.guild.id); ids=conf.get("whitelist_users", [])
    text="（なし）" if not ids else "\n".join([f"- <@{i}>" for i in ids])
    await ctx.reply(f"**WL Users**\n{text}", mention_author=False)

@bot.command(name="wlrole_add")
async def wlrole_add_cmd(ctx, role: discord.Role):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf=guild_conf(ctx.guild.id)
    if role.id not in conf["whitelist_roles"]:
        conf["whitelist_roles"].append(role.id); update_conf(ctx.guild.id, conf)
    await ctx.reply(f"✅ {role.mention} をホワイトリストに追加しました。", mention_author=False)

@bot.command(name="wlrole_remove")
async def wlrole_remove_cmd(ctx, role: discord.Role):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf=guild_conf(ctx.guild.id)
    if role.id in conf["whitelist_roles"]:
        conf["whitelist_roles"].remove(role.id); update_conf(ctx.guild.id, conf)
    await ctx.reply(f"🗑️ {role.mention} をホワイトリストから削除しました。", mention_author=False)

@bot.command(name="wlrole_list")
async def wlrole_list_cmd(ctx):
    conf=guild_conf(ctx.guild.id); ids=conf.get("whitelist_roles", [])
    text="（なし）" if not ids else "\n".join([f"- <@&{i}>" for i in ids])
    await ctx.reply(f"**WL Roles**\n{text}", mention_author=False)

@bot.command(name="lockdown")
async def lockdown_cmd(ctx, mode: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    mode=mode.lower(); conf=guild_conf(ctx.guild.id)
    if mode not in ("on","off"): return await ctx.reply("使い方: `!lockdown on` または `!lockdown off`", mention_author=False)
    conf["lockdown"]=(mode=="on"); update_conf(ctx.guild.id, conf)
    await ctx.reply(f"🔒 ロックダウン: **{mode}**", mention_author=False)

@bot.command(name="captcha")
async def captcha_cmd_prefix(ctx, mode: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    mode=mode.lower(); conf=guild_conf(ctx.guild.id)
    if mode not in ("on","off"): return await ctx.reply("使い方: `!captcha on` または `!captcha off`", mention_author=False)
    conf["captcha"]["enabled"]=(mode=="on"); update_conf(ctx.guild.id, conf)
    await ctx.reply(f"🧩 CAPTCHA: **{mode}**", mention_author=False)

@bot.command(name="verified_role")
async def verified_role_cmd(ctx, *, name: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf=guild_conf(ctx.guild.id); conf["captcha"]["verified_role_name"]=name; update_conf(ctx.guild.id, conf)
    await ctx.reply(f"✅ Verifiedロール名を `{name}` に設定しました。", mention_author=False)

@bot.command(name="probation")
async def probation_cmd(ctx, minutes: int):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf=guild_conf(ctx.guild.id); conf["probation_minutes"]=max(0,int(minutes)); update_conf(ctx.guild.id, conf)
    await ctx.reply(f"⏱️ Probation: {minutes} 分に設定しました。", mention_author=False)

@bot.command(name="burst_set")
async def burst_set_cmd(ctx, count: int, window_sec: int, spacing_min: Optional[float]=None, spacing_max: Optional[float]=None):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf=guild_conf(ctx.guild.id); b=conf.get("burst_guard") or {}
    b["count"]=int(count); b["window_sec"]=int(window_sec)
    if spacing_min is not None: b["spacing_min"]=float(spacing_min)
    if spacing_max is not None: b["spacing_max"]=float(spacing_max)
    conf["burst_guard"]=b; update_conf(ctx.guild.id, conf)
    await ctx.reply(f"✅ 連投検知: count={b['count']} / window={b['window_sec']}s / spacing=({b.get('spacing_min',0.7)}~{b.get('spacing_max',1.6)})", mention_author=False)

@bot.command(name="burst_punish")
async def burst_punish_cmd(ctx, mode: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    mode=mode.lower()
    if mode not in ("strip_and_mute","cooldown"): return await ctx.reply("使い方: !burst_punish strip_and_mute | cooldown", mention_author=False)
    conf=guild_conf(ctx.guild.id); bp=conf.get("burst_punish",{}) or {}; bp["mode"]=mode; conf["burst_punish"]=bp; update_conf(ctx.guild.id, conf)
    await ctx.reply(f"✅ バースト処罰モードを **{mode}** に設定しました。", mention_author=False)

@bot.command(name="burst_mute_role")
async def burst_mute_role_cmd(ctx, *, name: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf=guild_conf(ctx.guild.id); bp=conf.get("burst_punish",{}) or {}; bp["mute_role_name"]=name; conf["burst_punish"]=bp; update_conf(ctx.guild.id, conf)
    await ctx.reply(f"✅ バースト時のミュートロール名を `{name}` に設定しました。", mention_author=False)

@bot.command(name="cooldown_set")
async def cooldown_set_cmd(ctx, duration_sec: int, *, role_name: Optional[str] = None):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf=guild_conf(ctx.guild.id); cd=conf.get("cooldown") or {}; cd["duration_sec"]=int(duration_sec)
    if role_name: cd["role_name"]=role_name
    conf["cooldown"]=cd; update_conf(ctx.guild.id, conf)
    await ctx.reply(f"⏳ クールダウン: {cd['duration_sec']} 秒 / ロール: `{cd.get('role_name','CooldownMuted')}`", mention_author=False)

@bot.command(name="cooldown_status")
async def cooldown_status_cmd(ctx):
    if not ctx.guild: return
    conf=guild_conf(ctx.guild.id); cd=conf.get("cooldown",{}); now=datetime.now(timezone.utc); targets=[]
    for (gid, uid), until in list(cooldown_until.items()):
        if gid==ctx.guild.id and until>now:
            m=ctx.guild.get_member(uid); label=m.mention if m else f"`{uid}`"
            targets.append(f"{label}（残り ~{int((until-now).total_seconds()//60)}分）")
    text=f"ロール: `{cd.get('role_name','CooldownMuted')}`\n長さ: {int(cd.get('duration_sec',900))} 秒\n対象者: " + (", ".join(targets) if targets else "なし")
    await ctx.reply(text, mention_author=False)

# ---- Spotlight 設定コマンド（強化） ----

@bot.command(name="spotlight_source", help="ネタ元チャンネルを設定: !spotlight_source #channel")
async def spotlight_source_cmd(ctx: commands.Context, channel: discord.TextChannel):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf=guild_conf(ctx.guild.id); conf["spotlight"]["source_channel_id"]=channel.id; update_conf(ctx.guild.id, conf)
    _spotlight_restart_task(ctx.guild.id)
    await ctx.reply(f"📥 Spotlightのソースを {channel.mention} に設定しました。", mention_author=False)

@bot.command(name="spotlight_target", help="投稿先チャンネルを設定: !spotlight_target #channel")
async def spotlight_target_cmd(ctx: commands.Context, channel: discord.TextChannel):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf=guild_conf(ctx.guild.id); conf["spotlight"]["post_channel_id"]=channel.id; update_conf(ctx.guild.id, conf)
    _spotlight_restart_task(ctx.guild.id)
    await ctx.reply(f"📤 Spotlightの投稿先を {channel.mention} に設定しました。", mention_author=False)

@bot.command(name="spotlight_label", help='見出しの○○: !spotlight_label "テーマ"')
async def spotlight_label_cmd(ctx: commands.Context, *, label: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf=guild_conf(ctx.guild.id); conf["spotlight"]["label"]=label.strip(); update_conf(ctx.guild.id, conf)
    await ctx.reply(f"🏷️ 見出しを「今日の{label}はこれ！」に設定しました。", mention_author=False)

@bot.command(name="spotlight_every", help="実行間隔: 30m|2h|1d など")
async def spotlight_every_cmd(ctx: commands.Context, interval: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    sec=_parse_interval_to_sec(interval)
    if not sec: return await ctx.reply("使い方: `!spotlight_every 30m` / `!spotlight_every 6h` / `!spotlight_every 1d`", mention_author=False)
    conf=guild_conf(ctx.guild.id); sp=conf["spotlight"]; sp["interval_sec"]=sec; sp["next_run_ts"]=None; update_conf(ctx.guild.id, conf)
    _spotlight_restart_task(ctx.guild.id)
    await ctx.reply(f"⏱️ Spotlightの間隔を **{interval}** に設定しました。", mention_author=False)

@bot.command(name="spotlight_pick", help="拾う種類: text / image / text_or_image / text_and_image")
async def spotlight_pick_cmd(ctx: commands.Context, mode: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    mode = mode.lower()
    if mode not in ("text","image","text_or_image","text_and_image"):
        return await ctx.reply("使い方: !spotlight_pick text|image|text_or_image|text_and_image", mention_author=False)
    conf=guild_conf(ctx.guild.id); conf["spotlight"]["pick"]=mode; update_conf(ctx.guild.id, conf)
    await ctx.reply(f"🎛️ Spotlightの対象を **{mode}** に設定しました。", mention_author=False)

@bot.command(name="spotlight_filter", help="絞り込み: contains <文字列> / regex <パターン> / clear")
async def spotlight_filter_cmd(ctx: commands.Context, mode: str, *, query: Optional[str] = None):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    mode = mode.lower()
    conf = guild_conf(ctx.guild.id); sp = conf["spotlight"]
    if mode == "clear":
        sp["filter"] = {"mode": None, "query": None}; update_conf(ctx.guild.id, conf)
        return await ctx.reply("🧹 Spotlightのフィルタを解除しました。", mention_author=False)
    if mode not in ("contains","regex") or not query:
        return await ctx.reply('使い方: `!spotlight_filter contains キーワード` / `!spotlight_filter regex パターン` / `!spotlight_filter clear`', mention_author=False)
    sp["filter"] = {"mode": mode, "query": query}
    update_conf(ctx.guild.id, conf)
    await ctx.reply(f"🔎 フィルタを **{mode}**: `{query}` に設定しました。", mention_author=False)

@bot.command(name="spotlight_role", help="Spotlight候補の投稿者に必須のロールを設定: !spotlight_role @ロール")
async def spotlight_role_cmd(ctx: commands.Context, role: discord.Role):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf = guild_conf(ctx.guild.id); sp = conf["spotlight"]
    sp["required_role_id"] = role.id
    update_conf(ctx.guild.id, conf)
    _spotlight_restart_task(ctx.guild.id)
    await ctx.reply(f"🧷 Spotlightの必須ロールを {role.mention} に設定しました。", mention_author=False)

@bot.command(name="spotlight_role_clear", help="Spotlightの必須ロールを解除")
async def spotlight_role_clear_cmd(ctx: commands.Context):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf = guild_conf(ctx.guild.id); sp = conf["spotlight"]
    sp["required_role_id"] = None
    update_conf(ctx.guild.id, conf)
    _spotlight_restart_task(ctx.guild.id)
    await ctx.reply("🧷 Spotlightの必須ロールを解除しました。", mention_author=False)

@bot.command(name="spotlight_on", help="Spotlightを有効化")
async def spotlight_on_cmd(ctx: commands.Context):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf=guild_conf(ctx.guild.id); sp=conf["spotlight"]; sp["enabled"]=True; sp["next_run_ts"]=None; update_conf(ctx.guild.id, conf)
    _spotlight_restart_task(ctx.guild.id)
    await ctx.reply("✅ Spotlight を有効化しました。", mention_author=False)

@bot.command(name="spotlight_off", help="Spotlightを停止")
async def spotlight_off_cmd(ctx: commands.Context):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf=guild_conf(ctx.guild.id); sp=conf["spotlight"]; sp["enabled"]=False; update_conf(ctx.guild.id, conf)
    _spotlight_restart_task(ctx.guild.id)
    await ctx.reply("🛑 Spotlight を停止しました。", mention_author=False)

@bot.command(name="spotlight_now", help="今すぐ1件投稿（オプションでこの回だけキーワード指定）")
async def spotlight_now_cmd(ctx: commands.Context, *, contains: Optional[str] = None):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    await _spotlight_run_once(ctx.guild.id, override_contains=contains)
    conf=guild_conf(ctx.guild.id); sp=conf["spotlight"]; interval=int(sp.get("interval_sec",86400))
    sp["next_run_ts"]=(datetime.now(timezone.utc)+timedelta(seconds=interval)).timestamp(); update_conf(ctx.guild.id, conf)
    await ctx.message.add_reaction("✅")

@bot.command(name="spotlight_status", help="Spotlightの設定状況を表示")
async def spotlight_status_cmd(ctx: commands.Context):
    conf=guild_conf(ctx.guild.id); sp=conf["spotlight"]; on="ON" if sp.get("enabled") else "OFF"
    src_str = f"<#{sp.get('source_channel_id')}>" if sp.get("source_channel_id") else "未設定"
    dst_str = f"<#{sp.get('post_channel_id')}>" if sp.get("post_channel_id") else (f"<#{conf.get('notify_channel_id')}>" if conf.get("notify_channel_id") else "未設定")
    nxt = sp.get("next_run_ts")
    nxt_txt = datetime.fromtimestamp(nxt, tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M") if nxt else "未スケジュール"
    filt = sp.get("filter", {}) or {}
    f_txt = (f"{filt.get('mode')} : `{filt.get('query')}`" if filt.get("mode") and filt.get("query") else "なし")
    req_role_id = sp.get("required_role_id")
    req_role_txt = (f"<@&{req_role_id}>" if req_role_id else "なし")
    active_name = conf.get("spotlight_active_profile") or "（なし）"

    emb = discord.Embed(title="🎯 Spotlight 状況", color=0x00BFFF)
    emb.add_field(name="状態", value=on, inline=True)
    emb.add_field(name="ラベル", value=sp.get("label","投稿"), inline=True)
    emb.add_field(name="間隔", value=f"{int(sp.get('interval_sec',86400))} sec", inline=True)
    emb.add_field(name="ソース", value=src_str, inline=True)
    emb.add_field(name="投稿先", value=dst_str, inline=True)
    emb.add_field(name="対象(pick)", value=sp.get("pick","text_or_image"), inline=True)
    emb.add_field(name="フィルタ", value=f_txt, inline=True)
    emb.add_field(name="必須ロール", value=req_role_txt, inline=True)
    emb.add_field(name="Active Profile", value=active_name, inline=True)
    emb.add_field(name="次回予定", value=nxt_txt + " JST", inline=True)
    await ctx.reply(embed=emb, mention_author=False)

# ---- Spotlight プロファイル（保存・切替・一覧・削除・表示） ----

def _sp_profiles(conf: dict) -> Dict[str, dict]:
    if "spotlight_profiles" not in conf:
        conf["spotlight_profiles"] = {}
    return conf["spotlight_profiles"]

@bot.command(name="spotlight_profile_save", help='現在のSpotlight設定を名前付きで保存: !spotlight_profile_save 夏の画像特集')
async def spotlight_profile_save_cmd(ctx: commands.Context, *, name: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf = guild_conf(ctx.guild.id)
    profs = _sp_profiles(conf)
    profs[name] = dict(conf.get("spotlight", {}))
    conf["spotlight_active_profile"] = name
    update_conf(ctx.guild.id, conf)
    await ctx.reply(f"💾 プロファイル **{name}** に現在のSpotlight設定を保存しました。（Activeに設定）", mention_author=False)

@bot.command(name="spotlight_profile_load", help='保存したプロファイルを読み込み: !spotlight_profile_load 夏の画像特集')
async def spotlight_profile_load_cmd(ctx: commands.Context, *, name: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf = guild_conf(ctx.guild.id)
    profs = _sp_profiles(conf)
    data = profs.get(name)
    if not data:
        return await ctx.reply(f"⚠️ プロファイル **{name}** は見つかりません。`!spotlight_profile_list` を確認してね。", mention_author=False)
    conf["spotlight"] = dict(data)
    conf["spotlight_active_profile"] = name
    update_conf(ctx.guild.id, conf)
    _spotlight_restart_task(ctx.guild.id)

    sp = conf["spotlight"]
    src_str = f"<#{sp.get('source_channel_id')}>" if sp.get("source_channel_id") else "未設定"
    dst_str = f"<#{sp.get('post_channel_id')}>" if sp.get("post_channel_id") else (f"<#{conf.get('notify_channel_id')}>" if conf.get("notify_channel_id") else "未設定")
    nxt = sp.get("next_run_ts")
    nxt_txt = datetime.fromtimestamp(nxt, tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M") if nxt else "未スケジュール"
    filt = sp.get("filter", {}) or {}
    f_txt = (f"{filt.get('mode')} : `{filt.get('query')}`" if filt.get("mode") and filt.get("query") else "なし")
    req_role_id = sp.get("required_role_id")
    req_role_txt = (f"<@&{req_role_id}>" if req_role_id else "なし")

    emb = discord.Embed(title=f"✅ プロファイルを適用: {name}", color=0x00BFFF)
    emb.add_field(name="状態", value=("ON" if sp.get("enabled") else "OFF"), inline=True)
    emb.add_field(name="ラベル", value=sp.get("label","投稿"), inline=True)
    emb.add_field(name="間隔", value=f"{int(sp.get('interval_sec',86400))} sec", inline=True)
    emb.add_field(name="ソース", value=src_str, inline=True)
    emb.add_field(name="投稿先", value=dst_str, inline=True)
    emb.add_field(name="対象(pick)", value=sp.get("pick","text_or_image"), inline=True)
    emb.add_field(name="フィルタ", value=f_txt, inline=True)
    emb.add_field(name="必須ロール", value=req_role_txt, inline=True)
    emb.add_field(name="次回予定", value=nxt_txt + " JST", inline=True)
    await ctx.reply(embed=emb, mention_author=False)

@bot.command(name="spotlight_profile_use", help='load の別名（ショートカット）')
async def spotlight_profile_use_cmd(ctx: commands.Context, *, name: str):
    await spotlight_profile_load_cmd.callback(ctx, name=name)  # type: ignore

@bot.command(name="spotlight_profile_list", help='保存済みプロファイル一覧')
async def spotlight_profile_list_cmd(ctx: commands.Context):
    conf = guild_conf(ctx.guild.id)
    profs = conf.get("spotlight_profiles", {})
    active = conf.get("spotlight_active_profile")
    if not profs:
        return await ctx.reply("（保存済みプロファイルはありません）", mention_author=False)
    lines = []
    for n in sorted(profs.keys()):
        mark = " ⭐" if n == active else ""
        lines.append(f"- {n}{mark}")
    await ctx.reply("**Spotlight Profiles**\n" + "\n".join(lines), mention_author=False)

@bot.command(name="spotlight_profile_delete", help='プロファイル削除: !spotlight_profile_delete 夏の画像特集')
async def spotlight_profile_delete_cmd(ctx: commands.Context, *, name: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf = guild_conf(ctx.guild.id)
    profs = _sp_profiles(conf)
    if name not in profs:
        return await ctx.reply(f"⚠️ **{name}** は存在しません。", mention_author=False)
    del profs[name]
    if conf.get("spotlight_active_profile") == name:
        conf["spotlight_active_profile"] = None
    update_conf(ctx.guild.id, conf)
    await ctx.reply(f"🗑️ プロファイル **{name}** を削除しました。", mention_author=False)

@bot.command(name="spotlight_profile_show", help='プロファイル内容の表示: !spotlight_profile_show 夏の画像特集')
async def spotlight_profile_show_cmd(ctx: commands.Context, *, name: str):
    conf = guild_conf(ctx.guild.id)
    profs = conf.get("spotlight_profiles", {})
    sp = profs.get(name)
    if not sp:
        return await ctx.reply(f"⚠️ **{name}** は存在しません。", mention_author=False)

    src_str = f"<#{sp.get('source_channel_id')}>" if sp.get("source_channel_id") else "未設定"
    dst_str = f"<#{sp.get('post_channel_id')}>" if sp.get("post_channel_id") else (f"<#{conf.get('notify_channel_id')}>" if conf.get("notify_channel_id") else "未設定")
    nxt = sp.get("next_run_ts")
    nxt_txt = datetime.fromtimestamp(nxt, tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M") if nxt else "未スケジュール"
    filt = sp.get("filter", {}) or {}
    f_txt = (f"{filt.get('mode')} : `{filt.get('query')}`" if filt.get("mode") and filt.get("query") else "なし")
    req_role_id = sp.get("required_role_id")
    req_role_txt = (f"<@&{req_role_id}>" if req_role_id else "なし")

    emb = discord.Embed(title=f"📂 Spotlight Profile: {name}", color=0x1E90FF)
    emb.add_field(name="状態", value=("ON" if sp.get("enabled") else "OFF"), inline=True)
    emb.add_field(name="ラベル", value=sp.get("label","投稿"), inline=True)
    emb.add_field(name="間隔", value=f"{int(sp.get('interval_sec',86400))} sec", inline=True)
    emb.add_field(name="ソース", value=src_str, inline=True)
    emb.add_field(name="投稿先", value=dst_str, inline=True)
    emb.add_field(name="対象(pick)", value=sp.get("pick","text_or_image"), inline=True)
    emb.add_field(name="フィルタ", value=f_txt, inline=True)
    emb.add_field(name="必須ロール", value=req_role_txt, inline=True)
    emb.add_field(name="次回予定", value=nxt_txt + " JST", inline=True)
    await ctx.reply(embed=emb, mention_author=False)

# ---- ステータス表示（全体） ----
@bot.command(name="security_status")
async def security_status_cmd(ctx: commands.Context):
    conf = guild_conf(ctx.guild.id)
    ch = conf.get("notify_channel_id")
    wl_u = ", ".join([f"<@{i}>" for i in conf.get('whitelist_users', [])]) or "（なし）"
    wl_r = ", ".join([f"<@&{i}>" for i in conf.get('whitelist_roles', [])]) or "（なし）"

    sp = conf.get("spotlight", {})
    src_str = f"<#{sp.get('source_channel_id')}>" if sp.get("source_channel_id") else "未"
    dst_str = f"<#{sp.get('post_channel_id')}>" if sp.get("post_channel_id") else (f"<#{conf.get('notify_channel_id')}>" if conf.get("notify_channel_id") else "未")
    nxt = sp.get("next_run_ts")
    nxt_txt = datetime.fromtimestamp(nxt, tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M") if nxt else "未スケジュール"
    filt = sp.get("filter", {}) or {}
    f_txt = (f"{filt.get('mode')} : `{filt.get('query')}`" if filt.get("mode") and filt.get("query") else "なし")
    req_role_id = sp.get("required_role_id")
    req_role_txt = (f"<@&{req_role_id}>" if req_role_id else "なし")
    active_name = conf.get("spotlight_active_profile") or "（なし）"

    emb = discord.Embed(title="🔐 SecureBotPlus 設定", color=0x2E8B57)
    emb.add_field(name="通知チャンネル", value=(f"<#{ch}>" if ch else "未設定"), inline=True)
    emb.add_field(name="ロックダウン", value=("ON" if conf.get("lockdown") else "OFF"), inline=True)
    emb.add_field(name="CAPTCHA", value=("ON" if conf["captcha"]["enabled"] else "OFF"), inline=True)
    emb.add_field(name="Verifiedロール", value=conf["captcha"]["verified_role_name"], inline=True)
    emb.add_field(name="Probation(分)", value=str(conf.get("probation_minutes", 10)), inline=True)
    b = conf.get("burst_guard", {})
    emb.add_field(name="連投検知", value=f"{b.get('count',10)}回 / {b.get('window_sec',10)}s / {b.get('spacing_min',0.7)}~{b.get('spacing_max',1.6)}s", inline=False)
    cd = conf.get("cooldown", {})
    emb.add_field(name="クールダウン", value=f"{cd.get('duration_sec',900)}s / {cd.get('role_name','CooldownMuted')}", inline=True)
    bp = conf.get("burst_punish", {})
    emb.add_field(name="バースト処罰", value=f"mode: {bp.get('mode','strip_and_mute')} / mute_role: {bp.get('mute_role_name','Muted')}", inline=True)
    lg = conf.get("logs", {})
    lines = []
    for k in LOG_KINDS:
        ch_id = lg.get("channels", {}).get(k); enabled = lg.get("enabled", {}).get(k, True)
        dest = f"<#{ch_id}>" if ch_id else "notify先"
        lines.append(f"`{k}`: {'ON' if enabled else 'OFF'} / {dest}")
    emb.add_field(name="ログ割当", value="\n".join(lines), inline=False)

    emb.add_field(
        name="Spotlight",
        value=(f"ON/OFF={'ON' if sp.get('enabled') else 'OFF'} / pick={sp.get('pick','text_or_image')} / "
               f"filter={f_txt} / label='{sp.get('label','投稿')}' / every={int(sp.get('interval_sec',86400))}s / "
               f"src={src_str} / dst={dst_str} / role={req_role_txt} / Active={active_name} / next={nxt_txt} JST"),
        inline=False
    )

    emb.add_field(name="WL Users", value=wl_u, inline=False)
    emb.add_field(name="WL Roles", value=wl_r, inline=False)
    await ctx.reply(embed=emb, mention_author=False)

@bot.command(name="security_overview")
async def security_overview_cmd(ctx: commands.Context):
    conf = guild_conf(ctx.guild.id)
    def yn(b): return "ON" if b else "OFF"

    wl_users = conf.get("whitelist_users", [])
    wl_roles = conf.get("whitelist_roles", [])
    u_names = [(ctx.guild.get_member(uid).mention if ctx.guild.get_member(uid) else f"`{uid}`") for uid in wl_users]
    r_names = [(ctx.guild.get_role(rid).mention if ctx.guild.get_role(rid) else f"`{rid}`") for rid in wl_roles]

    captcha = conf.get("captcha", {})
    antispam = conf.get("antispam", {})
    cooldown = conf.get("cooldown", {})
    burst = conf.get("burst_guard", {})
    bp = conf.get("burst_punish", {})
    lg = conf.get("logs", {})
    log_ch_id = conf.get("notify_channel_id")
    log_ch = f"<#{log_ch_id}>" if log_ch_id else "未設定"

    sp = conf.get("spotlight", {})
    src_str = f"<#{sp.get('source_channel_id')}>" if sp.get("source_channel_id") else "未"
    dst_str = f"<#{sp.get('post_channel_id')}>" if sp.get("post_channel_id") else (f"<#{conf.get('notify_channel_id')}>" if conf.get("notify_channel_id") else "未")
    nxt = sp.get("next_run_ts")
    nxt_txt = datetime.fromtimestamp(nxt, tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M") if nxt else "未"
    filt = sp.get("filter", {}) or {}
    f_txt = (f"{filt.get('mode')} : `{filt.get('query')}`" if filt.get("mode") and filt.get("query") else "なし")
    req_role_id = sp.get("required_role_id")
    req_role_txt = (f"<@&{req_role_id}>" if req_role_id else "なし")
    active_name = conf.get("spotlight_active_profile") or "（なし）"

    emb = discord.Embed(title="🔐 Security Overview", color=0x2E8B57)
    emb.add_field(name="基本", value=f"Lockdown: **{yn(conf.get('lockdown', False))}**\nLogChannel(既定): {log_ch}", inline=False)
    emb.add_field(name="CAPTCHA", value=f"{yn(captcha.get('enabled', True))} / verified_role: `{captcha.get('verified_role_name','Verified')}` / quarantine_role: `{captcha.get('quarantine_role_name','Quarantine')}`", inline=True)
    emb.add_field(name="Antispam", value=f"probation: {conf.get('probation_minutes', 10)}m / URLs≤{antispam.get('max_urls_per_10s',4)} / mentions≤{antispam.get('max_mentions_per_msg',5)} / msgs/5s≤{antispam.get('max_msgs_per_5s',6)}", inline=False)
    emb.add_field(name="連投検知(Burst)", value=f"count: {burst.get('count',10)} / {burst.get('window_sec',10)}s / spacing: {burst.get('spacing_min',0.7)}~{burst.get('spacing_max',1.6)}s", inline=True)
    emb.add_field(name="バースト処罰", value=f"mode: {bp.get('mode','strip_and_mute')} / mute_role: `{bp.get('mute_role_name','Muted')}`", inline=True)

    lines = []
    for k in LOG_KINDS:
        ch_id = lg.get("channels", {}).get(k)
        enabled = lg.get("enabled", {}).get(k, True)
        dest = f"<#{ch_id}>" if ch_id else "notify先"
        lines.append(f"`{k}`: {'ON' if enabled else 'OFF'} / {dest}")
    emb.add_field(name="ログ割当", value="\n".join(lines), inline=False)

    if cooldown:
        emb.add_field(name="クールダウン", value=f"{cooldown.get('duration_sec',900)}s / role: `{cooldown.get('role_name','CooldownMuted')}`", inline=True)

    emb.add_field(
        name="Spotlight",
        value=(f"{'ON' if sp.get('enabled') else 'OFF'} / pick={sp.get('pick','text_or_image')} / filter={f_txt} / "
               f"label='{sp.get('label','投稿')}' / every={int(sp.get('interval_sec',86400))}s / "
               f"src={src_str} / dst={dst_str} / role={req_role_txt} / Active={active_name} / next={nxt_txt} JST"),
        inline=False
    )
    emb.add_field(name=f"WL Users ({len(u_names)})", value=("、".join(u_names) or "（なし）"), inline=False)
    emb.add_field(name=f"WL Roles ({len(r_names)})", value=("、".join(r_names) or "（なし）"), inline=False)
    await ctx.reply(embed=emb, mention_author=False)

# ---- WL限定コマンド設定（追加/削除/一覧/クリア） ----
@bot.command(name="cmdwl_add", help="WL限定にするコマンドを追加: !cmdwl_add lockdown burst_set ...")
async def cmdwl_add_cmd(ctx: commands.Context, *names: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    if not names:
        return await ctx.reply("使い方: `!cmdwl_add コマンド名 [コマンド名…]` 例: `!cmdwl_add lockdown burst_set`", mention_author=False)

    conf = guild_conf(ctx.guild.id)
    now = set(conf.get("restricted_commands") or [])
    known = {c.name for c in bot.commands}

    added, invalid = [], []
    for n in names:
        if n in known:
            if n not in now:
                now.add(n); added.append(n)
        else:
            invalid.append(n)

    conf["restricted_commands"] = sorted(now)
    update_conf(ctx.guild.id, conf)

    parts = []
    if added:   parts.append("追加: " + ", ".join(f"`{x}`" for x in added))
    if invalid: parts.append("存在しない(無視): " + ", ".join(f"`{x}`" for x in invalid))
    if not parts: parts.append("変更なし")
    await ctx.reply(" / ".join(parts), mention_author=False)

@bot.command(name="cmdwl_remove", help="WL限定から外す: !cmdwl_remove lockdown ...")
async def cmdwl_remove_cmd(ctx: commands.Context, *names: str):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    if not names:
        return await ctx.reply("使い方: `!cmdwl_remove コマンド名 [コマンド名…]`", mention_author=False)

    conf = guild_conf(ctx.guild.id)
    now = set(conf.get("restricted_commands") or [])
    removed = []

    for n in names:
        if n in now:
            now.remove(n); removed.append(n)

    conf["restricted_commands"] = sorted(now)
    update_conf(ctx.guild.id, conf)

    msg = ("削除: " + ", ".join(f"`{x}`" for x in removed)) if removed else "対象が見つかりませんでした。"
    await ctx.reply(msg, mention_author=False)

@bot.command(name="cmdwl_list", help="WL限定コマンドの一覧を表示")
async def cmdwl_list_cmd(ctx: commands.Context):
    conf = guild_conf(ctx.guild.id)
    lst = conf.get("restricted_commands") or []
    if not lst:
        return await ctx.reply("（WL限定コマンドは未設定です）", mention_author=False)
    lines = "\n".join(f"- `{n}`" for n in lst)
    await ctx.reply("**ホワイトリスト限定コマンド**\n" + lines, mention_author=False)

@bot.command(name="cmdwl_clear", help="WL限定コマンドを全消去（注意）")
async def cmdwl_clear_cmd(ctx: commands.Context):
    if not _need_manage_guild(ctx): return await _deny_manage_guild(ctx)
    conf = guild_conf(ctx.guild.id)
    conf["restricted_commands"] = []
    update_conf(ctx.guild.id, conf)
    await ctx.reply("🧹 すべてのWL限定コマンドをクリアしました。", mention_author=False)

# ====== 起動 ======
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    for g in bot.guilds:
        conf = guild_conf(g.id)
        if conf.get("spotlight", {}).get("enabled"):
            _spotlight_restart_task(g.id)

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("環境変数 TOKEN が設定されていません。PowerShell で $env:TOKEN=\"...\" を入れてから起動してください。")
    bot.run(TOKEN)
