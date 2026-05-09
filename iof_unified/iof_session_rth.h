// RTH bar-clock helpers (ET). Bar HHMM from Sierra SCDateTime::GetTime() seconds-since-midnight.
#pragma once

namespace iof_session {

inline int BarHhmmFromSecondsSinceMidnight(int secondsSinceMidnight)
{
    constexpr int kSecPerHour = 3600;
    return (secondsSinceMidnight / kSecPerHour) * 100
         + ((secondsSinceMidnight % kSecPerHour) / 60);
}

inline bool BeforeRthOpen(int barHhmm, int rthOpenHhmm) { return barHhmm < rthOpenHhmm; }

inline bool AtOrAfterRthOpen(int barHhmm, int rthOpenHhmm) { return barHhmm >= rthOpenHhmm; }

// No new signals at or after flatten HHMM (inclusive), per V18A trade-mgmt gate.
inline bool AtOrAfterFlatten(int barHhmm, int flattenHhmmInclusive) { return barHhmm >= flattenHhmmInclusive; }

// New entries allowed in [rthOpen, flatten) — half-open on flatten.
inline bool InRthEntryWindow(int barHhmm, int rthOpenHhmm, int flattenHhmmInclusive)
{
    return barHhmm >= rthOpenHhmm && barHhmm < flattenHhmmInclusive;
}

// VWAP / settle helpers: interval through flatten end inclusive.
inline bool InRthThroughFlattenInclusive(int barHhmm, int rthOpenHhmm, int flattenHhmmInclusive)
{
    return barHhmm >= rthOpenHhmm && barHhmm <= flattenHhmmInclusive;
}

} // namespace iof_session
