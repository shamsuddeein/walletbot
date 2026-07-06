from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django import forms
from django.conf import settings
from .models import Wallet, TokenBuy, MatchAlert
from tracker import helius as helius_api
from tracker.tasks import backfill_wallet_history_task

class EmailAdminAuthenticationForm(AdminAuthenticationForm):
    username = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"autofocus": True}),
        max_length=254
    )

admin.site.login_form = EmailAdminAuthenticationForm


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ["nickname", "address", "date_added", "added_by_telegram_id"]
    search_fields = ["nickname", "address"]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if "added_by_telegram_id" in form.base_fields:
            form.base_fields["added_by_telegram_id"].required = False
            form.base_fields["added_by_telegram_id"].help_text = (
                f"Optional. Defaults to primary owner ID ({settings.TELEGRAM_ALLOWED_USER_ID}) if left blank."
            )
        return form

    def save_model(self, request, obj, form, change):
        # Set default telegram ID if left blank
        if not obj.added_by_telegram_id:
            obj.added_by_telegram_id = settings.TELEGRAM_ALLOWED_USER_ID

        # Save to database
        super().save_model(request, obj, form, change)

        # Trigger sync and backfill on creation only
        if not change:
            try:
                ok = helius_api.register_wallet(obj.address)
                if ok:
                    self.message_user(request, f"Successfully registered webhook for wallet {obj.nickname} on Helius.")
                else:
                    self.message_user(request, f"⚠️ Warning: Wallet {obj.nickname} saved, but Helius webhook registration failed.", level='WARNING')
            except Exception as e:
                self.message_user(request, f"❌ Error registering on Helius: {e}", level='ERROR')

            # Trigger Celery backfill task
            backfill_wallet_history_task.delay(obj.address, obj.nickname, obj.added_by_telegram_id)
            self.message_user(request, f"Scheduled transaction history backfill task for {obj.nickname}.")

    def delete_model(self, request, obj):
        # Unregister from Helius
        try:
            ok = helius_api.unregister_wallet(obj.address)
            if ok:
                self.message_user(request, f"Successfully removed webhook for wallet {obj.nickname} on Helius.")
            else:
                self.message_user(request, f"⚠️ Warning: Wallet {obj.nickname} deleted, but Helius webhook unregistration failed.", level='WARNING')
        except Exception as e:
            self.message_user(request, f"❌ Error unregistering from Helius: {e}", level='ERROR')

        # Delete database record
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        # Unregister all selected wallets from Helius in bulk
        for obj in queryset:
            try:
                helius_api.unregister_wallet(obj.address)
            except Exception as e:
                self.message_user(request, f"❌ Error unregistering {obj.nickname} from Helius: {e}", level='ERROR')
        
        # Delete from database
        queryset.delete()


@admin.register(TokenBuy)
class TokenBuyAdmin(admin.ModelAdmin):
    list_display = ["wallet", "name", "symbol", "contract_address", "amount", "timestamp"]
    list_filter = ["wallet"]
    search_fields = ["name", "symbol", "contract_address"]


@admin.register(MatchAlert)
class MatchAlertAdmin(admin.ModelAdmin):
    list_display = ["new_buy", "matched_buy", "match_type", "name_score", "symbol_score", "logo_distance", "sent_at"]
    list_filter = ["match_type"]
