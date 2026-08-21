from django.urls import include, path

urlpatterns = [path("api/v1/crm/", include("apps.crm.urls"))]
