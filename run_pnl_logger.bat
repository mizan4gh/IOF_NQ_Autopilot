@echo off
REM Daily realized-P&L logger for the CausalImpact response series.
REM Scheduled via Windows Task Scheduler (task: IOF_NQ_PnL_Logger).
REM Self-healing: rebuilds live_pnl.csv from the full strategy CSV each run,
REM so a missed day loses no data as long as the source CSV retains history.
cd /d "C:\Users\17034\MyFolder\IOF_NQ_Production_Final"
python pnl_logger.py >> pnl_logger.log 2>&1
