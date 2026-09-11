import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_reasoning_transcript as t

failed = 0
for name in sorted(dir(t)):
    if name.startswith("test_"):
        try:
            getattr(t, name)()
            print(f"PASS {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
print("ALL TESTS PASSED" if failed == 0 else f"{failed} TEST(S) FAILED")
sys.exit(1 if failed else 0)
