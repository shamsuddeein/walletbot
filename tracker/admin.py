from django.contrib import admin
from .models import Wallet, TokenBuy, MatchAlert


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ["nickname", "address", "date_added", "added_by_telegram_id"]
    search_fields = ["nickname", "address"]


@admin.register(TokenBuy)
class TokenBuyAdmin(admin.ModelAdmin):
    list_display = ["wallet", "name", "symbol", "contract_address", "amount", "timestamp"]
    list_filter = ["wallet"]
    search_fields = ["name", "symbol", "contract_address"]


@admin.register(MatchAlert)
class MatchAlertAdmin(admin.ModelAdmin):
    list_display = ["new_buy", "matched_buy", "match_type", "name_score", "symbol_score", "logo_distance", "sent_at"]
    list_filter = ["match_type"]
