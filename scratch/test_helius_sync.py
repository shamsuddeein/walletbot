import os
import sys
import django
import requests

# Setup Django environment
sys.path.append('/home/deen/walletbot')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "walletbot.settings")
django.setup()

from django.conf import settings
from tracker.models import Wallet
from tracker import helius as helius_api

API_KEY = settings.HELIUS_API_KEY
WEBHOOK_ID = settings.HELIUS_WEBHOOK_ID
TEST_ADDRESS = "6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY"
TEST_NICKNAME = "shamo_test"

def fetch_helius_webhook_state():
    url = f"https://api.helius.xyz/v0/webhooks/{WEBHOOK_ID}?api-key={API_KEY}"
    r = requests.get(url)
    r.raise_for_status()
    data = r.json()
    return data.get("accountAddresses", [])

def run_sync_test():
    print("🧹 Cleaning any existing test wallets from DB...")
    Wallet.objects.filter(address=TEST_ADDRESS).delete()
    Wallet.objects.filter(nickname=TEST_NICKNAME).delete()

    print("\n--- TEST STEP 1: Register Wallet ---")
    print(f"Adding wallet '{TEST_NICKNAME}' with address '{TEST_ADDRESS}' to local DB...")
    Wallet.objects.create(
        address=TEST_ADDRESS,
        nickname=TEST_NICKNAME,
        added_by_telegram_id=6991671486
    )
    
    print("Calling Helius sync register_wallet()...")
    reg_ok = helius_api.register_wallet(TEST_ADDRESS)
    print(f"register_wallet result: {reg_ok}")
    
    print("Fetching active addresses directly from Helius dashboard API...")
    helius_addresses = fetch_helius_webhook_state()
    print(f"Helius Webhook Addresses: {helius_addresses}")
    assert TEST_ADDRESS in helius_addresses, "TEST FAILED: Added address not found in Helius!"
    print("✅ TEST PASSED: Address is successfully active on Helius!")

    print("\n--- TEST STEP 2: Unregister Wallet ---")
    print(f"Removing wallet '{TEST_NICKNAME}' from local DB...")
    Wallet.objects.filter(address=TEST_ADDRESS).delete()
    
    print("Calling Helius sync unregister_wallet()...")
    unreg_ok = helius_api.unregister_wallet(TEST_ADDRESS)
    print(f"unregister_wallet result: {unreg_ok}")
    
    print("Fetching active addresses directly from Helius dashboard API...")
    helius_addresses_after = fetch_helius_webhook_state()
    print(f"Helius Webhook Addresses: {helius_addresses_after}")
    assert TEST_ADDRESS not in helius_addresses_after, "TEST FAILED: Removed address still found in Helius!"
    assert "11111111111111111111111111111111" in helius_addresses_after, "TEST FAILED: Placeholder address not set on Helius!"
    print("✅ TEST PASSED: Address removed and placeholder is successfully active on Helius!")

    print("\n🎉 ALL TESTS PASSED! Webhook synchronization is 100% verified in both directions.")

if __name__ == "__main__":
    try:
        run_sync_test()
    except Exception as e:
        print(f"\n❌ TEST ERROR: {e}")
        sys.exit(1)
