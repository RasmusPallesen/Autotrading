"""
Gmail API monitor for ingesting email newsletters into research pipeline.
Handles OAuth2 authentication, fetching unread emails from whitelisted senders,
and extracting stock ticker mentions.
"""

import base64
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# Gmail API scopes - readonly for fetching, modify for marking as read
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly',
          'https://www.googleapis.com/auth/gmail.modify']


@dataclass
class EmailMessage:
    """Represents a parsed email message."""
    message_id: str
    sender: str
    subject: str
    body: str
    date: datetime
    snippet: str = ""


class EmailMonitor:
    """
    Gmail API client for fetching newsletter emails from whitelisted senders.
    """

    def __init__(self, credentials_path: str, token_path: str):
        """
        Initialize Gmail API client with OAuth2 credentials.

        Args:
            credentials_path: Path to credentials.json from Google Cloud Console
            token_path: Path to store/load token.json (auto-generated)
        """
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.service = None
        self._authenticate()

    def _authenticate(self):
        """
        Authenticate with Gmail API using OAuth2.
        First run opens browser for authorization, subsequent runs use saved token.
        """
        creds = None

        # Load existing token if available
        if os.path.exists(self.token_path):
            try:
                creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
                logger.info("Loaded existing Gmail API token from %s", self.token_path)
            except Exception as e:
                logger.warning("Could not load token from %s: %s", self.token_path, e)

        # If no valid credentials, authenticate
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    logger.info("Refreshed expired Gmail API token")
                except Exception as e:
                    logger.error("Token refresh failed: %s", e)
                    creds = None

            if not creds:
                if not os.path.exists(self.credentials_path):
                    raise FileNotFoundError(
                        f"Gmail credentials not found at {self.credentials_path}. "
                        "Please download credentials.json from Google Cloud Console."
                    )

                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)
                logger.info("Completed OAuth2 flow, obtained new credentials")

            # Save credentials for future runs
            try:
                with open(self.token_path, 'w') as token:
                    token.write(creds.to_json())
                logger.info("Saved Gmail API token to %s", self.token_path)
            except Exception as e:
                logger.warning("Could not save token to %s: %s", self.token_path, e)

        self.service = build('gmail', 'v1', credentials=creds)
        logger.info("Gmail API client initialized successfully")

    def fetch_unread_from_senders(self, sender_whitelist: List[str]) -> List[EmailMessage]:
        """
        Fetch unread emails from whitelisted senders.

        Args:
            sender_whitelist: List of email addresses to fetch from
                              e.g., ["noreply@benzinga.com", "notifications@simplywallst.com"]

        Returns:
            List of EmailMessage objects
        """
        if not self.service:
            logger.error("Gmail API service not initialized")
            return []

        if not sender_whitelist:
            logger.warning("Empty sender whitelist, skipping email fetch")
            return []

        # Build query: from:sender1 OR from:sender2 AND is:unread
        sender_query = " OR ".join(f"from:{sender}" for sender in sender_whitelist)
        query = f"({sender_query}) is:unread"

        try:
            results = self.service.users().messages().list(
                userId='me',
                q=query,
                maxResults=50  # Fetch up to 50 unread emails
            ).execute()

            messages = results.get('messages', [])

            if not messages:
                logger.debug("No unread emails from whitelisted senders")
                return []

            logger.info("Found %d unread emails from whitelisted senders", len(messages))

            parsed_messages = []
            for msg in messages:
                try:
                    email_msg = self._parse_message(msg['id'])
                    if email_msg:
                        parsed_messages.append(email_msg)
                except Exception as e:
                    logger.warning("Failed to parse message %s: %s", msg['id'], e)

            return parsed_messages

        except Exception as e:
            logger.error("Gmail API fetch error: %s", e)
            return []

    def _parse_message(self, message_id: str) -> Optional[EmailMessage]:
        """
        Fetch and parse a single email message.

        Args:
            message_id: Gmail message ID

        Returns:
            EmailMessage object or None if parsing fails
        """
        try:
            message = self.service.users().messages().get(
                userId='me',
                id=message_id,
                format='full'
            ).execute()

            headers = message['payload']['headers']
            subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), '')
            sender = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
            date_str = next((h['value'] for h in headers if h['name'].lower() == 'date'), '')
            snippet = message.get('snippet', '')

            # Parse date
            try:
                # Gmail date format: "Mon, 16 May 2026 12:34:56 +0000"
                from email.utils import parsedate_to_datetime
                date = parsedate_to_datetime(date_str)
            except Exception:
                date = datetime.now(timezone.utc)

            # Extract body
            body = self._extract_body(message['payload'])

            return EmailMessage(
                message_id=message_id,
                sender=sender,
                subject=subject,
                body=body,
                date=date,
                snippet=snippet
            )

        except Exception as e:
            logger.error("Failed to parse message %s: %s", message_id, e)
            return None

    def _extract_body(self, payload: dict) -> str:
        """
        Extract email body from Gmail API payload.
        Handles both plain text and HTML emails.

        Args:
            payload: Gmail message payload

        Returns:
            Email body as text
        """
        body = ""

        # Check for direct body in payload
        if 'body' in payload and 'data' in payload['body']:
            body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')

        # Check for multipart message
        elif 'parts' in payload:
            for part in payload['parts']:
                mime_type = part.get('mimeType', '')

                if mime_type == 'text/plain':
                    if 'data' in part['body']:
                        body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
                        break

                elif mime_type == 'text/html':
                    if 'data' in part['body']:
                        html = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
                        body = self._html_to_text(html)

                # Recursively check nested parts
                elif 'parts' in part:
                    nested_body = self._extract_body(part)
                    if nested_body:
                        body = nested_body

        return body

    def _html_to_text(self, html: str) -> str:
        """
        Convert HTML email body to plain text.
        Strips tags, removes tracking pixels, cleans up whitespace.

        Args:
            html: HTML content

        Returns:
            Plain text content
        """
        try:
            soup = BeautifulSoup(html, 'html.parser')

            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()

            # Remove tracking pixels (1x1 images)
            for img in soup.find_all('img'):
                if img.get('width') == '1' or img.get('height') == '1':
                    img.decompose()

            # Get text
            text = soup.get_text()

            # Clean up whitespace
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = '\n'.join(chunk for chunk in chunks if chunk)

            return text
        except Exception as e:
            logger.warning("HTML parsing error: %s", e)
            return html

    def mark_as_read(self, message_id: str):
        """
        Mark an email as read.

        Args:
            message_id: Gmail message ID
        """
        if not self.service:
            logger.error("Gmail API service not initialized")
            return

        try:
            self.service.users().messages().modify(
                userId='me',
                id=message_id,
                body={'removeLabelIds': ['UNREAD']}
            ).execute()
            logger.debug("Marked message %s as read", message_id)
        except Exception as e:
            logger.warning("Failed to mark message %s as read: %s", message_id, e)

    def extract_stock_mentions(self, text: str) -> List[str]:
        """
        Extract stock ticker symbols from email text.
        Looks for patterns like: $NVDA, AAPL, (MSFT), etc.

        Args:
            text: Email body text

        Returns:
            List of ticker symbols (uppercase, no duplicates)
        """
        if not text:
            return []

        symbols = set()

        # Pattern 1: $TICKER format (common in newsletters)
        dollar_tickers = re.findall(r'\$([A-Z]{1,5})(?:\b|[^A-Z])', text)
        symbols.update(dollar_tickers)

        # Pattern 2: (TICKER) format
        paren_tickers = re.findall(r'\(([A-Z]{1,5})\)', text)
        symbols.update(paren_tickers)

        # Pattern 3: Standalone uppercase words 2-5 chars (more conservative)
        # Only match if preceded by common stock mention keywords
        keyword_pattern = r'(?:stock|ticker|symbol|company|shares?|buy|sell|trade|trading)\s+([A-Z]{2,5})\b'
        keyword_tickers = re.findall(keyword_pattern, text, re.IGNORECASE)
        symbols.update(t.upper() for t in keyword_tickers)

        # Filter out common false positives
        excluded = {'USA', 'US', 'CEO', 'CFO', 'IPO', 'SEC', 'ETF', 'USD', 'AM', 'PM', 'EST', 'PST'}
        symbols = symbols - excluded

        return sorted(list(symbols))


def clean_html(html: str) -> str:
    """
    Utility function to clean HTML content for use in ResearchItem summaries.

    Args:
        html: HTML content

    Returns:
        Plain text content
    """
    try:
        soup = BeautifulSoup(html, 'html.parser')

        # Remove script, style, and footer elements
        for tag in soup(["script", "style", "footer"]):
            tag.decompose()

        # Remove unsubscribe links and similar
        for link in soup.find_all('a'):
            href = link.get('href', '')
            if any(kw in href.lower() for kw in ['unsubscribe', 'optout', 'preferences']):
                link.decompose()

        text = soup.get_text()

        # Clean whitespace
        lines = (line.strip() for line in text.splitlines())
        text = '\n'.join(line for line in lines if line)

        return text
    except Exception:
        return html
