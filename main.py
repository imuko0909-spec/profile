from __future__ import annotations

import asyncio
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import discord
from discord.ext import commands


# =========================================================
# 👤 VCプロフィールBot
# しゃべレア対応 / 強制二重投稿防止版
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

GUILD_ID = 1542420058775494666

MALE_PROFILE_CHANNEL_ID = 1542429626905657415
FEMALE_PROFILE_CHANNEL_ID = 1542429755750486026

# しゃべレア移動待ち
PROFILE_DELAY = 5

# 二重チェックする直近メッセージ数
DUPLICATE_CHECK_LIMIT = 20

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
    "vc-profile-bot"
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
            b"VC Profile Bot running."
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


# =========================================================
# 保存
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
# ボタン
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

            # 自分のBot以外は無視
            if (
                bot.user
                and
                message.author.id
                != bot.user.id
            ):
                continue

            # Embed確認
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

            # 通常本文も念のため確認
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

        log.info(
            "✅ プロフィール削除: %s",
            member
        )

    except discord.NotFound:
        pass

    except Exception:

        log.exception(
            "プロフィール削除エラー"
        )


# =========================================================
# 履歴からプロフィールカードを全部削除
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

    deleted = 0

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
                    target_text in description
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
                    target_text_space
                    in content
                ):

                    matched = True

            if matched:

                try:

                    await message.delete()

                    deleted += 1

                except Exception:
                    pass

    except Exception:

        log.exception(
            "履歴プロフィール削除エラー"
        )

    if deleted:

        log.info(
            "履歴からプロフィール削除: user=%s count=%s",
            user_id,
            deleted
        )


# =========================================================
# 5秒後にプロフィール表示
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

        # -----------------------------------------
        # しゃべレアで別VCへ移動済みなら
        # 古い予約を停止
        # -----------------------------------------

        if (
            current_vc.id
            !=
            expected_channel_id
        ):

            log.info(
                "古い予約停止: %s",
                current_member
            )

            return

        # -----------------------------------------
        # メモリ上ですでに表示済み
        # -----------------------------------------

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

            log.info(
                "メモリ二重防止: %s",
                current_member
            )

            return

        # -----------------------------------------
        # Discord履歴も確認
        #
        # 別プロセスが先に投稿していても
        # ここで止める
        # -----------------------------------------

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

            log.info(
                "🔥 強制二重投稿防止: %s",
                current_member
            )

            return

        # -----------------------------------------
        # プロフィール検索
        # -----------------------------------------

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

        embed.set_thumbnail(
            url=current_member.display_avatar.url
        )

        # -----------------------------------------
        # 投稿直前にもう一度チェック
        #
        # 2プロセスが同時に5秒経過した場合の
        # 競合対策
        # -----------------------------------------

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

            log.info(
                "🔥 投稿直前二重防止: %s",
                current_member
            )

            return

        # -----------------------------------------
        # 投稿
        # -----------------------------------------

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

        log.info(
            "プロフィール予約キャンセル: %s",
            member
        )

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

    # ミュートなどは無視
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

    # -----------------------------------------
    # 古い予約キャンセル
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

    # -----------------------------------------
    # 元VCのプロフィール削除
    # -----------------------------------------

    if before.channel:

        await delete_profile_cards_from_channel(
            before.channel,
            member.id
        )

    await delete_profile_card(
        member
    )

    # -----------------------------------------
    # 完全退出
    # -----------------------------------------

    if after.channel is None:

        log.info(
            "✅ VC退出: %s",
            member
        )

        return

    # -----------------------------------------
    # 新しいVCへ表示予約
    # -----------------------------------------

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
# プロフィールチャンネル投稿
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
# READY
# =========================================================

@bot.event
async def on_ready():

    log.info(
        "================================"
    )

    log.info(
        "✅ VCプロフィールBot 起動成功"
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
        "男性プロフィール: %s",
        MALE_PROFILE_CHANNEL_ID
    )

    log.info(
        "女性プロフィール: %s",
        FEMALE_PROFILE_CHANNEL_ID
    )

    log.info(
        "待機: %s秒",
        PROFILE_DELAY
    )

    log.info(
        "🔥 強制二重投稿防止: ON"
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
