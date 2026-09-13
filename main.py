from __future__ import annotations

import asyncio
import io
import logging
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import discord
from discord import app_commands
from discord.ext import commands, tasks

from PIL import Image, ImageDraw, ImageFont


# =========================================================
# 🍬 Candy
# VCプロフィール ＋ レベルBot 完全統合版
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

# ---------------------------------------------------------
# Candy サーバー
# ---------------------------------------------------------

GUILD_ID = 1542420058775494666

# 管理者ロール
ADMIN_ROLE_ID = 1542422448895565824

# プロフィールチャンネル
MALE_PROFILE_CHANNEL_ID = 1542429626905657415
FEMALE_PROFILE_CHANNEL_ID = 1542429755750486026

# しゃべレア移動待ち
PROFILE_DELAY = 5

# 二重チェック
DUPLICATE_CHECK_LIMIT = 20

# ---------------------------------------------------------
# SQLite
# Render Persistent Disk を使う場合は
# DATABASE_PATH=/var/data/candy_level.db
# にするのがおすすめ
# ---------------------------------------------------------

DB_PATH = os.getenv(
    "DATABASE_PATH",
    "data/candy_level.db"
)

# Render
PORT = int(
    os.getenv(
        "PORT",
        "10000"
    )
)


# =========================================================
# LOG
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger(
    "candy-bot"
)


# =========================================================
# DATABASE
# =========================================================

Path(DB_PATH).parent.mkdir(
    parents=True,
    exist_ok=True
)


def db_connect():

    conn = sqlite3.connect(
        DB_PATH
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_database():

    with db_connect() as conn:

        # -----------------------------------------
        # VC滞在時間
        # -----------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_stats (
                user_id INTEGER PRIMARY KEY,
                total_seconds REAL NOT NULL DEFAULT 0
            )
            """
        )

        # -----------------------------------------
        # レベルロール
        #
        # level = そのロールになる開始Lv
        #
        # 例
        # Lv1  Candy新人
        # Lv5  Candy常連
        # Lv20 CandyVIP
        # -----------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS level_roles (
                level INTEGER PRIMARY KEY,
                role_id INTEGER NOT NULL
            )
            """
        )

        conn.commit()


init_database()


# =========================================================
# DB - VC時間
# =========================================================

def db_get_seconds(
    user_id: int
) -> float:

    with db_connect() as conn:

        row = conn.execute(
            """
            SELECT total_seconds
            FROM voice_stats
            WHERE user_id = ?
            """,
            (
                user_id,
            )
        ).fetchone()

    if row is None:
        return 0.0

    return float(
        row["total_seconds"]
    )


def db_set_seconds(
    user_id: int,
    seconds: float
):

    seconds = max(
        0.0,
        seconds
    )

    with db_connect() as conn:

        conn.execute(
            """
            INSERT INTO voice_stats (
                user_id,
                total_seconds
            )
            VALUES (?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                total_seconds = excluded.total_seconds
            """,
            (
                user_id,
                seconds
            )
        )

        conn.commit()


def db_add_seconds(
    user_id: int,
    seconds: float
):

    if seconds <= 0:
        return

    with db_connect() as conn:

        conn.execute(
            """
            INSERT INTO voice_stats (
                user_id,
                total_seconds
            )
            VALUES (?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                total_seconds =
                    voice_stats.total_seconds
                    +
                    excluded.total_seconds
            """,
            (
                user_id,
                seconds
            )
        )

        conn.commit()


# =========================================================
# DB - レベルロール
# =========================================================

def db_set_level_role(
    level: int,
    role_id: int
):

    with db_connect() as conn:

        conn.execute(
            """
            INSERT INTO level_roles (
                level,
                role_id
            )
            VALUES (?, ?)

            ON CONFLICT(level)
            DO UPDATE SET
                role_id = excluded.role_id
            """,
            (
                level,
                role_id
            )
        )

        conn.commit()


def db_remove_level_role(
    level: int
):

    with db_connect() as conn:

        conn.execute(
            """
            DELETE FROM level_roles
            WHERE level = ?
            """,
            (
                level,
            )
        )

        conn.commit()


def db_get_level_roles():

    with db_connect() as conn:

        rows = conn.execute(
            """
            SELECT level, role_id
            FROM level_roles
            ORDER BY level ASC
            """
        ).fetchall()

    return rows


# =========================================================
# LEVEL SYSTEM
# =========================================================

def required_hours_for_level(
    level: int
) -> float:

    """
    Lv1 = 0時間
    Lv2 = 1時間
    Lv3 = 2.5時間
    Lv4 = 5時間
    Lv5 = 10時間

    Lv5以降は徐々に重くなる
    """

    if level <= 1:
        return 0.0

    fixed = {
        2: 1.0,
        3: 2.5,
        4: 5.0,
        5: 10.0
    }

    if level in fixed:
        return fixed[level]

    # -----------------------------------------
    # Lv5以降
    #
    # Lv10 約31時間
    # Lv20 約96時間
    # 高レベルになるほど必要時間増加
    # -----------------------------------------

    n = level - 5

    return (
        10.0
        +
        3.5 * n
        +
        0.15 * (n ** 2)
    )


def required_seconds_for_level(
    level: int
) -> float:

    return (
        required_hours_for_level(
            level
        )
        *
        3600
    )


def calculate_level(
    total_seconds: float
) -> int:

    level = 1

    # 上限1000
    for next_level in range(
        2,
        1001
    ):

        required = (
            required_seconds_for_level(
                next_level
            )
        )

        if total_seconds < required:
            break

        level = next_level

    return level


def get_level_progress(
    total_seconds: float
):

    level = calculate_level(
        total_seconds
    )

    current_required = (
        required_seconds_for_level(
            level
        )
    )

    next_required = (
        required_seconds_for_level(
            level + 1
        )
    )

    progress_seconds = max(
        0.0,
        total_seconds
        -
        current_required
    )

    needed_seconds = max(
        1.0,
        next_required
        -
        current_required
    )

    percent = min(
        1.0,
        progress_seconds
        /
        needed_seconds
    )

    return (
        level,
        progress_seconds,
        needed_seconds,
        percent,
        next_required
    )


# =========================================================
# 時間表示
# =========================================================

def format_duration(
    seconds: float
) -> str:

    seconds = max(
        0,
        int(seconds)
    )

    hours = seconds // 3600

    minutes = (
        seconds % 3600
    ) // 60

    return (
        f"{hours}時間 {minutes}分"
    )


# =========================================================
# Render Health Server
# =========================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        self.send_response(200)
        self.end_headers()

        self.wfile.write(
            b"Candy Bot running."
        )

    def log_message(
        self,
        format,
        *args
    ):
        return


def start_web_server():

    try:

        server = ThreadingHTTPServer(
            (
                "0.0.0.0",
                PORT
            ),
            HealthHandler
        )

        Thread(
            target=server.serve_forever,
            daemon=True
        ).start()

        log.info(
            "Health server started on port %s",
            PORT
        )

    except Exception:

        log.exception(
            "Health server error"
        )


# =========================================================
# Discord
# =========================================================

intents = discord.Intents.default()

intents.members = True
intents.voice_states = True
intents.message_content = True


bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


GUILD_OBJECT = discord.Object(
    id=GUILD_ID
)


# =========================================================
# プロフィール用保存
# =========================================================

profile_tasks: dict[
    int,
    asyncio.Task
] = {}


profile_messages: dict[
    int,
    dict
] = {}


profile_cache: dict[
    int,
    discord.Message
] = {}


# =========================================================
# VCレベル用
#
# user_id : 計測開始時刻
# =========================================================

voice_sessions: dict[
    int,
    float
] = {}


# =========================================================
# 管理者判定
# =========================================================

def is_admin_member(
    member: discord.Member
) -> bool:

    if member.guild_permissions.administrator:
        return True

    return any(
        role.id == ADMIN_ROLE_ID
        for role in member.roles
    )


async def require_admin(
    interaction: discord.Interaction
) -> bool:

    member = interaction.user

    if not isinstance(
        member,
        discord.Member
    ):

        await interaction.response.send_message(
            "❌ このコマンドはサーバー内でのみ使えます。",
            ephemeral=True
        )

        return False

    if is_admin_member(
        member
    ):

        return True

    await interaction.response.send_message(
        "❌ このコマンドは管理者専用です。",
        ephemeral=True
    )

    return False


# =========================================================
# VCがレベル対象か
# =========================================================

def is_countable_voice_channel(
    guild: discord.Guild,
    channel: discord.abc.GuildChannel | None
) -> bool:

    if channel is None:
        return False

    # AFKチャンネルは対象外
    if (
        guild.afk_channel
        and
        channel.id
        ==
        guild.afk_channel.id
    ):
        return False

    return True


# =========================================================
# VCセッション開始
# =========================================================

def start_voice_session(
    member: discord.Member
):

    if member.bot:
        return

    if (
        member.id
        in
        voice_sessions
    ):
        return

    voice_sessions[
        member.id
    ] = time.time()

    log.info(
        "⏱️ VC計測開始: %s",
        member
    )


# =========================================================
# VCセッション保存
# =========================================================

async def flush_voice_session(
    user_id: int,
    keep_running: bool = True
):

    started_at = voice_sessions.get(
        user_id
    )

    if started_at is None:
        return

    now = time.time()

    elapsed = max(
        0.0,
        now - started_at
    )

    if elapsed > 0:

        db_add_seconds(
            user_id,
            elapsed
        )

    if keep_running:

        voice_sessions[
            user_id
        ] = now

    else:

        voice_sessions.pop(
            user_id,
            None
        )


# =========================================================
# 現在の合計VC時間
# =========================================================

def get_live_total_seconds(
    user_id: int
) -> float:

    total = db_get_seconds(
        user_id
    )

    started_at = voice_sessions.get(
        user_id
    )

    if started_at is not None:

        total += max(
            0.0,
            time.time()
            -
            started_at
        )

    return total


# =========================================================
# 現在適用するレベルロール
# =========================================================

def get_target_level_role_id(
    level: int
):

    rows = db_get_level_roles()

    target = None

    for row in rows:

        required_level = int(
            row["level"]
        )

        if level >= required_level:

            target = int(
                row["role_id"]
            )

        else:

            break

    return target


# =========================================================
# レベルロール同期
# =========================================================

async def sync_level_roles(
    member: discord.Member
):

    if member.bot:
        return

    total_seconds = (
        get_live_total_seconds(
            member.id
        )
    )

    level = calculate_level(
        total_seconds
    )

    settings = db_get_level_roles()

    if not settings:
        return

    configured_role_ids = {
        int(row["role_id"])
        for row in settings
    }

    target_role_id = (
        get_target_level_role_id(
            level
        )
    )

    roles_to_remove = []

    for role in member.roles:

        if (
            role.id
            in
            configured_role_ids
            and
            role.id
            !=
            target_role_id
        ):

            roles_to_remove.append(
                role
            )

    try:

        if roles_to_remove:

            await member.remove_roles(
                *roles_to_remove,
                reason="Candy レベルロール自動更新"
            )

    except discord.Forbidden:

        log.warning(
            "レベルロール削除権限なし: %s",
            member
        )

    except Exception:

        log.exception(
            "レベルロール削除エラー"
        )

    if target_role_id is None:
        return

    target_role = (
        member.guild.get_role(
            target_role_id
        )
    )

    if target_role is None:
        return

    if target_role in member.roles:
        return

    try:

        await member.add_roles(
            target_role,
            reason=f"Candy Lv{level} 到達"
        )

        log.info(
            "🍬 レベルロール付与: %s -> %s",
            member,
            target_role.name
        )

    except discord.Forbidden:

        log.warning(
            "レベルロール付与権限なし: %s",
            target_role.name
        )

    except Exception:

        log.exception(
            "レベルロール付与エラー"
        )


# =========================================================
# 全員ロール再同期
# =========================================================

async def sync_all_level_roles(
    guild: discord.Guild
):

    for member in guild.members:

        if member.bot:
            continue

        try:

            await sync_level_roles(
                member
            )

        except Exception:

            log.exception(
                "全員ロール同期エラー: %s",
                member
            )

        # API負荷を軽減
        await asyncio.sleep(
            0.15
        )


# =========================================================
# 1分ごとにVC時間保存
#
# これによりRenderが落ちても
# 最大約1分程度のロスで済む
# =========================================================

@tasks.loop(
    seconds=60
)
async def level_flush_loop():

    guild = bot.get_guild(
        GUILD_ID
    )

    if guild is None:
        return

    user_ids = list(
        voice_sessions.keys()
    )

    for user_id in user_ids:

        member = guild.get_member(
            user_id
        )

        if member is None:

            await flush_voice_session(
                user_id,
                keep_running=False
            )

            continue

        # VCから消えていた場合
        if (
            member.voice is None
            or
            member.voice.channel is None
        ):

            await flush_voice_session(
                user_id,
                keep_running=False
            )

            await sync_level_roles(
                member
            )

            continue

        # AFKに移動していた場合
        if not is_countable_voice_channel(
            guild,
            member.voice.channel
        ):

            await flush_voice_session(
                user_id,
                keep_running=False
            )

            await sync_level_roles(
                member
            )

            continue

        await flush_voice_session(
            user_id,
            keep_running=True
        )

        await sync_level_roles(
            member
        )


@level_flush_loop.before_loop
async def before_level_flush():

    await bot.wait_until_ready()


# =========================================================
# プロフィールボタン
# =========================================================

class ProfileView(
    discord.ui.View
):

    def __init__(
        self,
        url: str
    ):

        super().__init__(
            timeout=None
        )

        self.add_item(
            discord.ui.Button(
                label="プロフィールを見る",
                emoji="📖",
                style=discord.ButtonStyle.link,
                url=url
            )
        )


# =========================================================
# プロフィール検索
# =========================================================

async def find_profile_message(
    member: discord.Member
):

    cached = profile_cache.get(
        member.id
    )

    if cached is not None:
        return cached

    guild = member.guild

    for channel_id in [
        MALE_PROFILE_CHANNEL_ID,
        FEMALE_PROFILE_CHANNEL_ID
    ]:

        channel = guild.get_channel(
            channel_id
        )

        if channel is None:
            continue

        try:

            async for message in channel.history(
                limit=None,
                oldest_first=False
            ):

                if (
                    message.author
                    and
                    message.author.id
                    ==
                    member.id
                ):

                    profile_cache[
                        member.id
                    ] = message

                    return message

        except Exception:

            log.exception(
                "プロフィール検索エラー"
            )

    return None


# =========================================================
# 直近履歴に同じプロフィールがあるか
# =========================================================

async def profile_already_exists(
    channel: discord.VoiceChannel,
    user_id: int
):

    target_text = (
        f"ID:{user_id}"
    )

    target_text_space = (
        f"ID: {user_id}"
    )

    try:

        async for message in channel.history(
            limit=DUPLICATE_CHECK_LIMIT
        ):

            if (
                bot.user
                and
                message.author.id
                != bot.user.id
            ):
                continue

            for embed in message.embeds:

                description = (
                    embed.description
                    or
                    ""
                )

                if (
                    target_text
                    in description
                    or
                    target_text_space
                    in description
                ):

                    return message

            content = (
                message.content
                or
                ""
            )

            if (
                target_text
                in content
                or
                target_text_space
                in content
            ):

                return message

    except discord.Forbidden:

        log.warning(
            "履歴確認権限なし: %s",
            channel.id
        )

    except Exception:

        log.exception(
            "二重投稿チェックエラー"
        )

    return None


# =========================================================
# 表示中プロフィール削除
# =========================================================

async def delete_profile_card(
    member: discord.Member
):

    data = profile_messages.pop(
        member.id,
        None
    )

    if not data:
        return

    channel = member.guild.get_channel(
        data["channel_id"]
    )

    if channel is None:
        return

    try:

        message = await channel.fetch_message(
            data["message_id"]
        )

        await message.delete()

    except discord.NotFound:
        pass

    except Exception:

        log.exception(
            "プロフィール削除エラー"
        )


# =========================================================
# 履歴からプロフィールカード削除
# =========================================================

async def delete_profile_cards_from_channel(
    channel: discord.VoiceChannel,
    user_id: int
):

    target_text = (
        f"ID:{user_id}"
    )

    target_text_space = (
        f"ID: {user_id}"
    )

    try:

        async for message in channel.history(
            limit=50
        ):

            if (
                bot.user
                and
                message.author.id
                != bot.user.id
            ):
                continue

            matched = False

            for embed in message.embeds:

                description = (
                    embed.description
                    or
                    ""
                )

                if (
                    target_text
                    in description
                    or
                    target_text_space
                    in description
                ):

                    matched = True
                    break

            if not matched:

                content = (
                    message.content
                    or
                    ""
                )

                if (
                    target_text in content
                    or
                    target_text_space in content
                ):

                    matched = True

            if matched:

                try:
                    await message.delete()
                except Exception:
                    pass

    except Exception:

        log.exception(
            "履歴プロフィール削除エラー"
        )


# =========================================================
# 5秒後プロフィール表示
# =========================================================

async def delayed_profile_post(
    member: discord.Member,
    expected_channel_id: int
):

    try:

        await asyncio.sleep(
            PROFILE_DELAY
        )

        guild = member.guild

        current_member = guild.get_member(
            member.id
        )

        if current_member is None:
            return

        if (
            current_member.voice is None
            or
            current_member.voice.channel is None
        ):
            return

        current_vc = (
            current_member.voice.channel
        )

        if (
            current_vc.id
            !=
            expected_channel_id
        ):
            return

        existing = profile_messages.get(
            current_member.id
        )

        if (
            existing
            and
            existing.get(
                "channel_id"
            )
            ==
            current_vc.id
        ):
            return

        duplicate = await profile_already_exists(
            current_vc,
            current_member.id
        )

        if duplicate:

            profile_messages[
                current_member.id
            ] = {
                "channel_id":
                    current_vc.id,
                "message_id":
                    duplicate.id
            }

            return

        profile_message = await find_profile_message(
            current_member
        )

        if profile_message:

            description = (
                f"{current_member.mention} さんが"
                "お部屋に参加しました！\n\n"
                "📖 **プロフィール**\n"
                "下のボタンからプロフィールを"
                "確認できます。\n\n"
                f"ID:{current_member.id}"
            )

        else:

            description = (
                f"{current_member.mention} さんが"
                "お部屋に参加しました！\n\n"
                "📖 **プロフィール**\n"
                "プロフィールはまだ登録されていません。\n\n"
                f"ID:{current_member.id}"
            )

        embed = discord.Embed(
            title="🏫 プロフィール",
            description=description
        )

        # サーバーアバター優先
        avatar = (
            current_member.guild_avatar
            or
            current_member.display_avatar
        )

        embed.set_thumbnail(
            url=avatar.url
        )

        await asyncio.sleep(
            0.5
        )

        duplicate = await profile_already_exists(
            current_vc,
            current_member.id
        )

        if duplicate:

            profile_messages[
                current_member.id
            ] = {
                "channel_id":
                    current_vc.id,
                "message_id":
                    duplicate.id
            }

            return

        if profile_message:

            sent = await current_vc.send(
                embed=embed,
                view=ProfileView(
                    profile_message.jump_url
                )
            )

        else:

            sent = await current_vc.send(
                embed=embed
            )

        profile_messages[
            current_member.id
        ] = {
            "channel_id":
                current_vc.id,
            "message_id":
                sent.id
        }

        log.info(
            "✅ プロフィール表示: %s -> %s",
            current_member,
            current_vc.name
        )

    except asyncio.CancelledError:
        return

    except Exception:

        log.exception(
            "プロフィール投稿エラー"
        )

    finally:

        current_task = asyncio.current_task()

        if (
            profile_tasks.get(
                member.id
            )
            is current_task
        ):

            profile_tasks.pop(
                member.id,
                None
            )


# =========================================================
# LEVEL CARD FONT
# =========================================================

def get_font(
    size: int
):

    font_paths = [

        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",

        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",

        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",

        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",

        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ]

    for path in font_paths:

        if os.path.exists(
            path
        ):

            try:

                return ImageFont.truetype(
                    path,
                    size
                )

            except Exception:
                pass

    return ImageFont.load_default()


# =========================================================
# LEVEL CARD生成
# =========================================================

def make_level_card(
    avatar_bytes: bytes,
    display_name: str,
    level: int,
    total_seconds: float,
    progress: float,
    progress_seconds: float,
    needed_seconds: float,
    role_name: str
) -> io.BytesIO:

    width = 1000
    height = 360

    image = Image.new(
        "RGB",
        (
            width,
            height
        ),
        (
            30,
            22,
            45
        )
    )

    draw = ImageDraw.Draw(
        image
    )

    # -----------------------------------------------------
    # 背景
    # -----------------------------------------------------

    for y in range(
        height
    ):

        ratio = (
            y
            /
            height
        )

        r = int(
            48
            +
            35 * ratio
        )

        g = int(
            34
            +
            20 * ratio
        )

        b = int(
            72
            +
            38 * ratio
        )

        draw.line(
            (
                0,
                y,
                width,
                y
            ),
            fill=(
                r,
                g,
                b
            )
        )

    # 飾り
    draw.ellipse(
        (
            720,
            -150,
            1100,
            230
        ),
        fill=(
            93,
            69,
            130
        )
    )

    draw.ellipse(
        (
            -120,
            220,
            260,
            600
        ),
        fill=(
            62,
            46,
            91
        )
    )

    # -----------------------------------------------------
    # Avatar
    # -----------------------------------------------------

    try:

        avatar = Image.open(
            io.BytesIO(
                avatar_bytes
            )
        ).convert(
            "RGBA"
        )

        avatar = avatar.resize(
            (
                190,
                190
            )
        )

        mask = Image.new(
            "L",
            (
                190,
                190
            ),
            0
        )

        mask_draw = ImageDraw.Draw(
            mask
        )

        mask_draw.ellipse(
            (
                0,
                0,
                190,
                190
            ),
            fill=255
        )

        avatar.putalpha(
            mask
        )

        # 外枠
        draw.ellipse(
            (
                54,
                74,
                256,
                276
            ),
            fill=(
                255,
                255,
                255
            )
        )

        image.paste(
            avatar,
            (
                60,
                80
            ),
            avatar
        )

    except Exception:

        log.exception(
            "Avatar描画失敗"
        )

    # -----------------------------------------------------
    # Fonts
    # -----------------------------------------------------

    font_name = get_font(
        38
    )

    font_level_small = get_font(
        24
    )

    font_level_big = get_font(
        80
    )

    font_normal = get_font(
        25
    )

    font_small = get_font(
        20
    )

    # -----------------------------------------------------
    # Name
    # -----------------------------------------------------

    draw.text(
        (
            295,
            54
        ),
        display_name[:24],
        font=font_name,
        fill=(
            255,
            255,
            255
        )
    )

    # -----------------------------------------------------
    # LEVEL
    # -----------------------------------------------------

    draw.text(
        (
            760,
            70
        ),
        "LEVEL",
        font=font_level_small,
        fill=(
            225,
            215,
            240
        )
    )

    draw.text(
        (
            830,
            90
        ),
        str(
            level
        ),
        font=font_level_big,
        fill=(
            255,
            255,
            255
        )
    )

    # -----------------------------------------------------
    # Total VC
    # -----------------------------------------------------

    draw.text(
        (
            295,
            125
        ),
        "VOICE TIME",
        font=font_small,
        fill=(
            213,
            196,
            230
        )
    )

    draw.text(
        (
            295,
            156
        ),
        format_duration(
            total_seconds
        ),
        font=font_normal,
        fill=(
            255,
            255,
            255
        )
    )

    # -----------------------------------------------------
    # Rank
    # -----------------------------------------------------

    if role_name:

        draw.text(
            (
                295,
                203
            ),
            f"RANK  {role_name}",
            font=font_small,
            fill=(
                240,
                220,
                255
            )
        )

    # -----------------------------------------------------
    # XP BAR
    # -----------------------------------------------------

    bar_x = 295
    bar_y = 260
    bar_width = 620
    bar_height = 28

    draw.rounded_rectangle(
        (
            bar_x,
            bar_y,
            bar_x + bar_width,
            bar_y + bar_height
        ),
        radius=14,
        fill=(
            41,
            32,
            58
        )
    )

    filled_width = int(
        bar_width
        *
        progress
    )

    if filled_width > 0:

        draw.rounded_rectangle(
            (
                bar_x,
                bar_y,
                bar_x + filled_width,
                bar_y + bar_height
            ),
            radius=14,
            fill=(
                219,
                170,
                255
            )
        )

    current_hours = (
        progress_seconds
        /
        3600
    )

    needed_hours = (
        needed_seconds
        /
        3600
    )

    progress_text = (
        f"{current_hours:.1f}h / "
        f"{needed_hours:.1f}h"
    )

    draw.text(
        (
            295,
            303
        ),
        progress_text,
        font=font_small,
        fill=(
            225,
            217,
            235
        )
    )

    draw.text(
        (
            780,
            303
        ),
        "Candy LEVEL",
        font=font_small,
        fill=(
            225,
            217,
            235
        )
    )

    buffer = io.BytesIO()

    image.save(
        buffer,
        format="PNG"
    )

    buffer.seek(0)

    return buffer


# =========================================================
# /level
#
# ★ 一般メンバーが使える唯一のレベルコマンド
# =========================================================

@bot.tree.command(
    name="level",
    description="自分のCandyレベルを確認します",
    guild=GUILD_OBJECT
)
async def level_command(
    interaction: discord.Interaction
):

    member = interaction.user

    if not isinstance(
        member,
        discord.Member
    ):
        return

    await interaction.response.defer()

    total_seconds = (
        get_live_total_seconds(
            member.id
        )
    )

    (
        level,
        progress_seconds,
        needed_seconds,
        percent,
        next_required
    ) = get_level_progress(
        total_seconds
    )

    # -----------------------------------------------------
    # 現在のランクロール
    # -----------------------------------------------------

    target_role_id = (
        get_target_level_role_id(
            level
        )
    )

    role_name = ""

    if target_role_id:

        role = interaction.guild.get_role(
            target_role_id
        )

        if role:

            role_name = role.name

    # -----------------------------------------------------
    # サーバーアバター優先
    # -----------------------------------------------------

    avatar_asset = (
        member.guild_avatar
        or
        member.display_avatar
    )

    try:

        avatar_bytes = await avatar_asset.read()

    except Exception:

        avatar_bytes = (
            await member.display_avatar.read()
        )

    card = await asyncio.to_thread(
        make_level_card,
        avatar_bytes,
        member.display_name,
        level,
        total_seconds,
        percent,
        progress_seconds,
        needed_seconds,
        role_name
    )

    file = discord.File(
        card,
        filename="candy_level.png"
    )

    embed = discord.Embed(
        description=(
            f"🍬 **{member.display_name}**\n"
            f"現在 **Lv.{level}**\n"
            f"累計VC **{format_duration(total_seconds)}**"
        )
    )

    embed.set_image(
        url="attachment://candy_level.png"
    )

    await interaction.followup.send(
        embed=embed,
        file=file
    )


# =========================================================
# 管理者
# /levelrole_set
# =========================================================

@bot.tree.command(
    name="levelrole_set",
    description="指定レベルから付与するランクロールを設定",
    guild=GUILD_OBJECT
)
@app_commands.default_permissions(
    manage_guild=True
)
@app_commands.describe(
    level="このロールになる開始レベル",
    role="付与するロール"
)
async def levelrole_set(
    interaction: discord.Interaction,
    level: app_commands.Range[int, 1, 1000],
    role: discord.Role
):

    if not await require_admin(
        interaction
    ):
        return

    if role.is_default():

        await interaction.response.send_message(
            "❌ @everyone は設定できません。",
            ephemeral=True
        )

        return

    db_set_level_role(
        level,
        role.id
    )

    await interaction.response.send_message(
        (
            f"✅ **Lv.{level}** から\n"
            f"{role.mention}\n"
            "になるように設定しました。\n\n"
            "以前のレベルロールは自動で外れます。"
        ),
        ephemeral=True
    )

    asyncio.create_task(
        sync_all_level_roles(
            interaction.guild
        )
    )


# =========================================================
# 管理者
# /levelrole_remove
# =========================================================

@bot.tree.command(
    name="levelrole_remove",
    description="レベルロール設定を削除",
    guild=GUILD_OBJECT
)
@app_commands.default_permissions(
    manage_guild=True
)
async def levelrole_remove(
    interaction: discord.Interaction,
    level: app_commands.Range[int, 1, 1000]
):

    if not await require_admin(
        interaction
    ):
        return

    db_remove_level_role(
        level
    )

    await interaction.response.send_message(
        f"✅ Lv.{level} のロール設定を削除しました。",
        ephemeral=True
    )

    asyncio.create_task(
        sync_all_level_roles(
            interaction.guild
        )
    )


# =========================================================
# 管理者
# /levelrole_list
# =========================================================

@bot.tree.command(
    name="levelrole_list",
    description="設定中のレベルロールを見る",
    guild=GUILD_OBJECT
)
@app_commands.default_permissions(
    manage_guild=True
)
async def levelrole_list(
    interaction: discord.Interaction
):

    if not await require_admin(
        interaction
    ):
        return

    rows = db_get_level_roles()

    if not rows:

        await interaction.response.send_message(
            "現在レベルロールは設定されていません。",
            ephemeral=True
        )

        return

    lines = []

    for row in rows:

        level = int(
            row["level"]
        )

        role_id = int(
            row["role_id"]
        )

        role = interaction.guild.get_role(
            role_id
        )

        if role:

            role_text = role.mention

        else:

            role_text = (
                f"削除済みロール ({role_id})"
            )

        lines.append(
            f"**Lv.{level}** → {role_text}"
        )

    await interaction.response.send_message(
        "🍬 **Candy レベルロール設定**\n\n"
        +
        "\n".join(
            lines
        ),
        ephemeral=True
    )


# =========================================================
# 管理者
# /level_set
# =========================================================

@bot.tree.command(
    name="level_set",
    description="メンバーのレベルを指定レベルに変更",
    guild=GUILD_OBJECT
)
@app_commands.default_permissions(
    manage_guild=True
)
async def level_set(
    interaction: discord.Interaction,
    member: discord.Member,
    level: app_commands.Range[int, 1, 1000]
):

    if not await require_admin(
        interaction
    ):
        return

    await flush_voice_session(
        member.id,
        keep_running=(
            member.voice is not None
            and
            member.voice.channel is not None
            and
            is_countable_voice_channel(
                member.guild,
                member.voice.channel
            )
        )
    )

    seconds = (
        required_seconds_for_level(
            level
        )
    )

    db_set_seconds(
        member.id,
        seconds
    )

    await sync_level_roles(
        member
    )

    await interaction.response.send_message(
        (
            f"✅ {member.mention} を "
            f"**Lv.{level}** に設定しました。\n"
            f"VC時間：**{format_duration(seconds)}**"
        ),
        ephemeral=True
    )


# =========================================================
# 管理者
# /leveltime_add
# =========================================================

@bot.tree.command(
    name="leveltime_add",
    description="メンバーのVC時間を追加",
    guild=GUILD_OBJECT
)
@app_commands.default_permissions(
    manage_guild=True
)
async def leveltime_add(
    interaction: discord.Interaction,
    member: discord.Member,
    hours: app_commands.Range[float, 0.1, 10000.0]
):

    if not await require_admin(
        interaction
    ):
        return

    await flush_voice_session(
        member.id,
        keep_running=(
            member.voice is not None
            and
            member.voice.channel is not None
            and
            is_countable_voice_channel(
                member.guild,
                member.voice.channel
            )
        )
    )

    db_add_seconds(
        member.id,
        hours * 3600
    )

    await sync_level_roles(
        member
    )

    total = get_live_total_seconds(
        member.id
    )

    level = calculate_level(
        total
    )

    await interaction.response.send_message(
        (
            f"✅ {member.mention} に "
            f"**{hours:g}時間** 追加しました。\n\n"
            f"現在：**Lv.{level}**\n"
            f"累計：**{format_duration(total)}**"
        ),
        ephemeral=True
    )


# =========================================================
# 管理者
# /leveltime_remove
# =========================================================

@bot.tree.command(
    name="leveltime_remove",
    description="メンバーのVC時間を減らす",
    guild=GUILD_OBJECT
)
@app_commands.default_permissions(
    manage_guild=True
)
async def leveltime_remove(
    interaction: discord.Interaction,
    member: discord.Member,
    hours: app_commands.Range[float, 0.1, 10000.0]
):

    if not await require_admin(
        interaction
    ):
        return

    await flush_voice_session(
        member.id,
        keep_running=(
            member.voice is not None
            and
            member.voice.channel is not None
            and
            is_countable_voice_channel(
                member.guild,
                member.voice.channel
            )
        )
    )

    current = db_get_seconds(
        member.id
    )

    new_seconds = max(
        0,
        current
        -
        (
            hours
            *
            3600
        )
    )

    db_set_seconds(
        member.id,
        new_seconds
    )

    await sync_level_roles(
        member
    )

    level = calculate_level(
        new_seconds
    )

    await interaction.response.send_message(
        (
            f"✅ {member.mention} から "
            f"**{hours:g}時間** 減らしました。\n\n"
            f"現在：**Lv.{level}**\n"
            f"累計：**{format_duration(new_seconds)}**"
        ),
        ephemeral=True
    )


# =========================================================
# 管理者
# /level_reset
# =========================================================

@bot.tree.command(
    name="level_reset",
    description="メンバーのレベルとVC時間をリセット",
    guild=GUILD_OBJECT
)
@app_commands.default_permissions(
    manage_guild=True
)
async def level_reset(
    interaction: discord.Interaction,
    member: discord.Member
):

    if not await require_admin(
        interaction
    ):
        return

    await flush_voice_session(
        member.id,
        keep_running=False
    )

    db_set_seconds(
        member.id,
        0
    )

    if (
        member.voice
        and
        member.voice.channel
        and
        is_countable_voice_channel(
            member.guild,
            member.voice.channel
        )
    ):

        start_voice_session(
            member
        )

    await sync_level_roles(
        member
    )

    await interaction.response.send_message(
        (
            f"✅ {member.mention} のレベルを"
            "**Lv.1** にリセットしました。"
        ),
        ephemeral=True
    )


# =========================================================
# 管理者
# /levelinfo
# =========================================================

@bot.tree.command(
    name="levelinfo",
    description="メンバーの詳しいレベル情報を見る",
    guild=GUILD_OBJECT
)
@app_commands.default_permissions(
    manage_guild=True
)
async def levelinfo(
    interaction: discord.Interaction,
    member: discord.Member
):

    if not await require_admin(
        interaction
    ):
        return

    total = get_live_total_seconds(
        member.id
    )

    level = calculate_level(
        total
    )

    next_seconds = (
        required_seconds_for_level(
            level + 1
        )
    )

    remaining = max(
        0,
        next_seconds
        -
        total
    )

    await interaction.response.send_message(
        (
            f"🍬 **{member.display_name}**\n\n"
            f"レベル：**Lv.{level}**\n"
            f"累計VC：**{format_duration(total)}**\n"
            f"次のレベルまで：**{format_duration(remaining)}**"
        ),
        ephemeral=True
    )


# =========================================================
# VC変化
# =========================================================

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState
):

    if member.bot:
        return

    if member.guild.id != GUILD_ID:
        return

    guild = member.guild

    # -----------------------------------------------------
    # LEVEL TRACKER
    #
    # ミュート状態は完全無視
    # VCにいるだけで加算
    # -----------------------------------------------------

    before_countable = (
        is_countable_voice_channel(
            guild,
            before.channel
        )
    )

    after_countable = (
        is_countable_voice_channel(
            guild,
            after.channel
        )
    )

    # 普通のVCへ入った
    if (
        not before_countable
        and
        after_countable
    ):

        start_voice_session(
            member
        )

    # 普通のVCから退出 / AFK移動
    elif (
        before_countable
        and
        not after_countable
    ):

        await flush_voice_session(
            member.id,
            keep_running=False
        )

        await sync_level_roles(
            member
        )

    # -----------------------------------------------------
    # ミュート等の変化だけなら
    # プロフィール処理はしない
    # -----------------------------------------------------

    if before.channel == after.channel:
        return

    log.info(
        "VC変化: %s | %s -> %s",
        member,
        (
            before.channel.name
            if before.channel
            else "NONE"
        ),
        (
            after.channel.name
            if after.channel
            else "NONE"
        )
    )

    # -----------------------------------------------------
    # 古いプロフィール予約キャンセル
    # -----------------------------------------------------

    old_task = profile_tasks.pop(
        member.id,
        None
    )

    if old_task:

        old_task.cancel()

        try:
            await old_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    # -----------------------------------------------------
    # 元VCプロフィール削除
    # -----------------------------------------------------

    if before.channel:

        await delete_profile_cards_from_channel(
            before.channel,
            member.id
        )

    await delete_profile_card(
        member
    )

    # -----------------------------------------------------
    # 完全退出
    # -----------------------------------------------------

    if after.channel is None:

        log.info(
            "✅ VC退出: %s",
            member
        )

        return

    # -----------------------------------------------------
    # 新VCプロフィール予約
    # -----------------------------------------------------

    task = asyncio.create_task(
        delayed_profile_post(
            member,
            after.channel.id
        )
    )

    profile_tasks[
        member.id
    ] = task


# =========================================================
# プロフィール投稿キャッシュ
# =========================================================

@bot.event
async def on_message(
    message: discord.Message
):

    if message.author.bot:
        return

    if message.guild is None:
        return

    if message.guild.id != GUILD_ID:
        return

    if message.channel.id in {
        MALE_PROFILE_CHANNEL_ID,
        FEMALE_PROFILE_CHANNEL_ID
    }:

        profile_cache[
            message.author.id
        ] = message

        log.info(
            "✅ プロフィールキャッシュ更新: %s",
            message.author
        )

    await bot.process_commands(
        message
    )


# =========================================================
# プロフィール削除
# =========================================================

@bot.event
async def on_raw_message_delete(
    payload: discord.RawMessageDeleteEvent
):

    if payload.guild_id != GUILD_ID:
        return

    if payload.channel_id not in {
        MALE_PROFILE_CHANNEL_ID,
        FEMALE_PROFILE_CHANNEL_ID
    }:
        return

    remove_users = []

    for user_id, message in profile_cache.items():

        if (
            message.id
            ==
            payload.message_id
        ):

            remove_users.append(
                user_id
            )

    for user_id in remove_users:

        profile_cache.pop(
            user_id,
            None
        )


# =========================================================
# 新規参加
# =========================================================

@bot.event
async def on_member_join(
    member: discord.Member
):

    if member.guild.id != GUILD_ID:
        return

    if member.bot:
        return

    # Lv1ロールが設定されていれば
    # Candy新人などを自動付与
    await sync_level_roles(
        member
    )


# =========================================================
# READY
# =========================================================

synced_once = False


@bot.event
async def on_ready():

    global synced_once

    guild = bot.get_guild(
        GUILD_ID
    )

    if guild is None:

        log.error(
            "Candyサーバーが見つかりません。"
        )

        return

    # -----------------------------------------------------
    # Slash Command同期
    # -----------------------------------------------------

    if not synced_once:

        try:

            synced = await bot.tree.sync(
                guild=GUILD_OBJECT
            )

            log.info(
                "✅ Slash Command同期: %s個",
                len(synced)
            )

            synced_once = True

        except Exception:

            log.exception(
                "Slash Command同期エラー"
            )

    # -----------------------------------------------------
    # Bot起動時すでにVCにいる人
    # -----------------------------------------------------

    for channel in guild.voice_channels:

        # AFKは除外
        if not is_countable_voice_channel(
            guild,
            channel
        ):
            continue

        for member in channel.members:

            if member.bot:
                continue

            if (
                member.id
                not in
                voice_sessions
            ):

                start_voice_session(
                    member
                )

    # -----------------------------------------------------
    # 1分保存ループ
    # -----------------------------------------------------

    if not level_flush_loop.is_running():

        level_flush_loop.start()

    log.info(
        "================================"
    )

    log.info(
        "🍬 Candy Bot 起動成功"
    )

    log.info(
        "Bot: %s",
        bot.user
    )

    log.info(
        "Server ID: %s",
        GUILD_ID
    )

    log.info(
        "👤 VCプロフィール: ON"
    )

    log.info(
        "🎤 VCレベル: ON"
    )

    log.info(
        "🔇 ミュート中: 加算"
    )

    log.info(
        "👤 1人VC: 加算"
    )

    log.info(
        "💤 AFK VC: 加算しない"
    )

    log.info(
        "⏱️ Lv5到達: 累計10時間"
    )

    log.info(
        "🍬 自動レベルロール: ON"
    )

    log.info(
        "================================"
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    start_web_server()

    if not TOKEN:

        raise RuntimeError(
            "DISCORD_TOKEN が設定されていません。"
        )

    bot.run(
        TOKEN
    )
