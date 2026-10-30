"""Explicit per-run approval of known model/SoC incompatibilities."""

from . import EubError


class CompatibilityOverride:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.approved = set()

    def check(self, mismatch: str):
        if not self.enabled:
            raise EubError(
                f"{mismatch} Use --override to review and explicitly confirm this incompatibility."
            )
        if mismatch in self.approved:
            return
        print(
            f"WARNING: INCOMPATIBLE DEVICE / IMAGE\n{mismatch}\n"
            "The selected images may not boot this device. "
            "ROM signature and anti-rollback checks still apply.",
            flush=True,
        )
        try:
            answer = (
                input("Continue with these incompatible images? [YES/NO] (NO): ")
                .strip()
                .upper()
            )
        except EOFError as exc:
            raise EubError(
                "Override cancelled: explicit YES confirmation was not received."
            ) from exc
        if answer != "YES":
            raise EubError(
                "Override cancelled: explicit YES confirmation was not received."
            )
        self.approved.add(mismatch)
        print("Override confirmed: continuing with the selected images.", flush=True)
