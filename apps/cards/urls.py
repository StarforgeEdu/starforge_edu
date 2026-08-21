from django.urls import path

from apps.cards.views.v1.card_views import (
    card_detail_view,
    card_revoke_view,
    card_scan_view,
    card_scans_collection_view,
    card_type_detail_view,
    card_types_collection_view,
    cards_collection_view,
    student_wallet_view,
    wallet_me_view,
    wallet_refund_view,
    wallet_spend_view,
    wallet_topup_view,
)

urlpatterns = [
    # Specific prefixes before the bare "" / "<pk>/" card routes.
    path("types/", card_types_collection_view, name="card-type-list"),
    path("types/<int:pk>/", card_type_detail_view, name="card-type-detail"),
    path("scan/", card_scan_view, name="card-scan"),
    path("scans/", card_scans_collection_view, name="card-scan-list"),
    path("wallets/me/", wallet_me_view, name="wallet-me"),
    path("wallets/<int:student_id>/", student_wallet_view, name="wallet-detail"),
    path("wallets/<int:student_id>/topup/", wallet_topup_view, name="wallet-topup"),
    path("wallets/<int:student_id>/spend/", wallet_spend_view, name="wallet-spend"),
    path("wallets/<int:student_id>/refund/", wallet_refund_view, name="wallet-refund"),
    path("", cards_collection_view, name="card-list"),
    path("<int:pk>/", card_detail_view, name="card-detail"),
    path("<int:pk>/revoke/", card_revoke_view, name="card-revoke"),
]
