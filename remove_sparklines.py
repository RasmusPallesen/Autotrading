"""
Patch script — removes sparkline charts and restores simple positions table.
Run from project root: python remove_sparklines.py
"""

SIMPLE_POSITIONS = '''if positions:
    st.subheader("Open Positions")
    pos_df = pd.DataFrame([{
        "Symbol":       p.get("symbol"),
        "Qty":          float(p.get("qty", 0)),
        "Entry Price":  f"${float(p.get(\'avg_entry_price\', 0)):,.2f}",
        "Current":      f"${float(p.get(\'current_price\', 0)):,.2f}",
        "Market Value": f"${float(p.get(\'market_value\', 0)):,.2f}",
        "Unreal. P&L":  f"${float(p.get(\'unrealized_pl\', 0)):,.2f}",
        "P&L %":        f"{float(p.get(\'unrealized_plpc\', 0))*100:.2f}%",
    } for p in positions])
    st.dataframe(pos_df, use_container_width=True, hide_index=True)'''

with open("dashboard.py", "r", encoding="utf-8") as f:
    content = f.read()

# Find the positions block — from "if positions:" up to the next "st.divider()"
import re

# Replace the entire sparkline positions block with the simple table
pattern = r'if positions:\s*\n\s*st\.subheader\("Open Positions"\).*?(?=st\.divider\(\)|st\.subheader\("Agent Decisions)'
match = re.search(pattern, content, re.DOTALL)

if match:
    content = content[:match.start()] + SIMPLE_POSITIONS + "\n\n" + content[match.end():]
    with open("dashboard.py", "w", encoding="utf-8") as f:
        f.write(content)
    print("SUCCESS: Sparklines removed, simple positions table restored")
else:
    print("Could not auto-patch. Writing simple replacement to positions_simple.txt")
    with open("positions_simple.txt", "w") as f:
        f.write(SIMPLE_POSITIONS)
    print("Replace the 'if positions:' block in dashboard.py with positions_simple.txt contents")
