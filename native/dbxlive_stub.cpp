// dbxlive_stub.cpp -- a probing stand-in for D-BOX's dbxLive64.dll.
//
// ============================================================================
//  DO NOT LOAD THIS INTO STAR CITIZEN. It was never loaded into the game, and
//  the tool that could have installed it there was deliberately deleted.
//
//  Star Citizen ships EasyAntiCheat, which is kernel-level and inspects the
//  modules in the protected process. This file builds an UNSIGNED DLL taking
//  the filename of a D-BOX-SIGNED one, which is the exact shape of DLL
//  hijacking no matter how harmless the code is -- and anti-cheat does not
//  evaluate intent. Self-signing does not help; a self-signed certificate is
//  not trusted. The downside is somebody's game account.
//
//  sc-api does not need any of this: it is the gRPC SERVER, and it uses
//  D-BOX's own signed DLL unmodified. This file is kept only because the
//  native ABI recorded below is the evidence for README section 3, and because
//  the reasoning for rejecting that approach is worth keeping legible.
// ============================================================================
//
// Purpose: discover the native LiveMotion ABI empirically instead of reverse
// engineering 14 vtable signatures by hand. See README section 3, Option B.
//
// What we already know from the shipped dbxLive64.dll (observed, not guessed):
//   * one export, GetEventHandler(), which returns a lazily-created SINGLETON
//     (MSVC magic static). The caller allocates nothing and frees nothing.
//   * the returned object is a dbox::LiveMotionConnectorManager whose vtable
//     has 14 slots.
//   * 13 method names exist in .rdata: Initialize, Terminate, Open, Close,
//     Start, Stop, ResetState, RegisterEvent, PostEvent, PostEventToSession,
//     SetOpenExtraSessions, PostEvents, PostEventsToSession. The 14th slot is
//     unidentified (a destructor, or a version query -- both plausible since
//     CompatibilityEventHandlerProxy overrides slots 0, 8, 9 and 13).
//
// What this stub does: exposes 14 slots in a known order and logs every call
// with its raw integer-register arguments, then returns 0 (which the gRPC
// capture already showed the client accepts as success). Correlating the
// observed call order with the known-good sequence in captures/sc-capture.jsonl
// (Initialize -> Open -> RegisterEvent x20 -> Start -> PostEvent...) names each
// slot from ground truth.
//
// IMPORTANT caveats, by design:
//   * Every slot is declared with four integer parameters. On x64 the first
//     four integer args arrive in RCX/RDX/R8/R9 with RCX = this, so a slot the
//     game actually calls with fewer arguments will log GARBAGE in the unused
//     registers. Do not read meaning into trailing values; cross-check against
//     how many arguments a slot plausibly takes.
//   * Floating-point arguments arrive in XMM0-3 and are NOT captured here.
//   * No virtual destructor is declared, so MSVC emits exactly 14 slots in
//     declaration order and nothing extra.

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <cstdint>
#include <cstdio>

namespace {

constexpr int kSlots = 14;
// Log the first N calls of each slot in full, then only count them. PostEvent
// runs at ~50 Hz per event type, so unbounded logging would both bloat the file
// and risk slowing the game's frame loop.
constexpr unsigned kVerboseCallsPerSlot = 24;

CRITICAL_SECTION g_lock;
FILE* g_log = nullptr;
bool g_ready = false;
unsigned g_calls[kSlots] = {};
unsigned long long g_seq = 0;

const char* kSlotHint[kSlots] = {
    "slot0",  "slot1",  "slot2",  "slot3",  "slot4",  "slot5",  "slot6",
    "slot7",  "slot8",  "slot9",  "slot10", "slot11", "slot12", "slot13",
};

void open_log() {
    char path[MAX_PATH] = {};
    DWORD n = GetEnvironmentVariableA("SC_API_STUB_LOG", path, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) {
        char base[MAX_PATH] = {};
        if (GetEnvironmentVariableA("ProgramData", base, MAX_PATH)) {
            lstrcpynA(path, base, MAX_PATH);
            lstrcatA(path, "\\sc-api");
            CreateDirectoryA(path, nullptr);
            lstrcatA(path, "\\dbxlive_stub.log");
        } else {
            lstrcpynA(path, "C:\\dbxlive_stub.log", MAX_PATH);
        }
    }
    g_log = fopen(path, "a");
    // NOTE: do not rely on _IOLBF here. The MSVC runtime treats _IOLBF as
    // _IOFBF (full buffering), unlike POSIX, so a line-buffered log would stay
    // in memory until fclose -- and we already know this game can crash, which
    // would lose the whole capture. logf() flushes explicitly instead.
}

void logf(const char* fmt, ...) {
    if (!g_ready) return;
    EnterCriticalSection(&g_lock);
    if (g_log) {
        va_list ap;
        va_start(ap, fmt);
        vfprintf(g_log, fmt, ap);
        va_end(ap);
        fputc('\n', g_log);
        // Explicit flush: survives a game crash, and call volume is already
        // rate-limited by kVerboseCallsPerSlot so this is not hot.
        fflush(g_log);
    }
    LeaveCriticalSection(&g_lock);
}

// Is [p, p+len) safe to read? The game passes pointers to structs we want to
// inspect, but a garbage register would otherwise fault inside the game.
bool readable(const void* p, size_t len) {
    if (!p) return false;
    MEMORY_BASIC_INFORMATION mbi{};
    if (VirtualQuery(p, &mbi, sizeof(mbi)) != sizeof(mbi)) return false;
    if (mbi.State != MEM_COMMIT) return false;
    const DWORD bad = PAGE_NOACCESS | PAGE_GUARD;
    if (mbi.Protect & bad) return false;
    const auto start = reinterpret_cast<uintptr_t>(mbi.BaseAddress);
    const auto end = start + mbi.RegionSize;
    const auto want = reinterpret_cast<uintptr_t>(p) + len;
    return want <= end;
}

void dump(const char* tag, uint64_t v, size_t len = 64) {
    const void* p = reinterpret_cast<const void*>(static_cast<uintptr_t>(v));
    if (!readable(p, len)) return;
    const auto* b = static_cast<const unsigned char*>(p);
    char line[16 + 64 * 3 + 1];
    int o = 0;
    o += wsprintfA(line + o, "%s", "");
    char hex[64 * 3 + 1];
    int h = 0;
    for (size_t i = 0; i < len; ++i) h += wsprintfA(hex + h, "%02x ", b[i]);
    logf("        %s -> %s", tag, hex);
}

uint64_t record(int slot, uint64_t a1, uint64_t a2, uint64_t a3) {
    if (!g_ready) return 0;

    EnterCriticalSection(&g_lock);
    const unsigned seen = ++g_calls[slot];
    const unsigned long long seq = ++g_seq;
    LeaveCriticalSection(&g_lock);

    if (seen <= kVerboseCallsPerSlot) {
        logf("[%llu] CALL slot=%d (%s) call#%u  a1=0x%llx a2=0x%llx a3=0x%llx",
             seq, slot, kSlotHint[slot], seen,
             (unsigned long long)a1, (unsigned long long)a2,
             (unsigned long long)a3);
        dump("a1", a1);
        dump("a2", a2);
    } else if (seen % 2000 == 0) {
        logf("[%llu] CALL slot=%d (%s) call#%u (summarising; detail suppressed)",
             seq, slot, kSlotHint[slot], seen);
    }
    return 0;
}

// 14 virtual slots, in declaration order == vtable order under MSVC.
// `this` arrives in RCX, so the declared parameters map to RDX/R8/R9.
struct Probe {
#define SLOT(n) \
    virtual uint64_t slot##n(uint64_t a1, uint64_t a2, uint64_t a3) { \
        return record(n, a1, a2, a3); \
    }
    SLOT(0) SLOT(1) SLOT(2) SLOT(3) SLOT(4) SLOT(5) SLOT(6)
    SLOT(7) SLOT(8) SLOT(9) SLOT(10) SLOT(11) SLOT(12) SLOT(13)
#undef SLOT
};

// A function-local static, mirroring the real DLL's singleton.
Probe g_probe;

}  // namespace

extern "C" __declspec(dllexport) void* GetEventHandler() {
    logf("GetEventHandler() -> %p", (void*)&g_probe);
    return &g_probe;
}

BOOL APIENTRY DllMain(HMODULE mod, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        InitializeCriticalSection(&g_lock);
        open_log();
        g_ready = true;

        char self[MAX_PATH] = {};
        GetModuleFileNameA(mod, self, MAX_PATH);
        char host[MAX_PATH] = {};
        GetModuleFileNameA(nullptr, host, MAX_PATH);
        logf("=== sc-api dbxLive stub loaded ===");
        logf("    module : %s", self);
        logf("    host   : %s  pid=%lu", host, GetCurrentProcessId());
        DisableThreadLibraryCalls(mod);
    } else if (reason == DLL_PROCESS_DETACH) {
        if (g_ready) {
            logf("=== detach; call totals ===");
            for (int i = 0; i < kSlots; ++i) {
                if (g_calls[i]) logf("    slot%-2d calls=%u", i, g_calls[i]);
            }
            EnterCriticalSection(&g_lock);
            if (g_log) { fflush(g_log); fclose(g_log); g_log = nullptr; }
            g_ready = false;
            LeaveCriticalSection(&g_lock);
        }
    }
    return TRUE;
}
