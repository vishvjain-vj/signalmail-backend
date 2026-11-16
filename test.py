import os
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv() 
c = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1")
r = c.chat.completions.create(
    model="openai/gpt-oss-120b",
    messages=[{"role": "user", "content": "Say OK"}],
)
print(r.choices[0].message.content)
