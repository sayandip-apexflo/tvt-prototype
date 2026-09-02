"""Notification-policy matching and recipient validation."""

from __future__ import annotations

import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from tvt_edge.db.models import AlertInstance, NotificationPolicy


EMAIL = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


def validate_recipients(values: object) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= 20:
        raise ValueError("notification policy requires 1 to 20 recipients")
    recipients = []
    for value in values:
        if not isinstance(value, str) or len(value) > 254 or not EMAIL.fullmatch(value):
            raise ValueError("notification policy contains an invalid recipient")
        recipients.append(value.lower())
    return sorted(set(recipients))


def matching_policies(
    session: Session, alert: AlertInstance
) -> list[NotificationPolicy]:
    candidates = session.scalars(
        select(NotificationPolicy)
        .where(NotificationPolicy.enabled.is_(True))
        .where(
            or_(
                NotificationPolicy.site_key == alert.site_key,
                NotificationPolicy.site_key.is_(None),
            )
        )
        .order_by(NotificationPolicy.name)
    ).all()
    result = []
    for policy in candidates:
        if policy.severity is not None and policy.severity != alert.severity:
            continue
        if policy.alert_name is not None and policy.alert_name != alert.alert_name:
            continue
        validate_recipients(policy.recipients)
        result.append(policy)
    return result
