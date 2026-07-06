from django.core.management.base import BaseCommand
from tracker.models import Wallet
from tracker.helius import _update_webhook_addresses

class Command(BaseCommand):
    help = "Synchronizes all wallet addresses in the database with the Helius webhook."

    def handle(self, *args, **options):
        self.stdout.write("Syncing Helius webhook with database wallets...")
        addresses = list(Wallet.objects.values_list("address", flat=True).distinct())
        
        self.stdout.write(f"Found {len(addresses)} unique wallets in the database: {addresses}")
        
        ok = _update_webhook_addresses(addresses)
        if ok:
            self.stdout.write(self.style.SUCCESS("Successfully synced Helius webhook with database wallets."))
        else:
            self.stdout.write(self.style.ERROR("Failed to sync Helius webhook."))
