"""Cards response presenters (the DRF serializer output shapes)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from apps.cards.models import Card, CardType, Wallet, WalletTransaction

_TWO_PLACES = Decimal("0.01")


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(Decimal(value).quantize(_TWO_PLACES))


def card_type_to_dict(ct: CardType) -> dict:
    return {
        "id": ct.id,
        "name": ct.name,
        "is_active": ct.is_active,
        "created_by": ct.created_by_id,
        "created_at": ct.created_at.isoformat(),
    }


def card_to_dict(card: Card) -> dict:
    return {
        "id": card.id,
        "student": card.student_id,
        "card_type": card.card_type_id,
        "code": card.code,
        "is_active": card.is_active,
        "issued_by": card.issued_by_id,
        "issued_at": card.issued_at.isoformat(),
        "revoked_at": card.revoked_at.isoformat() if card.revoked_at else None,
        "revoke_reason": card.revoke_reason,
    }


def wallet_to_dict(wallet: Wallet) -> dict:
    return {
        "student": wallet.student_id,
        "balance_uzs": _money(wallet.balance_uzs),
        "updated_at": wallet.updated_at.isoformat(),
    }


def wallet_txn_to_dict(txn: WalletTransaction) -> dict:
    return {
        "id": txn.id,
        "kind": txn.kind,
        "amount_uzs": _money(txn.amount_uzs),
        "balance_after_uzs": _money(txn.balance_after_uzs),
        "created_by": txn.created_by_id,
        "note": txn.note,
        "created_at": txn.created_at.isoformat(),
    }


def wallet_payload_to_dict(payload: dict) -> dict:
    return {
        "wallet": wallet_to_dict(payload["wallet"]),
        "transactions": [wallet_txn_to_dict(t) for t in payload["transactions"]],
    }


def scan_to_dict(result: dict[str, Any]) -> dict:
    out = dict(result)
    scanned_at = out.get("scanned_at")
    if scanned_at is not None and hasattr(scanned_at, "isoformat"):
        out["scanned_at"] = scanned_at.isoformat()
    return out
