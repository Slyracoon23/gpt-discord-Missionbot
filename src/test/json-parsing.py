import jsonlines


project_name_discord_option_list = []

with jsonlines.open("/home/slyracoon23/Documents/buildspace/gpt-discord-bot/src/buildspace-projects-copy.jsonl") as reader:
    for project in reader:
        project_name_discord_option_list.append(project["project-name"])
            

print(project_name_discord_option_list)