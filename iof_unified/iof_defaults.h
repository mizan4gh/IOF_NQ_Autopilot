// Single source for business defaults referenced by unified IOF ACSIL studies.
#pragma once

namespace iof_unified {

// Live / replay chart: **3000 (3k)** contract volume per bar unless you intentionally rescale all lookbacks.
constexpr int kTargetVolumeBars = 3000;

// Daily risk envelope (USD). Studies should use these for SetDefaults; operators override via inputs.
constexpr float kDefaultDailyLossUsd = 1000.f;
constexpr float kDefaultDailyProfitUsd = 1000.f;

// RTH for US index futures — bar filter uses chart clock; align chart TZ to exchange ET.
constexpr int kDefaultRthOpenHhmm = 935;   // 09:35 — matches IOF V18A RTH_OPEN
// Apex Trader Funding futures: flat before 4:59 PM ET; 1655 = 4:55 PM buffer.
constexpr int kDefaultFlattenHhmm = 1655;

} // namespace iof_unified
