#include <cassert>
#include "policy.h"
int main() {
    assert(punchDue(true, true, 29400, 29400, 31259, 20261011, 0));
    assert(punchDue(true, true, 31259, 29400, 31259, 20261011, 20261010));
    assert(!punchDue(true, true, 31260, 29400, 31259, 20261011, 0));
    assert(!punchDue(false, true, 30000, 29400, 31259, 20261011, 0));
    assert(!punchDue(true, false, 30000, 29400, 31259, 20261011, 0));
    assert(!punchDue(true, true, 30000, 29400, 31259, 20261011, 20261011));
    assert(rebootDue(true, true, 14400, 14400, 20261011, 20261010, 3600000, false));
    assert(!rebootDue(true, true, 14400, 14400, 20261011, 20261011, 3600000, false));
    assert(!rebootDue(true, true, 14400, 14400, 20261011, 0, 3599999, false));
    assert(!rebootDue(true, true, 30000, 14400, 20261011, 0, 3600000, true));
    assert(!rebootDue(false, true, 14400, 14400, 20261011, 0, 3600000, false));
}
