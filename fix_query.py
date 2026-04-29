"""
Patch script — fixes tuple index out of range in dashboard_cloud.py
Run from project root: python fix_query.py
"""

with open("dashboard_cloud.py", "r", encoding="utf-8") as f:
    content = f.read()

old = '''def query(sql: str, params=()) -> pd.DataFrame:
    backend, conn = get_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        return pd.read_sql(sql.replace("?", "%s"), conn, params=params)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return pd.DataFrame()'''

new = '''def query(sql: str, params=()) -> pd.DataFrame:
    backend, conn = get_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params or None)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            return pd.DataFrame(rows, columns=cols)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        st.sidebar.warning(f"Query error: {e}")
        return pd.DataFrame()'''

if old in content:
    content = content.replace(old, new)
    print("SUCCESS: query() function fixed")
else:
    print("Could not find exact match -- trying to patch manually")
    # Find and replace just the pd.read_sql line
    content = content.replace(
        "return pd.read_sql(sql.replace(\"?\", \"%s\"), conn, params=params)",
        """with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params or None)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            return pd.DataFrame(rows, columns=cols)"""
    )
    print("Patched pd.read_sql replacement")

with open("dashboard_cloud.py", "w", encoding="utf-8") as f:
    f.write(content)
