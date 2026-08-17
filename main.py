from __future__ import annotations

import logging
import os
import re
from typing import Optional

import discord


# ============================================================
# NOIR 新規開拓プロフィール表示Bot
#
# ・VC生成はしない
# ・既存パネルも触らない
# ・指定カテゴリー内のVC入室だけ監視
# ・VCのインチャへプロフィールを自動表示
# ・「プロフィールを見る」で本人のプロフィール投稿へジャンプ
# ============================================================


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()


# ============================================================
# NOIR 設定
# ============================================================

GUILD_ID = 1482224471606820874

# 新規開拓VCが生成されるカテゴリー
NEW_MEMBER_CATEGORY_ID = 1521453832788246690

# プロフィールチャンネル
MALE_PROFILE_CHANNEL_ID = 1482301104258547863
FEMALE_PROFILE_CHANNEL_ID = 1482301192263569522

# 性別ロール
MALE_ROLE_ID = 1482301549353897984
FEMALE_ROLE_ID = 1523690396515962981


# ============================================================
# その他設定
# ============================================================

# プロフィール検索で遡る最大件数
PROFILE_SCAN_LIMIT = 3000

# 同じVCで同じ人のプロフィールを
# 何度も表示しない
POST_ONCE_PER_ROOM = True


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger(
    "noir-profile-bot"
)


# ============================================================
# Discord Intents
# ============================================================

intents = discord.Intents.default()

intents.guilds = True
intents.voice_states = True

# プロフィール投稿の本文やEmbedから
# ユーザーIDを探すために使用
intents.message_content = True


# ============================================================
# Client
# ============================================================

client = discord.Client(
    intents=intents
)


# ============================================================
# キャッシュ
# ============================================================

# user_id -> profile jump URL
profile_cache: dict[int, str] = {}

# (voice_channel_id, user_id)
posted_in_room: set[tuple[int, int]] = set()


# ============================================================
# 共通
# ============================================================

async def get_channel_safe(
    channel_id: int,
):
    """
    キャッシュからチャンネルを取得。
    見つからなければAPIから取得。
    """

    channel = client.get_channel(
        channel_id
    )

    if channel is not None:
        return channel

    try:
        return await client.fetch_channel(
            channel_id
        )

    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException,
    ):
        return None


def member_gender(
    member: discord.Member,
) -> Optional[str]:
    """
    男性 / 女性ロール判定
    """

    role_ids = {
        role.id
        for role in member.roles
    }

    has_male = (
        MALE_ROLE_ID
        in role_ids
    )

    has_female = (
        FEMALE_ROLE_ID
        in role_ids
    )

    if has_male and not has_female:
        return "male"

    if has_female and not has_male:
        return "female"

    return None


def profile_channel_candidates(
    member: discord.Member,
) -> list[int]:
    """
    性別ロールがある場合は
    対応するプロフィールCHだけ検索。

    性別ロールが無い場合は
    男女両方のプロフィールCHを検索。

    仮メンバー等で男性ロールを
    付けていない場合にも対応。
    """

    gender = member_gender(
        member
    )

    if gender == "male":
        return [
            MALE_PROFILE_CHANNEL_ID
        ]

    if gender == "female":
        return [
            FEMALE_PROFILE_CHANNEL_ID
        ]

    return [
        MALE_PROFILE_CHANNEL_ID,
        FEMALE_PROFILE_CHANNEL_ID,
    ]


# ============================================================
# プロフィール判定
# ============================================================

def embed_to_text(
    embed: discord.Embed,
) -> str:
    """
    Embed全体を検索用文字列へ変換
    """

    parts: list[str] = []

    if embed.title:
        parts.append(
            embed.title
        )

    if embed.description:
        parts.append(
            embed.description
        )

    if embed.author:
        if embed.author.name:
            parts.append(
                embed.author.name
            )

    if embed.footer:
        if embed.footer.text:
            parts.append(
                embed.footer.text
            )

    for field in embed.fields:

        if field.name:
            parts.append(
                field.name
            )

        if field.value:
            parts.append(
                field.value
            )

    return "\n".join(
        parts
    )


def message_matches_member(
    message: discord.Message,
    member: discord.Member,
) -> bool:
    """
    プロフィール投稿が
    指定メンバー本人のものか判定。

    対応:
    ・本人が直接プロフィール投稿
    ・Botが本人をメンションして投稿
    ・本文にユーザーID
    ・EmbedにユーザーID / メンション
    """

    # ----------------------------------------
    # 本人が直接投稿しているプロフィール
    # ----------------------------------------

    if (
        not message.author.bot
        and message.author.id
        == member.id
    ):
        return True

    user_id_text = str(
        member.id
    )

    mention1 = (
        f"<@{member.id}>"
    )

    mention2 = (
        f"<@!{member.id}>"
    )

    # ----------------------------------------
    # メッセージ本文
    # ----------------------------------------

    content = (
        message.content
        or ""
    )

    if (
        user_id_text in content
        or mention1 in content
        or mention2 in content
    ):
        return True

    # ----------------------------------------
    # Discordのmention情報
    # ----------------------------------------

    for mentioned_user in message.mentions:

        if (
            mentioned_user.id
            == member.id
        ):
            return True

    # ----------------------------------------
    # Embed
    # ----------------------------------------

    for embed in message.embeds:

        text = embed_to_text(
            embed
        )

        if (
            user_id_text in text
            or mention1 in text
            or mention2 in text
        ):
            return True

    return False


# ============================================================
# プロフィール検索
# ============================================================

async def find_profile_message(
    member: discord.Member,
) -> Optional[discord.Message]:
    """
    本人のプロフィール投稿を検索。
    新しい投稿から順に探す。
    """

    # ----------------------------------------
    # キャッシュがあればURLは別関数で使用
    # ----------------------------------------

    channel_ids = profile_channel_candidates(
        member
    )

    for channel_id in channel_ids:

        channel = await get_channel_safe(
            channel_id
        )

        if channel is None:

            log.warning(
                "Profile channel not found: %s",
                channel_id,
            )

            continue

        if not hasattr(
            channel,
            "history",
        ):

            log.warning(
                "Channel has no history(): %s",
                channel_id,
            )

            continue

        try:

            async for message in channel.history(
                limit=PROFILE_SCAN_LIMIT,
                oldest_first=False,
            ):

                if message_matches_member(
                    message,
                    member,
                ):

                    profile_cache[
                        member.id
                    ] = message.jump_url

                    log.info(
                        "Profile found | user=%s | message=%s",
                        member.id,
                        message.id,
                    )

                    return message

        except discord.Forbidden:

            log.warning(
                "プロフィールCHを閲覧できません: %s",
                channel_id,
            )

        except discord.HTTPException:

            log.exception(
                "プロフィール検索エラー: %s",
                channel_id,
            )

    return None


async def get_profile_url(
    member: discord.Member,
) -> Optional[str]:
    """
    プロフィールURL取得
    """

    cached = profile_cache.get(
        member.id
    )

    if cached:
        return cached

    message = await find_profile_message(
        member
    )

    if message is None:
        return None

    return message.jump_url


# ============================================================
# プロフィールカード
# ============================================================

def create_profile_embed(
    member: discord.Member,
    profile_found: bool,
) -> discord.Embed:

    gender = member_gender(
        member
    )

    if gender == "female":

        color = discord.Color.from_rgb(
            245,
            120,
            190,
        )

    elif gender == "male":

        color = discord.Color.from_rgb(
            90,
            160,
            245,
        )

    else:

        color = discord.Color.from_rgb(
            170,
            130,
            240,
        )

    embed = discord.Embed(
        title="✨ プロフィール",
        description=(
            f"{member.mention} さんが"
            "参加しました！"
        ),
        color=color,
    )

    if profile_found:

        embed.add_field(
            name="🔗 プロフィール",
            value=(
                "下のボタンから"
                "プロフィールを確認できます。"
            ),
            inline=False,
        )

    else:

        embed.add_field(
            name="⚠️ プロフィール",
            value=(
                "プロフィール投稿を"
                "見つけられませんでした。"
            ),
            inline=False,
        )

    embed.set_thumbnail(
        url=member.display_avatar.url
    )

    embed.set_footer(
        text=f"ID: {member.id}"
    )

    return embed


def create_profile_view(
    profile_url: str,
) -> discord.ui.View:

    view = discord.ui.View(
        timeout=None
    )

    button = discord.ui.Button(
        label="プロフィールを見る",
        emoji="🔗",
        style=discord.ButtonStyle.link,
        url=profile_url,
    )

    view.add_item(
        button
    )

    return view


# ============================================================
# VCのインチャへプロフィール投稿
# ============================================================

async def post_profile_to_voice_chat(
    channel: discord.VoiceChannel,
    member: discord.Member,
) -> None:

    key = (
        channel.id,
        member.id,
    )

    if (
        POST_ONCE_PER_ROOM
        and key in posted_in_room
    ):

        log.info(
            "Profile already posted | room=%s user=%s",
            channel.id,
            member.id,
        )

        return

    profile_url = await get_profile_url(
        member
    )

    embed = create_profile_embed(
        member,
        profile_url is not None,
    )

    try:

        if profile_url:

            await channel.send(
                embed=embed,
                view=create_profile_view(
                    profile_url
                ),
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                ),
            )

        else:

            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                ),
            )

        posted_in_room.add(
            key
        )

        log.info(
            "Profile posted | room=%s | user=%s",
            channel.id,
            member.id,
        )

    except discord.Forbidden:

        log.warning(
            "VCインチャへ送信できません | room=%s",
            channel.id,
        )

    except discord.HTTPException:

        log.exception(
            "VCインチャ送信エラー | room=%s",
            channel.id,
        )


# ============================================================
# Bot起動
# ============================================================

@client.event
async def on_ready():

    log.info(
        "Logged in as %s (%s)",
        client.user,
        (
            client.user.id
            if client.user
            else "?"
        ),
    )

    guild = client.get_guild(
        GUILD_ID
    )

    if guild:

        log.info(
            "NOIR connected: %s (%s)",
            guild.name,
            guild.id,
        )

    else:

        log.warning(
            "NOIR guild not found: %s",
            GUILD_ID,
        )


# ============================================================
# VC入室監視
# ============================================================

@client.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):

    # Bot自身や他Botは無視
    if member.bot:
        return

    # NOIR以外は無視
    if member.guild.id != GUILD_ID:
        return

    # VC変更なし
    if before.channel == after.channel:
        return

    # 退出だけなら何もしない
    if after.channel is None:
        return

    # VoiceChannelだけ
    if not isinstance(
        after.channel,
        discord.VoiceChannel,
    ):
        return

    channel = after.channel

    # ----------------------------------------
    # 新規開拓カテゴリー以外は無視
    # ----------------------------------------

    if (
        channel.category_id
        != NEW_MEMBER_CATEGORY_ID
    ):
        return

    log.info(
        "New member VC join | user=%s | room=%s",
        member.id,
        channel.id,
    )

    # ----------------------------------------
    # VCのインチャへプロフィール
    # ----------------------------------------

    await post_profile_to_voice_chat(
        channel,
        member,
    )


# ============================================================
# VC削除時
# メモリ上の投稿履歴を掃除
# ============================================================

@client.event
async def on_guild_channel_delete(
    channel: discord.abc.GuildChannel,
):

    if channel.guild.id != GUILD_ID:
        return

    if channel.category_id != NEW_MEMBER_CATEGORY_ID:
        return

    remove_keys = [
        key
        for key in posted_in_room
        if key[0] == channel.id
    ]

    for key in remove_keys:
        posted_in_room.discard(
            key
        )

    log.info(
        "Deleted room cache cleared: %s",
        channel.id,
    )


# ============================================================
# プロフィール投稿が新しく作られた場合
# キャッシュを更新
# ============================================================

@client.event
async def on_message(
    message: discord.Message,
):

    if message.guild is None:
        return

    if message.guild.id != GUILD_ID:
        return

    if message.channel.id not in {
        MALE_PROFILE_CHANNEL_ID,
        FEMALE_PROFILE_CHANNEL_ID,
    }:
        return

    # ----------------------------------------
    # 本人が直接投稿した場合
    # ----------------------------------------

    if not message.author.bot:

        profile_cache[
            message.author.id
        ] = message.jump_url

        log.info(
            "Profile cache updated | user=%s",
            message.author.id,
        )

    # ----------------------------------------
    # メンションされたユーザー
    # ----------------------------------------

    for user in message.mentions:

        profile_cache[
            user.id
        ] = message.jump_url

    # ----------------------------------------
    # 本文 / EmbedからDiscord IDを抽出
    # ----------------------------------------

    searchable_text = (
        message.content
        or ""
    )

    for embed in message.embeds:

        searchable_text += (
            "\n"
            + embed_to_text(
                embed
            )
        )

    possible_ids = re.findall(
        r"\b\d{17,20}\b",
        searchable_text,
    )

    for raw_id in possible_ids:

        try:

            user_id = int(
                raw_id
            )

            profile_cache[
                user_id
            ] = message.jump_url

        except ValueError:
            pass


# ============================================================
# プロフィール編集時もキャッシュ更新
# ============================================================

@client.event
async def on_message_edit(
    before: discord.Message,
    after: discord.Message,
):

    if after.guild is None:
        return

    if after.guild.id != GUILD_ID:
        return

    if after.channel.id not in {
        MALE_PROFILE_CHANNEL_ID,
        FEMALE_PROFILE_CHANNEL_ID,
    }:
        return

    if not after.author.bot:

        profile_cache[
            after.author.id
        ] = after.jump_url

    for user in after.mentions:

        profile_cache[
            user.id
        ] = after.jump_url


# ============================================================
# 起動
# ============================================================

if __name__ == "__main__":

    if not TOKEN:

        raise RuntimeError(
            "環境変数 DISCORD_TOKEN が設定されていません。"
        )

    client.run(
        TOKEN
    )
