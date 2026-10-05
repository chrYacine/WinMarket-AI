"""Pure data-access functions for `contact_requests`."""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.web.database.models import ContactRequest


def create_contact_request(
    db: Session,
    *,
    first_name: str | None,
    last_name: str | None,
    email: str | None,
    company: str | None,
    job_title: str | None,
    employee_count: int | None,
    plan: str | None,
    message: str | None,
) -> ContactRequest:
    request = ContactRequest(
        first_name=first_name,
        last_name=last_name,
        email=email,
        company=company,
        job_title=job_title,
        employee_count=employee_count,
        plan=plan,
        message=message,
        status="new",
    )
    db.add(request)
    db.flush()
    return request
