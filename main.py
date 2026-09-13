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
from PIL import Image, ImageDraw, ImageEnhance, ImageFont


# =========================================================
# 🍬 Candy
# VCプロフィール + Voice/Text/Total Level Bot
#
# ★ レベル低下防止版
# 一度到達したレベルは通常処理では下がりません。
# 管理者が減算 / リセットした場合のみ下がります。
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()


# =========================================================
# Candy サーバー設定
# =========================================================

GUILD_ID = 1542420058775494666

ADMIN_ROLE_ID = 1542422448895565824

MALE_PROFILE_CHANNEL_ID = 1542429626905657415
FEMALE_PROFILE_CHANNEL_ID = 1542429755750486026

PROFILE_DELAY = 5
DUPLICATE_CHECK_LIMIT = 20


# =========================================================
# Text XP
# =========================================================

# 同じ人がTC XPをもらえる間隔
TEXT_XP_COOLDOWN = 60

# これ未満の文字数はXPなし
TEXT_MIN_LENGTH = 3

# 1回の有効発言で増えるXP
TEXT_XP_PER_MESSAGE = 1


# =========================================================
# DATABASE
# =========================================================

DB_PATH = os.getenv(
    "DATABASE_PATH",
    "data/candy_level.db"
)

DATA_DIR = Path(DB_PATH).parent

BACKGROUND_DIR = (
    DATA_DIR
    /
    "level_backgrounds"
)

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True
)

BACKGROUND_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# =========================================================
# Render
# =========================================================

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
    "candy-level-profile"
)


# =========================================================
# DATABASE接続
# =========================================================

def db_connect():

    conn = sqlite3.connect(
        DB_PATH,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


# =========================================================
# DATABASE初期化
# =========================================================

def init_database():

    with db_connect() as conn:

        # -----------------------------------------
        # VC累計時間
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
        # TC XP
        # -----------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS text_stats (
                user_id INTEGER PRIMARY KEY,
                total_xp INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        # -----------------------------------------
        # レベルロール
        # -----------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS level_roles (
                level INTEGER PRIMARY KEY,
                role_id INTEGER NOT NULL
            )
            """
        )

        # -----------------------------------------
        # ★ 最高到達レベル
        #
        # 通常計算値が一時的に低くなっても
        # ここに保存した最高Lvより下げない
        # -----------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS level_peaks (
                user_id INTEGER PRIMARY KEY,
                voice_level INTEGER NOT NULL DEFAULT 1,
                text_level INTEGER NOT NULL DEFAULT 1,
                total_level INTEGER NOT NULL DEFAULT 2
            )
            """
        )

        conn.commit()


init_database()


# =========================================================
# VC DB
# =========================================================

def db_get_voice_seconds(
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


def db_set_voice_seconds(
    user_id: int,
    seconds: float
):

    seconds = max(
        0.0,
        float(seconds)
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


def db_add_voice_seconds(
    user_id: int,
    seconds: float
):

    seconds = float(
        seconds
    )

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
# TEXT DB
# =========================================================

def db_get_text_xp(
    user_id: int
) -> int:

    with db_connect() as conn:

        row = conn.execute(
            """
            SELECT total_xp
            FROM text_stats
            WHERE user_id = ?
            """,
            (
                user_id,
            )
        ).fetchone()

    if row is None:
        return 0

    return int(
        row["total_xp"]
    )


def db_set_text_xp(
    user_id: int,
    xp: int
):

    xp = max(
        0,
        int(xp)
    )

    with db_connect() as conn:

        conn.execute(
            """
            INSERT INTO text_stats (
                user_id,
                total_xp
            )
            VALUES (?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                total_xp = excluded.total_xp
            """,
            (
                user_id,
                xp
            )
        )

        conn.commit()


def db_add_text_xp(
    user_id: int,
    xp: int
):

    xp = int(
        xp
    )

    if xp <= 0:
        return

    with db_connect() as conn:

        conn.execute(
            """
            INSERT INTO text_stats (
                user_id,
                total_xp
            )
            VALUES (?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                total_xp =
                    text_stats.total_xp
                    +
                    excluded.total_xp
            """,
            (
                user_id,
                xp
            )
        )

        conn.commit()


# =========================================================
# ★ 最高到達レベル DB
# =========================================================

def db_get_peaks(
    user_id: int
):

    with db_connect() as conn:

        row = conn.execute(
            """
            SELECT
                voice_level,
                text_level,
                total_level
            FROM level_peaks
            WHERE user_id = ?
            """,
            (
                user_id,
            )
        ).fetchone()

    if row is None:

        return (
            1,
            1,
            2
        )

    return (
        int(
            row["voice_level"]
        ),
        int(
            row["text_level"]
        ),
        int(
            row["total_level"]
        )
    )


def db_save_peak_levels(
    user_id: int,
    voice_level: int,
    text_level: int,
    total_level: int
):

    """
    通常処理用。

    既存の最高到達Lvより高い場合のみ更新。
    低い値が来ても下げません。
    """

    with db_connect() as conn:

        conn.execute(
            """
            INSERT INTO level_peaks (
                user_id,
                voice_level,
                text_level,
                total_level
            )
            VALUES (?, ?, ?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                voice_level =
                    MAX(
                        level_peaks.voice_level,
                        excluded.voice_level
                    ),

                text_level =
                    MAX(
                        level_peaks.text_level,
                        excluded.text_level
                    ),

                total_level =
                    MAX(
                        level_peaks.total_level,
                        excluded.total_level
                    )
            """,
            (
                user_id,
                voice_level,
                text_level,
                total_level
            )
        )

        conn.commit()


def db_force_peak_levels(
    user_id: int,
    voice_level: int,
    text_level: int,
    total_level: int
):

    """
    管理者が意図的にレベルを減らした時用。

    この関数だけは最高到達Lvを下げられます。
    """

    with db_connect() as conn:

        conn.execute(
            """
            INSERT INTO level_peaks (
                user_id,
                voice_level,
                text_level,
                total_level
            )
            VALUES (?, ?, ?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                voice_level = excluded.voice_level,
                text_level = excluded.text_level,
                total_level = excluded.total_level
            """,
            (
                user_id,
                voice_level,
                text_level,
                total_level
            )
        )

        conn.commit()


# =========================================================
# レベルロール DB
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
            SELECT
                level,
                role_id
            FROM level_roles
            ORDER BY level ASC
            """
        ).fetchall()

    return rows


# =========================================================
# VOICE LEVEL
#
# Lv5 = 累計10時間
# =========================================================

def voice_required_hours(
    level: int
) -> float:

    if level <= 1:
        return 0.0

    fixed = {
        2: 1.0,
        3: 2.5,
        4: 5.0,
        5: 10.0,
    }

    if level in fixed:
        return fixed[level]

    n = level - 5

    return (
        10.0
        +
        3.5 * n
        +
        0.15 * (n ** 2)
    )


def voice_required_seconds(
    level: int
) -> float:

    return (
        voice_required_hours(
            level
        )
        *
        3600
    )


def calculate_voice_level(
    seconds: float
) -> int:

    seconds = max(
        0.0,
        float(seconds)
    )

    level = 1

    for next_level in range(
        2,
        1001
    ):

        if (
            seconds
            <
            voice_required_seconds(
                next_level
            )
        ):

            break

        level = next_level

    return level


# =========================================================
# TEXT LEVEL
# =========================================================

def text_required_xp(
    level: int
) -> int:

    if level <= 1:
        return 0

    fixed = {
        2: 30,
        3: 90,
        4: 180,
        5: 300,
    }

    if level in fixed:
        return fixed[level]

    n = level - 5

    return int(
        300
        +
        100 * n
        +
        15 * (n ** 2)
    )


def calculate_text_level(
    xp: int
) -> int:

    xp = max(
        0,
        int(xp)
    )

    level = 1

    for next_level in range(
        2,
        1001
    ):

        if (
            xp
            <
            text_required_xp(
                next_level
            )
        ):

            break

        level = next_level

    return level


# =========================================================
# TOTAL LEVEL
# =========================================================

def calculate_total_level(
    voice_level: int,
    text_level: int
) -> int:

    return (
        int(voice_level)
        +
        int(text_level)
    )


# =========================================================
# 進捗計算
# =========================================================

def voice_progress_from_level(
    seconds: float,
    level: int
):

    current_required = (
        voice_required_seconds(
            level
        )
    )

    next_required = (
        voice_required_seconds(
            level + 1
        )
    )

    current = max(
        0.0,
        seconds
        -
        current_required
    )

    needed = max(
        1.0,
        next_required
        -
        current_required
    )

    percent = max(
        0.0,
        min(
            1.0,
            current / needed
        )
    )

    return (
        current,
        needed,
        percent
    )


def text_progress_from_level(
    xp: int,
    level: int
):

    current_required = (
        text_required_xp(
            level
        )
    )

    next_required = (
        text_required_xp(
            level + 1
        )
    )

    current = max(
        0,
        xp
        -
        current_required
    )

    needed = max(
        1,
        next_required
        -
        current_required
    )

    percent = max(
        0.0,
        min(
            1.0,
            current / needed
        )
    )

    return (
        current,
        needed,
        percent
    )


# =========================================================
# 時間表示
# =========================================================

def format_duration(
    seconds: float
):

    seconds = max(
        0,
        int(seconds)
    )

    hours = (
        seconds
        //
        3600
    )

    minutes = (
        seconds
        %
        3600
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

        self.send_response(
            200
        )

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
            "Health server started: %s",
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
# メモリ
# =========================================================

profile_tasks = {}
profile_messages = {}
profile_cache = {}

voice_sessions = {}

text_xp_cooldowns = {}


# =========================================================
# 管理者判定
# =========================================================

def is_admin_member(
    member: discord.Member
):

    if member.guild_permissions.administrator:
        return True

    return any(
        role.id
        ==
        ADMIN_ROLE_ID

        for role in member.roles
    )


async def require_admin(
    interaction: discord.Interaction
):

    if not isinstance(
        interaction.user,
        discord.Member
    ):

        await interaction.response.send_message(
            "❌ サーバー内で使用してください。",
            ephemeral=True
        )

        return False

    if is_admin_member(
        interaction.user
    ):

        return True

    await interaction.response.send_message(
        "❌ 管理者専用コマンドです。",
        ephemeral=True
    )

    return False


# =========================================================
# VC加算対象判定
# =========================================================

def is_countable_voice_channel(
    guild: discord.Guild,
    channel
):

    if channel is None:
        return False

    # AFKチャンネルは加算しない
    if (
        guild.afk_channel
        and
        guild.afk_channel.id
        ==
        channel.id
    ):

        return False

    return True


# =========================================================
# VC SESSION
# =========================================================

def start_voice_session(
    member: discord.Member
):

    if member.bot:
        return

    if member.id in voice_sessions:
        return

    voice_sessions[
        member.id
    ] = time.time()

    log.info(
        "🎤 VC計測開始: %s",
        member
    )


async def flush_voice_session(
    user_id: int,
    keep_running: bool = True
):

    started = voice_sessions.get(
        user_id
    )

    if started is None:
        return

    now = time.time()

    elapsed = max(
        0.0,
        now - started
    )

    if elapsed > 0:

        db_add_voice_seconds(
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


def get_live_voice_seconds(
    user_id: int
):

    total = db_get_voice_seconds(
        user_id
    )

    started = voice_sessions.get(
        user_id
    )

    if started is not None:

        total += max(
            0.0,
            time.time()
            -
            started
        )

    return total


# =========================================================
# ★ レベル取得
#
# calculate → 現在計算値
# peak      → 最高到達値
#
# 通常表示では高い方を使用
# =========================================================

def get_member_levels(
    user_id: int,
    save_peak: bool = True
):

    voice_seconds = (
        get_live_voice_seconds(
            user_id
        )
    )

    text_xp = (
        db_get_text_xp(
            user_id
        )
    )

    calculated_voice = (
        calculate_voice_level(
            voice_seconds
        )
    )

    calculated_text = (
        calculate_text_level(
            text_xp
        )
    )

    calculated_total = (
        calculate_total_level(
            calculated_voice,
            calculated_text
        )
    )

    (
        peak_voice,
        peak_text,
        peak_total
    ) = db_get_peaks(
        user_id
    )

    voice_level = max(
        calculated_voice,
        peak_voice
    )

    text_level = max(
        calculated_text,
        peak_text
    )

    total_level = max(
        calculate_total_level(
            voice_level,
            text_level
        ),
        calculated_total,
        peak_total
    )

    if save_peak:

        db_save_peak_levels(
            user_id,
            voice_level,
            text_level,
            total_level
        )

    return (
        voice_seconds,
        text_xp,
        voice_level,
        text_level,
        total_level
    )


# =========================================================
# 管理者が意図的に減算した後
# 最高到達レベルも現在値へ更新
# =========================================================

def force_current_levels_as_peak(
    user_id: int
):

    voice_seconds = (
        get_live_voice_seconds(
            user_id
        )
    )

    text_xp = (
        db_get_text_xp(
            user_id
        )
    )

    voice_level = (
        calculate_voice_level(
            voice_seconds
        )
    )

    text_level = (
        calculate_text_level(
            text_xp
        )
    )

    total_level = (
        calculate_total_level(
            voice_level,
            text_level
        )
    )

    db_force_peak_levels(
        user_id,
        voice_level,
        text_level,
        total_level
    )

    return (
        voice_level,
        text_level,
        total_level
    )


# =========================================================
# LEVEL ROLE
# =========================================================

def get_target_level_role_id(
    total_level: int
):

    target = None

    for row in db_get_level_roles():

        required_level = int(
            row["level"]
        )

        if (
            total_level
            >=
            required_level
        ):

            target = int(
                row["role_id"]
            )

        else:

            break

    return target


async def sync_level_roles(
    member: discord.Member
):

    if member.bot:
        return

    (
        voice_seconds,
        text_xp,
        voice_level,
        text_level,
        total_level
    ) = get_member_levels(
        member.id,
        save_peak=True
    )

    settings = (
        db_get_level_roles()
    )

    if not settings:
        return

    configured_ids = {
        int(
            row["role_id"]
        )
        for row in settings
    }

    target_role_id = (
        get_target_level_role_id(
            total_level
        )
    )

    remove_roles = []

    for role in member.roles:

        if (
            role.id
            in configured_ids
            and
            role.id
            != target_role_id
        ):

            remove_roles.append(
                role
            )

    try:

        if remove_roles:

            await member.remove_roles(
                *remove_roles,
                reason="Candy レベルランク更新"
            )

    except discord.Forbidden:

        log.warning(
            "ロール削除権限なし: %s",
            member
        )

    except Exception:

        log.exception(
            "ランクロール削除失敗"
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
            reason=(
                f"Candy Total Lv."
                f"{total_level}"
            )
        )

        log.info(
            "🍬 ランク更新: %s → %s",
            member,
            target_role.name
        )

    except discord.Forbidden:

        log.warning(
            "ロール付与権限なし: %s",
            target_role.name
        )

    except Exception:

        log.exception(
            "ランクロール付与失敗"
        )


# =========================================================
# 1分ごとVC保存
# =========================================================

@tasks.loop(
    seconds=60
)
async def voice_save_loop():

    guild = bot.get_guild(
        GUILD_ID
    )

    if guild is None:
        return

    for user_id in list(
        voice_sessions.keys()
    ):

        member = guild.get_member(
            user_id
        )

        if member is None:

            await flush_voice_session(
                user_id,
                False
            )

            continue

        if (
            member.voice is None
            or
            member.voice.channel is None
        ):

            await flush_voice_session(
                user_id,
                False
            )

            await sync_level_roles(
                member
            )

            continue

        if not is_countable_voice_channel(
            guild,
            member.voice.channel
        ):

            await flush_voice_session(
                user_id,
                False
            )

            continue

        await flush_voice_session(
            user_id,
            True
        )

        await sync_level_roles(
            member
        )


@voice_save_loop.before_loop
async def before_voice_save_loop():

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

    for channel_id in [
        MALE_PROFILE_CHANNEL_ID,
        FEMALE_PROFILE_CHANNEL_ID
    ]:

        channel = (
            member.guild.get_channel(
                channel_id
            )
        )

        if channel is None:
            continue

        try:

            async for message in channel.history(
                limit=None
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
                "プロフィール検索失敗"
            )

    return None


# =========================================================
# プロフィール二重チェック
# =========================================================

async def profile_already_exists(
    channel,
    user_id: int
):

    targets = (
        f"ID:{user_id}",
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

                if any(
                    target in description
                    for target in targets
                ):

                    return message

            content = (
                message.content
                or
                ""
            )

            if any(
                target in content
                for target in targets
            ):

                return message

    except Exception:

        log.exception(
            "プロフィール二重確認失敗"
        )

    return None


# =========================================================
# プロフィール削除
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

    channel = (
        member.guild.get_channel(
            data["channel_id"]
        )
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
            "プロフィール削除失敗"
        )


async def delete_profile_cards_from_channel(
    channel,
    user_id: int
):

    targets = (
        f"ID:{user_id}",
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

                text = (
                    embed.description
                    or
                    ""
                )

                if any(
                    target in text
                    for target in targets
                ):

                    matched = True
                    break

            if not matched:

                content = (
                    message.content
                    or
                    ""
                )

                if any(
                    target in content
                    for target in targets
                ):

                    matched = True

            if matched:

                try:

                    await message.delete()

                except Exception:
                    pass

    except Exception:

        log.exception(
            "履歴プロフィール削除失敗"
        )


# =========================================================
# プロフィール投稿
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

        current_member = (
            guild.get_member(
                member.id
            )
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

        duplicate = (
            await profile_already_exists(
                current_vc,
                current_member.id
            )
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

        profile_message = (
            await find_profile_message(
                current_member
            )
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

        duplicate = (
            await profile_already_exists(
                current_vc,
                current_member.id
            )
        )

        if duplicate:
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

    except asyncio.CancelledError:
        return

    except Exception:

        log.exception(
            "プロフィール投稿失敗"
        )

    finally:

        task = asyncio.current_task()

        if (
            profile_tasks.get(
                member.id
            )
            is task
        ):

            profile_tasks.pop(
                member.id,
                None
            )


# =========================================================
# FONT
# =========================================================

def get_font(
    size: int
):

    paths = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]

    for path in paths:

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
# 標準背景
# =========================================================

def make_default_background(
    width: int,
    height: int
):

    image = Image.new(
        "RGB",
        (
            width,
            height
        ),
        (
            52,
            37,
            74
        )
    )

    draw = ImageDraw.Draw(
        image
    )

    for y in range(
        height
    ):

        ratio = (
            y
            /
            height
        )

        draw.line(
            (
                0,
                y,
                width,
                y
            ),
            fill=(
                int(
                    55
                    +
                    45 * ratio
                ),
                int(
                    38
                    +
                    25 * ratio
                ),
                int(
                    80
                    +
                    55 * ratio
                )
            )
        )

    draw.ellipse(
        (
            680,
            -180,
            1100,
            240
        ),
        fill=(
            125,
            85,
            160
        )
    )

    draw.ellipse(
        (
            -170,
            220,
            270,
            660
        ),
        fill=(
            85,
            55,
            115
        )
    )

    return image


# =========================================================
# 個人背景
# =========================================================

def get_background_path(
    user_id: int
):

    return (
        BACKGROUND_DIR
        /
        f"{user_id}.jpg"
    )


def save_background_image(
    user_id: int,
    data: bytes
):

    image = Image.open(
        io.BytesIO(
            data
        )
    )

    image = image.convert(
        "RGB"
    )

    target_w = 1000
    target_h = 420

    source_ratio = (
        image.width
        /
        image.height
    )

    target_ratio = (
        target_w
        /
        target_h
    )

    if source_ratio > target_ratio:

        new_h = target_h

        new_w = int(
            new_h
            *
            source_ratio
        )

    else:

        new_w = target_w

        new_h = int(
            new_w
            /
            source_ratio
        )

    image = image.resize(
        (
            new_w,
            new_h
        ),
        Image.Resampling.LANCZOS
    )

    left = (
        new_w
        -
        target_w
    ) // 2

    top = (
        new_h
        -
        target_h
    ) // 2

    image = image.crop(
        (
            left,
            top,
            left + target_w,
            top + target_h
        )
    )

    image.save(
        get_background_path(
            user_id
        ),
        "JPEG",
        quality=90
    )


# =========================================================
# LEVEL CARD
# =========================================================

def make_level_card(
    avatar_bytes: bytes,
    display_name: str,
    total_level: int,
    voice_level: int,
    text_level: int,
    voice_seconds: float,
    text_xp: int,
    voice_percent: float,
    voice_current: float,
    voice_needed: float,
    text_percent: float,
    text_current: int,
    text_needed: int,
    role_name: str,
    background_path: str
):

    width = 1000
    height = 420

    if (
        background_path
        and
        os.path.exists(
            background_path
        )
    ):

        try:

            image = Image.open(
                background_path
            ).convert(
                "RGB"
            )

            image = image.resize(
                (
                    width,
                    height
                )
            )

        except Exception:

            image = (
                make_default_background(
                    width,
                    height
                )
            )

    else:

        image = (
            make_default_background(
                width,
                height
            )
        )

    enhancer = ImageEnhance.Brightness(
        image
    )

    image = enhancer.enhance(
        0.58
    )

    draw = ImageDraw.Draw(
        image,
        "RGBA"
    )

    draw.rounded_rectangle(
        (
            25,
            25,
            975,
            395
        ),
        radius=30,
        fill=(
            15,
            12,
            22,
            125
        )
    )

    # -----------------------------------------
    # Avatar
    # -----------------------------------------

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
                180,
                180
            )
        )

        mask = Image.new(
            "L",
            (
                180,
                180
            ),
            0
        )

        md = ImageDraw.Draw(
            mask
        )

        md.ellipse(
            (
                0,
                0,
                180,
                180
            ),
            fill=255
        )

        avatar.putalpha(
            mask
        )

        draw.ellipse(
            (
                54,
                79,
                246,
                271
            ),
            fill=(
                255,
                255,
                255,
                235
            )
        )

        image.paste(
            avatar,
            (
                60,
                85
            ),
            avatar
        )

    except Exception:

        log.exception(
            "Avatar描画失敗"
        )

    font_name = get_font(
        32
    )

    font_big = get_font(
        70
    )

    font_medium = get_font(
        25
    )

    font_small = get_font(
        18
    )

    font_tiny = get_font(
        15
    )

    # -----------------------------------------
    # 名前
    # -----------------------------------------

    draw.text(
        (
            285,
            45
        ),
        display_name[:22],
        font=font_name,
        fill=(
            255,
            255,
            255,
            255
        )
    )

    # -----------------------------------------
    # ランク
    # -----------------------------------------

    if role_name:

        draw.text(
            (
                286,
                89
            ),
            f"RANK  {role_name}",
            font=font_small,
            fill=(
                239,
                218,
                255,
                255
            )
        )

    # -----------------------------------------
    # TOTAL
    # -----------------------------------------

    draw.text(
        (
            790,
            45
        ),
        "TOTAL LEVEL",
        font=font_small,
        fill=(
            230,
            220,
            245,
            255
        )
    )

    draw.text(
        (
            830,
            70
        ),
        str(
            total_level
        ),
        font=font_big,
        fill=(
            255,
            255,
            255,
            255
        )
    )

    # -----------------------------------------
    # TEXT
    # -----------------------------------------

    draw.text(
        (
            285,
            145
        ),
        f"TEXT LEVEL  {text_level}",
        font=font_medium,
        fill=(
            255,
            255,
            255,
            255
        )
    )

    draw.text(
        (
            730,
            151
        ),
        f"XP {text_xp}",
        font=font_small,
        fill=(
            235,
            225,
            245,
            255
        )
    )

    tx = 285
    ty = 188
    tw = 620
    th = 20

    draw.rounded_rectangle(
        (
            tx,
            ty,
            tx + tw,
            ty + th
        ),
        radius=10,
        fill=(
            20,
            18,
            28,
            210
        )
    )

    text_fill = int(
        tw
        *
        text_percent
    )

    if text_fill > 0:

        draw.rounded_rectangle(
            (
                tx,
                ty,
                tx + text_fill,
                ty + th
            ),
            radius=10,
            fill=(
                236,
                179,
                255,
                255
            )
        )

    draw.text(
        (
            285,
            214
        ),
        f"{text_current} / {text_needed} XP",
        font=font_tiny,
        fill=(
            230,
            225,
            235,
            255
        )
    )

    # -----------------------------------------
    # VOICE
    # -----------------------------------------

    draw.text(
        (
            285,
            255
        ),
        f"VOICE LEVEL  {voice_level}",
        font=font_medium,
        fill=(
            255,
            255,
            255,
            255
        )
    )

    draw.text(
        (
            690,
            261
        ),
        format_duration(
            voice_seconds
        ),
        font=font_small,
        fill=(
            235,
            225,
            245,
            255
        )
    )

    vx = 285
    vy = 298
    vw = 620
    vh = 20

    draw.rounded_rectangle(
        (
            vx,
            vy,
            vx + vw,
            vy + vh
        ),
        radius=10,
        fill=(
            20,
            18,
            28,
            210
        )
    )

    voice_fill = int(
        vw
        *
        voice_percent
    )

    if voice_fill > 0:

        draw.rounded_rectangle(
            (
                vx,
                vy,
                vx + voice_fill,
                vy + vh
            ),
            radius=10,
            fill=(
                180,
                194,
                255,
                255
            )
        )

    draw.text(
        (
            285,
            324
        ),
        (
            f"{voice_current / 3600:.1f}h / "
            f"{voice_needed / 3600:.1f}h"
        ),
        font=font_tiny,
        fill=(
            230,
            225,
            235,
            255
        )
    )

    draw.text(
        (
            62,
            305
        ),
        "Candy",
        font=font_medium,
        fill=(
            255,
            235,
            255,
            255
        )
    )

    draw.text(
        (
            62,
            340
        ),
        "LEVEL CARD",
        font=font_tiny,
        fill=(
            225,
            210,
            230,
            255
        )
    )

    output = io.BytesIO()

    image.save(
        output,
        "PNG"
    )

    output.seek(
        0
    )

    return output


# =========================================================
# 一般
# /level
# =========================================================

@bot.tree.command(
    name="level",
    description="自分のCandyレベルを表示します",
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

    (
        voice_seconds,
        text_xp,
        voice_level,
        text_level,
        total_level
    ) = get_member_levels(
        member.id,
        save_peak=True
    )

    # -----------------------------------------
    # 進捗
    # -----------------------------------------

    (
        voice_current,
        voice_needed,
        voice_percent
    ) = voice_progress_from_level(
        voice_seconds,
        voice_level
    )

    (
        text_current,
        text_needed,
        text_percent
    ) = text_progress_from_level(
        text_xp,
        text_level
    )

    # -----------------------------------------
    # ランク
    # -----------------------------------------

    role_name = ""

    target_role_id = (
        get_target_level_role_id(
            total_level
        )
    )

    if target_role_id:

        role = interaction.guild.get_role(
            target_role_id
        )

        if role:

            role_name = role.name

    # -----------------------------------------
    # サーバーアバター優先
    # -----------------------------------------

    avatar = (
        member.guild_avatar
        or
        member.display_avatar
    )

    avatar_bytes = (
        await avatar.read()
    )

    background_path = str(
        get_background_path(
            member.id
        )
    )

    card = await asyncio.to_thread(
        make_level_card,
        avatar_bytes,
        member.display_name,
        total_level,
        voice_level,
        text_level,
        voice_seconds,
        text_xp,
        voice_percent,
        voice_current,
        voice_needed,
        text_percent,
        text_current,
        text_needed,
        role_name,
        background_path
    )

    file = discord.File(
        card,
        filename="candy_level.png"
    )

    await interaction.followup.send(
        file=file
    )


# =========================================================
# 一般
# /level_background
# =========================================================

@bot.tree.command(
    name="level_background",
    description="自分のレベルカード背景を変更します",
    guild=GUILD_OBJECT
)
@app_commands.describe(
    image="背景にしたい画像"
)
async def level_background(
    interaction: discord.Interaction,
    image: discord.Attachment
):

    member = interaction.user

    if not isinstance(
        member,
        discord.Member
    ):

        return

    if (
        image.size
        >
        8 * 1024 * 1024
    ):

        await interaction.response.send_message(
            "❌ 画像は8MB以下にしてください。",
            ephemeral=True
        )

        return

    content_type = (
        image.content_type
        or
        ""
    )

    if not content_type.startswith(
        "image/"
    ):

        await interaction.response.send_message(
            "❌ 画像ファイルを選択してください。",
            ephemeral=True
        )

        return

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        data = await image.read()

        await asyncio.to_thread(
            save_background_image,
            member.id,
            data
        )

        await interaction.followup.send(
            (
                "✅ レベルカードの背景を変更しました！🍬\n"
                "次に `/level` を使うと反映されます。"
            ),
            ephemeral=True
        )

    except Exception:

        log.exception(
            "背景画像保存失敗"
        )

        await interaction.followup.send(
            "❌ 画像の保存に失敗しました。",
            ephemeral=True
        )


# =========================================================
# 一般
# /level_background_reset
# =========================================================

@bot.tree.command(
    name="level_background_reset",
    description="レベルカードの背景を初期状態に戻します",
    guild=GUILD_OBJECT
)
async def level_background_reset(
    interaction: discord.Interaction
):

    path = get_background_path(
        interaction.user.id
    )

    try:

        if path.exists():

            path.unlink()

    except Exception:

        log.exception(
            "背景削除失敗"
        )

    await interaction.response.send_message(
        "✅ レベルカード背景をCandy標準に戻しました。",
        ephemeral=True
    )


# =========================================================
# 管理者
# LEVEL ROLE SET
# =========================================================

@bot.tree.command(
    name="levelrole_set",
    description="Total Levelに応じたロールを設定",
    guild=GUILD_OBJECT
)
@app_commands.default_permissions(
    manage_guild=True
)
async def levelrole_set(
    interaction: discord.Interaction,
    level: app_commands.Range[int, 1, 2000],
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
            f"✅ **Total Lv.{level}** から\n"
            f"{role.mention}\n"
            "になるよう設定しました。"
        ),
        ephemeral=True
    )


# =========================================================
# 管理者
# LEVEL ROLE REMOVE
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
    level: app_commands.Range[int, 1, 2000]
):

    if not await require_admin(
        interaction
    ):

        return

    db_remove_level_role(
        level
    )

    await interaction.response.send_message(
        f"✅ Total Lv.{level} の設定を削除しました。",
        ephemeral=True
    )


# =========================================================
# 管理者
# LEVEL ROLE LIST
# =========================================================

@bot.tree.command(
    name="levelrole_list",
    description="レベルロール設定を確認",
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
            "レベルロールはまだ設定されていません。",
            ephemeral=True
        )

        return

    lines = []

    for row in rows:

        role = interaction.guild.get_role(
            int(
                row["role_id"]
            )
        )

        role_text = (
            role.mention
            if role
            else "削除済みロール"
        )

        lines.append(
            (
                f"**Total Lv.{row['level']}**"
                f" → {role_text}"
            )
        )

    await interaction.response.send_message(
        (
            "🍬 **Candy Level Role**\n\n"
            +
            "\n".join(
                lines
            )
        ),
        ephemeral=True
    )


# =========================================================
# 管理者
# VOICE ADD
# =========================================================

@bot.tree.command(
    name="voice_add",
    description="メンバーのVC時間を追加",
    guild=GUILD_OBJECT
)
@app_commands.default_permissions(
    manage_guild=True
)
async def voice_add(
    interaction: discord.Interaction,
    member: discord.Member,
    hours: app_commands.Range[
        float,
        0.1,
        10000.0
    ]
):

    if not await require_admin(
        interaction
    ):

        return

    currently_counting = (
        member.voice is not None
        and
        member.voice.channel is not None
        and
        is_countable_voice_channel(
            member.guild,
            member.voice.channel
        )
    )

    await flush_voice_session(
        member.id,
        currently_counting
    )

    db_add_voice_seconds(
        member.id,
        hours * 3600
    )

    # 高くなった最高到達Lvを保存
    get_member_levels(
        member.id,
        save_peak=True
    )

    await sync_level_roles(
        member
    )

    await interaction.response.send_message(
        (
            f"✅ {member.mention} にVC "
            f"**{hours:g}時間**追加しました。"
        ),
        ephemeral=True
    )


# =========================================================
# 管理者
# VOICE REMOVE
#
# ★ 管理者操作なのでレベルを下げることを許可
# =========================================================

@bot.tree.command(
    name="voice_remove",
    description="メンバーのVC時間を減らす",
    guild=GUILD_OBJECT
)
@app_commands.default_permissions(
    manage_guild=True
)
async def voice_remove(
    interaction: discord.Interaction,
    member: discord.Member,
    hours: app_commands.Range[
        float,
        0.1,
        10000.0
    ]
):

    if not await require_admin(
        interaction
    ):

        return

    currently_counting = (
        member.voice is not None
        and
        member.voice.channel is not None
        and
        is_countable_voice_channel(
            member.guild,
            member.voice.channel
        )
    )

    await flush_voice_session(
        member.id,
        currently_counting
    )

    current = (
        db_get_voice_seconds(
            member.id
        )
    )

    new_seconds = max(
        0.0,
        current
        -
        hours * 3600
    )

    db_set_voice_seconds(
        member.id,
        new_seconds
    )

    # ★ 管理者操作なので最高Lvも現在値へ変更
    force_current_levels_as_peak(
        member.id
    )

    await sync_level_roles(
        member
    )

    await interaction.response.send_message(
        (
            f"✅ {member.mention} のVC時間を "
            f"**{hours:g}時間**減らしました。"
        ),
        ephemeral=True
    )


# =========================================================
# 管理者
# TEXT XP ADD
# =========================================================

@bot.tree.command(
    name="textxp_add",
    description="メンバーのText XPを追加",
    guild=GUILD_OBJECT
)
@app_commands.default_permissions(
    manage_guild=True
)
async def textxp_add(
    interaction: discord.Interaction,
    member: discord.Member,
    xp: app_commands.Range[
        int,
        1,
        1000000
    ]
):

    if not await require_admin(
        interaction
    ):

        return

    db_add_text_xp(
        member.id,
        xp
    )

    get_member_levels(
        member.id,
        save_peak=True
    )

    await sync_level_roles(
        member
    )

    await interaction.response.send_message(
        (
            f"✅ {member.mention} に "
            f"**{xp} Text XP**追加しました。"
        ),
        ephemeral=True
    )


# =========================================================
# 管理者
# TEXT XP REMOVE
#
# ★ 管理者操作なのでレベル低下を許可
# =========================================================

@bot.tree.command(
    name="textxp_remove",
    description="メンバーのText XPを減らす",
    guild=GUILD_OBJECT
)
@app_commands.default_permissions(
    manage_guild=True
)
async def textxp_remove(
    interaction: discord.Interaction,
    member: discord.Member,
    xp: app_commands.Range[
        int,
        1,
        1000000
    ]
):

    if not await require_admin(
        interaction
    ):

        return

    current = (
        db_get_text_xp(
            member.id
        )
    )

    new_xp = max(
        0,
        current
        -
        xp
    )

    db_set_text_xp(
        member.id,
        new_xp
    )

    # ★ 管理者操作なので最高Lvも下げる
    force_current_levels_as_peak(
        member.id
    )

    await sync_level_roles(
        member
    )

    await interaction.response.send_message(
        (
            f"✅ {member.mention} から "
            f"**{xp} Text XP**減らしました。"
        ),
        ephemeral=True
    )


# =========================================================
# 管理者
# LEVEL INFO
# =========================================================

@bot.tree.command(
    name="levelinfo",
    description="メンバーのレベル詳細を見る",
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

    (
        voice_seconds,
        text_xp,
        voice_level,
        text_level,
        total_level
    ) = get_member_levels(
        member.id,
        save_peak=True
    )

    (
        peak_voice,
        peak_text,
        peak_total
    ) = db_get_peaks(
        member.id
    )

    await interaction.response.send_message(
        (
            f"🍬 **{member.display_name}**\n\n"

            f"⭐ Total Level：**{total_level}**\n"
            f"🎤 Voice Level：**{voice_level}**\n"
            f"💬 Text Level：**{text_level}**\n\n"

            f"🎤 VC累計："
            f"**{format_duration(voice_seconds)}**\n"

            f"💬 Text XP："
            f"**{text_xp}**\n\n"

            f"🔒 保存済み最高Lv\n"
            f"Voice：**{peak_voice}**\n"
            f"Text：**{peak_text}**\n"
            f"Total：**{peak_total}**"
        ),
        ephemeral=True
    )


# =========================================================
# 管理者
# LEVEL RESET
# =========================================================

@bot.tree.command(
    name="level_reset",
    description="メンバーのVoice/Textレベルを完全リセット",
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
        False
    )

    db_set_voice_seconds(
        member.id,
        0
    )

    db_set_text_xp(
        member.id,
        0
    )

    # 初期状態
    # Voice 1 + Text 1 = Total 2

    db_force_peak_levels(
        member.id,
        1,
        1,
        2
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
            f"✅ {member.mention} の"
            "Voice / Textレベルを"
            "完全リセットしました。"
        ),
        ephemeral=True
    )


# =========================================================
# VC STATE
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

    # -----------------------------------------
    # 普通VCへ入室
    # -----------------------------------------

    if (
        not before_countable
        and
        after_countable
    ):

        start_voice_session(
            member
        )

    # -----------------------------------------
    # 普通VCから退出 / AFKへ
    # -----------------------------------------

    elif (
        before_countable
        and
        not after_countable
    ):

        await flush_voice_session(
            member.id,
            False
        )

        await sync_level_roles(
            member
        )

    # -----------------------------------------
    # 同じVC内でミュート等だけ変化
    #
    # VC時間は止まりません。
    # -----------------------------------------

    if (
        before.channel
        ==
        after.channel
    ):

        return

    # -----------------------------------------
    # プロフィール処理
    # -----------------------------------------

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

    if before.channel:

        await delete_profile_cards_from_channel(
            before.channel,
            member.id
        )

    await delete_profile_card(
        member
    )

    if after.channel is None:
        return

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
# MESSAGE
#
# プロフィールキャッシュ + Text XP
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

    # -----------------------------------------
    # プロフィールキャッシュ
    # -----------------------------------------

    if message.channel.id in {
        MALE_PROFILE_CHANNEL_ID,
        FEMALE_PROFILE_CHANNEL_ID
    }:

        profile_cache[
            message.author.id
        ] = message

    # -----------------------------------------
    # Text XP
    # -----------------------------------------

    content = (
        message.content
        or
        ""
    ).strip()

    if (
        len(content)
        >=
        TEXT_MIN_LENGTH
    ):

        user_id = (
            message.author.id
        )

        now = time.time()

        last = (
            text_xp_cooldowns.get(
                user_id,
                0
            )
        )

        if (
            now
            -
            last
            >=
            TEXT_XP_COOLDOWN
        ):

            db_add_text_xp(
                user_id,
                TEXT_XP_PER_MESSAGE
            )

            text_xp_cooldowns[
                user_id
            ] = now

            # ★ レベル最高値保存
            get_member_levels(
                user_id,
                save_peak=True
            )

            try:

                await sync_level_roles(
                    message.author
                )

            except Exception:

                log.exception(
                    "Text XPロール同期失敗"
                )

    await bot.process_commands(
        message
    )


# =========================================================
# プロフィール削除キャッシュ
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

    remove = []

    for user_id, message in profile_cache.items():

        if (
            message.id
            ==
            payload.message_id
        ):

            remove.append(
                user_id
            )

    for user_id in remove:

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

    # 初期最高Lv作成
    db_save_peak_levels(
        member.id,
        1,
        1,
        2
    )

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
            "Candyサーバーが見つかりません"
        )

        return

    # -----------------------------------------
    # Slash同期
    # -----------------------------------------

    if not synced_once:

        try:

            synced = await bot.tree.sync(
                guild=GUILD_OBJECT
            )

            log.info(
                "✅ Slash同期: %s個",
                len(synced)
            )

            synced_once = True

        except Exception:

            log.exception(
                "Slash同期失敗"
            )

    # -----------------------------------------
    # 起動時すでにVCにいるメンバー
    # -----------------------------------------

    for channel in guild.voice_channels:

        if not is_countable_voice_channel(
            guild,
            channel
        ):

            continue

        for member in channel.members:

            if member.bot:
                continue

            start_voice_session(
                member
            )

    # -----------------------------------------
    # 1分保存
    # -----------------------------------------

    if not voice_save_loop.is_running():

        voice_save_loop.start()

    log.info(
        "================================"
    )

    log.info(
        "🍬 Candy Level Bot 起動成功"
    )

    log.info(
        "👤 VCプロフィール: ON"
    )

    log.info(
        "🎤 Voice Level: ON"
    )

    log.info(
        "💬 Text Level: ON"
    )

    log.info(
        "⭐ Total Level: Voice + Text"
    )

    log.info(
        "🔒 レベル低下防止: ON"
    )

    log.info(
        "💾 最高到達レベル保存: ON"
    )

    log.info(
        "🎤 Voice Lv5 = 累計10時間"
    )

    log.info(
        "👤 1人VC = 加算"
    )

    log.info(
        "🔇 ミュート = 加算"
    )

    log.info(
        "💤 AFK = 加算なし"
    )

    log.info(
        "💬 TC cooldown = %s秒",
        TEXT_XP_COOLDOWN
    )

    log.info(
        "🖼️ 個人背景 = ON"
    )

    log.info(
        "Database: %s",
        DB_PATH
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
