import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
from core.auth import UserContext
from modules.phase1a import card_builder

async def test():
    user = UserContext(
        broker_id="test", tenant_id="danube", role="admin",
        session_id="test", display_name="Test",
        email="test@test.com", org_id="danube",
    )
    for unit in ["1 BR", "1BR", "1 Bedroom", None]:
        try:
            card, _, _ = await card_builder.build_card(
                intent="get_floor_plan",
                entities={"project_name": "Greenz", "unit_type": unit, "language": "en"},
                user=user,
            )
            fname = card.download_url.split("/")[-1]
            print(f"unit_type={unit!r} -> OK: {fname}")
        except Exception as e:
            print(f"unit_type={unit!r} -> ERROR: {e}")

asyncio.run(test())
