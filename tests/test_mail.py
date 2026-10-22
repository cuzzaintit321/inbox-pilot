from datetime import datetime, timedelta, timezone
from app.mail import score_thread


def msg(sender='recruiter@example.org', days=5, body='Could you please reply?', labels=None):
    return {'from_email':sender,'timestamp':datetime.now(timezone.utc)-timedelta(days=days), 'body':body,'labels':labels or []}


def test_candidate_requires_answer():
    assert score_thread([msg()], ['me@example.org'])[0]


def test_skip_recent():
    assert not score_thread([msg(days=1)], ['me@example.org'])[0]


def test_skip_my_last_reply():
    assert not score_thread([msg(sender='me@example.org')], ['me@example.org'])[0]


def test_skip_promotions():
    assert not score_thread([msg(labels=['CATEGORY_PROMOTIONS'])], ['me@example.org'])[0]


def test_skip_noreply():
    assert not score_thread([msg(sender='no-reply@example.org')], ['me@example.org'])[0]
