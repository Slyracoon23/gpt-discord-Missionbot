from src.constants import (
    ALLOWED_SERVER_IDS,
)
import logging

logger = logging.getLogger(__name__)
from src.base import Message
from discord import Message as DiscordMessage
from typing import Optional, List
import discord

from src.constants import MAX_CHARS_PER_REPLY_MSG, INACTIVATE_THREAD_PREFIX


def discord_message_to_message(message: DiscordMessage) -> Optional[Message]:
    if (
        message.type == discord.MessageType.thread_starter_message
        and message.reference.cached_message
        and len(message.reference.cached_message.embeds) > 0
        and len(message.reference.cached_message.embeds[0].fields) > 0
    ):
        field = message.reference.cached_message.embeds[0].fields[0]
        if field.value:
            return Message(user=field.name, text=field.value)
    else:
        if message.content:
            return Message(user=message.author.name, text=message.content)
    return None


def split_into_shorter_messages(message: str) -> List[str]:
    return [
        message[i : i + MAX_CHARS_PER_REPLY_MSG]
        for i in range(0, len(message), MAX_CHARS_PER_REPLY_MSG)
    ]


def is_last_message_stale(
    interaction_message: DiscordMessage, last_message: DiscordMessage, bot_id: str
) -> bool:
    return (
        last_message
        and last_message.id != interaction_message.id
        and last_message.author
        and last_message.author.id != bot_id
    )
    
def is_last_message_termination_message(
    interaction_message: DiscordMessage, last_message: DiscordMessage, bot_id: str
) -> bool:
    return (
        last_message
        and last_message.author
        and last_message.author.id != bot_id
        and  (last_message.content in ['Thank you for your input. I have recorded your response.', 'I have discarded our conversation. Please start over.'])
    )

    
def is_last_message_stop_message(
    interaction_message: DiscordMessage, last_message: DiscordMessage, bot_id: str
) -> bool:
    return (
        last_message
        and last_message.author
        and last_message.author.id != bot_id
        and any(stop_word in last_message.content.lower() for stop_word in ['✅', '❌'])
    )

def is_evaluator_active(
    messages: List[Message]
) -> bool:
    return len(messages) >= 10
    
def is_summarize_active(
     messages: List[Message]
) -> bool:
    # rewrite to check bounds
    return len(messages) >= 9 and len(messages) <= 11

    # STOP_WORD = '✍️'
    # return (
    #     any(STOP_WORD in message.text for message in messages)
    # )


async def close_thread(thread: discord.Thread):
    await thread.edit(name=INACTIVATE_THREAD_PREFIX)
    await thread.send(
        embed=discord.Embed(
            description="**Thread closed** - Context limit reached, closing...",
            color=discord.Color.blue(),
        )
    )
    await thread.edit(archived=True, locked=True)


def should_block(guild: Optional[discord.Guild]) -> bool:
    if guild is None:
        # dm's not supported
        logger.info(f"DM not supported")
        return True

    if guild.id and guild.id not in ALLOWED_SERVER_IDS:
        # not allowed in this server
        logger.info(f"Guild {guild} not allowed")
        return True
    return False
