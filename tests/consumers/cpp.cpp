#include <unicode/unistr.h>

int main() {
    icu::UnicodeString text = icu::UnicodeString::fromUTF8("Grüße");
    return text.length() == 5 ? 0 : 1;
}
