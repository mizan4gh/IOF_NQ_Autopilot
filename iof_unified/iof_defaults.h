// Single source for business defaults referenced by unified IOF ACSIL studies.
#pragma once

namespace iof_unified {

// Live / replay chart: **3000 (3k)** contract volume per bar unless you intentionally rescale all lookbacks.
constexpr int kTargetVolumeBars = 3000;

// Daily risk envelope (USD). Studies should use these for SetDefaults; operators override via inputs.
constexpr float kDefaultDailyLossUsd = 1000.f;
constexpr float kDefaultDailyProfitUsd = 0.f;   // disabled — no profit cap by default

// RTH for US index futures — bar filter uses chart clock; align chart TZ to exchange ET.
constexpr int kDefaultRthOpenHhmm = 935;   // 09:35 — matches IOF V18A RTH_OPEN
// [09e31b2] Flatten lowered from 1655 → 1555 (3:55 PM ET). Provides buffer
// well ahead of the Apex Trader Funding 4:59 PM ET flat-by rule while
// avoiding the close-of-session volatility window.
constexpr int kDefaultFlattenHhmm = 1555;

} // namespace iof_unified
