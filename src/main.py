import discord
from discord import Message as DiscordMessage
from discord import ChannelType
from discord.ext import tasks
import logging
from src.base import Message, Conversation
from src.constants import (
    BOT_INVITE_URL,
    DISCORD_BOT_TOKEN,
    EXAMPLE_CONVOS,
    ACTIVATE_THREAD_PREFX,
    MAX_THREAD_MESSAGES,
    SECONDS_DELAY_RECEIVING_MSG,
    AWS_SERVER_PUBLIC_KEY,
    AWS_SERVER_SECRET_KEY,
)

import asyncio
from src.utils import (
    logger,
    should_block,
    close_thread,
    is_last_message_stale,
    is_last_message_stop_message,
    is_summarize_active,
    is_evaluator_active,
    discord_message_to_message,
)
from src import completion
from src.completion import (
    generate_completion_response,
    generate_summarisation_response,
    generate_starter_response,
    generate_evaluator_response,
    process_response,
    is_last_response_termination_message,
    generate_survey_question,
    generate_survey_summary,
)
from src.moderation import (
    moderate_message,
    send_moderation_blocked_message,
    send_moderation_flagged_message,
)
import requests
import re

import json
import jsonlines


import boto3
import json
import decimal

from botocore.exceptions import ClientError

session = boto3.Session(
    aws_access_key_id=AWS_SERVER_PUBLIC_KEY,
    aws_secret_access_key=AWS_SERVER_SECRET_KEY,
)

dynamodb = session.resource('dynamodb', region_name='eu-central-1')


project_name_discord_option_list = []
project_list = []


with jsonlines.open("src/buildspace-projects.jsonl") as reader:
    for project in reader:
        project_name_discord_option_list.append(
            discord.SelectOption(label=project["project-name"]))

        project_list.append(project)


CHANNEL_ID = 1071524186490679308  # Missio channel ID
FORUM_CHANNEL_ID = 1071801106545516564  # Forum Channel Id


# Helper class to convert a DynamoDB item to JSON.
class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            if abs(o) % 1 > 0:
                return float(o)
            else:
                return int(o)
        return super(DecimalEncoder, self).default(o)


logging.basicConfig(
    format="[%(asctime)s] [%(filename)s:%(lineno)d] %(message)s", level=logging.INFO
)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)


class SimpleView(discord.ui.View):

    @discord.ui.button(label="Click me!")
    async def hello(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("You clicked the button!", ephemeral=True)
        await interaction.user.send("You clicked the button!")


class ForumView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Survey: Voice your opinion!")
    async def survey_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        discourse_topic_id = interaction.channel.name.split()[0]
        await interaction.response.send_message(f"You asked to parcipate in a Survey! ID: {discourse_topic_id}", ephemeral=True)
        # await survey_discourse_command_manual(interaction, discourse_topic_id, interaction.user)


# Possibly create an ephemeral message
@client.event
async def on_member_join(member):
    channel = client.get_channel(1065386164594417727)  # Welcome channel ID
    embed = discord.Embed(
        title=f"Welcome {member.name}", description=f"Thanks for joining {member.guild.name}!")  # F-Strings!

    # embed.set_thumbnail(url=member.avatar_url) # Set the embed's thumbnail to the member's avatar image!

    await channel.send(embed=embed)

    view = SimpleView()
    # button = discord.ui.Button(label="Default Button", style=discord.ButtonStyle.primary)
    # view.add_item(button)
    await channel.send("Click the button!", view=view)


# @client.event
# async def on_button_click(interaction):
#     # if interaction.component.label.startswith("Default Button"):
#     await interaction.response.send_message("You clicked the button!", ephemeral=True)
#     await interaction.user.send("You clicked the button!")


@tasks.loop(seconds=180)
async def pollDiscoure():
    print("Poll Discourse function being called")

    # Get all threads id
    forum_channel = client.get_channel(FORUM_CHANNEL_ID)

    print("Forum channel: ", forum_channel)

    discord_topic_id = []
    for thread in forum_channel.threads:
        discord_topic_id.append(thread.name.split()[0])

    print("Discourse topic id: ", discord_topic_id)

    # Get all post ids from discourse

    # Get the topic from Discourse
    api_url = "https://forum.citydao.io/latest.json"

    headers = {
        'Api-Key': '82fe71fa8cfc68f59a9582b1c3561c1cb5f4da634585877f09927c30889cd318',
        'Api-Username': 'system'
    }

    response = requests.request("GET", api_url, headers=headers)

    if response.status_code != 200:
        logger.info("Error getting topic from Discourse")
        return

    json_obj = json.loads(response.text)

    topic_list = json_obj["topic_list"]["topics"]

    discourse_topic_id_list = list(
        map(lambda topic: str(topic["id"]), topic_list))

    # Get all topic ids that are not already included in the channel
    topic_id_to_be_included = list(
        set(discourse_topic_id_list).symmetric_difference(set(discord_topic_id)))

    # Inlcude the new topics in the channel

    print("Topic id to be included: ", topic_id_to_be_included)

    for topic_id in topic_id_to_be_included[:3]:
        await create_forum_post_manual(topic_id)


@client.event
async def on_ready():
    logger.info(
        f"We have logged in as {client.user}. Invite URL: {BOT_INVITE_URL}")
    completion.MY_BOT_NAME = client.user.name
    completion.MY_BOT_EXAMPLE_CONVOS = []
    for c in EXAMPLE_CONVOS:
        messages = []
        for m in c.messages:
            if m.user == "Lenard":
                messages.append(Message(user=client.user.name, text=m.text))
            else:
                messages.append(m)
        completion.MY_BOT_EXAMPLE_CONVOS.append(
            Conversation(messages=messages))
    await tree.sync()
    pollDiscoure.start()

# calls for each message


@client.event
async def on_message(message: DiscordMessage):
    try:
        # TODO: allow DMs to be used for chat
        # block servers not in allow list
        if should_block(guild=message.guild):
            return

        # ignore messages from the bot
        if message.author == client.user:
            return

        # ignore messages not in a thread
        channel = message.channel
        if not isinstance(channel, discord.Thread):
            logger.info("Not a thread")
            return

        # ignore threads not created by the bot
        thread = channel
        if thread.owner_id != client.user.id:
            return

        # ignore threads that are archived locked or title is not what we want
        if (
            thread.archived
            or thread.locked
            or not thread.name.startswith(ACTIVATE_THREAD_PREFX)
        ):
            # ignore this thread
            return

        if thread.message_count > MAX_THREAD_MESSAGES:
            # too many messages, no longer going to reply
            await close_thread(thread=thread)
            return

        # moderate the message
        flagged_str, blocked_str = moderate_message(
            message=message.content, user=message.author
        )
        await send_moderation_blocked_message(
            guild=message.guild,
            user=message.author,
            blocked_str=blocked_str,
            message=message.content,
        )
        if len(blocked_str) > 0:
            try:
                await message.delete()
                await thread.send(
                    embed=discord.Embed(
                        description=f"❌ **{message.author}'s message has been deleted by moderation.**",
                        color=discord.Color.red(),
                    )
                )
                return
            except Exception as e:
                await thread.send(
                    embed=discord.Embed(
                        description=f"❌ **{message.author}'s message has been blocked by moderation but could not be deleted. Missing Manage Messages permission in this Channel.**",
                        color=discord.Color.red(),
                    )
                )
                return
        await send_moderation_flagged_message(
            guild=message.guild,
            user=message.author,
            flagged_str=flagged_str,
            message=message.content,
            url=message.jump_url,
        )
        if len(flagged_str) > 0:
            await thread.send(
                embed=discord.Embed(
                    description=f"⚠️ **{message.author}'s message has been flagged by moderation.**",
                    color=discord.Color.yellow(),
                )
            )

        # wait a bit in case user has more messages
        if SECONDS_DELAY_RECEIVING_MSG > 0:
            await asyncio.sleep(SECONDS_DELAY_RECEIVING_MSG)
            if is_last_message_stale(
                interaction_message=message,
                last_message=thread.last_message,
                bot_id=client.user.id,
            ):
                # there is another message, so ignore this one
                return

        logger.info(
            f"Thread message to process - {message.author}: {message.content[:50]} - {thread.name} {thread.jump_url}"
        )

        channel_messages = [
            discord_message_to_message(message)
            async for message in thread.history(limit=MAX_THREAD_MESSAGES)
        ]
        channel_messages = [x for x in channel_messages if x is not None]
        channel_messages.reverse()

        # if the last message is an stop/signal message, then summarise the conversation
        if is_last_message_stop_message(
            interaction_message=message,
            last_message=thread.last_message,
            bot_id=client.user.id,
        ):
            COMPLETED_MESSAGE = '✅'
            STOP_MESSAGE = '❌'

            if thread.last_message.content.lower() == COMPLETED_MESSAGE:
                # generate closing message and summarise
                await thread.send(
                    embed=discord.Embed(
                        description=f"**{message.author}'s message has been Submitted!**",
                        color=discord.Color.green(),
                    )
                )

                return

            elif thread.last_message.content.lower() == STOP_MESSAGE:
                # generate closing message and Exit
                await thread.send(
                    embed=discord.Embed(
                        description=f"❌ **{message.author}'s Conversation has been deleted.**",
                        color=discord.Color.red(),
                    )
                )

                return

            else:
                # User needs to send a valid closing message
                await thread.send(
                    embed=discord.Embed(
                        description=f"**Invalid response** - Please send ✅ or ❌",
                        color=discord.Color.yellow(),
                    )
                )
                return

        elif is_summarize_active(
            messages=channel_messages,
        ):
            # generate summarized response mode
            async with thread.typing():
                response_data = await generate_summarisation_response(
                    messages=channel_messages, user=message.author
                )

        elif is_evaluator_active(
            messages=channel_messages,
        ):

            # generate summarized response mode
            async with thread.typing():
                response_data = await generate_evaluator_response(
                    messages=channel_messages, user=message.author
                )

        else:
            # generate standard response mode
            async with thread.typing():
                response_data = await generate_starter_response(
                    messages=channel_messages, user=message.author
                )

        if is_last_message_stale(
            interaction_message=message,
            last_message=thread.last_message,
            bot_id=client.user.id,
        ):
            # there is another message and its not from us, so ignore this response
            return

        if is_last_response_termination_message(
            response_data=response_data,
        ):

            # Find parent of thread to get the channel ID
            projectName = thread.name.split()[1].lower()

            existing_tables = list(
                map(lambda x: x.name, list(dynamodb.tables.all())))

            if projectName not in existing_tables:
                table = dynamodb.create_table(
                    AttributeDefinitions=[
                        {
                            'AttributeName': 'id',
                            'AttributeType': 'N'
                        },
                    ],
                    KeySchema=[
                        {
                            'AttributeName': 'id',
                            'KeyType': 'HASH'
                        },
                    ],
                    ProvisionedThroughput={
                        'ReadCapacityUnits': 5,
                        'WriteCapacityUnits': 5,
                    },
                    TableName=projectName,
                )

                table.meta.client.get_waiter(
                    'table_exists').wait(TableName=projectName)

            table = dynamodb.Table(projectName)

            #  record the dislog in the database
            response = table.put_item(
                Item={
                    'id':  message.author.id,
                    'DiscordID': message.author.id,
                    'ServerID': message.guild.id,
                    'RoleID': ','.join(map(lambda x: x.name, message.author.roles)),
                    # last item of list with substring -- ITS VERY UGLY
                    'Attestation': next((i for i in map(lambda x: x.text, channel_messages[::-1]) if "To summarize" in i), None),
                    'Dialogue': ','.join(map(lambda x: x.text, channel_messages[::])),
                }
            )

            print("PutItem succeeded:")
            print(json.dumps(response, indent=4, cls=DecimalEncoder))

            embed = discord.Embed(
                description=f"""
            Your conversation is Saved! Thank you for your time.
            """,
                color=discord.Color.dark_teal(),
            )
            # edit the embed of the message
            await thread.send(embed=embed)

        # send response
        await process_response(
            user=message.author, thread=thread, response_data=response_data
        )
    except Exception as e:
        logger.exception(e)


# /query message:
@tree.command(name="survey-discourse", description="Create a query message to start a conversation in DMs")
@discord.app_commands.checks.has_permissions(send_messages=True)
@discord.app_commands.checks.bot_has_permissions(send_messages=True)
async def survey_discourse_command(int: discord.Interaction, url: str, user: discord.User):
    # DM specific user
    try:

        # only support creating thread in text channel
        if not isinstance(int.channel, discord.TextChannel):
            return

        # block servers not in allow list
        if should_block(guild=int.guild):
            return

        ######################### Discourse URL ############################
        # Get URL and parse it
        # Disource URL is a topic URL https://docs.discourse.org/#tag/Topics/operation/getTopic

        # validate domain name
        domain_name = "https://forum.citydao.io/t/"
        if not url.startswith(domain_name):
            logger.info("Invalid URL")
            return

        # extract ID from URL using regular expressions
        match = re.search(r'\/\d+', url)

        if match:
            id = match.group()[1:]  # remove the first slash
        else:
            logger.info("No ID found in URL")
            return

        # Get the topic from Discourse
        api_url = f'{domain_name}{id}.json'

        headers = {
            'Api-Key': '82fe71fa8cfc68f59a9582b1c3561c1cb5f4da634585877f09927c30889cd318',
            'Api-Username': 'system'
        }

        response = requests.request("GET", api_url, headers=headers)

        if response.status_code != 200:
            logger.info("Error getting topic from Discourse")
            return

        json_obj = json.loads(response.text)

        topic_slug = json_obj['post_stream']['posts'][0]['topic_slug']

        post_proposal = json_obj['post_stream']['posts'][0]['cooked']

        embed = discord.Embed(
            description=f"""
            Missio is on the job 🤖💬
            """,
            color=discord.Color.dark_teal(),
        )

        # reply to the interaction
        await int.response.send_message(embed=embed)

        # Summarize the topic
        survey_summary = await generate_survey_summary(
            survey_post=post_proposal
        )

        # PROMPT: You're a helpful survyor bot. The master wants to ask the members of the DAO specific question regarding the following proposal. Your task is to analyse the following text and output a list of 5 questions that asks the DAO member his opinion about the proposal. Start off each question `Survey: `.
        # Get the topic question from LLM
        survey_question = await generate_survey_question(
            survey_post=post_proposal, summary=survey_summary
        )

        text_channel = client.get_channel(CHANNEL_ID)

        # create private thread
        thread = await text_channel.create_thread(
            name=f"{ACTIVATE_THREAD_PREFX} CityDAO {user.name[:20]} - {survey_question[:30]}",
            slowmode_delay=1,
            reason="gpt-bot",
            auto_archive_duration=60,
            type=ChannelType.private_thread
        )

        # Edit sent embed
        embed = discord.Embed(
            description=f"""
            Hey <@{user.id}>! Missio wants to ask you something 🤖💬
            
            {topic_slug}
            
            {survey_summary}
            
            {survey_question}
            """,
            color=discord.Color.dark_teal(),
        )

        embed.add_field(name=f"Proposal Link",
                        value=f"[Click here to view Original Proposal]({url})", inline=False)

        # edit the embed of the message
        await thread.send(embed=embed)

        # Add the user to the thread by @ mentioning them
        await thread.send(f"This is a private thread. Only you and the bot can see this thread. <@{user.id}>")

        # Send DM invite link
        # await user.send(inviteLink)

        async with thread.typing():
            # fetch completion
            messages = [Message(user=user.name, text=survey_question)]
            response_data = await generate_starter_response(
                messages=messages, user=user
            )
            # send the result
            await process_response(
                user=user, thread=thread, response_data=response_data
            )

    except Exception as e:
        logger.error(f"Report command error: {e}")
        return


async def survey_discourse_command_manual(int: discord.Interaction, discourse_topic_id: str, user: discord.User):
    # DM specific user
    try:
        # block servers not in allow list
        if should_block(guild=int.guild):
            return

        ######################### Discourse URL ############################
        # Get URL and parse it
        # Disource URL is a topic URL https://docs.discourse.org/#tag/Topics/operation/getTopic

        # validate domain name
        domain_name = "https://forum.citydao.io/t/"

        # Get the topic from Discourse
        api_url = f'{domain_name}{discourse_topic_id}.json'

        headers = {
            'Api-Key': '82fe71fa8cfc68f59a9582b1c3561c1cb5f4da634585877f09927c30889cd318',
            'Api-Username': 'system'
        }

        response = requests.request("GET", api_url, headers=headers)

        if response.status_code != 200:
            logger.info("Error getting topic from Discourse")
            return

        json_obj = json.loads(response.text)

        topic_slug = json_obj['post_stream']['posts'][0]['topic_slug']

        post_proposal = json_obj['post_stream']['posts'][0]['cooked']

        # embed = discord.Embed(
        #     description=f"""
        #     Missio is on the job 🤖💬
        #     """,
        #     color=discord.Color.dark_teal(),
        # )

        # # reply to the interaction
        # await int.response.send_message(embed=embed)

        # Summarize the topic
        survey_summary = await generate_survey_summary(
            survey_post=post_proposal
        )

        # PROMPT: You're a helpful survyor bot. The master wants to ask the members of the DAO specific question regarding the following proposal. Your task is to analyse the following text and output a list of 5 questions that asks the DAO member his opinion about the proposal. Start off each question `Survey: `.
        # Get the topic question from LLM
        survey_question = await generate_survey_question(
            survey_post=post_proposal, summary=survey_summary
        )

        text_channel = client.get_channel(CHANNEL_ID)

        # create private thread
        thread = await text_channel.create_thread(
            name=f"{ACTIVATE_THREAD_PREFX} CityDAO {user.name[:20]} - {survey_question[:30]}",
            slowmode_delay=1,
            reason="gpt-bot",
            auto_archive_duration=60,
            type=ChannelType.private_thread
        )

        # Edit sent embed
        embed = discord.Embed(
            description=f"""
            Hey <@{user.id}>! Missio wants to ask you something 🤖💬
            
            {topic_slug}
            
            {survey_summary}
            
            {survey_question}
            """,
            color=discord.Color.dark_teal(),
        )

        url = f'{domain_name}{discourse_topic_id}'

        embed.add_field(name=f"Proposal Link",
                        value=f"[Click here to view Original Proposal]({url})", inline=False)

        # edit the embed of the message
        await thread.send(embed=embed)

        # Add the user to the thread by @ mentioning them
        await thread.send(f"This is a private thread. Only you and the bot can see this thread. <@{user.id}>")

        # Send DM invite link
        # await user.send(inviteLink)

        async with thread.typing():
            # fetch completion
            messages = [Message(user=user.name, text=survey_question)]
            response_data = await generate_starter_response(
                messages=messages, user=user
            )
            # send the result
            await process_response(
                user=user, thread=thread, response_data=response_data
            )

    except Exception as e:
        logger.error(f"Report command error: {e}")
        return


# /query message:
@tree.command(name="create-forum-post", description="Create a query message to start a conversation in DMs")
@discord.app_commands.checks.has_permissions(send_messages=True)
@discord.app_commands.checks.bot_has_permissions(send_messages=True)
async def create_forum_post_command(int: discord.Interaction, url: str):
    # DM specific user
    try:

        # block servers not in allow list
        if should_block(guild=int.guild):
            return

        ######################### Discourse URL ############################
        # Get URL and parse it
        # Disource URL is a topic URL https://docs.discourse.org/#tag/Topics/operation/getTopic

        # validate domain name
        domain_name = "https://forum.citydao.io/t/"
        if not url.startswith(domain_name):
            logger.info("Invalid URL")
            return

        # extract ID from URL using regular expressions
        match = re.search(r'\/\d+', url)

        if match:
            id = match.group()[1:]  # remove the first slash
        else:
            logger.info("No ID found in URL")
            return

        # Get the topic from Discourse
        api_url = f'{domain_name}{id}.json'

        headers = {
            'Api-Key': '82fe71fa8cfc68f59a9582b1c3561c1cb5f4da634585877f09927c30889cd318',
            'Api-Username': 'system'
        }

        response = requests.request("GET", api_url, headers=headers)

        if response.status_code != 200:
            logger.info("Error getting topic from Discourse")
            return

        json_obj = json.loads(response.text)

        topic_slug = json_obj['post_stream']['posts'][0]['topic_slug']

        post_proposal = json_obj['post_stream']['posts'][0]['cooked']

        embed = discord.Embed(
            description=f"""
            Missio is on the job 🤖💬
            """,
            color=discord.Color.dark_teal(),
        )

        # reply to the interaction
        await int.response.send_message(embed=embed)

        # Summarize the topic
        survey_summary = await generate_survey_summary(
            survey_post=post_proposal
        )

        # PROMPT: You're a helpful survyor bot. The master wants to ask the members of the DAO specific question regarding the following proposal. Your task is to analyse the following text and output a list of 5 questions that asks the DAO member his opinion about the proposal. Start off each question `Survey: `.
        # Get the topic question from LLM
        survey_question = await generate_survey_question(
            survey_post=post_proposal, summary=survey_summary
        )

        # Create the forum post

        forum_channel = client.get_channel(FORUM_CHANNEL_ID)

        embed = discord.Embed(
            description=f"""
            Missio 🤖💬

            {topic_slug}

            {survey_summary}

            {survey_question}
            """,
            color=discord.Color.dark_teal(),
        )
        embed.add_field(name=f"Proposal Link",


                        value=f"[Click here to view Original Proposal]({url})", inline=False)

        forumView = ForumView()

        # create forum post
        thread = await forum_channel.create_thread(
            name=f"{id} CityDAO:{topic_slug[:30]}",
            # content="This is a Forum Thread",  # Bug in library, this is not working
            slowmode_delay=1,
            reason="gpt-bot",
            embed=embed,
            view=forumView,
            auto_archive_duration=60,
            # type=ChannelType.private_thread
        )

        # Edit sent embed

        # # edit the embed of the message
        # await thread.send(content="This is a Forum Thread", embed=embed)

        # Add the user to the thread by @ mentioning them
        # await thread.send(f"This is a Forum Thread>")

        # Send DM invite link
        # await user.send(inviteLink)

        # async with thread.typing():
        #     # fetch completion
        #     messages = [Message(user=user.name, text=survey_question)]
        #     response_data = await generate_starter_response(
        #         messages=messages, user=user
        #     )
        #     # send the result
        #     await process_response(
        #         user=user, thread=thread, response_data=response_data
        #     )

    except Exception as e:
        logger.error(f"Report command error: {e}")
        return


async def create_forum_post_manual(topic_id: str):
    # DM specific user
    try:

        ######################### Discourse URL ############################
        # Get URL and parse it
        # Disource URL is a topic URL https://docs.discourse.org/#tag/Topics/operation/getTopic

        # validate domain name
        domain_name = "https://forum.citydao.io/t/"

        # Get the topic from Discourse
        api_url = f'{domain_name}{topic_id}.json'

        headers = {
            'Api-Key': '82fe71fa8cfc68f59a9582b1c3561c1cb5f4da634585877f09927c30889cd318',
            'Api-Username': 'system'
        }

        response = requests.request("GET", api_url, headers=headers)

        if response.status_code != 200:
            logger.info("Error getting topic from Discourse")
            return

        json_obj = json.loads(response.text)

        topic_slug = json_obj['post_stream']['posts'][0]['topic_slug']

        post_proposal = json_obj['post_stream']['posts'][0]['cooked']

        # Summarize the topic
        survey_summary = await generate_survey_summary(
            survey_post=post_proposal
        )

        # PROMPT: You're a helpful survyor bot. The master wants to ask the members of the DAO specific question regarding the following proposal. Your task is to analyse the following text and output a list of 5 questions that asks the DAO member his opinion about the proposal. Start off each question `Survey: `.
        # Get the topic question from LLM
        survey_question = await generate_survey_question(
            survey_post=post_proposal, summary=survey_summary
        )

        # Create the forum post

        forum_channel = client.get_channel(FORUM_CHANNEL_ID)

        embed = discord.Embed(
            description=f"""
            Missio 🤖💬

            {topic_slug}

            {survey_summary}

            {survey_question}
            """,
            color=discord.Color.dark_teal(),
        )

        url = f"https://forum.citydao.io/t/{topic_id}"
        embed.add_field(name=f"Proposal Link",
                        value=f"[Click here to view Original Proposal]({url})", inline=False)

        forumView = ForumView()

        # create forum post
        thread = await forum_channel.create_thread(
            name=f"{topic_id} CityDAO:{topic_slug[:30]}",
            # content="This is a Forum Thread",  # Bug in library, this is not working
            slowmode_delay=1,
            reason="gpt-bot",
            embed=embed,
            view=forumView,
            auto_archive_duration=60,
            # type=ChannelType.private_thread
        )

    except Exception as e:
        logger.error(f"Report command error: {e}")
        return

client.run(DISCORD_BOT_TOKEN)
