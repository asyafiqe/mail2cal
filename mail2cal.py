import imaplib
import email
from email.header import decode_header
import json
import os
import sys
from datetime import datetime, timedelta, date
import re
import time
import requests
from caldav import DAVClient
from icalendar import Calendar, Event
import pytz
import html2text
import logging
import uuid
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import pickle
from difflib import SequenceMatcher
import hashlib

# Load environment variables from .env file
load_dotenv()

# Configuration - Set these environment variables or update directly
CONFIG = {
    'GMAIL_USER': os.getenv('GMAIL_USER', 'your-email@gmail.com'),
    'GMAIL_APP_PASSWORD': os.getenv('GMAIL_APP_PASSWORD', 'your-app-password'),
    'CALDAV_URL': os.getenv('CALDAV_URL', 'http://localhost:5232'),
    'CALDAV_USERNAME': os.getenv('CALDAV_USERNAME', 'username'),
    'CALDAV_PASSWORD': os.getenv('CALDAV_PASSWORD', 'password'),
    'CALENDAR_NAME': os.getenv('CALENDAR_NAME', 'default'),  # Calendar name in Radicale
    'SEARCH_SUBJECT': os.getenv('SEARCH_SUBJECT', 'Meeting Request'),  # Subject pattern to match
    'OPENROUTER_API_KEY': os.getenv('OPENROUTER_API_KEY', 'your-openrouter-key'),
    'OPENROUTER_MODEL': os.getenv('OPENROUTER_MODEL', 'openai/gpt-3.5-turbo'),  # or gpt-4
    'CHECK_INTERVAL': int(os.getenv('CHECK_INTERVAL', '60')),  # seconds
    'TIMEZONE': os.getenv('TIMEZONE', 'UTC'),  # Your local timezone
    'MARK_AS_PROCESSED': os.getenv('MARK_AS_PROCESSED', 'true').lower() == 'true',
    'MAX_EMAIL_BODY_CHARS': int(os.getenv('MAX_EMAIL_BODY_CHARS', '3000')),
    'RETRY_INTERVAL': int(os.getenv('RETRY_INTERVAL', '60')),  # Interval to retry on error
    'GOOGLE_CREDENTIALS_FILE': os.getenv('GOOGLE_CREDENTIALS_FILE', './credentials/google_credentials.json'),
    'GOOGLE_TOKEN_FILE': os.getenv('GOOGLE_TOKEN_FILE', './credentials/google_token.json'),
    'GOOGLE_CALENDAR_NAME': os.getenv('GOOGLE_CALENDAR_NAME', 'primary'),
    'CALDAV_RETRY_ATTEMPTS': int(os.getenv('CALDAV_RETRY_ATTEMPTS', '5')),
    'CALDAV_RETRY_DELAY': int(os.getenv('CALDAV_RETRY_DELAY', '10')),
    'EVENT_PREFIX': os.getenv('EVENT_PREFIX', ""),
    'ENABLE_CALDAV': os.getenv('ENABLE_CALDAV', 'true').lower() == 'true',
    'ENABLE_GOOGLE_CALENDAR': os.getenv('ENABLE_GOOGLE_CALENDAR', 'true').lower() == 'true',
    'SIMILARITY_THRESHOLD': float(os.getenv('SIMILARITY_THRESHOLD', '0.7')),  # 70% similarity threshold
}

# Configure stdout to use UTF-8
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Setup logging
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("./logs/mail2calendar.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class EmailCalendarAutomator:
    def __init__(self):
        self.timezone = pytz.timezone(CONFIG['TIMEZONE'])
        self.processed_emails = set()
        self.google_service = None
        self.caldav_calendar = None
        # Track created event UIDs to prevent duplicates
        self.created_event_uids = set()
        # Caching for performance
        self._caldav_event_cache = None
        self._caldav_cache_time = None
        self._google_event_cache = None
        self._google_cache_time = None

    def initialize_calendars(self):
        """Initialize CalDAV and Google Calendar connections based on configuration"""
        # Initialize CalDAV if enabled
        if CONFIG['ENABLE_CALDAV']:
            caldav_success = False
            for attempt in range(CONFIG['CALDAV_RETRY_ATTEMPTS']):
                try:
                    self.caldav_calendar = self.connect_caldav()
                    logger.info("CalDAV calendar initialized successfully")
                    caldav_success = True
                    break
                except Exception as e:
                    logger.warning(f"CalDAV connection attempt {attempt + 1}/{CONFIG['CALDAV_RETRY_ATTEMPTS']} failed: {e}")
                    if attempt < CONFIG['CALDAV_RETRY_ATTEMPTS'] - 1:
                        logger.info(f"Retrying CalDAV connection in {CONFIG['CALDAV_RETRY_DELAY']} seconds...")
                        time.sleep(CONFIG['CALDAV_RETRY_DELAY'])
                    else:
                        logger.error(f"All CalDAV connection attempts failed")
                        self.caldav_calendar = None
        else:
            logger.info("CalDAV is disabled via ENABLE_CALDAV=false")
            self.caldav_calendar = None

        # Initialize Google Calendar if enabled
        if CONFIG['ENABLE_GOOGLE_CALENDAR']:
            try:
                self.google_service = self.authenticate_google()
                self.list_google_calendars(self.google_service)
                logger.info("Google Calendar initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Google Calendar: {e}")
                self.google_service = None
        else:
            logger.info("Google Calendar is disabled via ENABLE_GOOGLE_CALENDAR=false")
            self.google_service = None

    def connect_gmail(self):
        """Connect to Gmail IMAP server"""
        try:
            mail = imaplib.IMAP4_SSL('imap.gmail.com')
            mail.login(CONFIG['GMAIL_USER'], CONFIG['GMAIL_APP_PASSWORD'])
            logger.info("Connected to Gmail successfully")
            return mail
        except imaplib.IMAP4.error as e:
            logger.error(f"Gmail authentication error: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to connect to Gmail: {e}")
            raise

    def connect_caldav(self):
        """Connect to CalDAV server (Radicale)"""
        try:
            client = DAVClient(
                url=CONFIG['CALDAV_URL'],
                username=CONFIG['CALDAV_USERNAME'],
                password=CONFIG['CALDAV_PASSWORD']
            )
            principal = client.principal()
            calendars = principal.calendars()
            # Debug: List all available calendars
            logger.debug("Available calendars:")
            # Find the specified calendar by display name
            target_calendar = None
            for calendar in calendars:
                try:
                    # Try to get the display name
                    display_name = str(calendar.name) if hasattr(calendar, 'name') else ''
                    if not display_name:
                        # Fallback method
                        props = calendar.get_properties(['{DAV:}displayname'])
                        display_name = props.get('{DAV:}displayname', '')
                    logger.debug(f"  - Name: '{display_name}', URL: {calendar.url}")
                    if display_name == CONFIG['CALENDAR_NAME']:
                        target_calendar = calendar
                        logger.info(f"Found existing calendar: {CONFIG['CALENDAR_NAME']}")
                        break
                except Exception as e:
                    logger.warning(f"Could not get name for calendar {calendar.url}: {e}")
                    continue
            if not target_calendar:
                logger.warning(f"Calendar '{CONFIG['CALENDAR_NAME']}' not found, creating it")
                # Try to create the calendar if not found
                target_calendar = principal.make_calendar(name=CONFIG['CALENDAR_NAME'])
                logger.info(f"Created new calendar: {CONFIG['CALENDAR_NAME']}")
            logger.info("Connected to CalDAV successfully")
            return target_calendar
        except Exception as e:
            logger.error(f"Failed to connect to CalDAV: {e}")
            raise

    def get_email_body(self, msg):
        """Extract plain text body from email message"""
        body = ""
        html_body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                if "attachment" not in content_disposition:
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or 'utf-8'
                            decoded_text = payload.decode(charset, errors='replace')
                            if content_type == "text/plain":
                                body = decoded_text
                            elif content_type == "text/html":
                                html_body = decoded_text
                    except Exception as e:
                        logger.warning(f"Error decoding email part: {e}")
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or 'utf-8'
                    body = payload.decode(charset, errors='replace')
            except Exception as e:
                logger.warning(f"Error decoding email body: {e}")
        # If we only have HTML, convert it to plain text
        if not body and html_body:
            converter = html2text.HTML2Text()
            converter.ignore_links = False
            converter.ignore_images = True
            body = converter.handle(html_body)
        # Limit body size
        max_chars = CONFIG['MAX_EMAIL_BODY_CHARS']
        if len(body) > max_chars:
            logger.info(f"Truncating email body from {len(body)} to {max_chars} characters")
            body = body[:max_chars] + "... [truncated]"
        return body.strip()

    def parse_email_with_ai(self, subject, body, sender=None):
        """Use OpenRouter (OpenAI-compatible) to parse email content into event details"""
        current_datetime = datetime.now(self.timezone).strftime("%Y-%m-%d %H:%M:%S %Z")
        prompt = f"""
        The current date and time is: {current_datetime}
        Parse this email and extract calendar event information. Return ONLY valid JSON with these fields:
        - title (string): Event title/summary. Start the event title with "{CONFIG['EVENT_PREFIX']}".
        - start_date (string): ISO format date/time (YYYY-MM-DDTHH:MM:SS+00:00) in UTC
        - end_date (string): ISO format date/time (YYYY-MM-DDTHH:MM:SS+00:00) in UTC
        - location (string, optional): Event location
        - description (string, optional): Event description, Zoom/Meeting url (if available)
        If dates are relative (like "tomorrow" or "next Friday"), calculate actual dates based on the current date.
        If times are ambiguous (like "3pm"), use context to determine AM/PM.
        If end time is not specified, assume 1 hour duration.
        If no valid event information can be found, return empty JSON {{}}.
        Email Details:
        From: {sender or 'Unknown'}
        Subject: {subject}
        Body:
        {body}
        Response format MUST be valid JSON:
        {{
            "title": "...",
            "start_date": "...",
            "end_date": "...",
            "location": "...",
            "description": "..."
        }}
        """
        headers = {
            "Authorization": f"Bearer {CONFIG['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/email-calendar-automator"  # Replace with your domain
        }
        data = {
            "model": CONFIG['OPENROUTER_MODEL'],
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 1000
        }
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=data,
                timeout=30
            )
            response.raise_for_status()
            result = response.json()
            content = result['choices'][0]['message']['content'].strip()
            # Extract JSON from potential markdown code blocks
            if "```" in content:
                # Extract content between code blocks
                match = re.search(r'```(?:json)?(.*?)```', content, re.DOTALL)
                if match:
                    content = match.group(1).strip()
            # Try to find JSON object in response
            match = re.search(r'({.*})', content, re.DOTALL)
            if match:
                content = match.group(1)
            try:
                event_data = json.loads(content)
                # Validate required fields
                if not all(k in event_data for k in ['title', 'start_date', 'end_date']):
                    if event_data:  # If we got some data but not complete
                        logger.warning(f"AI response missing required fields: {event_data}")
                    else:
                        logger.info("No event details found in email")
                    return None
                # Ensure dates are in ISO format with timezone
                for date_field in ['start_date', 'end_date']:
                    dt = event_data[date_field]
                    # Add timezone info if missing
                    if not ('+' in dt or 'Z' in dt):
                        event_data[date_field] = f"{dt}+00:00"
                logger.info(f"Parsed event: {event_data['title']} from {event_data['start_date']} to {event_data['end_date']}")
                return event_data
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse AI JSON response: {e}")
                logger.debug(f"AI Response: {content}")
                return None
        except requests.RequestException as e:
            logger.error(f"Failed to call OpenRouter API: {e}")
            return None

    def get_caldav_events(self, calendar):
        """Retrieve all events from CalDAV calendar with caching"""
        now = time.time()
        # Use cache if less than 2 minutes old (more frequent updates)
        if self._caldav_event_cache and self._caldav_cache_time and (now - self._caldav_cache_time) < 120:
            return self._caldav_event_cache
        
        try:
            events = calendar.events()
            parsed_events = []
            for event in events:
                try:
                    ical = Calendar.from_ical(event.data)
                    for component in ical.walk():
                        if component.name == "VEVENT":
                            uid = str(component.get('uid', ''))
                            # Skip if we've already processed this UID recently
                            if uid in self.created_event_uids:
                                continue
                                
                            parsed_events.append({
                                'uid': uid,
                                'summary': str(component.get('summary', '')),
                                'start': component.get('dtstart').dt if component.get('dtstart') else None,
                                'end': component.get('dtend').dt if component.get('dtend') else None,
                                'location': str(component.get('location', '')),
                                'description': str(component.get('description', '')),
                                'raw': component
                            })
                except Exception as e:
                    logger.debug(f"Error parsing CalDAV event: {e}")
                    continue
            self._caldav_event_cache = parsed_events
            self._caldav_cache_time = now
            return parsed_events
        except Exception as e:
            logger.error(f"Error retrieving CalDAV events: {e}")
            return []

    def get_google_events(self, service, time_min=None, time_max=None):
        """Retrieve events from Google Calendar with caching"""
        now = time.time()
        # Use cache if less than 5 minutes old and no time constraints
        if not time_min and not time_max and self._google_event_cache and self._google_cache_time and (now - self._google_cache_time) < 300:
            return self._google_event_cache
            
        try:
            calendar_id = self.get_calendar_id_by_name(service, CONFIG['GOOGLE_CALENDAR_NAME'])
            # Default to next 365 days if no time range specified
            if not time_min:
                time_min = datetime.now(pytz.UTC) - timedelta(days=30)
            if not time_max:
                time_max = datetime.now(pytz.UTC) + timedelta(days=335)
                
            events_result = service.events().list(
                calendarId=calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            events = events_result.get('items', [])
            parsed_events = []
            for event in events:
                try:
                    start = event['start'].get('dateTime', event['start'].get('date'))
                    end = event['end'].get('dateTime', event['end'].get('date'))
                    
                    # Parse datetime strings
                    if 'T' in start:
                        start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                    else:
                        start_dt = datetime.fromisoformat(start).date()
                        
                    if 'T' in end:
                        end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
                    else:
                        end_dt = datetime.fromisoformat(end).date()
                        
                    parsed_events.append({
                        'id': event.get('id'),
                        'summary': event.get('summary', ''),
                        'start': start_dt,
                        'end': end_dt,
                        'location': event.get('location', ''),
                        'description': event.get('description', ''),
                        'raw': event
                    })
                except Exception as e:
                    logger.debug(f"Error parsing Google event: {e}")
                    continue
                    
            # Update cache only for full calendar fetch
            if not time_min and not time_max:
                self._google_event_cache = parsed_events
                self._google_cache_time = now
            return parsed_events
        except Exception as e:
            logger.error(f"Error retrieving Google events: {e}")
            return []
            
    def is_event_duplicate(self, new_event_data, existing_events):
        """Check if event already exists in the list of existing events"""
        for existing_event in existing_events:
            # Check time overlap first
            if not self.events_overlap(
                new_event_data['start_date'], new_event_data['end_date'],
                existing_event['start'], existing_event['end']
            ):
                continue
                
            # Check content similarity
            title_sim = self.calculate_similarity(new_event_data['title'], existing_event['summary'])
            desc_sim = self.calculate_similarity(new_event_data['description'], existing_event['description'])
            loc_sim = self.calculate_similarity(new_event_data['location'], existing_event['location'])
            
            # If very high similarity, consider it a duplicate
            if title_sim > 0.9 and desc_sim > 0.8:
                return True
                
        return False
    def calculate_similarity(self, text1, text2):
        """Calculate similarity ratio between two texts"""
        if not text1 and not text2:
            return 1.0
        if not text1 or not text2:
            return 0.0
        return SequenceMatcher(None, str(text1).lower(), str(text2).lower()).ratio()

    def events_overlap(self, start1, end1, start2, end2):
        """Check if two time periods overlap"""
        # Handle date-only events
        if isinstance(start1, datetime) and isinstance(start2, datetime):
            return max(start1, start2) < min(end1, end2)
        elif isinstance(start1, date) and isinstance(start2, date):
            return max(start1, start2) <= min(end1, end2)
        # Mixed datetime/date - convert date to datetime
        else:
            if isinstance(start1, date) and not isinstance(start1, datetime):
                start1 = datetime.combine(start1, datetime.min.time()).replace(tzinfo=pytz.UTC)
            if isinstance(end1, date) and not isinstance(end1, datetime):
                end1 = datetime.combine(end1, datetime.max.time()).replace(tzinfo=pytz.UTC)
            if isinstance(start2, date) and not isinstance(start2, datetime):
                start2 = datetime.combine(start2, datetime.min.time()).replace(tzinfo=pytz.UTC)
            if isinstance(end2, date) and not isinstance(end2, datetime):
                end2 = datetime.combine(end2, datetime.max.time()).replace(tzinfo=pytz.UTC)
            return max(start1, start2) < min(end1, end2)

    def find_similar_events(self, new_event, caldav_events=None, google_events=None):
        """Find events with >60% similarity in time and content (lowered threshold)"""
        similar_events = []
        threshold = 0.6  # Lowered threshold for better detection
        
        # Check CalDAV events
        if CONFIG['ENABLE_CALDAV'] and caldav_events is not None:
            for event in caldav_events:
                # Skip if it's the same event we're comparing
                if hasattr(new_event, 'get') and new_event.get('uid') == event.get('uid'):
                    continue
                    
                # Time overlap check
                if not self.events_overlap(
                    new_event['start_date'], new_event['end_date'],
                    event['start'], event['end']
                ):
                    continue
                    
                # Content similarity
                title_sim = self.calculate_similarity(new_event['title'], event['summary'])
                desc_sim = self.calculate_similarity(new_event['description'], event['description'])
                loc_sim = self.calculate_similarity(new_event['location'], event['location'])
                
                # Weighted similarity (title is most important)
                overall_sim = (title_sim * 0.5 + desc_sim * 0.3 + loc_sim * 0.2)
                if overall_sim > threshold:
                    similar_events.append({
                        'type': 'caldav',
                        'event': event,
                        'similarity': overall_sim
                    })
        
        # Check Google events
        if CONFIG['ENABLE_GOOGLE_CALENDAR'] and google_events is not None:
            for event in google_events:
                # Time overlap check
                if not self.events_overlap(
                    new_event['start_date'], new_event['end_date'],
                    event['start'], event['end']
                ):
                    continue
                    
                # Content similarity
                title_sim = self.calculate_similarity(new_event['title'], event['summary'])
                desc_sim = self.calculate_similarity(new_event['description'], event['description'])
                loc_sim = self.calculate_similarity(new_event['location'], event['location'])
                
                # Weighted similarity
                overall_sim = (title_sim * 0.5 + desc_sim * 0.3 + loc_sim * 0.2)
                if overall_sim > threshold:
                    similar_events.append({
                        'type': 'google',
                        'event': event,
                        'similarity': overall_sim
                    })
        
        return sorted(similar_events, key=lambda x: x['similarity'], reverse=True)

    def update_caldav_event(self, calendar, old_event, new_event_data):
        """Update an existing CalDAV event with new data"""
        try:
            # Get the actual event object from CalDAV, not just the parsed data
            event_obj = calendar.event_by_uid(old_event['uid'])
            if not event_obj:
                logger.warning(f"Event with UID {old_event['uid']} not found in CalDAV calendar")
                return False
    
            # Parse the current iCalendar data
            cal = Calendar.from_ical(event_obj.data)
            for component in cal.walk():
                if component.name == "VEVENT":
                    # Update fields
                    component['summary'] = new_event_data['title']
    
                    # Handle datetime conversion
                    start_dt = new_event_data['start_date']
                    end_dt = new_event_data['end_date']
                    if isinstance(start_dt, str):
                        start_dt = datetime.fromisoformat(start_dt.replace('Z', '+00:00'))
                    if isinstance(end_dt, str):
                        end_dt = datetime.fromisoformat(end_dt.replace('Z', '+00:00'))

                    # Ensure timezone awareness
                    if start_dt.tzinfo is None:
                        start_dt = pytz.UTC.localize(start_dt)
                    if end_dt.tzinfo is None:
                        end_dt = pytz.UTC.localize(end_dt)

                    component['dtstart'] = start_dt
                    component['dtend'] = end_dt

                    if new_event_data.get('location'):
                        component['location'] = new_event_data['location']
                    if new_event_data.get('description'):
                        component['description'] = new_event_data['description']

                    # Update timestamps
                    component['last-modified'] = datetime.now(pytz.UTC)
                    break

            # Serialize and save updated event using the existing event's save() method
            event_obj.data = cal.to_ical()
            event_obj.save()  # This does PUT and avoids no_overwrite issues

            logger.info(f"Updated CalDAV event: {new_event_data['title']}")
            return True
        except Exception as e:
            logger.error(f"Failed to update CalDAV event: {e}")
        return False

    def update_google_event(self, service, old_event, new_event_data):
        """Update an existing Google Calendar event with new data"""
        try:
            calendar_id = self.get_calendar_id_by_name(service, CONFIG['GOOGLE_CALENDAR_NAME'])
            
            # Handle datetime conversion
            start_dt = new_event_data['start_date']
            end_dt = new_event_data['end_date']
            
            if isinstance(start_dt, str):
                start_dt = datetime.fromisoformat(start_dt.replace('Z', '+00:00'))
            if isinstance(end_dt, str):
                end_dt = datetime.fromisoformat(end_dt.replace('Z', '+00:00'))
                
            # Prepare update body
            event_body = {
                'summary': new_event_data['title'],
                'start': {
                    'dateTime': start_dt.isoformat() if isinstance(start_dt, datetime) else start_dt.isoformat(),
                    'timeZone': CONFIG['TIMEZONE'],
                },
                'end': {
                    'dateTime': end_dt.isoformat() if isinstance(end_dt, datetime) else end_dt.isoformat(),
                    'timeZone': CONFIG['TIMEZONE'],
                },
            }
            
            if new_event_data.get('location'):
                event_body['location'] = new_event_data['location']
            if new_event_data.get('description'):
                event_body['description'] = new_event_data['description']
                
            # Update the event
            updated_event = service.events().update(
                calendarId=calendar_id,
                eventId=old_event['id'],
                body=event_body
            ).execute()
            
            logger.info(f"Updated Google Calendar event: {new_event_data['title']}")
            return True
        except Exception as e:
            logger.error(f"Failed to update Google Calendar event: {e}")
            return False

    def create_calendar_event(self, calendar, event_data):
        """Create calendar event in Radicale"""
        try:
            # First, check if similar event already exists
            caldav_events = self.get_caldav_events(calendar)
            if self.is_event_duplicate(event_data, caldav_events):
                logger.info(f"Skipping duplicate event creation: {event_data['title']}")
                return True

            cal = Calendar()
            event = Event()
            
            # Add required properties
            event.add('summary', event_data['title'])
            
            # Handle datetime objects
            start_dt = event_data['start_date']
            end_dt = event_data['end_date']
            
            if isinstance(start_dt, str):
                start_dt = datetime.fromisoformat(start_dt.replace('Z', '+00:00'))
            if isinstance(end_dt, str):
                end_dt = datetime.fromisoformat(end_dt.replace('Z', '+00:00'))
                
            # Ensure timezone awareness
            if start_dt.tzinfo is None:
                start_dt = pytz.UTC.localize(start_dt)
            if end_dt.tzinfo is None:
                end_dt = pytz.UTC.localize(end_dt)
                
            event.add('dtstart', start_dt)
            event.add('dtend', end_dt)
            
            # Add optional properties
            if event_data.get('location'):
                event.add('location', event_data['location'])
            if event_data.get('description'):
                event.add('description', event_data['description'])
            
            # Add required timestamps
            now = datetime.now(pytz.UTC)
            event.add('dtstamp', now)
            event.add('created', now)
            event.add('last-modified', now)
            
            # Create unique UID and track it
            event_uid = str(uuid.uuid4())
            event.add('uid', event_uid)
            self.created_event_uids.add(event_uid)
            
            # Add to calendar
            cal.add_component(event)
            
            # Save to CalDAV server
            calendar.save_event(cal.to_ical())
            logger.info(f"Created calendar event: {event_data['title']} at {start_dt}")
            return True
        except Exception as e:
            logger.error(f"Failed to create calendar event: {e}")
            return False

    def process_emails(self, mail):
        """Process unread emails matching the subject pattern"""
        try:
            mail.select('inbox')
            search_criteria = f'(UNSEEN SUBJECT "{CONFIG["SEARCH_SUBJECT"]}")'
            status, messages = mail.search(None, search_criteria)
            if status != 'OK':
                logger.error("Failed to search emails")
                return
            if not messages[0]:
                logger.debug("No new matching emails found")
                return
                
            email_ids = messages[0].split()
            logger.info(f"Found {len(email_ids)} new matching emails")
            
            for email_id in email_ids:
                email_id_str = email_id.decode() if isinstance(email_id, bytes) else email_id
                if email_id_str in self.processed_emails:
                    logger.debug(f"Skipping already processed email {email_id_str}")
                    continue
                    
                # Refresh cache for each email to get most recent events
                caldav_events = self.get_caldav_events(self.caldav_calendar) if self.caldav_calendar else []
                google_events = self.get_google_events(self.google_service) if self.google_service else []
                
                try:
                    # Fetch email
                    status, msg_data = mail.fetch(email_id, '(RFC822)')
                    if status != 'OK':
                        logger.error(f"Failed to fetch email {email_id_str}")
                        continue
                    # Parse email
                    msg = email.message_from_bytes(msg_data[0][1])
                    # Get message ID for tracking
                    message_id = msg.get('Message-ID', email_id_str)
                    # Get subject
                    subject_raw = msg.get('Subject', '')
                    subject = decode_header(subject_raw)[0][0]
                    if isinstance(subject, bytes):
                        subject = subject.decode('utf-8', errors='replace')
                    # Get sender
                    sender_raw = msg.get('From', '')
                    sender = decode_header(sender_raw)[0][0]
                    if isinstance(sender, bytes):
                        sender = sender.decode('utf-8', errors='replace')
                    # Get body
                    body = self.get_email_body(msg)
                    logger.info(f"Processing email from {sender}: {subject}")
                    # Parse with AI
                    event_data = self.parse_email_with_ai(subject, body, sender)
                    if event_data:
                        # Convert ISO strings to datetime objects for comparison
                        try:
                            start_dt = datetime.fromisoformat(event_data['start_date'].replace('Z', '+00:00'))
                            end_dt = datetime.fromisoformat(event_data['end_date'].replace('Z', '+00:00'))
                            event_data['start_date'] = start_dt
                            event_data['end_date'] = end_dt
                        except Exception as e:
                            logger.error(f"Error parsing event dates: {e}")
                            continue
                            
                        # Check for similar existing events
                        similar_events = self.find_similar_events(
                            event_data, 
                            caldav_events if CONFIG['ENABLE_CALDAV'] else None,
                            google_events if CONFIG['ENABLE_GOOGLE_CALENDAR'] else None
                        )
                        
                        if similar_events:
                            logger.info(f"Found {len(similar_events)} similar existing events")
                            # Update the most similar event
                            most_similar = similar_events[0]
                            updated = False
                            
                            if most_similar['type'] == 'caldav' and CONFIG['ENABLE_CALDAV'] and self.caldav_calendar:
                                updated = self.update_caldav_event(self.caldav_calendar, most_similar['event'], event_data)
                            elif most_similar['type'] == 'google' and CONFIG['ENABLE_GOOGLE_CALENDAR'] and self.google_service:
                                updated = self.update_google_event(self.google_service, most_similar['event'], event_data)
                                
                            if updated:
                                logger.info(f"Updated existing event instead of creating new one: {event_data['title']}")
                                self.processed_emails.add(email_id_str)
                                if CONFIG['MARK_AS_PROCESSED']:
                                    mail.store(email_id, '+FLAGS', '\\Seen')
                                continue  # Skip creating new event
                        
                        success_caldav = False
                        success_google = False
                        # Create calendar event in CalDAV (if enabled and available)
                        if CONFIG['ENABLE_CALDAV'] and self.caldav_calendar:
                            success_caldav = self.create_calendar_event(self.caldav_calendar, event_data)
                        elif CONFIG['ENABLE_CALDAV'] and not self.caldav_calendar:
                            logger.warning("CalDAV calendar not available, skipping CalDAV event creation")
                        elif not CONFIG['ENABLE_CALDAV']:
                            logger.info("CalDAV is disabled, skipping CalDAV event creation")
                        # Create calendar event in Google Calendar (if enabled and available)
                        if CONFIG['ENABLE_GOOGLE_CALENDAR'] and self.google_service:
                            try:
                                success_google = self.create_google_event(self.google_service, event_data)
                            except Exception as e:
                                logger.error(f"Failed to create event in Google Calendar: {e}")
                                success_google = False
                        elif CONFIG['ENABLE_GOOGLE_CALENDAR'] and not self.google_service:
                            logger.warning("Google Calendar not available, skipping Google event creation")
                        elif not CONFIG['ENABLE_GOOGLE_CALENDAR']:
                            logger.info("Google Calendar is disabled, skipping Google event creation")
                        if success_caldav or success_google:
                            logger.info(f"Successfully processed email and synced to available calendars: {subject}")
                            self.processed_emails.add(email_id_str)
                            if CONFIG['MARK_AS_PROCESSED']:
                                mail.store(email_id, '+FLAGS', '\\Seen')
                        else:
                            logger.warning(f"Failed to sync event to any calendar for: {subject}")
                            mail.store(email_id, '-FLAGS', '\\Seen')  # Keep unread
                    else:
                        logger.warning(f"Could not extract event from email: {subject}")
                        # Keep as unread if configured to do so
                        if not CONFIG['MARK_AS_PROCESSED']:
                            mail.store(email_id, '-FLAGS', '\\Seen')
                        else:
                            # Mark as read but log that no event was found
                            mail.store(email_id, '+FLAGS', '\\Seen')
                            logger.info(f"Marked email as read despite no event data: {subject}")
                            self.processed_emails.add(email_id_str)
                except Exception as e:
                    logger.error(f"Error processing email {email_id_str}: {e}")
                    # Keep email as unread
                    mail.store(email_id, '-FLAGS', '\\Seen')
        except Exception as e:
            logger.error(f"Error in process_emails: {e}")

    # Google Calendar Scopes
    GOOGLE_SCOPES = ['https://www.googleapis.com/auth/calendar']

    def authenticate_google(self):
        """Authenticate and return Google Calendar service object"""
        creds = None
        token_file = CONFIG['GOOGLE_TOKEN_FILE']
        creds_file = CONFIG['GOOGLE_CREDENTIALS_FILE']
        if os.path.exists(token_file):
            with open(token_file, 'rb') as token:
                creds = pickle.load(token)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(creds_file, self.GOOGLE_SCOPES)
                creds = flow.run_local_server(port=5353, open_browser=False)
            with open(token_file, 'wb') as token:
                pickle.dump(creds, token)
        service = build('calendar', 'v3', credentials=creds)
        logger.info("Authenticated with Google Calendar")
        return service

    def list_google_calendars(self, service):
        """List all Google Calendars to help with configuration"""
        try:
            calendar_list = service.calendarList().list().execute()
            for calendar_entry in calendar_list['items']:
                logger.debug(f"Calendar ID: {calendar_entry['id']}, Summary: {calendar_entry['summary']}")
        except Exception as e:
            logger.error(f"Failed to list Google calendars: {e}")

    def get_calendar_id_by_name(self, service, calendar_name):
        """Get Google Calendar ID by its display name"""
        try:
            calendar_list = service.calendarList().list().execute()
            for calendar_entry in calendar_list['items']:
                if calendar_entry['summary'] == calendar_name:
                    return calendar_entry['id']
            logger.warning(f"Calendar with name '{calendar_name}' not found. Using primary.")
            return 'primary'
        except Exception as e:
            logger.error(f"Error fetching calendar ID: {e}")
            return 'primary'

    def create_google_event(self, service, event_data):
        """Create an event in Google Calendar"""
        try:
            # First, check if similar event already exists
            time_min = event_data['start_date'] - timedelta(days=1)
            time_max = event_data['end_date'] + timedelta(days=1)
            google_events = self.get_google_events(service, time_min, time_max)
            
            if self.is_event_duplicate(event_data, google_events):
                logger.info(f"Skipping duplicate Google event creation: {event_data['title']}")
                return True

            calendar_id = self.get_calendar_id_by_name(service, CONFIG['GOOGLE_CALENDAR_NAME'])
            
            # Handle datetime objects
            start_dt = event_data['start_date']
            end_dt = event_data['end_date']
            
            if isinstance(start_dt, datetime):
                start_iso = start_dt.isoformat()
            else:
                start_iso = start_dt
            if isinstance(end_dt, datetime):
                end_iso = end_dt.isoformat()
            else:
                end_iso = end_dt
                
            event_body = {
                'summary': event_data['title'],
                'start': {
                    'dateTime': start_iso,
                    'timeZone': CONFIG['TIMEZONE'],
                },
                'end': {
                    'dateTime': end_iso,
                    'timeZone': CONFIG['TIMEZONE'],
                },
            }
            
            if event_data.get('location'):
                event_body['location'] = event_data['location']
            if event_data.get('description'):
                event_body['description'] = event_data['description']
                
            event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
            
            # Track the event ID
            if event.get('id'):
                self.created_event_uids.add(event.get('id'))
                
            logger.info(f"Google Calendar event created: {event.get('htmlLink')} in calendar '{CONFIG['GOOGLE_CALENDAR_NAME']}'")
            return True
        except Exception as e:
            logger.error(f"Failed to create Google Calendar event: {e}")
            return False

    def run_once(self, init_calendars=True):
        """Run the automation once"""
        mail = None
        try:
            # Initialize calendars at the start (only if requested)
            if init_calendars:
                self.initialize_calendars()
            mail = self.connect_gmail()
            self.process_emails(mail)
        except Exception as e:
            logger.error(f"Automation run failed: {e}")
        finally:
            if mail:
                try:
                    mail.logout()
                except Exception as e:
                    logger.warning(f"Error during mail logout: {e}")

    def run_continuous(self):
        """Run continuously checking for new emails"""
        logger.info("Starting continuous email-to-calendar automation...")
        # Initialize calendars once at the start with early exit on failure
        try:
            self.initialize_calendars()
        except Exception as e:
            logger.error(f"Failed to initialize calendars at startup: {e}")
            logger.critical("Cannot continue without calendar connections. Exiting.")
            sys.exit(1)
        consecutive_errors = 0
        max_consecutive_errors = 5
        while True:
            try:
                self.run_once(init_calendars=False)  # Don't re-initialize calendars
                consecutive_errors = 0
                logger.debug(f"Sleeping for {CONFIG['CHECK_INTERVAL']} seconds...")
                time.sleep(CONFIG['CHECK_INTERVAL'])
            except KeyboardInterrupt:
                logger.info("Stopping automation due to keyboard interrupt...")
                break
            except Exception as e:
                consecutive_errors += 1
                retry_interval = CONFIG['RETRY_INTERVAL'] * min(consecutive_errors, 5)
                logger.error(f"Continuous run error: {e}")
                logger.warning(f"Consecutive errors: {consecutive_errors}/{max_consecutive_errors}")
                if consecutive_errors >= max_consecutive_errors:
                    logger.critical(f"Too many consecutive errors ({consecutive_errors}). Stopping service.")
                    break
                logger.info(f"Retrying in {retry_interval} seconds...")
                time.sleep(retry_interval)

def main():
    """Main entry point - Check connections at startup based on configuration"""
    try:
        logger.info("Email-to-Calendar Automation starting up...")
        logger.info("Checking connections at startup based on configuration...")
        # Test Gmail connection
        try:
            logger.info("Testing Gmail connection...")
            automator = EmailCalendarAutomator()
            mail = automator.connect_gmail()
            mail.logout()
            logger.info("✓ Gmail connection successful")
        except Exception as e:
            logger.critical(f"✗ Failed to connect to Gmail: {e}")
            return 1
        # Test CalDAV connection if enabled
        if CONFIG['ENABLE_CALDAV']:
            try:
                logger.info("Testing CalDAV connection...")
                caldav_success = False
                for attempt in range(CONFIG['CALDAV_RETRY_ATTEMPTS']):
                    try:
                        client = DAVClient(
                            url=CONFIG['CALDAV_URL'],
                            username=CONFIG['CALDAV_USERNAME'],
                            password=CONFIG['CALDAV_PASSWORD']
                        )
                        principal = client.principal()
                        calendars = principal.calendars()
                        logger.info(f"✓ CalDAV connection successful (found {len(calendars)} calendars)")
                        caldav_success = True
                        break
                    except Exception as e:
                        logger.warning(f"CalDAV connection attempt {attempt + 1}/{CONFIG['CALDAV_RETRY_ATTEMPTS']} failed: {e}")
                        if attempt < CONFIG['CALDAV_RETRY_ATTEMPTS'] - 1:
                            logger.info(f"Retrying CalDAV connection in {CONFIG['CALDAV_RETRY_DELAY']} seconds...")
                            time.sleep(CONFIG['CALDAV_RETRY_DELAY'])
                        else:
                            logger.critical(f"✗ All CalDAV connection attempts failed")
                            return 1
            except Exception as e:
                logger.critical(f"✗ Failed to connect to CalDAV: {e}")
                return 1
        else:
            logger.info("CalDAV is disabled via ENABLE_CALDAV=false")
        # Test Google Calendar connection if enabled
        if CONFIG['ENABLE_GOOGLE_CALENDAR']:
            try:
                logger.info("Testing Google Calendar connection...")
                automator = EmailCalendarAutomator()
                service = automator.authenticate_google()
                calendar_list = service.calendarList().list().execute()
                logger.info(f"✓ Google Calendar connection successful (found {len(calendar_list['items'])} calendars)")
            except Exception as e:
                logger.critical(f"✗ Failed to connect to Google Calendar: {e}")
                return 1
        else:
            logger.info("Google Calendar is disabled via ENABLE_GOOGLE_CALENDAR=false")
        logger.info("All enabled connections successful! Starting automation...")
        # Start the automation
        automator = EmailCalendarAutomator()
        # Run once or continuously based on environment variable
        if os.getenv('RUN_ONCE', '').lower() == 'true':
            logger.info("Running in one-time mode")
            automator.run_once()
        else:
            logger.info("Running in continuous mode")
            automator.run_continuous()
    except Exception as e:
        logger.critical(f"Fatal error in main: {e}")
        return 1
    return 0

if __name__ == "__main__":
    exit_code = main()
    exit(exit_code)
