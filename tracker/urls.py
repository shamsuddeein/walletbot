from django.urls import path
from .views import HeliusWebhookView

urlpatterns = [
    path("helius/", HeliusWebhookView.as_view(), name="helius-webhook"),
]
