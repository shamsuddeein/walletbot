from django.core.management.base import BaseCommand
from tracker.models import Wallet, TokenBuy, MatchAlert
from datetime import timedelta

class Command(BaseCommand):
    help = "Finds and deletes duplicate TokenBuy and MatchAlert rows."

    def handle(self, *args, **options):
        # 1. Clean up duplicate TokenBuys
        self.stdout.write("Cleaning up duplicate TokenBuy rows...")
        deleted_token_buys_count = 0
        
        wallets = Wallet.objects.all()
        for wallet in wallets:
            # Get unique contract addresses for this wallet
            mints = TokenBuy.objects.filter(wallet=wallet).values_list("contract_address", flat=True).distinct()
            for mint in mints:
                buys = list(TokenBuy.objects.filter(wallet=wallet, contract_address=mint).order_by("timestamp"))
                if len(buys) <= 1:
                    continue
                
                # Iterate and compare with the last kept buy
                last_kept = buys[0]
                to_delete_ids = []
                for b in buys[1:]:
                    time_diff = abs((b.timestamp - last_kept.timestamp).total_seconds())
                    if time_diff <= 5:
                        to_delete_ids.append(b.id)
                    else:
                        last_kept = b
                
                if to_delete_ids:
                    deleted_count, _ = TokenBuy.objects.filter(id__in=to_delete_ids).delete()
                    deleted_token_buys_count += deleted_count

        self.stdout.write(self.style.SUCCESS(f"Removed {deleted_token_buys_count} duplicate TokenBuy rows."))

        # 2. Clean up duplicate MatchAlerts
        self.stdout.write("Cleaning up duplicate MatchAlert rows...")
        deleted_alerts_count = 0

        for wallet in wallets:
            alerts = MatchAlert.objects.filter(new_buy__wallet=wallet).select_related("new_buy", "matched_buy").order_by("sent_at")
            
            seen_pairs = set()
            to_delete_alert_ids = []
            for a in alerts:
                # Pair key
                pair_key = (a.new_buy.contract_address, a.matched_buy.contract_address)
                if pair_key in seen_pairs:
                    to_delete_alert_ids.append(a.id)
                else:
                    seen_pairs.add(pair_key)
            
            if to_delete_alert_ids:
                deleted_count, _ = MatchAlert.objects.filter(id__in=to_delete_alert_ids).delete()
                deleted_alerts_count += deleted_count

        self.stdout.write(self.style.SUCCESS(f"Removed {deleted_alerts_count} duplicate MatchAlert rows."))
