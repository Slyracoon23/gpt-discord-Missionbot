import discord
from discord import Message as DiscordMessage
from discord import ChannelType
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


import boto3, json, decimal

from botocore.exceptions import ClientError

session = boto3.Session(
    aws_access_key_id=AWS_SERVER_PUBLIC_KEY,
    aws_secret_access_key=AWS_SERVER_SECRET_KEY,
)

dynamodb = session.resource('dynamodb', region_name='eu-central-1')

table = dynamodb.Table('Discord-Attestations')

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
intents.message_content = True

client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)


@client.event
async def on_ready():
    logger.info(f"We have logged in as {client.user}. Invite URL: {BOT_INVITE_URL}")
    completion.MY_BOT_NAME = client.user.name
    completion.MY_BOT_EXAMPLE_CONVOS = []
    for c in EXAMPLE_CONVOS:
        messages = []
        for m in c.messages:
            if m.user == "Lenard":
                messages.append(Message(user=client.user.name, text=m.text))
            else:
                messages.append(m)
        completion.MY_BOT_EXAMPLE_CONVOS.append(Conversation(messages=messages))
    await tree.sync()

## Create a report page
#/report message:
@tree.command(name="report", description="Create a report page image and url link")
async def report_command(int: discord.Interaction, user: discord.Member):
    try:
        # Make an embed for the report page
        embed = discord.Embed(
            description=f"""
            <@{user.id}> wants a reportcard! 🤖💬
            
            **Report Card** 📝
            """,
            color=discord.Color.dark_teal(),
        )
        
        embed.add_field(name=f"{user.name}'s Report Card" ,value="[Click here to view your report]( https://wandb.ai/slyracoon23/openai-wandb-embedding-table/reports/DAO-Discourse-Results--VmlldzozMjU4NzYx )", inline=False)
        
        await int.response.send_message(embed=embed)
            
    except Exception as e:
        logger.error(f"Report command error: {e}")
        return


# /query message:
@tree.command(name="query", description="Create a new thread for Query")
@discord.app_commands.checks.has_permissions(send_messages=True)
@discord.app_commands.checks.has_permissions(view_channel=True)
@discord.app_commands.checks.bot_has_permissions(send_messages=True)
@discord.app_commands.checks.bot_has_permissions(view_channel=True)
@discord.app_commands.checks.bot_has_permissions(manage_threads=True)
async def chat_command(int: discord.Interaction, message: str, user: discord.Member):
    try:
        # only support creating thread in text channel
        if not isinstance(int.channel, discord.TextChannel):
            return

        # block servers not in allow list
        if should_block(guild=int.guild):
            return

        queryor = int.user
        logger.info(f"Query command by {queryor} {message[:20]} to {user}")
        try:
            # moderate the message
            flagged_str, blocked_str = moderate_message(message=message, user=queryor)
            await send_moderation_blocked_message(
                guild=int.guild,
                user=queryor,
                blocked_str=blocked_str,
                message=message,
            )
            if len(blocked_str) > 0:
                # message was blocked
                await int.response.send_message(
                    f"Your prompt has been blocked by moderation.\n{message}",
                    ephemeral=True,
                )
                return
            
            
            # Add thread dialog description
            embed = discord.Embed(
                description=f"""
                <@{queryor.id}> wants to query <@{user.id}>! 🤖💬
                
                :writing_hand:  ---> Summarize the conversation
                
                :white_check_mark: --->  End chat and submit prompt
                
                :x: --->  End chat and delete prompt
                """,
                color=discord.Color.green(),
            )
                
            
            embed.add_field(name=user.name, value=message)

            if len(flagged_str) > 0:
                # message was flagged
                embed.color = discord.Color.yellow()
                embed.title = "⚠️ This prompt was flagged by moderation."

            await int.response.send_message(embed=embed)
            response = await int.original_response()

            await send_moderation_flagged_message(
                guild=int.guild,
                user=user,
                flagged_str=flagged_str,
                message=message,
                url=response.jump_url,
            )
            
        except Exception as e:
            logger.exception(e)
            await int.response.send_message(
                f"Failed to start chat {str(e)}", ephemeral=True
            )
            return
        
          # create the thread
        thread = await response.create_thread(
            name=f"{ACTIVATE_THREAD_PREFX} {user.name[:20]} - {message[:30]}",
            slowmode_delay=1,
            reason="gpt-bot",
            auto_archive_duration=60,
        )
            
        async with thread.typing():
            # fetch completion
            messages = [Message(user=user.name, text=message)]
            response_data = await generate_starter_response(
                messages=messages, user=user
            )
            # send the result
            await process_response(
                user=user, thread=thread, response_data=response_data
            )
            
    except Exception as e:
        logger.exception(e)
        await int.response.send_message(
            f"Failed to start chat {str(e)}", ephemeral=True
        )


# /chat message:
@tree.command(name="chat", description="Create a new thread for conversation")
@discord.app_commands.checks.has_permissions(send_messages=True)
@discord.app_commands.checks.has_permissions(view_channel=True)
@discord.app_commands.checks.bot_has_permissions(send_messages=True)
@discord.app_commands.checks.bot_has_permissions(view_channel=True)
@discord.app_commands.checks.bot_has_permissions(manage_threads=True)
async def chat_command(int: discord.Interaction, message: str):
    try:
        # only support creating thread in text channel
        if not isinstance(int.channel, discord.TextChannel):
            return

        # block servers not in allow list
        if should_block(guild=int.guild):
            return

        user = int.user
        logger.info(f"Chat command by {user} {message[:20]}")
        try:
            # moderate the message
            flagged_str, blocked_str = moderate_message(message=message, user=user)
            await send_moderation_blocked_message(
                guild=int.guild,
                user=user,
                blocked_str=blocked_str,
                message=message,
            )
            if len(blocked_str) > 0:
                # message was blocked
                await int.response.send_message(
                    f"Your prompt has been blocked by moderation.\n{message}",
                    ephemeral=True,
                )
                return

            # Add thread dialog description
            embed = discord.Embed(
                description=f"""
                <@{user.id}> wants to chat! 🤖💬
                
                :writing_hand:  ---> Summarize the conversation
                
                :white_check_mark: --->  End chat and submit prompt
                
                :x: --->  End chat and delete prompt
                """,
                color=discord.Color.green(),
            )
            
            embed.add_field(name=user.name, value=message)

            if len(flagged_str) > 0:
                # message was flagged
                embed.color = discord.Color.yellow()
                embed.title = "⚠️ This prompt was flagged by moderation."

            await int.response.send_message(embed=embed)
            response = await int.original_response()

            await send_moderation_flagged_message(
                guild=int.guild,
                user=user,
                flagged_str=flagged_str,
                message=message,
                url=response.jump_url,
            )
        except Exception as e:
            logger.exception(e)
            await int.response.send_message(
                f"Failed to start chat {str(e)}", ephemeral=True
            )
            return

        # create the thread
        thread = await response.create_thread(
            name=f"{ACTIVATE_THREAD_PREFX} {user.name[:20]} - {message[:30]}",
            slowmode_delay=1,
            reason="gpt-bot",
            auto_archive_duration=60,
        )
        async with thread.typing():
            # fetch completion
            messages = [Message(user=user.name, text=message)]
            response_data = await generate_completion_response(
                messages=messages, user=user
            )
            # send the result
            await process_response(
                user=user, thread=thread, response_data=response_data
            )
    except Exception as e:
        logger.exception(e)
        await int.response.send_message(
            f"Failed to start chat {str(e)}", ephemeral=True
        )


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
                    
            if thread.last_message.content.lower() == COMPLETED_MESSAGE :
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

        elif  is_evaluator_active(
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
            # async with thread.typing():
            #     response_data = await generate_completion_response(
            #         messages=channel_messages, user=message.author
            #     )

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
                
            #  record the dislog in the database
            response = table.put_item(
            Item={
                    'id' :  message.author.id,
                    'DiscordID': message.author.id,
                    'ServerID': message.guild.id,
                    'RoleID': ','.join(map(lambda x: x.name, message.author.roles)),
                    'Attestation': next((i for i in map(lambda x: x.text, channel_messages[::-1]) if "To summarize" in i), None), # last item of list with substring -- ITS VERY UGLY
                    'Dialogue': ','.join(map(lambda x: x.text, channel_messages[::-1])),
                }
            )
        
            print("PutItem succeeded:")
            print(json.dumps(response, indent=4, cls=DecimalEncoder))
                           
            
        # send response
        await process_response(
            user=message.author, thread=thread, response_data=response_data
        )
    except Exception as e:
        logger.exception(e)





# /query message:
@tree.command(name="survey", description="Create a query message to start a conversation in DMs") 
@discord.app_commands.checks.has_permissions(send_messages=True)
@discord.app_commands.checks.bot_has_permissions(send_messages=True)
async def query_command(int: discord.Interaction, url: str, user: discord.User):
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
            id = match.group()[1:] # remove the first slash
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
        
        
        
        
        
        
        return
        
        
        
        
        
        
        
        
        ###################### MAKE PRIVATE THREAD ####### 
        
        
        # Make an embed for the report page
        embed = discord.Embed(
            description=f"""
            Survey {url} has been Sent! 🤖💬
            
            """,
            color=discord.Color.dark_teal(),
        )
        
        embed.add_field(name=f"Live Feed" ,value="[Click here to view attestations]( https://wandb.ai/slyracoon23/openai-wandb-embedding-table/reports/DAO-Discourse-Results--VmlldzozMjU4NzYx )", inline=False)
        
        await int.response.send_message(embed=embed)
        
        CHANNEL_ID = 1052665674549428254
        
        text_channel = client.get_channel(CHANNEL_ID)


          # create private thread
        thread = await text_channel.create_thread(
            name=f"{ACTIVATE_THREAD_PREFX}",
            slowmode_delay=1,
            reason="gpt-bot",
            auto_archive_duration=60,
            type=ChannelType.private_thread # ERROR MESSAGE, NO ATTRIBUTE TYPE
        )
        
        
        # Add the user to the thread
        # await thread.add_user(user)
        
        await thread.send(f"This is a private thread. Only you and the bot can see this thread. <@{user.id}>")
            
        # Send DM invite link
        # await user.send(inviteLink)
            
    except Exception as e:
        logger.error(f"Report command error: {e}")
        return

    
    

client.run(DISCORD_BOT_TOKEN)
