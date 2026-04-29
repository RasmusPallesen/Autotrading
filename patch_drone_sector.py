"""
Patch script — adds drone/defence sector to config.py and decision_engine.py
Run from project root: python patch_drone_sector.py
"""

# ── Patch config.py ────────────────────────────────────────────────────────────
with open("config.py", "r", encoding="utf-8") as f:
    content = f.read()

# Add drone watchlist after biotech
old = '''    # ── General (broad market / crypto proxy) ─────────────────────────────────
    general: List[str] = field(default_factory=lambda: ['''

new = '''    # ── Drone & Defence Technology ────────────────────────────────────────────
    drone_defence: List[str] = field(default_factory=lambda: [
        "KTOS",   # Kratos Defence — autonomous combat drones, AI targeting
        "AVAV",   # AeroVironment — Switchblade loitering munition, NATO deployed
        "RCAT",   # Red Cat Holdings — Teal drones, US Army standard
        "NOC",    # Northrop Grumman — Global Hawk, Triton surveillance drones
        "LMT",    # Lockheed Martin — F-35, missile defence, drone systems
        "RTX",    # RTX/Raytheon — Coyote counter-drone, loitering munitions
        "AXON",   # Axon Enterprise — drone-mounted systems, law enforcement
        "UMAC",   # Unusual Machines — US-made drones + counter-drone detection
    ])

    # ── General (broad market / crypto proxy) ─────────────────────────────────
    general: List[str] = field(default_factory=lambda: ['''

content = content.replace(old, new)

# Add drone to all_symbols
old = '''        return list(dict.fromkeys(
            self.ai_chips + self.ai_software + self.green_energy +
            self.medtech_diabetes + self.biotech + self.general
        ))'''
new = '''        return list(dict.fromkeys(
            self.ai_chips + self.ai_software + self.green_energy +
            self.medtech_diabetes + self.biotech + self.drone_defence + self.general
        ))'''
content = content.replace(old, new)

# Add top drone stocks to active trading list
old = '''            # Biotech
            "MANE", "RXRX",'''
new = '''            # Biotech
            "MANE", "RXRX",
            # Drone & Defence
            "KTOS", "AVAV",'''
content = content.replace(old, new)

# Add drone to preferred sectors list
old = '''        "biotech", "biopharmaceutical", "clinical stage", "gene therapy",
        "dermatology", "hair loss", "oncology"'''
new = '''        "biotech", "biopharmaceutical", "clinical stage", "gene therapy",
        "dermatology", "hair loss", "oncology",
        "drone", "autonomous", "defence", "defense", "military", "loitering munition",
        "counter-drone", "unmanned", "UAV", "UAS", "aerospace"'''
content = content.replace(old, new)

with open("config.py", "w", encoding="utf-8") as f:
    f.write(content)
print("config.py patched with drone/defence sector")

# ── Patch decision_engine.py ───────────────────────────────────────────────────
with open("agent\\decision_engine.py", "r", encoding="utf-8") as f:
    content = f.read()

# Add DRONE to SECTOR_MAP
old = '''    # General
    "AAPL": "GENERAL", "TSLA": "GENERAL", "COIN": "GENERAL", "MSTR": "GENERAL",
}'''
new = '''    # Drone & Defence
    "KTOS": "DRONE", "AVAV": "DRONE", "RCAT": "DRONE",
    "NOC": "DRONE", "LMT": "DRONE", "RTX": "DRONE",
    "AXON": "DRONE", "UMAC": "DRONE",
    # General
    "AAPL": "GENERAL", "TSLA": "GENERAL", "COIN": "GENERAL", "MSTR": "GENERAL",
}'''
content = content.replace(old, new)

# Add DRONE to PREFERRED_SECTORS
old = 'PREFERRED_SECTORS = {"AI_CHIPS", "AI_SOFTWARE", "GREEN_ENERGY", "MEDTECH", "BIOTECH"}'
new = 'PREFERRED_SECTORS = {"AI_CHIPS", "AI_SOFTWARE", "GREEN_ENERGY", "MEDTECH", "BIOTECH", "DRONE"}'
content = content.replace(old, new)

# Add DRONE to sector labels
old = '''            "BIOTECH": "Biotech & Clinical Stage — binary catalyst events possible",
            "GENERAL": "General Market",'''
new = '''            "BIOTECH": "Biotech & Clinical Stage — binary catalyst events possible",
            "DRONE": "Drone & Defence Technology — autonomous systems, counter-drone, NATO spending",
            "GENERAL": "General Market",'''
content = content.replace(old, new)

# Add DRONE to system prompt sectors
old = '''5. Biotech & Clinical Stage — companies with binary catalyst events (FDA decisions,
   clinical trial readouts). On days with >15% moves, IGNORE lagging technical
   indicators and focus on fundamental catalyst. Standard mean-reversion logic
   does NOT apply to binary events.
6. Market Scanner Discoveries'''
new = '''5. Biotech & Clinical Stage — companies with binary catalyst events (FDA decisions,
   clinical trial readouts). On days with >15% moves, IGNORE lagging technical
   indicators and focus on fundamental catalyst. Standard mean-reversion logic
   does NOT apply to binary events.
6. Drone & Defence Technology — autonomous combat drones, counter-drone systems,
   NATO modernisation spending. Structural tailwind from Ukraine war lessons and
   $38B US drone procurement through 2030. Treat contract wins as binary catalysts.
7. Market Scanner Discoveries'''
content = content.replace(old, new)

with open("agent\\decision_engine.py", "w", encoding="utf-8") as f:
    f.write(content)
print("decision_engine.py patched with DRONE sector")

# ── Update watchlist CSV ───────────────────────────────────────────────────────
drone_symbols = ["KTOS", "AVAV", "RCAT", "NOC", "LMT", "RTX", "AXON", "UMAC"]

with open("watchlist.csv", "r", encoding="utf-8") as f:
    existing = f.read().strip().splitlines()

for sym in drone_symbols:
    if sym not in existing:
        existing.append(sym)

with open("watchlist.csv", "w", encoding="utf-8") as f:
    f.write("\n".join(existing) + "\n")
print("watchlist.csv updated with drone symbols")

print("\nDone! Summary:")
print("  New watchlist size: ~55 symbols")
print("  Active trading: KTOS, AVAV added")
print("  Preferred sector: DRONE configured with +0.05 confidence boost")
print("  Restart start_agent.bat and start_research.bat")
