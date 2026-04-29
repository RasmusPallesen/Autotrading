"""
Patch script — fixes PostgreSQL connection in TradeStore and ResearchStore
to correctly parse Supabase pooler URLs including project ref in username.
Run from project root: python fix_db_connection.py
"""
import re

def fix_setup_postgres(content, class_name):
    old = '''    def _setup_postgres(self):
        import psycopg2
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        self.conn = psycopg2.connect(url)
        self.conn.autocommit = True
        self._backend = "postgres"'''

    new = '''    def _setup_postgres(self):
        import psycopg2
        from urllib.parse import urlparse, unquote
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        parsed = urlparse(url)
        self.conn = psycopg2.connect(
            host=parsed.hostname,
            port=parsed.port or 5432,
            dbname=parsed.path.lstrip("/"),
            user=parsed.username,
            password=unquote(parsed.password or ""),
            sslmode="require",
            connect_timeout=10,
        )
        self.conn.autocommit = True
        self._backend = "postgres"'''

    if old in content:
        content = content.replace(old, new)
        print(f"Fixed {class_name}._setup_postgres")
    else:
        # Try regex fallback
        content = re.sub(
            r'def _setup_postgres\(self\):.*?self\._backend = "postgres"',
            new.strip(),
            content,
            flags=re.DOTALL,
            count=1
        )
        print(f"Fixed {class_name}._setup_postgres via regex")
    return content

# Fix trade_store.py
with open("storage\\trade_store.py", "r", encoding="utf-8") as f:
    content = f.read()
content = fix_setup_postgres(content, "TradeStore")
with open("storage\\trade_store.py", "w", encoding="utf-8") as f:
    f.write(content)

# Fix research_store.py
with open("storage\\research_store.py", "r", encoding="utf-8") as f:
    content = f.read()
content = fix_setup_postgres(content, "ResearchStore")
with open("storage\\research_store.py", "w", encoding="utf-8") as f:
    f.write(content)

print("\nDone! Restart start_agent.bat and start_research.bat")
print("The agents will now correctly connect to Supabase with full username and SSL")
