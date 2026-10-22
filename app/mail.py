"""Gmail access and candidate ranking. No automatic sending."""
import base64
import email.utils
import re
from datetime import datetime, timedelta, timezone
from html import unescape

SCOPES = ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.send"]


def service(credentials_dict):
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    credentials = Credentials.from_authorized_user_info(credentials_dict, scopes=SCOPES)
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def header(msg, name):
    return next((h['value'] for h in msg.get('payload', {}).get('headers', []) if h['name'].lower() == name.lower()), '')


def plain_body(payload):
    if payload.get('mimeType') == 'text/plain' and payload.get('body', {}).get('data'):
        return base64.urlsafe_b64decode(payload['body']['data'] + '===').decode('utf-8', 'replace')
    for part in payload.get('parts', []):
        result = plain_body(part)
        if result:
            return result
    if payload.get('body', {}).get('data'):
        raw = base64.urlsafe_b64decode(payload['body']['data'] + '===').decode('utf-8', 'replace')
        return re.sub(r'<[^>]*>', ' ', unescape(raw))
    return ''


def unpack_message(msg):
    raw_from = header(msg, 'From')
    from_name, from_email = email.utils.parseaddr(raw_from)
    to_email = email.utils.parseaddr(header(msg, 'To'))[1]
    return {
        'id': msg['id'], 'thread_id': msg['threadId'],
        'from_name': from_name or from_email, 'from_email': from_email,
        'to_email': to_email, 'reply_to': email.utils.parseaddr(header(msg, 'Reply-To'))[1] or from_email,
        'subject': header(msg, 'Subject'), 'message_id': header(msg, 'Message-ID'),
        'references': header(msg, 'References'), 'body': plain_body(msg.get('payload', {})),
        'timestamp': datetime.fromtimestamp(int(msg['internalDate']) / 1000, timezone.utc),
        'labels': msg.get('labelIds', []),
    }


def is_automated(message):
    email = message['from_email'].lower()
    body = message['body'][:2000].lower()
    return any(s in email for s in ('noreply', 'no-reply', 'donotreply', 'mailer-daemon')) or any(
        s in body for s in ('unsubscribe from these emails', 'manage email preferences', 'email preferences | unsubscribe')
    ) or 'CATEGORY_PROMOTIONS' in message['labels']


def score_thread(messages, me, min_days=3):
    """Return whether a conversation needs review; never assume obligation from unread alone."""
    if not messages:
        return False, 'Empty thread'
    last = messages[-1]
    if last['from_email'].lower() in {x.lower() for x in me}:
        return False, 'You replied last'
    if is_automated(last):
        return False, 'Automated or promotional'
    days = (datetime.now(timezone.utc) - last['timestamp']).total_seconds() / 86400
    if days < min_days:
        return False, f'Waiting {days:.1f} days (threshold {min_days})'
    if not any(x in last['body'].lower() for x in ('?', 'please', 'let me know', 'can you', 'could you', 'reply', 'response', 'confirm', 'available', 'following up', 'deadline')):
        return False, 'No clear request for response'
    return True, f'Awaiting response for {int(days)} days'


def load_candidates(svc, days=7, min_days=3, limit=100):
    # Gmail search uses latest activity: load full threads to check who wrote last.
    query = f'newer_than:{days}d -in:trash -in:spam'
    ids, token = [], None
    while len(ids) < limit:
        data = svc.users().messages().list(userId='me', q=query, maxResults=min(100, limit-len(ids)), pageToken=token).execute()
        ids.extend(x['threadId'] for x in data.get('messages', []))
        token = data.get('nextPageToken')
        if not token: break
    profile = svc.users().getProfile(userId='me').execute()
    me = [profile['emailAddress']]
    threads = []
    for thread_id in dict.fromkeys(ids):
        thread = svc.users().threads().get(userId='me', id=thread_id, format='full').execute()
        messages = sorted((unpack_message(m) for m in thread.get('messages', [])), key=lambda m:m['timestamp'])
        ok, reason = score_thread(messages, me, min_days)
        if ok:
            last = messages[-1]
            threads.append({'id': thread_id, 'subject': last['subject'], 'from_name': last['from_name'], 'from_email': last['reply_to'], 'reason': reason, 'last_received': last['timestamp'].strftime('%b %d, %Y'), 'messages': messages})
    threads.sort(key=lambda x:x['messages'][-1]['timestamp'])
    return threads


def send_reply(svc, thread):
    """Explicitly invoked only by the approved send endpoint."""
    from email.message import EmailMessage
    last = thread['messages'][-1]
    msg = EmailMessage()
    msg['To'] = last['reply_to']
    subject = last['subject']
    msg['Subject'] = subject if subject.lower().startswith('re:') else 'Re: ' + subject
    if last['message_id']:
        msg['In-Reply-To'] = last['message_id']
        msg['References'] = (last['references'] + ' ' + last['message_id']).strip()
    msg.set_content(thread['approved_body'])
    encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return svc.users().messages().send(userId='me', body={'raw':encoded, 'threadId':thread['id']}).execute()
