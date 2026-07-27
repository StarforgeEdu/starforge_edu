"""Rule visibility + acknowledgment status. Rules are few per center, so the
role-filter is evaluated in Python (a JSON-list intersection)."""

from __future__ import annotations

from apps.compliance.models import Rule, RuleAcknowledgment


def rules_for_roles(roles) -> list[Rule]:
    """Active rules that apply to a user holding `roles` (empty applies_to_roles
    means the rule applies to everyone)."""
    role_set = set(roles)
    out = []
    for rule in Rule.objects.filter(is_active=True):
        targets = rule.applies_to_roles or []
        if not targets or (set(targets) & role_set):
            out.append(rule)
    return out


def acknowledged_rule_ids_current(user, rules) -> set[int]:
    """Rule ids the user has acknowledged AT each rule's CURRENT version."""
    acks = set(RuleAcknowledgment.objects.filter(user=user, rule__in=rules).values_list("rule_id", "version"))
    return {r.id for r in rules if (r.id, r.version) in acks}


def pending_rules(user, roles) -> list[Rule]:
    rules = rules_for_roles(roles)
    acked = acknowledged_rule_ids_current(user, rules)
    return [r for r in rules if r.id not in acked]
