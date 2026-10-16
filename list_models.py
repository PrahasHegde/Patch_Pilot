import os
from openai import OpenAI
c = OpenAI(base_url="https://api.tokenfactory.nebius.com/v1/", api_key=os.environ["NEBIUS_API_KEY"])
for m in c.models.list().data:
    if "nvidia" in m.id.lower():
        print(m.id)