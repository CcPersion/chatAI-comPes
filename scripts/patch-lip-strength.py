#!/usr/bin/env python3
"""Apply an idempotent, configurable lip-motion boost to LiveTalking Wav2Lip."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


DEFAULT_TARGET = Path("/root/setup/LiveTalking/avatars/wav2lip_avatar.py")
INIT_ANCHOR = "        self.model = model\n"
CALL_ANCHOR = "            pred = self.model(audiofeat_batch, img_batch)\n"
PATCHED_CALL = "            pred = self.model(audiofeat_batch, img_batch, a_alpha=self.lip_strength)\n"


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TARGET
    source = target.read_text(encoding="utf-8")
    changed = False

    if "self.lip_strength" not in source:
        if INIT_ANCHOR not in source:
            raise RuntimeError("LiveTalking model initialization anchor was not found")
        strength_init = (
            INIT_ANCHOR
            + "        try:\n"
            + "            self.lip_strength = min(1.6, max(1.0, float(os.getenv(\"LIP_STRENGTH\", \"1.35\"))))\n"
            + "        except ValueError:\n"
            + "            self.lip_strength = 1.35\n"
            + "        logger.info(f\"Wav2Lip 口型强度: {self.lip_strength:.2f}\")\n"
        )
        source = source.replace(INIT_ANCHOR, strength_init, 1)
        changed = True

    if CALL_ANCHOR in source:
        source = source.replace(CALL_ANCHOR, PATCHED_CALL, 1)
        changed = True
    elif PATCHED_CALL not in source:
        raise RuntimeError("LiveTalking inference call anchor was not found")

    if not changed:
        print(f"already patched: {target}")
        return 0

    backup = target.with_suffix(target.suffix + ".bak-lip-strength")
    if not backup.exists():
        shutil.copy2(target, backup)
    target.write_text(source, encoding="utf-8")
    print(f"patched: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
