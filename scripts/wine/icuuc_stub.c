/* Stand-in for Windows' system icuuc.dll, for running PySide6 under Wine.
 *
 * Qt 6's Windows builds link Qt6Core.dll against the ICU that Windows 10+
 * ships in System32 (unversioned symbols), and Wine has no ICU — so
 * `from PySide6 import QtCore` fails with "DLL load failed". Qt6Core only
 * imports the ucnv_* converter API from it, and only reaches for ICU for
 * encodings beyond its built-in UTF-8/16/32 + Latin-1. This stub exports
 * those names and reports every converter as unavailable, so Qt falls back
 * to the built-ins; qeth uses nothing else. The full import list comes from
 *   x86_64-w64-mingw32-objdump -p PySide6/Qt6Core.dll
 * — re-check it when PySide6 is bumped (a new import fails the load again).
 *
 * Dev tooling only: never shipped. Real Windows has the real icuuc.dll. */

#include <stddef.h>
#include <stdint.h>

#define EXPORT __declspec(dllexport)

enum { U_FILE_ACCESS_ERROR = 4, U_UNSUPPORTED_ERROR = 16 };

static void fail(int *err, int code)
{
    if (err && *err <= 0)
        *err = code;
}

EXPORT void *ucnv_open(const char *name, int *err)
{
    (void)name;
    fail(err, U_FILE_ACCESS_ERROR); /* what ICU reports for an unknown name */
    return NULL;
}

EXPORT int32_t ucnv_countAvailable(void) { return 0; }
EXPORT const char *ucnv_getAvailableName(int32_t n) { (void)n; return NULL; }

EXPORT const char *ucnv_getStandardName(const char *name, const char *standard, int *err)
{
    (void)name; (void)standard;
    fail(err, U_UNSUPPORTED_ERROR);
    return NULL;
}

/* Everything below takes a UConverter, which ucnv_open never hands out —
 * unreachable in practice, but defined so the import table resolves. */
EXPORT void ucnv_close(void *cnv) { (void)cnv; }
EXPORT void ucnv_reset(void *cnv) { (void)cnv; }
EXPORT int8_t ucnv_getMaxCharSize(const void *cnv) { (void)cnv; return 1; }

EXPORT const char *ucnv_getName(const void *cnv, int *err)
{
    (void)cnv;
    fail(err, U_UNSUPPORTED_ERROR);
    return NULL;
}

EXPORT void ucnv_getToUCallBack(const void *cnv, void **action, const void **ctx)
{
    (void)cnv;
    if (action) *action = NULL;
    if (ctx) *ctx = NULL;
}

EXPORT void ucnv_getFromUCallBack(const void *cnv, void **action, const void **ctx)
{
    (void)cnv;
    if (action) *action = NULL;
    if (ctx) *ctx = NULL;
}

EXPORT void ucnv_setToUCallBack(void *cnv, void *action, const void *ctx,
                                void **old_action, const void **old_ctx, int *err)
{
    (void)cnv; (void)action; (void)ctx; (void)old_action; (void)old_ctx;
    fail(err, U_UNSUPPORTED_ERROR);
}

EXPORT void ucnv_setFromUCallBack(void *cnv, void *action, const void *ctx,
                                  void **old_action, const void **old_ctx, int *err)
{
    (void)cnv; (void)action; (void)ctx; (void)old_action; (void)old_ctx;
    fail(err, U_UNSUPPORTED_ERROR);
}

EXPORT void ucnv_toUnicode(void *cnv, void **target, const void *target_limit,
                           const char **source, const char *source_limit,
                           int32_t *offsets, int8_t flush, int *err)
{
    (void)cnv; (void)target; (void)target_limit; (void)source;
    (void)source_limit; (void)offsets; (void)flush;
    fail(err, U_UNSUPPORTED_ERROR);
}

EXPORT void ucnv_fromUnicode(void *cnv, char **target, const char *target_limit,
                             const void **source, const void *source_limit,
                             int32_t *offsets, int8_t flush, int *err)
{
    (void)cnv; (void)target; (void)target_limit; (void)source;
    (void)source_limit; (void)offsets; (void)flush;
    fail(err, U_UNSUPPORTED_ERROR);
}

EXPORT int32_t ucnv_toUCountPending(const void *cnv, int *err)
{
    (void)cnv;
    fail(err, U_UNSUPPORTED_ERROR);
    return 0;
}

EXPORT int32_t ucnv_fromUCountPending(const void *cnv, int *err)
{
    (void)cnv;
    fail(err, U_UNSUPPORTED_ERROR);
    return 0;
}

EXPORT void ucnv_cbToUWriteUChars(void *args, const void *source, int32_t length,
                                  int32_t offset, int *err)
{
    (void)args; (void)source; (void)length; (void)offset;
    fail(err, U_UNSUPPORTED_ERROR);
}

EXPORT void ucnv_cbFromUWriteUChars(void *args, const void **source, const void *source_limit,
                                    int32_t offset, int *err)
{
    (void)args; (void)source; (void)source_limit; (void)offset;
    fail(err, U_UNSUPPORTED_ERROR);
}

/* Qt takes these callbacks' addresses to install substitution behaviour. */
EXPORT void UCNV_TO_U_CALLBACK_SUBSTITUTE(const void *ctx, void *args, const char *units,
                                          int32_t length, int reason, int *err)
{
    (void)ctx; (void)args; (void)units; (void)length; (void)reason;
    fail(err, U_UNSUPPORTED_ERROR);
}

EXPORT void UCNV_FROM_U_CALLBACK_SUBSTITUTE(const void *ctx, void *args, const void *units,
                                            int32_t length, int32_t code_point, int reason, int *err)
{
    (void)ctx; (void)args; (void)units; (void)length; (void)code_point; (void)reason;
    fail(err, U_UNSUPPORTED_ERROR);
}
