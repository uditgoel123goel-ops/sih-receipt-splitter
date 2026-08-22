from groq import Groq

# Replace PASTE_YOUR_GROQ_KEY_HERE with your new key (it should start with "gsk_")
client = Groq(api_key="HIDDEN_FOR_GITHUB")

response = client.chat.completions.create(
    model="qwen/qwen3.6-27b",
    messages=[
        {
            "role": "user",
            "content": "Reply exactly with: 'Hello SIH! The alternative AI is successfully connected.'"
        }
    ]
)

print("Backend says:", response.choices[0].message.content)