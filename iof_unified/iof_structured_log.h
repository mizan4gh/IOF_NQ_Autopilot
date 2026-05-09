// Structured prefixes for Message Log triage (grep-friendly). Keep messages one line when possible.
#pragma once

#include "sierrachart.h"

namespace iof_unified {

inline void LogLine(SCStudyInterfaceRef& sc, const char* category, const SCString& body, int isError = 0)
{
    SCString m;
    m.Format("[IOF:%s] %s", category, body.GetChars());
    sc.AddMessageToLog(m, isError);
}

inline void LogSession(SCStudyInterfaceRef& sc, const SCString& body, int isError = 0) { LogLine(sc, "SESSION", body, isError); }
inline void LogRisk(SCStudyInterfaceRef& sc, const SCString& body, int isError = 0) { LogLine(sc, "RISK", body, isError); }
inline void LogEntry(SCStudyInterfaceRef& sc, const SCString& body, int isError = 0) { LogLine(sc, "ENTRY", body, isError); }
inline void LogExit(SCStudyInterfaceRef& sc, const SCString& body, int isError = 0) { LogLine(sc, "EXIT", body, isError); }
inline void LogOrder(SCStudyInterfaceRef& sc, const SCString& body, int isError = 0) { LogLine(sc, "ORDER", body, isError); }
inline void LogDaily(SCStudyInterfaceRef& sc, const SCString& body, int isError = 0) { LogLine(sc, "DAILY", body, isError); }
inline void LogState(SCStudyInterfaceRef& sc, const SCString& body, int isError = 1) { LogLine(sc, "STATE", body, isError); }

} // namespace iof_unified
