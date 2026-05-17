# Gmail API Setup for Email Newsletter Ingestion

The research agent can now ingest email newsletters from Benzinga, Simply Wall St, and The Motley Fool automatically. This requires a one-time Gmail API setup.

## Prerequisites

- Gmail account where you receive the newsletters
- Google Cloud account (free tier is fine)

## Setup Steps

### 1. Enable Gmail API in Google Cloud Console

1. Go to https://console.cloud.google.com/
2. Create a new project (or use an existing one)
   - Click "Select a project" → "NEW PROJECT"
   - Name it "Autotrading Research Agent"
   - Click "CREATE"

3. Enable the Gmail API:
   - In the left sidebar, go to **APIs & Services** → **Library**
   - Search for "Gmail API"
   - Click on it and click **ENABLE**

### 2. Create OAuth2 Credentials

1. Go to **APIs & Services** → **Credentials**
2. Click **+ CREATE CREDENTIALS** → **OAuth client ID**
3. If prompted to configure consent screen:
   - Choose **External** (unless you have a Google Workspace)
   - Fill in app name: "Autotrading Research Agent"
   - User support email: your email
   - Developer contact: your email
   - Click **SAVE AND CONTINUE**
   - Skip scopes (click **SAVE AND CONTINUE**)
   - Add your email as a test user
   - Click **SAVE AND CONTINUE**

4. Create OAuth client ID:
   - Application type: **Desktop app**
   - Name: "Autotrading Desktop Client"
   - Click **CREATE**

5. Download the credentials:
   - Click the **Download** icon (⬇) next to your newly created OAuth client
   - This downloads a file named `client_secret_XXXXX.json`

### 3. Place Credentials File

1. Rename the downloaded file to `gmail_credentials.json`
2. Move it to this directory: `/home/user/Autotrading/secrets/gmail_credentials.json`

**Example:**
```bash
mv ~/Downloads/client_secret_*.json /home/user/Autotrading/secrets/gmail_credentials.json
```

### 4. First Authentication

1. Start the research agent:
   ```bash
   cd /home/user/Autotrading
   python research/research_agent.py
   ```

2. On first run, the OAuth flow will:
   - Print a URL to the console
   - Open your default browser automatically
   - Ask you to authorize the app to access your Gmail

3. Complete the authorization:
   - Sign in with your Gmail account
   - Click **Allow** when prompted for Gmail permissions
   - You'll see "The authentication flow has completed"
   - Close the browser tab

4. The token is saved to `/home/user/Autotrading/secrets/gmail_token.json`
   - Future runs will use this token automatically
   - It refreshes automatically when it expires

## Verification

After setup, check the research agent logs:

```bash
tail -f logs/research.log | grep -i email
```

You should see:
- `Email monitor initialized (Gmail API)`
- `Email monitor: X items from Y newsletters` (once per hour)

## Whitelisted Senders

By default, emails are fetched from:
- `noreply@benzinga.com`
- `notifications@simplywallst.com`
- `noreply@fool.com`
- `alerts@fool.com`
- `stockadvisor@fool.com`

To add more senders, edit `research/collector.py` → `fetch_email_items()` → `sender_whitelist`

## Troubleshooting

### "Gmail credentials not found"
- Ensure `gmail_credentials.json` exists in `/home/user/Autotrading/secrets/`
- Check the filename is exactly `gmail_credentials.json` (not `.json.json`)

### "Email monitor init failed"
- Run: `pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client`
- Check credentials file is valid JSON (no corruption)

### "No unread emails from whitelisted senders"
- Verify you have unread emails from Benzinga/Motley Fool in your inbox
- Check sender addresses match the whitelist exactly
- Gmail search query used: `(from:noreply@benzinga.com OR ...) is:unread`

### OAuth consent screen shows "Unverified app" warning
- This is normal for personal OAuth apps
- Click "Advanced" → "Go to Autotrading Research Agent (unsafe)"
- This warning only appears once during initial setup

### Token expired / authentication errors
- Delete `/home/user/Autotrading/secrets/gmail_token.json`
- Restart the research agent to re-authenticate

## Security

- `gmail_credentials.json` and `gmail_token.json` are automatically added to `.gitignore`
- Never commit these files to version control
- To revoke access: https://myaccount.google.com/permissions → Remove "Autotrading Research Agent"

## Rate Limits

- Gmail API free tier: 1 billion requests/day, 250 requests/user/second
- Our usage: ~24 requests/day (1 per hour)
- No cost or quota concerns

## Next Steps

Once email ingestion is working:
1. Monitor conviction scores — do email signals improve analysis quality?
2. Check database: `SELECT * FROM research_signals WHERE key_points LIKE '%email%'`
3. Fine-tune sender whitelist based on which newsletters provide best signals
4. Optional: Implement structured parsing for specific newsletter formats (see plan Phase 4)
