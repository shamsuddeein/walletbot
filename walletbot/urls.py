from django.contrib import admin
from django.urls import path, include
from tracker.views import admin_analytics_view

urlpatterns = [
    path("admin/analytics/", admin_analytics_view, name="admin_analytics"),
    path("admin/", admin.site.urls),
    path("webhook/", include("tracker.urls")),
]
