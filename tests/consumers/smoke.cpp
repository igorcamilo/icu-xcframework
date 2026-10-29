#include <unicode/ubrk.h>
#include <unicode/uchar.h>
#include <unicode/ucnv.h>
#include <unicode/ucol.h>
#include <unicode/udat.h>
#include <unicode/unorm2.h>
#include <unicode/unum.h>
#include <unicode/uregex.h>
#include <cstdio>

// Return a failing line number even in optimized Release builds (no assert/NDEBUG).
#define CHECK(condition) do { if (!(condition)) { std::fprintf(stderr, "ICU smoke failure at line %d\n", __LINE__); return 1; } } while (0)

int main() {
    CHECK(u_isalpha(0x00E9));
    UErrorCode status = U_ZERO_ERROR;
    UChar result[128];
    const UChar decomposed[] = {0x65, 0x0301, 0};
    const auto* normalizer = unorm2_getNFCInstance(&status);
    CHECK(U_SUCCESS(status) && normalizer);
    CHECK(unorm2_normalize(normalizer, decomposed, 2, result, 128, &status) == 1);
    CHECK(U_SUCCESS(status) && result[0] == 0x00E9);

    auto* collator = ucol_open("de", &status);
    CHECK(U_SUCCESS(status) && collator);
    const UChar a[] = {0x61, 0}, z[] = {0x7A, 0};
    CHECK(ucol_strcoll(collator, a, 1, z, 1) == UCOL_LESS);
    ucol_close(collator);

    const UChar pattern[] = {0x5B, 0x61, 0x2D, 0x7A, 0x5D, 0x2B, 0};
    auto* regex = uregex_open(pattern, -1, 0, nullptr, &status);
    CHECK(U_SUCCESS(status) && regex);
    const UChar word[] = {0x61, 0x62, 0x63, 0};
    uregex_setText(regex, word, -1, &status);
    CHECK(uregex_matches(regex, 0, &status) && U_SUCCESS(status));
    uregex_close(regex);

    const UChar words[] = {0x63, 0x61, 0x66, 0xE9, 0x20, 0x62, 0x61, 0x72, 0};
    auto* breaks = ubrk_open(UBRK_WORD, "fr", words, -1, &status);
    CHECK(U_SUCCESS(status) && breaks);
    CHECK(ubrk_first(breaks) == 0 && ubrk_next(breaks) == 4);
    ubrk_close(breaks);

    auto* converter = ucnv_open("windows-1252", &status);
    CHECK(U_SUCCESS(status) && converter);
    const char encoded[] = {static_cast<char>(0xE9)};
    CHECK(ucnv_toUChars(converter, result, 128, encoded, 1, &status) == 1);
    CHECK(U_SUCCESS(status) && result[0] == 0x00E9);
    ucnv_close(converter);

    auto* number = unum_open(UNUM_DECIMAL, nullptr, 0, "fr_FR", nullptr, &status);
    CHECK(U_SUCCESS(status) && number);
    CHECK(unum_formatDouble(number, 1234.5, result, 128, nullptr, &status) > 0);
    CHECK(U_SUCCESS(status));
    unum_close(number);

    const UChar utc[] = {0x55, 0x54, 0x43, 0};
    auto* date = udat_open(UDAT_SHORT, UDAT_SHORT, "en_US", utc, -1, nullptr, 0, &status);
    CHECK(U_SUCCESS(status) && date);
    CHECK(udat_format(date, 0, result, 128, nullptr, &status) > 0 && U_SUCCESS(status));
    udat_close(date);
    std::puts("ICU runtime smoke tests passed");
    return 0;
}
