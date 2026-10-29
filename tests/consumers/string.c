#include <unicode/ustring.h>

int main(void) {
    const UChar text[] = {0x41, 0x42, 0};
    return u_strlen(text) == 2 ? 0 : 1;
}
