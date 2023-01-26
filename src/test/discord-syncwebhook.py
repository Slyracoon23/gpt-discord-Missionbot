from discord import SyncWebhook
import requests 

with requests.Session() as s:

    webhook_url = 'https://discord.com/api/webhooks/1066853545581752410/FkB1zJ4RNPka8o3Y-jRPGy7H7O4TS27mxfSHYu5-VpcvC-MT1Es1VkzXCQcPrqo8uNm4'
    webhook = SyncWebhook.from_url(webhook_url, session=s)

    # Send a message to the channel
    webhook.send('Hello world!')

