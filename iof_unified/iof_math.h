// Shared float math for IOF ACSIL (no Sierra types). Include from studies and hook headers.
#pragma once

#include <algorithm>
#include <cmath>

namespace iof_unified {

inline float FAbs(float x) { return x < 0.f ? -x : x; }
inline float FMax(float a, float b) { return a > b ? a : b; }
inline float FMin(float a, float b) { return a < b ? a : b; }

} // namespace iof_unified
