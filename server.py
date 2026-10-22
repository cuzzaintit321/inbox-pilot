"""Run with: python server.py. Local-first Gmail inbox review application."""
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from dotenv import load_dotenv
from google_auth_oauthlib.flow import Flow
from app.mail import SCOPES, service, load_candidates, send_reply
from app.drafts import draft_reply

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
# Local only in-memory state; restarting clears authorization and reviewed drafts.
auth_store = {}
review_store = {}


def client_config():
    return {'web': {'client_id': os.environ['GOOGLE_CLIENT_ID'], 'client_secret': os.environ['GOOGLE_CLIENT_SECRET'],
                    'auth_uri':'https://accounts.google.com/o/oauth2/auth', 'token_uri':'https://oauth2.googleapis.com/token',
                    'redirect_uris':[os.getenv('GOOGLE_REDIRECT_URI','http://localhost:5000/oauth/callback')]}}


def current_id():
    if 'sid' not in session: session['sid'] = secrets.token_urlsafe(24)
    return session['sid']


def credentials():
    return auth_store.get(current_id())


def demo_items():
    now = datetime.now(timezone.utc)
    return [
        {'id':'demo-1','subject':'Interview scheduling', 'from_name':'Taylor from Recruiting','from_email':'recruiter@example.com',
         'reason':'Awaiting response for 4 days','last_received':(now-timedelta(days=4)).strftime('%b %d, %Y'),
         'messages':[{'id':'sample1','from_email':'recruiter@example.com','from_name':'Taylor from Recruiting','reply_to':'recruiter@example.com','subject':'Interview scheduling',
                      'body':'Hi, we would like to schedule an interview. Could you share some times that work for you next week?', 'message_id':'<demo@example.com>','references':'','timestamp':now-timedelta(days=4)}]},
        {'id':'demo-2','subject':'Request for additional information','from_name':'Alex from HR','from_email':'hr@example.com',
         'reason':'Awaiting response for 5 days','last_received':(now-timedelta(days=5)).strftime('%b %d, %Y'),
         'messages':[{'id':'sample2','from_email':'hr@example.com','from_name':'Alex from HR','reply_to':'hr@example.com','subject':'Request for additional information',
                      'body':'Hello, could you confirm which certifications you hold so we can move your application forward?', 'message_id':'<demo2@example.com>','references':'','timestamp':now-timedelta(days=5)}]}
    ]


@app.get('/')
def index():
    sid = current_id()
    return render_template('index.html', connected=bool(credentials()), configured=bool(os.getenv('GOOGLE_CLIENT_ID') and os.getenv('GOOGLE_CLIENT_SECRET')),
                           items=review_store.get(sid, []), demo=session.get('demo', False), days=session.get('days',7), threshold=session.get('threshold',3))


@app.get('/oauth/start')
def oauth_start():
    if not os.getenv('GOOGLE_CLIENT_ID') or not os.getenv('GOOGLE_CLIENT_SECRET'):
        flash('Configure Google OAuth credentials in .env first.', 'error'); return redirect(url_for('index'))
    flow = Flow.from_client_config(client_config(), scopes=SCOPES, redirect_uri=os.getenv('GOOGLE_REDIRECT_URI','http://localhost:5000/oauth/callback'))
    url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
    session['oauth_state'] = state
    return redirect(url)


@app.get('/oauth/callback')
def oauth_callback():
    state = session.pop('oauth_state', None)
    if not state or state != request.args.get('state'):
        flash('OAuth state mismatch. Please reconnect.', 'error'); return redirect(url_for('index'))
    flow = Flow.from_client_config(client_config(), scopes=SCOPES, state=state, redirect_uri=os.getenv('GOOGLE_REDIRECT_URI','http://localhost:5000/oauth/callback'))
    flow.fetch_token(authorization_response=request.url)
    auth_store[current_id()] = flow.credentials.to_json()
    session['demo'] = False
    flash('Gmail connected. Your inbox remains private to this local app.', 'success')
    return redirect(url_for('index'))


@app.post('/demo')
def demo():
    session['demo'] = True
    review_store[current_id()] = prepare(demo_items())
    return redirect(url_for('index'))


def prepare(threads):
    for t in threads:
        t['approved_body'], t['ai_generated'] = draft_reply(t)
        t['status'] = 'review'
    return threads


@app.post('/scan')
def scan():
    days = max(1,min(90,int(request.form.get('days',7))))
    threshold = max(0,min(30,int(request.form.get('threshold',3))))
    session['days'],session['threshold'] = days,threshold
    if session.get('demo') and not credentials():
        review_store[current_id()] = prepare(demo_items())
    elif credentials():
        try:
            results = load_candidates(service(credentials()),days=days,min_days=threshold)
            review_store[current_id()] = prepare(results)
        except Exception as exc:
            flash('Scan failed. Check your Gmail authorization and try reconnecting.', 'error')
            app.logger.warning('Gmail scan error: %s', exc)
    else: flash('Connect Gmail or open demo mode first.', 'error')
    return redirect(url_for('index'))


@app.post('/draft/<thread_id>')
def edit(thread_id):
    thread = next((t for t in review_store.get(current_id(),[]) if t['id']==thread_id),None)
    if not thread: return 'Thread not found',404
    body = request.form.get('body','').strip()
    if not body or len(body)>15000:
        flash('Please enter a reply of 1–15,000 characters.', 'error');return redirect(url_for('index')+'#'+thread_id)
    thread['approved_body']=body
    thread['status']='review'
    flash('Your edited reply is saved locally for review. Nothing was sent.', 'success')
    return redirect(url_for('index')+'#'+thread_id)


@app.post('/send/<thread_id>')
def send(thread_id):
    thread = next((t for t in review_store.get(current_id(),[]) if t['id']==thread_id),None)
    if not thread: return 'Thread not found',404
    if session.get('demo') or thread_id.startswith('demo-'):
        flash('Demo mode never sends email. Connect Gmail to send real replies.', 'error');return redirect(url_for('index')+'#'+thread_id)
    if not credentials(): return 'Not connected',403
    if request.form.get('confirm') != 'SEND THIS REPLY':
        flash('Send not authorized. Type the exact confirmation phrase.', 'error');return redirect(url_for('index')+'#'+thread_id)
    if request.form.get('body','') != thread['approved_body']:
        flash('The draft changed. Save your edits before approving.', 'error');return redirect(url_for('index')+'#'+thread_id)
    if thread['status']=='sent':
        flash('This reply was already sent.', 'error');return redirect(url_for('index')+'#'+thread_id)
    try:
        send_reply(service(credentials()), thread)
        thread['status']='sent'
        flash('Reply sent for this thread only.', 'success')
    except Exception as exc:
        app.logger.warning('Send error: %s',exc)
        flash('Gmail could not send this reply. Nothing has been marked sent.', 'error')
    return redirect(url_for('index')+'#'+thread_id)


@app.post('/disconnect')
def disconnect():
    auth_store.pop(current_id(),None);review_store.pop(current_id(),None);session.clear()
    flash('Disconnected and cleared local session data.', 'success')
    return redirect(url_for('index'))


if __name__=='__main__':
    app.run(host='127.0.0.1',port=int(os.getenv('PORT','5000')),debug=False)
