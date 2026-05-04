@echo off
cd /d "C:\Users\Rasmus Pallesen\Documents\GitHub\Autotrading"
title Trading Agent - Dashboard


echo.
echo  ============================================
echo   Trading Agent - Dashboard
echo  ============================================
echo.

set ALPACA_API_KEY=AKTPVGOUVJSYTXAZ2YTNTNZ5Z2
set ALPACA_SECRET_KEY=EBpwMkixWzXTJVKetZGXwayPkjxAUtG9HqGgmZ9fS4VT
set ALPACA_PAPER=false
set ANTHROPIC_API_KEY=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
set DATABASE_URL=postgresql://trading_agent.vaxbdxaheqambvqbkklj:TradingAgent2026@aws-1-eu-central-1.pooler.supabase.com:5432/postgres

call .venv\Scripts\activate

echo  Starting dashboard at http://localhost:8501
echo  Press Ctrl+C to stop
echo.
streamlit run dashboard.py

pause
