================================================================================
IOF NQ Autopilot — PRODUCTION / FINAL bundle for Sierra Chart ACSIL
================================================================================

Study name:  IOF NQ — Pure Orderflow Autopilot
DLL name:    IOF_NQ_Autopilot  (see SCDLLName in the .cpp)
Build line:  v12.19 (see header comment in IOF_NQ_Autopilot.cpp)

This folder mirrors IOFv02/ production sources. The study itself is a **single
.cpp**: shared logic from iof_unified/*.h and iof_v1_hooks.h is **inlined** in
IOF_NQ_Autopilot.cpp so Sierra ACS_Source / remote build work when you
may only place **one** source file (no subfolder required).

--------------------------------------------------------------------------------
WHAT TO COMPILE
--------------------------------------------------------------------------------
  Required file only:
    IOF_NQ_Autopilot.cpp

  Sierra supplies: sierrachart.h (do not copy from this repo).

  Optional in this folder: iof_unified\*.h and iof_v1_hooks.h — copies for
  development sync from IOFv02; the running study code lives inside the .cpp.

  Do NOT add a second autopilot .cpp in the same ACS_Source build unless you
  intend two DLLs.

  **Optional indicator (separate DLL):** `IOF_NQ_EdgeDiscovery.cpp` →
  `SCDLLName("IOF_NQ_EdgeDiscovery")`. Plots dist_vwap_atr, cum_delta_z, and
  related features from the Python `edge_discovery.py` pool study. Build it
  as its own custom study — do not merge into Autopilot .cpp unless you rename
  SCDLLName / export.

--------------------------------------------------------------------------------
SIERRA CHART — INSTALL STEPS (typical)
--------------------------------------------------------------------------------
  1. Copy **only** IOF_NQ_Autopilot.cpp into your ACS_Source studies folder
     (same place Sierra expects custom study sources — often next to other .cpp).

  2. Analysis >> Build Custom Studies DLL (local) or remote build: select this
     .cpp. No iof_unified folder is required for compile.

  3. Restart Sierra if it cached an old DLL. Add the study to a 3000-volume
     NQ (or MNQ per inputs) chart with Bid/Ask volume; see IOFv02 checklist in
     the main repo: IOFv02\SIERRA_REPLAY_CHECKLIST.txt

--------------------------------------------------------------------------------
DLL / SOURCE NAME CONSISTENCY CHECK
--------------------------------------------------------------------------------
  To ensure the produced DLL name matches the source and bundle naming:

    PowerShell:
      .\IOF_NQ_Production_Final\VERIFY_PRODUCTION_BUNDLE.ps1

  Expected canonical mapping:
    Source:   IOF_NQ_Autopilot.cpp
    SCDLLName("IOF_NQ_Autopilot")
    DLL (64): IOF_NQ_Autopilot_64.dll

  Keep this folder's build target to the canonical source above.
  Version-stamped files (v12.xx) are archive snapshots, not build targets.

--------------------------------------------------------------------------------
SYNC FROM DEVELOPMENT TREE
--------------------------------------------------------------------------------
  Canonical edits live under MyBabyBot\IOFv02\ in the main repo. After you
  change code there, refresh this bundle before shipping a build:

    PowerShell (from repo root):
      .\IOF_NQ_Production_Final\SYNC_FROM_IOFv02.ps1

  Script options: -CppOnly (main .cpp only), -CopySierraDocs (checklist + BUILD),
  -WhatIf (dry run), -NoVersionStamp. See Get-Help .\SYNC_FROM_IOFv02.ps1 -Full.

  That overwrites the copies here from IOFv02 so this folder stays “final”
  without maintaining two divergent codebases by hand.

--------------------------------------------------------------------------------
RISK / SESSION DEFAULTS (iof_unified/iof_defaults.h)
--------------------------------------------------------------------------------
  Daily max loss / profit target defaults: $1000 / $1000 (override in study inputs).
  Position size: 1 contract only (study max + input clamp); CSV DAILY_PROFIT / DAILY_LOSS flatten.
  RTH bar clock helpers: iof_session_rth.h
  Chart assumption: 3000 contract volume bars unless you rescale lookbacks.
  ATR comes from Sierra Wilder(14) on chart OHLC (price space); it does not use bar volume size.

================================================================================
Last bundle refresh: see VERSION.txt
================================================================================
