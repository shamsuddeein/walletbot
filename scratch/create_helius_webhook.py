import os
import sys
import django
import requests

# Setup Django environment
sys.path.append('/home/deen/walletbot')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "walletbot.settings")
django.setup()

from django.conf import settings
from pathlib import Path
import re

API_KEY = settings.HELIUS_API_KEY
WEBHOOK_URL = f"{settings.WEBHOOK_BASE_URL}/webhook/helius/"
DEAD_PLACEHOLDER = "11111111111111111111111111111111"

def create_initial_webhook():
    print(f"Creating a new paused webhook on Helius with URL: {WEBHOOK_URL}...")
    url = f"https://api.helius.xyz/v0/webhooks?api-key={API_KEY}"
    payload = {
        "webhookURL": WEBHOOK_URL,
        "transactionTypes": ["SWAP"],
        "accountAddresses": [DEAD_PLACEHOLDER],
        "webhookType": "enhanced"
    }
    
    r = requests.post(url, json=payload)
    if r.status_code not in [200, 201]:
        raise Exception(f"Failed to create Helius webhook: {r.text}")
        
    data = r.json()
    webhook_id = data.get("webhookID")
    print(f"✅ Webhook successfully created on Helius! ID: {webhook_id}")
    
    # Save the new ID to .env
    env_path = Path(settings.BASE_DIR) / ".env"
    env_content = env_path.read_text()
    
    # Replace HELIUS_WEBHOOK_ID value
    new_content = re.sub(
        r"^HELIUS_WEBHOOK_ID=.*$",
        f"HELIUS_WEBHOOK_ID={webhook_id}",
        env_content,
        flags=re.MULTILINE
    )
    env_path.write_text(new_content)
    print("✅ Local .env updated with the new HELIUS_WEBHOOK_ID!")
    return webhook_id

if __name__ == "__main__":
    try:
        create_initial_webhook()
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        sys.exit(1)
