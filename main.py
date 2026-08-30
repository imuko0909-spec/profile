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
# しゃべレア対応 / 二重投稿防止版
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

GUILD_ID = 1542420058775494666

MALE_PROFILE_CHANNEL_ID = 1542429626905657415
FEMALE_PROFILE_CHANNEL_ID = 1542429755750486026

# しゃべレアの自動移動を待つ秒数
PROFILE_DELAY = 5

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
# 保存用
# =========================================================

# ユーザーごとの表示予約Task
profile_tasks: dict[
    int,
    asyncio.Task
] = {}


# 現在表示されているプロフィールカード
#
# user_id:
# {
#     "channel_id": int,
#     "message_id": int
# }
#
profile_messages: dict[
    int,
    dict
] = {}


# プロフィール投稿キャッシュ
#
# user_id:
# discord.Message
#
profile_cache: dict[
    int,
    discord.Message
] = {}


# =========================================================
# プロフィールリンクボタン
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

    # -----------------------------------------
    # キャッシュがあれば即返す
    # -----------------------------------------

    cached = profile_cache.get(
        member.id
    )

    if cached is not None:

        return cached

    guild = member.guild

    channel_ids = [
        MALE_PROFILE_CHANNEL_ID,
        FEMALE_PROFILE_CHANNEL_ID
    ]

    # -----------------------------------------
    # 男性・女性プロフィール両方を探す
    # -----------------------------------------

    for channel_id in channel_ids:

        channel = guild.get_channel(
            channel_id
        )

        if channel is None:

            log.warning(
                "プロフィールチャンネルが見つかりません: %s",
                channel_id
            )

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

        except discord.Forbidden:

            log.warning(
                "プロフィールチャンネルを閲覧できません: %s",
                channel_id
            )

        except Exception:

            log.exception(
                "プロフィール検索エラー"
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

        # すでに削除済み
        pass

    except discord.Forbidden:

        log.warning(
            "プロフィールカードを削除する権限がありません"
        )

    except Exception:

        log.exception(
            "プロフィールカード削除エラー"
        )


# =========================================================
# 5秒後プロフィール表示
# =========================================================

async def delayed_profile_post(
    member: discord.Member,
    expected_channel_id: int
):

    try:

        # -----------------------------------------
        # しゃべレアの移動待ち
        # -----------------------------------------

        await asyncio.sleep(
            PROFILE_DELAY
        )

        guild = member.guild

        # -----------------------------------------
        # 最新メンバー情報を確認
        # -----------------------------------------

        current_member = guild.get_member(
            member.id
        )

        if current_member is None:

            return

        # VCから抜けていたら終了
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
        # 予約したVCと現在地が違う
        #
        # しゃべレア等で移動済みなので
        # 古い予約は無効
        # -----------------------------------------

        if (
            current_vc.id
            !=
            expected_channel_id
        ):

            log.info(
                "古いプロフィール予約を中止: %s",
                current_member
            )

            return

        # -----------------------------------------
        # すでに同じVCに表示済みなら
        # 二重投稿しない
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
                "二重投稿防止: %s",
                current_member
            )

            return

        # -----------------------------------------
        # 念のため以前のカードを削除
        # -----------------------------------------

        if existing:

            await delete_profile_card(
                current_member
            )

        # -----------------------------------------
        # プロフィール検索
        # -----------------------------------------

        profile_message = await find_profile_message(
            current_member
        )

        # -----------------------------------------
        # Embed
        # -----------------------------------------

        if profile_message:

            description = (
                f"{current_member.mention} さんが"
                "お部屋に参加しました！\n\n"
                "📖 **プロフィール**\n"
                "下のボタンからプロフィールを"
                "確認できます。\n\n"
                f"**ID:** {current_member.id}"
            )

        else:

            description = (
                f"{current_member.mention} さんが"
                "お部屋に参加しました！\n\n"
                "📖 **プロフィール**\n"
                "プロフィールはまだ登録されていません。\n\n"
                f"**ID:** {current_member.id}"
            )

        embed = discord.Embed(
            title="🏫 プロフィール",
            description=description
        )

        embed.set_thumbnail(
            url=current_member.display_avatar.url
        )

        # -----------------------------------------
        # VCインチャへ投稿
        # -----------------------------------------

        if profile_message:

            view = ProfileView(
                profile_message.jump_url
            )

            sent = await current_vc.send(
                embed=embed,
                view=view
            )

        else:

            sent = await current_vc.send(
                embed=embed
            )

        # -----------------------------------------
        # 投稿記録
        # -----------------------------------------

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

    except discord.Forbidden:

        log.warning(
            "VCチャットへ投稿する権限がありません"
        )

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
# VC入退室・移動
# =========================================================

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState
):

    # Botは無視
    if member.bot:

        return

    # 対象サーバーのみ
    if member.guild.id != GUILD_ID:

        return

    # -----------------------------------------
    # 同じVCなら無視
    #
    # ミュート・スピーカー変更など
    # -----------------------------------------

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
    # 古い表示予約をキャンセル
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
    # 元VCのプロフィールを削除
    # -----------------------------------------

    await delete_profile_card(
        member
    )

    # -----------------------------------------
    # VC退出
    # -----------------------------------------

    if after.channel is None:

        log.info(
            "✅ VC退出: %s",
            member
        )

        return

    # -----------------------------------------
    # VC入室・移動
    #
    # このVC IDを予約時の目的地として保存
    # -----------------------------------------

    expected_channel_id = (
        after.channel.id
    )

    task = asyncio.create_task(
        delayed_profile_post(
            member,
            expected_channel_id
        )
    )

    profile_tasks[
        member.id
    ] = task


# =========================================================
# プロフィール投稿更新
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
    # プロフィールチャンネルへの投稿なら
    # キャッシュ更新
    # -----------------------------------------

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
# プロフィール投稿削除
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

    for (
        user_id,
        message
    ) in profile_cache.items():

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

        log.info(
            "プロフィールキャッシュ削除: %s",
            user_id
        )


# =========================================================
# Bot起動時に
# すでにVCにいる人は記録だけ初期化
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
        "待機時間: %s秒",
        PROFILE_DELAY
    )

    log.info(
        "二重投稿防止: ON"
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
