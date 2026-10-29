#include <unicode/alphaindex.h>
#include <unicode/locid.h>
#include <cstdio>
#include <memory>
#include <string>
#include <vector>

#define CHECK(value) do { if (!(value)) { std::fprintf(stderr, "AlphabeticIndex failure at line %d\n", __LINE__); return 1; } } while (0)

static std::string utf8(const icu::UnicodeString& value) {
    std::string text;
    value.toUTF8String(text);
    return text;
}

int main() {
    UErrorCode status = U_ZERO_ERROR;
    icu::AlphabeticIndex english(icu::Locale::getEnglish(), status);
    const char* input[] = {"Zoe", "Álvaro", "Bob", "Åsa", "Émile", "Alice"};
    for (const char* name : input) english.addRecord(icu::UnicodeString::fromUTF8(name), name, status);
    CHECK(U_SUCCESS(status));
    CHECK(english.getRecordCount(status) == 6);
    std::vector<std::string> ordered;
    std::vector<std::string> labels;
    while (english.nextBucket(status)) {
        if (english.getBucketLabelType() == U_ALPHAINDEX_NORMAL) labels.push_back(utf8(english.getBucketLabel()));
        while (english.nextRecord(status)) {
            auto name = utf8(english.getRecordName());
            CHECK(name == static_cast<const char*>(english.getRecordData()));
            ordered.push_back(name);
        }
    }
    CHECK(U_SUCCESS(status) && labels.size() == 26 && labels.front() == "A" && labels.back() == "Z");
    CHECK((ordered == std::vector<std::string>{"Alice", "Álvaro", "Åsa", "Bob", "Émile", "Zoe"}));

    struct Example { const char* locale; const char* name; const char* label; bool english; };
    const Example examples[] = {
        {"en", "Émile", "E", false}, {"en", "Åsa", "A", false},
        {"sv", "Åke", "Å", false}, {"sv", "Äsa", "Ä", false}, {"sv", "Östen", "Ö", false},
        {"de", "Änne", "A", false}, {"ru", "Жанна", "Ж", true}, {"ru", "Alice", "A", true},
        {"ja", "さとう", "さ", false}, {"ko", "김민수", "ㄱ", false},
        {"zh", "张伟", "Z", false}, {"zh", "李雷", "L", false},
    };
    for (const auto& example : examples) {
        icu::AlphabeticIndex builder(icu::Locale::createCanonical(example.locale), status);
        if (example.english) builder.addLabels(icu::Locale::getEnglish(), status);
        std::unique_ptr<icu::AlphabeticIndex::ImmutableIndex> snapshot(builder.buildImmutableIndex(status));
        CHECK(U_SUCCESS(status) && snapshot);
        auto bucket = snapshot->getBucketIndex(icu::UnicodeString::fromUTF8(example.name), status);
        CHECK(U_SUCCESS(status) && snapshot->getBucket(bucket));
        CHECK(utf8(snapshot->getBucket(bucket)->getLabel()) == example.label);
        CHECK(snapshot->getBucket(bucket)->getLabelType() == U_ALPHAINDEX_NORMAL);
        std::printf("%s: %s -> %s\n", example.locale, example.name, example.label);
    }
    std::unique_ptr<icu::AlphabeticIndex::ImmutableIndex> index(english.buildImmutableIndex(status));
    CHECK(U_SUCCESS(status) && index);
    auto underflow = index->getBucketIndex(icu::UnicodeString::fromUTF8("123"), status);
    auto overflow = index->getBucketIndex(icu::UnicodeString::fromUTF8("Жанна"), status);
    CHECK(U_SUCCESS(status));
    CHECK(index->getBucket(underflow)->getLabelType() == U_ALPHAINDEX_UNDERFLOW);
    CHECK(index->getBucket(overflow)->getLabelType() == U_ALPHAINDEX_OVERFLOW);
    CHECK(index->getBucket(-1) == nullptr && index->getBucket(index->getBucketCount()) == nullptr);
    auto retained = [&status] {
        icu::AlphabeticIndex temporary(icu::Locale::createCanonical("sv"), status);
        return std::unique_ptr<icu::AlphabeticIndex::ImmutableIndex>(temporary.buildImmutableIndex(status));
    }();
    CHECK(U_SUCCESS(status) && retained);
    auto bucket = retained->getBucketIndex(icu::UnicodeString::fromUTF8("Östen"), status);
    CHECK(U_SUCCESS(status) && utf8(retained->getBucket(bucket)->getLabel()) == "Ö");
    std::puts("Upstream AlphabeticIndex: labels, locale-specific buckets, record ordering, and immutable snapshots passed");
    return 0;
}
